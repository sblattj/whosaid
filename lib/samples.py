#!/usr/bin/env python3
"""
samples.py — per-speaker sample-clip export for human verification (GitHub issue #1).

Reads a whosaid <base>.diarization.json sidecar and, for each speaker cluster,
cuts one (or --per-speaker N) short representative WAV clip from the ORIGINAL
audio so a human can quickly confirm "who is SPEAKER_02" before trusting an
auto-label or enrolling — replacing the by-hand `ffmpeg` workflow issue #1
described.

Representative clip: the LONGEST segment for a cluster (ties broken by
earliest start), preferring segments >= PREFERRED_MIN_SECONDS; a clip is
clamped to --seconds, starting at the segment's own start. A cluster with no
segment >= MIN_CANDIDATE_SECONDS is skipped (nothing worth confirming) with a
stderr note, and the run still succeeds for the other clusters.

Source audio: sidecar["source"]["path"] (written by diarize_sherpa.py since
GitHub issue #1 parts 2-3), or --audio FILE for a sidecar written before that
existed.

Output: <outdir>/<base>.samples/<SPEAKER_NN>[-<Name>][-N].wav — mono 16kHz,
via `ffmpeg -ss START -i SRC -t DUR -ac 1 -ar 16000 OUT` (same extraction
shape as the CLI's own --ref/enroll clip cuts). <outdir> defaults to the
sidecar's own directory; <base> comes from the sidecar's "base" field.

Prints one TSV line per exported file to stdout:
    SPEAKER_NN<TAB>name<TAB>start<TAB>duration<TAB>path
and, with --json, a trailing JSON line:
    {"samples": [{"cluster":..., "name":..., "start":..., "duration":..., "path":...}, ...]}
Invoked as plain `python3` (stdlib only — no uv, no third-party deps).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

MIN_CANDIDATE_SECONDS = 1.0
PREFERRED_MIN_SECONDS = 4.0  # documents the preference; longest-first sort already honors it
DEFAULT_SECONDS = 8.0
DEFAULT_PER_SPEAKER = 1


def sanitize(name: str) -> str:
    """Map anything outside [A-Za-z0-9_-] to '_', matching the CLI's own name gate."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def pick_segments(segments: list, speaker: str, per_speaker: int) -> list:
    """Up to `per_speaker` representative segments for `speaker`: longest first
    (ties by earliest start), excluding anything shorter than
    MIN_CANDIDATE_SECONDS. Sorting longest-first already satisfies "prefer
    segments >= PREFERRED_MIN_SECONDS" whenever one exists."""
    cand = [
        s for s in segments
        if s.get("speaker") == speaker and (float(s["end"]) - float(s["start"])) >= MIN_CANDIDATE_SECONDS
    ]
    if not cand:
        return []
    cand.sort(key=lambda s: (-(float(s["end"]) - float(s["start"])), float(s["start"])))
    return cand[:per_speaker]


def export_clip(src: Path, start: float, seconds: float, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{seconds:.3f}",
        "-ac", "1", "-ar", "16000", str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"ffmpeg not found ({exc})") from exc
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg failed for {dest.name}: {(proc.stderr or '').strip()[-500:]}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Export one short representative WAV clip per speaker cluster from a "
                    "whosaid <base>.diarization.json sidecar, for a quick human listen before "
                    "trusting an auto-label or enrolling.",
    )
    p.add_argument("sidecar", help="Path to <base>.diarization.json")
    p.add_argument(
        "--outdir",
        help="Output ROOT directory (default: the sidecar's own directory); "
             "<base>.samples/ is created under it",
    )
    p.add_argument(
        "--audio",
        help="Source audio file to cut clips from, overriding the sidecar's own "
             "'source.path' — required for a sidecar written before GitHub issue #1 "
             "parts 2-3 added that field",
    )
    p.add_argument("--per-speaker", type=int, default=DEFAULT_PER_SPEAKER,
                    help=f"Clips per cluster (default: {DEFAULT_PER_SPEAKER})")
    p.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                    help=f"Max clip length in seconds (default: {DEFAULT_SECONDS})")
    p.add_argument("--json", action="store_true",
                    help='Also print a final {"samples": [...]} JSON line (default: TSV only)')
    return p


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)

    sidecar_path = Path(args.sidecar)
    try:
        data = json.loads(sidecar_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"samples: FATAL could not read sidecar {sidecar_path}: {exc}", file=sys.stderr)
        return 1

    base = data.get("base") or sidecar_path.name.removesuffix(".diarization.json")
    segments = data.get("segments") or []
    names = data.get("names") or {}

    audio_path_str = args.audio
    if not audio_path_str:
        source = data.get("source") or {}
        audio_path_str = source.get("path")
    if not audio_path_str:
        print(
            "samples: FATAL sidecar has no 'source.path' (it predates GitHub issue #1 "
            "parts 2-3, which added source metadata) and no --audio FILE was given — "
            "pass --audio FILE naming the original recording.",
            file=sys.stderr,
        )
        return 1
    audio_path = Path(audio_path_str)
    if not audio_path.is_file():
        print(f"samples: FATAL audio file not found: {audio_path}", file=sys.stderr)
        return 1

    per_speaker = max(1, args.per_speaker)
    outroot = Path(args.outdir) if args.outdir else sidecar_path.resolve().parent
    outdir = outroot / f"{base}.samples"

    speakers = sorted({s.get("speaker") for s in segments if s.get("speaker")})
    if not speakers:
        print("samples: FATAL sidecar has no segments/speakers to sample", file=sys.stderr)
        return 1

    results = []
    for speaker in speakers:
        picks = pick_segments(segments, speaker, per_speaker)
        if not picks:
            print(
                f"samples: NOTE skipping {speaker} — no segment >= {MIN_CANDIDATE_SECONDS}s",
                file=sys.stderr,
            )
            continue
        name = names.get(speaker, speaker)
        name_suffix = f"-{sanitize(name)}" if name and name != speaker else ""
        for i, seg in enumerate(picks):
            start = float(seg["start"])
            dur = min(float(seg["end"]) - start, args.seconds)
            idx_suffix = "" if i == 0 else f"-{i + 1}"
            dest = outdir / f"{speaker}{name_suffix}{idx_suffix}.wav"
            try:
                export_clip(audio_path, start, dur, dest)
            except RuntimeError as exc:
                print(f"samples: FATAL {exc}", file=sys.stderr)
                return 1
            rec = {
                "cluster": speaker,
                "name": name,
                "start": round(start, 3),
                "duration": round(dur, 3),
                "path": str(dest),
            }
            results.append(rec)
            print(f"{rec['cluster']}\t{rec['name']}\t{rec['start']}\t{rec['duration']}\t{rec['path']}")

    if not results:
        print(
            f"samples: FATAL no clips were exported (every cluster had segments < "
            f"{MIN_CANDIDATE_SECONDS}s)",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps({"samples": results}))

    return 0


if __name__ == "__main__":
    sys.exit(main())
