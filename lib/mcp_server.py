#!/usr/bin/env python3
"""
mcp_server.py — a fully local MCP (Model Context Protocol) server for `whosaid`.

Exposes the `whosaid` speaker-attributed transcription pipeline to AI agents /
Claude over stdio. It NEVER re-implements the pipeline: every tool shells the
existing `whosaid` CLI (at REPO_DIR/"whosaid", cwd=REPO_DIR) or reads the files
it writes, so the MCP surface and the CLI can never drift apart.

Launch (via the CLI subcommand added to the `whosaid` dispatcher):
    whosaid mcp
which is:
    uv run --quiet --with "mcp[cli]" python lib/mcp_server.py

Everything stays on the machine — audio, text, and voice embeddings never leave.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# SDK import.
#
# The official `mcp` Python SDK moved the high-level server class from
# `mcp.server.fastmcp.FastMCP` (SDK 1.x) to `mcp.server.mcpserver.MCPServer`
# (SDK 2.x). The decorator surface (`.tool(description=, annotations=)`,
# `.resource(uri)`, `.run()`) is the same, so we alias whichever exists.
# ToolAnnotations lives in `mcp.types` in both.
# ---------------------------------------------------------------------------
try:  # SDK >= 2.0 (what `--with "mcp[cli]"` resolves to today)
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - SDK 1.x fallback
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

from mcp.types import ToolAnnotations

__version__ = "1.1.0"

# ---------------------------------------------------------------------------
# Paths / environment (mirror the `whosaid` bash dispatcher's own derivations).
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parent.parent
WHOSAID = REPO_DIR / "whosaid"

# Make Homebrew tools (ffmpeg, ffprobe, uv) visible even under a minimal PATH,
# exactly like the `whosaid` script does. Direct ffmpeg/ffprobe calls below rely
# on this; the CLI re-exports it for its own children.
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")


def _voice_refs() -> Path:
    """Enrollment-clip directory: WHOSAID_VOICE_REFS or REPO_DIR/voices."""
    return Path(os.environ.get("WHOSAID_VOICE_REFS") or (REPO_DIR / "voices"))


def _speaker_db() -> Path:
    """Persistent speaker registry: WHOSAID_SPEAKER_DB or ~/.config/whosaid/speakers.json."""
    return Path(
        os.environ.get("WHOSAID_SPEAKER_DB")
        or (Path.home() / ".config" / "whosaid" / "speakers.json")
    )


def _base_for(audio: str) -> str:
    """Derive the dot-safe output base the SAME way the CLI does."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", Path(audio).stem)


def _run_cli(args: list, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """Shell the `whosaid` CLI from the repo root. Never raises on nonzero exit."""
    return subprocess.run(
        [str(WHOSAID), *[str(a) for a in args]],
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _cli_version() -> Optional[str]:
    """Shell `whosaid version` and return just the version token (e.g. "1.1.0"),
    the second whitespace-separated token of its stdout ("whosaid 1.1.0 ...").
    None on any failure, so a broken/missing CLI never breaks whosaid_doctor.
    """
    try:
        proc = _run_cli(["version"], timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    tokens = (proc.stdout or "").split()
    return tokens[1] if len(tokens) >= 2 else None


def _read_text_or_none(path: Path) -> Optional[str]:
    """Full file text, or None if missing/empty/unreadable."""
    try:
        t = path.read_text()
        return t if t.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _probe_duration(path: str) -> Optional[float]:
    """Audio duration (s) via ffprobe; None if it can't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True,
        ).stdout.strip()
        return round(float(out), 3)
    except Exception:  # noqa: BLE001
        return None


def _mean_volume_db(path: str) -> Optional[float]:
    """Mean volume (dB) via ffmpeg volumedetect; None if unparsable."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        m = re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", proc.stderr)
        return float(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


# Time-window shape accepted by whosaid_enroll_from_file's ss/t/to: seconds
# ("200", "12.5") or M:SS / H:MM:SS ("3:20", "1:02:03") — mirrors the CLI's
# `is_valid_time`/`time_to_seconds` in the `whosaid` script exactly.
_TIME_RE = re.compile(
    r"^[0-9]+(\.[0-9]+)?$"
    r"|^[0-9]{1,2}:[0-9]{1,2}(\.[0-9]+)?$"
    r"|^[0-9]{1,2}:[0-9]{1,2}:[0-9]{1,2}(\.[0-9]+)?$"
)


def _time_to_seconds(value: str) -> float:
    """Convert a `_TIME_RE`-validated string to plain seconds."""
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", value):
        return float(value)
    parts = [float(p) for p in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"unparsable time: {value!r}")


# ---------------------------------------------------------------------------
# Server instructions (loaded once — cross-cutting semantics, NOT per tool).
# ---------------------------------------------------------------------------
SERVER_INSTRUCTIONS = """whosaid turns audio into a transcript where every turn is labeled with who spoke. It runs 100% locally on Apple Silicon via the MLX Whisper GPU pipeline plus sherpa-onnx diarization — audio, text, and voice embeddings NEVER leave the machine; there are no API keys and no cloud calls.

Output-file contract: each transcribe writes, using <base> = the input's basename (or your `name`, with any character outside [A-Za-z0-9_-] mapped to '_'): <base>.txt (plain transcript), <base>.speakers.txt (speaker-labeled), <base>.speaker-cards.txt (one card per speaker: turn count, talk time, sample snippets — to tell who each SPEAKER_NN is), and <base>.diarization.json (a sidecar that whosaid_relabel reuses).

Typical workflow: whosaid_transcribe -> read the speaker cards in the result -> whosaid_samples if you want to LISTEN to a clip per cluster before trusting a name -> whosaid_relabel to put real names on the SPEAKER_NN clusters. Relabeling saves each name to a persistent local registry, so the same voice is auto-named in every later transcript. whosaid_list_speakers shows who is already known.

Not exposed here: `enroll` and `record` are interactive microphone operations that need a live terminal — run them from the `whosaid` CLI. The first transcribe downloads ~1.5 GB of models; run whosaid_doctor to check readiness. Full flag/env/long-audio reference: read the whosaid://guide resource."""


mcp = _Server(name="whosaid", instructions=SERVER_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Tool descriptions (verbatim from the frozen spec — a pinning test asserts the
# load-bearing substrings; do not edit without editing the spec + test).
# ---------------------------------------------------------------------------
_DESC_TRANSCRIBE = (
    "Transcribe an audio file and label each turn with WHO said it — 100% locally on "
    "the Apple-Silicon GPU (no cloud, no API keys). Writes <base>.txt, <base>.speakers.txt "
    "(speaker-labeled), <base>.speaker-cards.txt and a <base>.diarization.json sidecar next "
    "to the input (or `outdir`). The FIRST run downloads ~1.5 GB of models. Speakers you "
    "haven't named come back as SPEAKER_00/01…; the result includes the speaker cards — read "
    "them, then call whosaid_relabel to name them (names then auto-apply to future "
    "transcripts). If you already know who was in the room, pass `expected_speakers` "
    "(names of voices you have enrolled) — clustering is then anchored to their "
    "voiceprints, absentees are dropped, and nobody has to guess a count. Keywords: "
    "transcribe, diarize, speaker diarization, who spoke, meeting "
    "notes, call recording, whisper, voice attribution, subtitles, srt, vtt."
)

_DESC_RELABEL = (
    "Put real names on SPEAKER_NN clusters and REMEMBER them: each name is saved to your "
    "local speaker registry and auto-applied to EVERY future transcript. Reads the cached "
    "<base>.diarization.json sidecar and rewrites <base>.speakers.txt + <base>.speaker-cards.txt "
    "in place — no re-transcription, no re-diarization. Run whosaid_transcribe first, read its "
    "speaker cards to tell who is who, then map clusters to names. Pass auto=true (with an empty "
    "assignments map) to instead re-apply registry matching + the absorb pass over the cached "
    "sidecar, which folds phantom cluster splits into their real speaker. Keywords: rename speaker, "
    "label speaker, assign name, identify voice, who is SPEAKER_00, correct labels."
)

_DESC_LIST_SPEAKERS = (
    "List the voices whosaid can already auto-name: enrolled clips in `voices/` plus "
    "voiceprints saved in the local speaker registry (~/.config/whosaid/speakers.json). "
    "Read-only. Call this before whosaid_relabel so you reuse an existing name instead of "
    "creating a duplicate. Keywords: known speakers, enrolled voices, registry, recognized "
    "voices, who can it identify."
)

_DESC_DOCTOR = (
    "Read-only readiness check: Apple-Silicon/arch + macOS, ffmpeg and uv, Whisper and "
    "diarization model-cache state, enrolled voices, and the mic/audio device list. Run this "
    "FIRST when a transcribe fails — each missing prerequisite prints the exact fix (usually "
    "`whosaid setup`). Keywords: health check, prerequisites, diagnose, models cached, setup "
    "status, is it ready."
)

_DESC_ENROLL = (
    "Enroll a named voice from an EXISTING audio clip (≥15 s of one person speaking, "
    "reasonably clean) so future transcripts auto-label them — converts and writes "
    "voices/<Name>.wav. This does NOT record from the mic: for mic enrollment use the "
    "interactive `whosaid enroll` CLI (it needs a terminal and Microphone permission). Fails "
    "clearly if the clip is too short or silent. Keywords: enroll voice, add speaker, register "
    "voice, voiceprint, teach a new voice from a file."
)

_DESC_SAMPLES = (
    "Export ONE short representative WAV clip per SPEAKER_NN cluster, cut from the ORIGINAL "
    "recording, so a human can quickly LISTEN and confirm an identity before trusting an "
    "auto-label or enrolling that voice — replaces cutting clips by hand with ffmpeg. Picks "
    "each cluster's longest diarized segment, clamped to `seconds` (default 8), and writes "
    "<base>.samples/<SPEAKER_NN>[-<Name>].wav (mono 16kHz). Needs the sidecar's 'source' "
    "metadata (present since GitHub issue #1 parts 2-3); an older sidecar without it must be "
    "re-transcribed, or use the `whosaid samples --audio FILE` CLI flag directly. Keywords: "
    "sample clip, listen to speaker, verify identity, confirm voice, spot check, per-speaker "
    "snippet, sanity check a label."
)


# ---------------------------------------------------------------------------
# 1) whosaid_transcribe
# ---------------------------------------------------------------------------
@mcp.tool(
    name="whosaid_transcribe",
    description=_DESC_TRANSCRIBE,
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def whosaid_transcribe(
    audio: str,
    outdir: Optional[str] = None,
    accuracy: Literal["fast", "accurate"] = "fast",
    format: Literal["txt", "srt", "vtt", "tsv", "json", "all"] = "all",
    lang: str = "en",
    speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
    expected_speakers: Optional[list[str]] = None,
    anchor_threshold: Optional[float] = None,
    diarize: bool = True,
    match_threshold: Optional[float] = None,
) -> dict:
    """Transcribe + diarize by shelling the CLI, then read the output files."""
    if not Path(audio).exists():
        return {
            "ok": False,
            "error": f"audio file not found: {audio}",
            "fix": "pass an existing audio file path (absolute is safest)",
        }

    if (min_speakers is not None and max_speakers is not None
            and min_speakers >= 1 and max_speakers >= 1 and min_speakers > max_speakers):
        return {
            "ok": False,
            "error": f"min_speakers ({min_speakers}) is greater than max_speakers ({max_speakers})",
            "fix": "pass min_speakers <= max_speakers, or just `speakers` for an exact count",
        }

    # Diarization needs the Whisper .json. Force a format that produces it.
    if diarize and format not in ("all", "json"):
        effective_format = "all"
    else:
        effective_format = format

    out = outdir or (os.path.dirname(audio) or ".")

    args = [audio, "--outdir", out, "--format", effective_format, "--lang", lang]
    if accuracy == "accurate":
        args.append("--accurate")
    if speakers is not None and speakers >= 1:
        args += ["--speakers", str(speakers)]
    else:
        # A range only means anything for the AUTO estimate; an exact --speakers
        # overrides it, so do not send both.
        if min_speakers is not None and min_speakers >= 1:
            args += ["--min-speakers", str(min_speakers)]
        if max_speakers is not None and max_speakers >= 1:
            args += ["--max-speakers", str(max_speakers)]
    # Registry-anchored diarization: the roster biases clustering itself, so it is
    # NOT mutually exclusive with a count — unlike min/max above, which only shape
    # the auto estimate. Names that are not known voices make the CLI exit fatally
    # with the list of known names, which is the right, visible failure.
    for name in (expected_speakers or []):
        name = str(name).strip()
        if name:
            args += ["--expected-speakers", name]
    if anchor_threshold is not None:
        args += ["--anchor-threshold", str(anchor_threshold)]
    if not diarize:
        args.append("--no-diarize")
    if match_threshold is not None:
        args += ["--match-threshold", str(match_threshold)]

    proc = _run_cli(args)

    base = _base_for(audio)
    out_dir = Path(out)
    transcript_txt = out_dir / f"{base}.txt"
    speakers_txt = out_dir / f"{base}.speakers.txt"
    speaker_cards_path = out_dir / f"{base}.speaker-cards.txt"
    sidecar_path = out_dir / f"{base}.diarization.json"

    transcript_text = _read_text_or_none(transcript_txt)
    if transcript_text is None:
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
        if "not cached" in combined or "setup" in combined or "download" in combined:
            fix = "run: whosaid setup"
        elif "apple silicon" in combined or "arm64" in combined or os.uname().machine != "arm64":
            fix = "run whosaid_doctor (whosaid needs Apple Silicon / arm64)"
        else:
            fix = "run whosaid_doctor"
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "transcript was not produced").strip()[-1000:],
            "fix": fix,
        }

    speakers_text = _read_text_or_none(speakers_txt)
    speaker_cards = _read_text_or_none(speaker_cards_path)

    # Authoritative cluster list + names come from the sidecar; regex is a fallback.
    names: dict = {}
    num_speakers: Optional[int] = None
    registry_matches: list = []
    source_meta: Optional[dict] = None
    count_warning: Optional[str] = None
    count_estimate: Optional[dict] = None
    if sidecar_path.exists():
        try:
            data = json.loads(sidecar_path.read_text())
            names = dict(data.get("names") or {})
            registry_matches = data.get("registry_matches") or []
            source_meta = data.get("source")
            num_speakers = data.get("num_speakers")
            count_warning = data.get("count_warning")
            count_estimate = data.get("count_estimate")
            if num_speakers is None:
                num_speakers = len({s["speaker"] for s in data.get("segments", [])}) or None
        except Exception:  # noqa: BLE001
            names = {}
    if not names:
        clusters = sorted(set(re.findall(r"SPEAKER_\d+", speakers_text or "")))
        names = {c: c for c in clusters}
        if num_speakers is None:
            num_speakers = len(clusters) or None

    named_speakers = sorted({v for k, v in names.items() if v != k})
    unnamed_clusters = sorted(k for k, v in names.items() if v == k)

    if unnamed_clusters:
        next_step = (
            f"{len(unnamed_clusters)} unnamed speaker(s). Read speaker_cards, then call "
            f"whosaid_relabel(base='{base}', assignments={{'SPEAKER_00':'Name', ...}})."
        )
    else:
        next_step = "All speakers named."

    if not diarize:
        summary = f"Transcribed {base}: plain transcript only (diarization disabled)."
    else:
        summary = (
            f"Transcribed {base}: {num_speakers if num_speakers is not None else '?'} speaker(s), "
            f"{len(named_speakers)} named, {len(unnamed_clusters)} to name."
        )
        if count_warning:
            summary += f" WARNING: {count_warning}"

    return {
        "ok": True,
        "base": base,
        "outdir": str(out_dir),
        "transcript_txt": str(transcript_txt),
        "speakers_txt": str(speakers_txt) if speakers_text is not None else None,
        "speaker_cards_txt": str(speaker_cards_path) if speaker_cards is not None else None,
        "sidecar_json": str(sidecar_path) if sidecar_path.exists() else None,
        "registry_matches": registry_matches,
        "source": source_meta,
        "duration_seconds": _probe_duration(audio),
        "num_speakers": num_speakers,
        "count_warning": count_warning,
        "count_estimate": count_estimate,
        "named_speakers": named_speakers,
        "unnamed_clusters": unnamed_clusters,
        "speaker_cards": speaker_cards,
        "next_step": next_step,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# 2) whosaid_relabel
# ---------------------------------------------------------------------------
def _resolve_sidecar(base: str, outdir: Optional[str]) -> Optional[Path]:
    """Locate the .diarization.json sidecar the way the CLI does (cwd=REPO_DIR)."""
    p = Path(base)
    if p.is_file():
        return p
    for root in (Path.cwd(), REPO_DIR):
        cand = root / f"{base}.diarization.json" if not p.is_absolute() else Path(f"{base}.diarization.json")
        if cand.exists():
            return cand
        if p.is_absolute():
            break
    if outdir:
        c2 = Path(outdir) / f"{Path(base).name}.diarization.json"
        if c2.exists():
            return c2
    return None


@mcp.tool(
    name="whosaid_relabel",
    description=_DESC_RELABEL,
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def whosaid_relabel(
    base: str,
    assignments: dict,
    outdir: Optional[str] = None,
    auto: bool = False,
    match_threshold: Optional[float] = None,
) -> dict:
    """Name SPEAKER_NN clusters and persist them, by shelling `whosaid relabel`.

    With auto=True the assignments map may be empty: whosaid re-applies registry
    matching + the absorb pass over the cached sidecar (no re-diarization),
    merging phantom cluster splits into their real speaker.
    """
    if not isinstance(assignments, dict) or (not assignments and not auto):
        return {
            "ok": False,
            "error": "assignments must be a non-empty map of SPEAKER_NN -> Name (or pass auto=true)",
            "fix": "pass at least one entry, e.g. {'SPEAKER_00':'Jane'}, or auto=true with {}",
        }
    bad = []
    for k, v in assignments.items():
        if not re.fullmatch(r"SPEAKER_\d+", str(k)):
            bad.append(f"key '{k}' (must match SPEAKER_NN)")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(v)):
            bad.append(f"value '{v}' (must match [A-Za-z0-9_-]+)")
    if bad:
        return {
            "ok": False,
            "error": "invalid assignments: " + "; ".join(bad),
            "fix": "keys must be SPEAKER_NN clusters and names must be [A-Za-z0-9_-]+",
        }

    specs = [f"{k}={v}" for k, v in assignments.items()]
    args = ["relabel", base, *specs]
    if auto:
        args.append("--auto")
    if match_threshold is not None:
        args += ["--match-threshold", str(match_threshold)]
    if outdir:
        args += ["-o", outdir]

    proc = _run_cli(args)
    if proc.returncode != 0:
        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if "diarization sidecar" in combined or "could not find" in combined:
            return {
                "ok": False,
                "error": combined[-1000:],
                "fix": f"run whosaid_transcribe first to produce {Path(base).name}.diarization.json",
            }
        return {
            "ok": False,
            "error": combined[-1000:] or "relabel failed",
            "fix": "run whosaid_doctor",
        }

    # Success: locate the (now-updated) sidecar + rewritten speaker transcript.
    sidecar = _resolve_sidecar(base, outdir)
    data_base = Path(base).name.removesuffix(".diarization.json")
    speakers_txt: Optional[str] = None
    if sidecar and sidecar.exists():
        try:
            d = json.loads(sidecar.read_text())
            data_base = d.get("base", data_base)
        except Exception:  # noqa: BLE001
            pass
        sp = sidecar.parent / f"{data_base}.speakers.txt"
        speakers_txt = str(sp) if sp.exists() else None

    return {
        "ok": True,
        "base": data_base,
        "renamed": dict(assignments),
        "speakers_txt": speakers_txt,
        "registry_path": str(_speaker_db()),
        "summary": (
            (f"Re-applied registry + absorb naming from the sidecar"
             + (f" plus {len(assignments)} explicit assignment(s)" if assignments else "")
             + "." )
            if auto else
            (f"Renamed {len(assignments)} cluster(s); saved to the local speaker registry "
             f"so they auto-name in future transcripts.")
        ),
    }


# ---------------------------------------------------------------------------
# 3) whosaid_list_speakers
# ---------------------------------------------------------------------------
@mcp.tool(
    name="whosaid_list_speakers",
    description=_DESC_LIST_SPEAKERS,
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def whosaid_list_speakers() -> dict:
    """Pure file reads: enrolled clips + registered voiceprints. No shell, no uv."""
    vr = _voice_refs()
    voices = set()
    if vr.is_dir():
        for pat in ("*.wav", "*.m4a"):
            for f in vr.glob(pat):
                voices.add(f.stem)
    enrolled_voices = sorted(voices)

    db = _speaker_db()
    registry_speakers: list = []
    note = ""
    if db.exists():
        try:
            data = json.loads(db.read_text())
            for s in data.get("speakers", []):
                entry = {"name": s.get("name")}
                if s.get("model"):
                    entry["model"] = s.get("model")
                registry_speakers.append(entry)
        except Exception as e:  # noqa: BLE001
            note = f"registry unreadable ({e})"
    else:
        note = "no registry yet — name speakers with whosaid_relabel"

    summary = (
        f"{len(enrolled_voices)} enrolled voice(s), "
        f"{len(registry_speakers)} registered voiceprint(s)."
    )
    if note:
        summary += f" ({note})"

    return {
        "enrolled_voices": enrolled_voices,
        "registry_speakers": registry_speakers,
        "registry_path": str(db),
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# 4) whosaid_doctor
# ---------------------------------------------------------------------------
@mcp.tool(
    name="whosaid_doctor",
    description=_DESC_DOCTOR,
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def whosaid_doctor() -> dict:
    """Shell `whosaid doctor`, return its report + a ready flag."""
    proc = _run_cli(["doctor"])
    report = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    ready = "✗" not in report  # '✗'
    if ready:
        summary = "ready"
    else:
        summary = next(
            (ln.strip() for ln in report.splitlines() if "✗" in ln),
            "not ready",
        )
    return {
        "ok": proc.returncode == 0,
        "report": report,
        "ready": ready,
        "summary": summary,
        "version": _cli_version(),
    }


# ---------------------------------------------------------------------------
# 5) whosaid_enroll_from_file
# ---------------------------------------------------------------------------
@mcp.tool(
    name="whosaid_enroll_from_file",
    description=_DESC_ENROLL,
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def whosaid_enroll_from_file(
    name: str,
    audio: str,
    ss: Optional[str] = None,
    t: Optional[str] = None,
    to: Optional[str] = None,
) -> dict:
    """Non-interactive voice enrollment from an existing clip (mirrors CLI checks).

    ss/t/to optionally cut a time window out of `audio` FIRST — same shape and
    semantics as `whosaid enroll --from FILE --ss/--t/--to` (seconds or
    M:SS/H:MM:SS; `to` is converted to a duration internally, never passed to
    ffmpeg as `-to`, for the same seek-relative-`-to` ambiguity reason the CLI
    avoids it). Omit all three to enroll from the whole file, unchanged.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
        return {
            "ok": False,
            "error": f"invalid name '{name}'",
            "fix": "use only letters, numbers, underscore, hyphen",
        }
    if not Path(audio).exists():
        return {
            "ok": False,
            "error": f"audio file not found: {audio}",
            "fix": "pass an existing audio clip path",
        }
    if t and to:
        return {
            "ok": False,
            "error": "t and to are mutually exclusive",
            "fix": "pass a duration (t) or an end time (to), not both",
        }
    for label, value in (("ss", ss), ("t", t), ("to", to)):
        if value is not None and not _TIME_RE.match(value):
            return {
                "ok": False,
                "error": f"invalid {label} '{value}'",
                "fix": "use seconds (200, 12.5) or M:SS / H:MM:SS",
            }

    clip_source = audio
    extracted_dir: Optional[Path] = None
    if ss or t or to:
        start = ss or "0"
        extract_dur: Optional[str] = None
        if t:
            extract_dur = t
        elif to:
            try:
                delta = _time_to_seconds(to) - _time_to_seconds(start)
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "fix": "use seconds or M:SS/H:MM:SS"}
            if delta <= 0:
                return {
                    "ok": False,
                    "error": f"to ({to}) must be after ss ({start})",
                    "fix": "pass an end time after the start time",
                }
            extract_dur = f"{delta:.3f}"

        extracted_dir = Path(tempfile.mkdtemp(prefix="whosaid-enroll-"))
        extracted = extracted_dir / "clip.wav"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-ss", start, "-i", audio]
        if extract_dur is not None:
            cmd += ["-t", extract_dur]
        cmd += ["-ac", "1", "-ar", "16000", str(extracted)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not extracted.exists() or extracted.stat().st_size == 0:
            shutil.rmtree(extracted_dir, ignore_errors=True)
            return {
                "ok": False,
                "error": (proc.stderr or "ffmpeg extraction failed").strip()[-1000:],
                "fix": "check ss/t/to fall within the file",
            }
        clip_source = str(extracted)

    try:
        dur = _probe_duration(clip_source)
        if dur is None:
            return {
                "ok": False,
                "error": f"could not read audio duration for {clip_source}",
                "fix": "check the file is a valid audio clip",
            }
        if dur < 15:
            return {
                "ok": False,
                "error": f"clip is only {dur}s",
                "fix": "need ≥15 s of speech from one person",
            }

        mean = _mean_volume_db(clip_source)
        if mean is not None and mean <= -85:
            return {
                "ok": False,
                "error": f"clip is silent (mean volume {mean} dB)",
                "fix": "silent/likely wrong file — check the recording (or Microphone permission on capture)",
            }

        voice_refs = _voice_refs()
        voice_refs.mkdir(parents=True, exist_ok=True)
        dest = voice_refs / f"{name}.wav"

        if extracted_dir is not None:
            # Already extracted at mono/16kHz above — just move it into place.
            shutil.move(clip_source, str(dest))
        else:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                 "-i", audio, "-ac", "1", "-ar", "16000", str(dest)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0 or not dest.exists():
                return {
                    "ok": False,
                    "error": (proc.stderr or "ffmpeg conversion failed").strip()[-1000:],
                    "fix": "check ffmpeg is installed (whosaid_doctor) and the clip is valid audio",
                }

        return {
            "ok": True,
            "name": name,
            "voice_path": str(dest),
            "duration_seconds": dur,
            "summary": f"Enrolled '{name}' from {Path(audio).name}; future transcripts will auto-label this voice.",
        }
    finally:
        if extracted_dir is not None:
            shutil.rmtree(extracted_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6) whosaid_samples
# ---------------------------------------------------------------------------
@mcp.tool(
    name="whosaid_samples",
    description=_DESC_SAMPLES,
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def whosaid_samples(
    base: str,
    outdir: Optional[str] = None,
    per_speaker: int = 1,
    seconds: float = 8.0,
) -> dict:
    """Shell `whosaid samples ... --json` and return the parsed per-cluster clip list.

    Writes files (one short WAV per speaker cluster), so read_only_hint=False;
    idempotent_hint=True because re-running with the same args overwrites the
    same deterministic filenames rather than accumulating new ones.
    """
    args = ["samples", base, "--per-speaker", str(per_speaker), "--seconds", str(seconds), "--json"]
    if outdir:
        args += ["-o", outdir]

    proc = _run_cli(args)
    if proc.returncode != 0:
        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if "--audio" in combined:
            return {
                "ok": False,
                "error": combined[-1000:],
                "fix": "this sidecar predates 'source' metadata (GitHub issue #1 parts 2-3) — "
                       "re-transcribe to regenerate it, or run "
                       "`whosaid samples <base> --audio FILE` from the CLI directly",
            }
        if "diarization sidecar" in combined or "could not find" in combined:
            return {
                "ok": False,
                "error": combined[-1000:],
                "fix": f"run whosaid_transcribe first to produce {Path(base).name}.diarization.json",
            }
        return {
            "ok": False,
            "error": combined[-1000:] or "samples failed",
            "fix": "run whosaid_doctor",
        }

    samples = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                samples = json.loads(line).get("samples", [])
            except json.JSONDecodeError:
                pass

    return {
        "ok": True,
        "base": base,
        "samples": samples,
        "summary": f"Exported {len(samples)} sample clip(s) — listen to each before trusting its label.",
    }


# ---------------------------------------------------------------------------
# Resource: whosaid://guide (on-demand deep detail — not in the always-loaded schema)
# ---------------------------------------------------------------------------
_GUIDE = """# whosaid — deep reference (whosaid://guide)

Local, GPU-accelerated, speaker-attributed transcription on Apple Silicon.
Nothing — audio, text, or voice embeddings — ever leaves the machine.

## Transcribe flags (whosaid_transcribe maps onto these)
- `audio` → one input file. Output base = the basename with any char outside
  `[A-Za-z0-9_-]` mapped to `_` (so `v1.2` → `v1_2`).
- `outdir` → `-o/--outdir DIR` (default: alongside the input).
- `accuracy` → `"fast"` uses the default **turbo** model
  (`mlx-community/whisper-large-v3-turbo`, ~1.5 GB, fastest); `"accurate"` passes
  `--accurate` = full **large-v3** (`mlx-community/whisper-large-v3-mlx`, slower,
  best fidelity on names/numbers).
- `format` → `-f/--format`: `txt | srt | vtt | tsv | json | all` (default `all`).
  Diarization needs the Whisper `.json`, so whosaid_transcribe silently upgrades
  any non-json/all format to `all` when `diarize=True`.
- `lang` → `-l/--lang CODE` (default `en`; `auto` to auto-detect).
- `speakers` → `--speakers N`: speaker-count hint (e.g. 2 for a 1:1 call). Omit to
  auto-detect the number of speakers.
- `expected_speakers` → `--expected-speakers NAME[,NAME…]` (repeatable): the roster
  of people you expect. **Registry-anchored diarization** — each turn is compared to
  the enrolled voiceprint of every listed person, and a turn at or above
  `anchor_threshold` (`--anchor-threshold`, default `0.70`, env
  `WHOSAID_ANCHOR_THRESHOLD`) is pinned to that person; the remaining turns are
  clustered into new speakers as usual. Every name must ALREADY be a known voice
  (enrolled clip or registry entry) or the run fails with the list of known names.
  A listed person who never speaks is dropped, so this is the right tool for a
  recurring team with varying attendance — unlike `speakers=N`, which forces an
  exact count and merges distinct people when fewer show up. It also forces the
  chunked diarization path at any length (anchoring needs per-turn voiceprints).
  The per-anchor result (`turns`, `mean_cosine`) comes back under `anchors`, and
  each anchored cluster appears in `registry_matches` with `pass: "anchor"`.
- `diarize=False` → `--no-diarize`: plain transcript only, no speaker labels.
- `match_threshold` → `--match-threshold F`: cosine a known voice must reach to
  claim a cluster (default `0.50`, env `WHOSAID_MATCH_THRESHOLD`). Omit it to use
  the default. Also accepted by whosaid_relabel (applies to `auto=True`).

CLI-only transcribe flags (not surfaced as MCP params; use the resource/CLI):
`-m/--model REPO`, `-n/--name NAME`, and the long-audio controls below.

## Output files (written next to the input, or in `outdir`)
- `<base>.txt` — plain transcript.
- `<base>.speakers.txt` — speaker-labeled transcript.
- `<base>.speaker-cards.txt` — one card per speaker (turn count, talk time, a few
  representative snippets) so you can tell who each `SPEAKER_NN` is.
- `<base>.diarization.json` — sidecar with segments + per-cluster voiceprints;
  whosaid_relabel reuses it to rename clusters with no re-transcription.
- `<base>.rttm` — standard diarization RTTM.

## Naming speakers (the registry)
- Unnamed clusters come back as `SPEAKER_00`, `SPEAKER_01`, … (biggest talker is
  `SPEAKER_00`). Read the speaker cards, then whosaid_relabel to assign names.
- Each name is persisted to the **local speaker registry**
  (`~/.config/whosaid/speakers.json`, override `WHOSAID_SPEAKER_DB`), keyed by the
  embedding model. A voice you name once is auto-named in every later transcript.
- Auto-naming matches a cluster's voiceprint to a known voice by **cosine
  similarity ≥ 0.50** (the `match_threshold` param / `--match-threshold` flag,
  alias `--ref-threshold`). Below that, the cluster keeps its `SPEAKER_NN` label
  rather than taking a low-confidence name. Enrolled clips in `voices/` (or
  `WHOSAID_VOICE_REFS`) are matched the same way. Raise it (e.g. 0.6) if you see
  wrong names; lower it to catch more.
- Every match decision — including near-misses — is machine-readable in
  `<base>.diarization.json` under `registry_matches`
  (`{cluster, name, similarity, threshold, matched, pass}`; `pass` is
  `anchor`/`registry`/`ref`/`absorb`; an `anchor` record carries an extra `turns`),
  and is returned by whosaid_transcribe.
- The sidecar also carries `source` (`path`, `duration_seconds`, `creation_time`
  from the container tag), so no separate `ffprobe` is needed for meeting timestamps.
- whosaid_list_speakers shows enrolled clips + registered voiceprints.
- whosaid_samples exports one short WAV per SPEAKER_NN cluster (longest segment,
  clamped to `seconds`) so you can listen before trusting a label — needs the
  sidecar's `source` metadata, so it only works on a transcript made after
  GitHub issue #1 parts 2-3 (or re-transcribe an older one).

## Long audio runs in parallel
Recordings over ~15 min (900 s) auto-chunk: the file is split into windows,
each window is segmented + embedded in its own process, then all voiceprints are
clustered globally so speakers stay consistent across chunk boundaries — several
times faster with the same result. Tuning (CLI flags):
- `--no-chunk` — force a single whole-file pass (disable chunking).
- `-j/--jobs N` — parallel diarization workers (default: auto).
- `--chunk-seconds S` — window length (default: auto; honored at any length).

## Environment overrides
- `WHOSAID_MODEL` — default Whisper model repo.
- `WHOSAID_LANG` — default language code.
- `WHOSAID_VOICE_REFS` — enrollment-clip directory (default `./voices`).
- `WHOSAID_SPEAKER_DB` — speaker registry path (default
  `~/.config/whosaid/speakers.json`).
- `WHOSAID_MATCH_THRESHOLD` — registry/reference match cosine threshold (default
  `0.50`); clusters below it stay `SPEAKER_NN`.
- `WHOSAID_ABSORB_THRESHOLD` — absorb-pass cosine threshold (default `0.85`).
- `DIARIZE_EMB_NAME` — speaker-embedding model (default NeMo TitaNet-small,
  English-native; set the zh-cn 3D-Speaker model for Mandarin audio). Voiceprints
  are keyed by this model, so switching it re-enrolls speakers.
- `WHOSAID_REC_DEVICE` — avfoundation audio device index (default 0; CLI record/enroll).
- `WHOSAID_INSTALL_DIR` — command install directory (default `~/.local/bin`).
- `HF_HOME` — Hugging Face cache (Whisper model cache).
- `SHERPA_DIARIZE_CACHE` — sherpa-onnx diarization model cache.

## Not exposed over MCP
`whosaid enroll` and `whosaid record` need a live terminal + Microphone
permission — run them from the CLI. To enroll from a file you already have, use
whosaid_enroll_from_file. Run whosaid_doctor first if a transcribe fails; each
missing prerequisite prints its exact fix (usually `whosaid setup`).
"""


@mcp.resource("whosaid://guide")
def guide() -> str:
    """Full flag / env / long-audio reference, read on demand by agents."""
    return _GUIDE


if __name__ == "__main__":
    mcp.run()
