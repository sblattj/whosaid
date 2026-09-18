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
import sys
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

# Shared meeting-workspace helpers (lib/wsconfig.py: config, meeting discovery,
# search-db path). `python lib/mcp_server.py` already has lib/ first on
# sys.path; importing this module from elsewhere (tests) works the same way
# once lib/ is added here.
_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import wsconfig  # noqa: E402

__version__ = "1.2.0"

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

Speakers can carry role tags (self/boss/peer/report/external) set via whosaid_relabel's `roles` param; roles appear in speaker cards and transcripts and tell you whose commitments matter most (boss ranks higher). "self" is the user's own voice.

Dev-commitments = the cross-meeting corpus of commitments the self-roled speaker made to others/team. Extracted per-meeting into commitments.md/commitments.json (via `python3 lib/workspace.py commitments --transcript T --json-out F`, or `whosaid ingest --commitments`), then rolled up into _COMMITMENTS.md/_commitments.json with stable CM-NNN ids. whosaid_worklist(owner="me") answers "what did I sign up for, ranked": the owner's commitments plus the action items they own, tiered P1/P2/P3 (boss-requested, blocking or deadline cues, repeat across meetings, recency, cue strength) with a score and why strings; the same view the roll-up writes to _WORKLIST-<Owner>.md.

Not exposed here: `enroll` and `record` are interactive microphone operations that need a live terminal. Run them from the `whosaid` CLI. The first transcribe downloads ~1.5 GB of models; run whosaid_doctor to check readiness. Full flag/env/long-audio reference: read the whosaid://guide resource.

Meeting workspace (search across many transcripts): point the server at a workspace directory by launching it with WHOSAID_WORKSPACE=<dir>, or pass `workspace` on each call; there is no cwd fallback. Flow: whosaid_search (turn-level hits with meeting folder + timestamp) -> whosaid_context (the verbatim minute around one hit; do not read whole transcripts) -> whosaid_items / whosaid_item / whosaid_person for action items and per-person commitments; whosaid_worklist for one person's ranked P1/P2/P3 worklist across both corpora. whosaid_meetings, whosaid_prs and whosaid_speakers list what the graph knows; whosaid_workspace_status says whether the index exists. The index (_search.db, _WIKI.md) is built by `whosaid index <ws>` from the CLI or the watcher, never from here: every workspace tool is read-only. Resources: whosaid://workspace/wiki, whosaid://workspace/action-items, whosaid://workspace/index, whosaid://workspace/meeting/{folder}/transcript and whosaid://workspace/meeting/{folder}/action-items. Everything stays local (SQLite FTS5 plus optional embeddings from a localhost Ollama)."""


try:
    # SDK 2.x MCPServer reports `version` in serverInfo; 1.x FastMCP has no such parameter.
    mcp = _Server(name="whosaid", instructions=SERVER_INSTRUCTIONS, version=__version__)
except TypeError:
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
    "voiceprints, absentees are dropped, and nobody has to guess a count. Any speaker "
    "role tags (self/boss/…) come back under `roles`. Keywords: "
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
    "sidecar, which folds phantom cluster splits into their real speaker. Optionally pass "
    "`roles` ({Name: role} — self/boss/peer/report/external) to role-tag speakers in the "
    "registry, cards, and transcripts. Keywords: rename speaker, "
    "label speaker, assign name, identify voice, who is SPEAKER_00, correct labels."
)

_DESC_LIST_SPEAKERS = (
    "List the voices whosaid can already auto-name: enrolled clips in `voices/` plus "
    "voiceprints saved in the local speaker registry (~/.config/whosaid/speakers.json). "
    "Read-only. Call this before whosaid_relabel so you reuse an existing name instead of "
    "creating a duplicate. Each registry entry carries its role tag (self/boss/…) when set. "
    "Keywords: known speakers, enrolled voices, registry, recognized "
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
    roles: Optional[dict] = None
    if sidecar_path.exists():
        try:
            data = json.loads(sidecar_path.read_text())
            names = dict(data.get("names") or {})
            registry_matches = data.get("registry_matches") or []
            source_meta = data.get("source")
            num_speakers = data.get("num_speakers")
            count_warning = data.get("count_warning")
            count_estimate = data.get("count_estimate")
            roles = data.get("roles")
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
        "roles": roles,
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
    roles: Optional[dict[str, str]] = None,
) -> dict:
    """Name SPEAKER_NN clusters and persist them, by shelling `whosaid relabel`.

    With auto=True the assignments map may be empty: whosaid re-applies registry
    matching + the absorb pass over the cached sidecar (no re-diarization),
    merging phantom cluster splits into their real speaker.

    `roles` optionally maps Name -> role (conventional: self, boss, peer,
    report, external; free-form allowed) and is passed through as repeatable
    `--role NAME=ROLE` flags; roles are saved to the registry and rendered in
    speaker cards and transcripts.
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

    role_specs: list = []
    if roles is not None:
        if not isinstance(roles, dict):
            return {
                "ok": False,
                "error": "roles must be a map of Name -> role (e.g. {'Karen':'boss'})",
                "fix": "pass a dict like {'Karen':'boss'} (roles: self, boss, peer, report, external) or omit it",
            }
        bad_roles = []
        for name, role in roles.items():
            name = str(name).strip()
            if not name:
                bad_roles.append(f"empty name for role '{role}'")
            elif not isinstance(role, str) or not role.strip():
                bad_roles.append(f"role for '{name}' must be a non-empty string")
            else:
                role_specs.append((name, role.strip().lower()))
        if bad_roles:
            return {
                "ok": False,
                "error": "invalid roles: " + "; ".join(bad_roles),
                "fix": "roles maps Name -> non-empty role string (e.g. {'Karen':'boss'}; "
                       "conventional: self, boss, peer, report, external)",
            }

    specs = [f"{k}={v}" for k, v in assignments.items()]
    args = ["relabel", base, *specs]
    if auto:
        args.append("--auto")
    if match_threshold is not None:
        args += ["--match-threshold", str(match_threshold)]
    for name, role in role_specs:
        args += ["--role", f"{name}={role}"]
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
        "roles_applied": sorted(name for name, _ in role_specs),
        "speakers_txt": speakers_txt,
        "registry_path": str(_speaker_db()),
        "summary": (
            (f"Re-applied registry + absorb naming from the sidecar"
             + (f" plus {len(assignments)} explicit assignment(s)" if assignments else "")
             + (f", {len(role_specs)} role(s) applied" if role_specs else "")
             + "." )
            if auto else
            (f"Renamed {len(assignments)} cluster(s)"
             + (f", tagged {len(role_specs)} role(s)" if role_specs else "")
             + "; saved to the local speaker registry "
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
                entry = {"name": s.get("name"), "role": s.get("role")}
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
# Meeting workspace (GitHub issue #14): read-only search / graph tools.
#
# Same rule as the tools above: nothing is re-implemented here. Every workspace
# tool shells one of the stdlib CLIs lib/search.py (FTS5 + optional embeddings
# over the workspace's *.speakers.txt) or lib/graph.py (people, meetings,
# action items, commitments, PR mentions) with --json and passes the JSON
# through. The index itself is built by `whosaid index <ws>` (CLI or watcher),
# never from here, so every tool below is read-only and idempotent.
#
# Workspace resolution for tools: the `workspace` argument, else the
# WHOSAID_WORKSPACE environment variable, else an error dict. An MCP server's
# cwd is whatever the client happened to launch it from, so unlike the CLIs
# (wsconfig.resolve_workspace) there is deliberately no cwd fallback.
# ---------------------------------------------------------------------------
LIB_DIR = REPO_DIR / "lib"
SEARCH_PY = LIB_DIR / "search.py"
GRAPH_PY = LIB_DIR / "graph.py"
WORKSPACE_PY = LIB_DIR / "workspace.py"
WS_TIMEOUT = 60.0  # seconds: local SQLite plus, for meaning search, one localhost Ollama call

WORKSPACE_FILES = ("_WIKI.md", "_ACTION-ITEMS.md", "_INDEX.md")
MEETING_ACTION_ITEMS = "action-items.md"  # per-meeting file written by `whosaid ingest --action-items`

_SEARCH_MODES = ("exact", "meaning", "hybrid")
_AT_RE = re.compile(r"^[0-9]{1,3}:[0-9]{2}(:[0-9]{2})?$")  # MM:SS or HH:MM:SS
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # e.g. AI-042
_MAX_K = 100
_MAX_WINDOW = 3600  # seconds of context either side; more than that is "read the transcript"


def _read_only() -> ToolAnnotations:
    """Annotations shared by every workspace tool: read-only, idempotent, local."""
    return ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )


def _ws_error(error: str, hint: str) -> dict:
    return {"ok": False, "error": error, "hint": hint}


def _resolve_ws(workspace: Optional[str]) -> tuple:
    """(workspace dir, None) or (None, error dict). Argument > $WHOSAID_WORKSPACE; never cwd."""
    raw = (workspace or "").strip() or os.environ.get("WHOSAID_WORKSPACE", "").strip()
    if not raw:
        return None, _ws_error(
            "no workspace given",
            "pass workspace=<meeting workspace dir> on the call, or launch the server with "
            "WHOSAID_WORKSPACE=<dir> so every workspace tool and resource defaults to it",
        )
    ws = Path(raw).expanduser()
    if not ws.is_dir():
        return None, _ws_error(
            f"workspace is not a directory: {ws}",
            "pass the directory that holds the meeting folders "
            "(and _search.db once `whosaid index` has run)",
        )
    return ws.resolve(), None


def _safe_folder(value: Optional[str]) -> bool:
    """A meeting folder is one path component: no separators, no '..', not hidden."""
    v = (value or "").strip()
    if not v or v.startswith("."):
        return False
    return "/" not in v and "\\" not in v and ".." not in v


def _run_ws(script: Path, args: list, ws: Path) -> tuple:
    """Run `python <script> <args...> --json` from the repo root.

    Returns (parsed JSON, None) or (None, error dict). Never raises: a missing
    script, a non-zero exit, a timeout, or unparsable stdout all come back as
    {"ok": False, "error": <last stderr line>, "hint": ...}, where the hint is
    `run: whosaid index <ws>` (the CLIs' own hint when the index is missing)
    or, for exit 2, an argument problem.
    """
    index_hint = f"run: whosaid index {ws}"
    if not script.is_file():
        return None, _ws_error(
            f"{script.name} is missing from {LIB_DIR}",
            "this whosaid checkout lacks the workspace search stack; reinstall or update it",
        )
    cmd = [sys.executable, str(script), *[str(a) for a in args], "--json"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True,
            timeout=WS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, _ws_error(
            f"{script.name} {args[0] if args else ''} timed out after {WS_TIMEOUT:.0f}s".replace("  ", " "),
            "retry with a narrower query, or check that the local Ollama is reachable "
            "if meaning search is enabled",
        )
    except OSError as exc:
        return None, _ws_error(f"could not start {script.name}: {exc}", "run whosaid_doctor")
    if proc.returncode != 0:
        lines = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
        last = lines[-1] if lines else (proc.stdout or "").strip()[-500:]
        if proc.returncode == 2:
            hint = "check the arguments (usage error)"
        elif "whosaid index" in last or not wsconfig.search_db(ws).is_file():
            hint = index_hint
        else:  # the index exists and the CLI did not ask for a rebuild: an argument problem
            hint = f"the index exists; check the arguments (rebuild if stale with: whosaid index {ws})"
        return None, _ws_error(last or f"{script.name} exited {proc.returncode}", hint)
    try:
        return json.loads(proc.stdout or "null"), None
    except json.JSONDecodeError as exc:
        return None, _ws_error(f"{script.name} returned unparsable JSON: {exc}", index_hint)


def _as_list(payload, key: str) -> list:
    """The CLIs emit a bare JSON array; tolerate a {key: [...]} wrapper too."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return payload[key]
    return []


_DESC_WS_SEARCH = (
    "Search every speaker-labeled transcript in a meeting workspace and get back the matching "
    "TURNS (meeting folder, timestamp, speaker, text, score), not whole files. mode: exact "
    "(SQLite FTS5 full text; quoted phrases and prefix* work), meaning (local Ollama "
    "embeddings; falls back to exact when the index has none), hybrid (both, the default). "
    "Filter by `speaker` label or `meeting` folder; `k` caps the hits (1-100). Then call "
    "whosaid_context on a hit to read the verbatim minute around it. Read-only: the index is "
    "built by `whosaid index <ws>` from the CLI, never here. Workspace = the `workspace` "
    "argument or WHOSAID_WORKSPACE. Keywords: search transcripts, who said, find a quote, "
    "when did we discuss, meeting search, full text, semantic search."
)

_DESC_WS_CONTEXT = (
    "Read the verbatim transcript turns around ONE moment of ONE meeting: `meeting` is the "
    "workspace folder name from a search hit, `at` its timestamp (HH:MM:SS or MM:SS), "
    "`before`/`after` the window in seconds (default 60 s before, 120 s after). Call this "
    "after whosaid_search instead of loading a whole transcript: it is the cheap way to "
    "confirm what was actually said and by whom. Read-only. Keywords: context, surrounding "
    "turns, what was said before, exact quote, verify a hit."
)

_DESC_WS_ITEMS = (
    "List action items from the workspace's entity graph, filtered by `owner`, `requester`, "
    "`status` or `type`. Each item carries its id (AI-NNN), text, owner, status, the meetings "
    "it was raised in and any commitments. Built by `whosaid index`; read-only. Keywords: "
    "action items, todos, commitments, who owes what, open items, follow-ups."
)

_DESC_WS_ITEM = (
    "One action item by id (AI-NNN) with its full history: every meeting it came up in, the "
    "timestamped turns where it was discussed, commitments and status changes, related PRs. "
    "Use whosaid_context on those timestamps for the verbatim discussion. Read-only."
)

_DESC_WS_PERSON = (
    "Per-person commitments view (GitHub issue #13): what `name` owns, what they asked "
    "others for, deadlines they gave, and the meetings they were in. Omit `name` to list "
    "every known person with counts. Names are speaker labels exactly as they appear in the "
    "transcripts (see whosaid_speakers). Read-only. Keywords: my action items, what does X "
    "owe, commitments by person, who promised what."
)

_DESC_WS_MEETINGS = (
    "List the meetings in the workspace as the graph knows them: folder, date, duration, "
    "attendees (speaker labels), action-item count. The folder names are what whosaid_search "
    "hits carry, what whosaid_context takes, and what the "
    "whosaid://workspace/meeting/{folder}/... resources use. Read-only."
)

_DESC_WS_PRS = (
    "List pull-request mentions found in the transcripts: PR numbers or links, who mentioned "
    "them, in which meeting and when. Read-only. Keywords: PR, pull request, code review, "
    "merged, deployed."
)

_DESC_WS_SPEAKERS = (
    "List every speaker label in the search index with turn counts and the meetings they "
    "appear in. Use the exact labels as the `speaker` filter of whosaid_search or the `name` "
    "of whosaid_person. Read-only."
)

_DESC_WS_WORKLIST = (
    "Ranked personal worklist (GitHub issue #13): what `owner` signed up for across every "
    "meeting, from the roll-up corpora (dev-commitments CM-NNN plus the action items AI-NNN "
    "they own), each item tiered P1/P2/P3 with a numeric score and short why strings. "
    "P1 = boss-requested, a blocking/urgency cue, a deadline cue, or seen in 3+ meetings; "
    "P2 = seen in 2 meetings, requested by anyone, or a strong cue in the latest meeting; "
    "P3 = the rest; negated items are never P1. owner is a speaker label or 'me' (the "
    "self-roled speaker, else the whosaid.toml [workspace] owner). Deterministic, no LLM, "
    "same view as _WORKLIST-<Owner>.md. Read-only. Keywords: my worklist, what did I "
    "promise, what did I sign up for, my priorities, ranked commitments."
)
_DESC_WS_STATUS = (
    "Read-only health check for a meeting workspace: whether the search index (_search.db) "
    "exists and what it holds (meetings, turns, embeddings), whether the rendered _WIKI.md, "
    "_ACTION-ITEMS.md and _INDEX.md exist, how many meeting folders there are, and the "
    "configured owner and group names from whosaid.toml. Run this first when a search returns "
    "nothing: it tells you whether `whosaid index <ws>` has been run. Keywords: workspace "
    "status, is it indexed, index health."
)


# ---------------------------------------------------------------------------
# 7) whosaid_search
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_search", description=_DESC_WS_SEARCH, annotations=_read_only())
def whosaid_search(
    query: str,
    mode: Literal["exact", "meaning", "hybrid"] = "hybrid",
    speaker: Optional[str] = None,
    meeting: Optional[str] = None,
    k: int = 10,
    workspace: Optional[str] = None,
) -> dict:
    """Shell `search.py query <ws> <query> --mode M -k N [--speaker S] [--meeting M] --json`.

    Returns {"ok", "workspace", "mode", "hits": [{meeting, t_sec, t_str, speaker,
    text, score, source}], "count"} plus "engine" when the CLI reports one.
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    if not (query or "").strip():
        return _ws_error("query is empty", "pass a word, phrase, or question to search for")
    if mode not in _SEARCH_MODES:
        return _ws_error(f"invalid mode '{mode}'", "use one of: " + ", ".join(_SEARCH_MODES))
    try:
        k = int(k)
    except (TypeError, ValueError):
        return _ws_error(f"invalid k '{k}'", f"pass an integer between 1 and {_MAX_K}")
    if not 1 <= k <= _MAX_K:
        return _ws_error(f"k must be between 1 and {_MAX_K}, got {k}", "lower k, then page by narrowing the query")
    if meeting is not None and not _safe_folder(meeting):
        return _ws_error(f"invalid meeting folder '{meeting}'", "pass a folder name from whosaid_meetings (no path separators)")

    args = ["query", ws, query.strip(), "--mode", mode, "-k", k]
    if speaker and speaker.strip():
        args += ["--speaker", speaker.strip()]
    if meeting:
        args += ["--meeting", meeting.strip()]

    payload, err = _run_ws(SEARCH_PY, args, ws)
    if err:
        return err
    hits = _as_list(payload, "hits")
    out = {"ok": True, "workspace": str(ws), "mode": mode, "hits": hits, "count": len(hits)}
    if isinstance(payload, dict) and payload.get("engine"):
        out["engine"] = payload["engine"]
    if hits:
        out["next_step"] = "call whosaid_context(meeting, t_str) on a hit for the verbatim turns around it"
    else:
        out["next_step"] = "no hits: loosen the query, or run whosaid_workspace_status to confirm the index exists"
    return out


# ---------------------------------------------------------------------------
# 8) whosaid_context
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_context", description=_DESC_WS_CONTEXT, annotations=_read_only())
def whosaid_context(
    meeting: str,
    at: str,
    before: int = 60,
    after: int = 120,
    workspace: Optional[str] = None,
) -> dict:
    """Shell `search.py context <ws> <meeting> <at> --before B --after A --json`.

    The one to call after a whosaid_search hit: it returns just the verbatim
    turns around `at` ({"meeting", "at", "turns": [{t_sec, t_str, speaker,
    text}], "count"}) instead of a whole transcript.
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    if not _safe_folder(meeting):
        return _ws_error(f"invalid meeting folder '{meeting}'", "pass a folder name from whosaid_meetings or a search hit (no path separators)")
    at = (at or "").strip()
    if not _AT_RE.match(at):
        return _ws_error(f"invalid at '{at}'", "use HH:MM:SS or MM:SS, e.g. the t_str of a search hit")
    for label, value in (("before", before), ("after", after)):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return _ws_error(f"invalid {label} '{value}'", f"pass whole seconds between 0 and {_MAX_WINDOW}")
        if not 0 <= value <= _MAX_WINDOW:
            return _ws_error(f"{label} must be between 0 and {_MAX_WINDOW} seconds, got {value}", "narrow the window; read the transcript resource for more")
    before, after = int(before), int(after)

    args = ["context", ws, meeting.strip(), at, "--before", before, "--after", after]
    payload, err = _run_ws(SEARCH_PY, args, ws)
    if err:
        return err
    turns = _as_list(payload, "turns")
    return {
        "ok": True,
        "workspace": str(ws),
        "meeting": meeting.strip(),
        "at": at,
        "before": before,
        "after": after,
        "turns": turns,
        "count": len(turns),
    }


# ---------------------------------------------------------------------------
# 9) whosaid_items
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_items", description=_DESC_WS_ITEMS, annotations=_read_only())
def whosaid_items(
    owner: Optional[str] = None,
    requester: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 - the MCP-facing name; mirrors `graph.py items --type`
    workspace: Optional[str] = None,
) -> dict:
    """Shell `graph.py items <ws> [--owner ..] [--requester ..] [--status ..] [--type ..] --json`."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    args = ["items", ws]
    for flag, value in (("--owner", owner), ("--requester", requester), ("--status", status), ("--type", type)):
        if value is not None and str(value).strip():
            args += [flag, str(value).strip()]
    payload, err = _run_ws(GRAPH_PY, args, ws)
    if err:
        return err
    items = _as_list(payload, "items")
    return {"ok": True, "workspace": str(ws), "items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# 10) whosaid_item
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_item", description=_DESC_WS_ITEM, annotations=_read_only())
def whosaid_item(
    id: str,  # noqa: A002 - the MCP-facing name; the AI-NNN id from whosaid_items
    workspace: Optional[str] = None,
) -> dict:
    """Shell `graph.py item <ws> <id> --json` and return {"ok", "id", "item": {...}}."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    item_id = (id or "").strip()
    if not _ITEM_ID_RE.match(item_id):
        return _ws_error(f"invalid item id '{id}'", "pass an id such as AI-042 (from whosaid_items)")
    payload, err = _run_ws(GRAPH_PY, ["item", ws, item_id], ws)
    if err:
        return err
    return {"ok": True, "workspace": str(ws), "id": item_id, "item": payload}


# ---------------------------------------------------------------------------
# 11) whosaid_person
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_person", description=_DESC_WS_PERSON, annotations=_read_only())
def whosaid_person(
    name: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Shell `graph.py person <ws> [Name] --json`.

    With `name`: {"ok", "name", "person": {...}} (that person's commitments view).
    Without: {"ok", "people": [...], "count"} (everyone the graph knows).
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    who = (name or "").strip()
    args = ["person", ws] + ([who] if who else [])
    payload, err = _run_ws(GRAPH_PY, args, ws)
    if err:
        return err
    if who:
        return {"ok": True, "workspace": str(ws), "name": who, "person": payload}
    people = _as_list(payload, "people")
    return {"ok": True, "workspace": str(ws), "people": people, "count": len(people)}


# ---------------------------------------------------------------------------
# 12) whosaid_meetings, 13) whosaid_prs, 14) whosaid_speakers
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_meetings", description=_DESC_WS_MEETINGS, annotations=_read_only())
def whosaid_meetings(workspace: Optional[str] = None) -> dict:
    """Shell `graph.py meetings <ws> --json` and return {"ok", "meetings": [...], "count"}."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    payload, err = _run_ws(GRAPH_PY, ["meetings", ws], ws)
    if err:
        return err
    meetings = _as_list(payload, "meetings")
    return {"ok": True, "workspace": str(ws), "meetings": meetings, "count": len(meetings)}


@mcp.tool(name="whosaid_prs", description=_DESC_WS_PRS, annotations=_read_only())
def whosaid_prs(workspace: Optional[str] = None) -> dict:
    """Shell `graph.py prs <ws> --json` and return {"ok", "prs": [...], "count"}."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    payload, err = _run_ws(GRAPH_PY, ["prs", ws], ws)
    if err:
        return err
    prs = _as_list(payload, "prs")
    return {"ok": True, "workspace": str(ws), "prs": prs, "count": len(prs)}


@mcp.tool(name="whosaid_speakers", description=_DESC_WS_SPEAKERS, annotations=_read_only())
def whosaid_speakers(workspace: Optional[str] = None) -> dict:
    """Shell `search.py speakers <ws> --json` and return {"ok", "speakers": [...], "count"}."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    payload, err = _run_ws(SEARCH_PY, ["speakers", ws], ws)
    if err:
        return err
    speakers = _as_list(payload, "speakers")
    return {"ok": True, "workspace": str(ws), "speakers": speakers, "count": len(speakers)}


# ---------------------------------------------------------------------------
# 15) whosaid_workspace_status
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_workspace_status", description=_DESC_WS_STATUS, annotations=_read_only())
def whosaid_workspace_status(workspace: Optional[str] = None) -> dict:
    """`search.py status <ws> --json` plus local file checks and the whosaid.toml owner.

    Always returns ok=True once the workspace resolves: a missing index is a
    finding ("search_error" + "hint"), not a failure. Config is reported as the
    owner label and the group NAMES only, never group members.
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    status, serr = _run_ws(SEARCH_PY, ["status", ws], ws)
    cfg = wsconfig.load_config(ws)
    files = {name: (ws / name).is_file() for name in WORKSPACE_FILES}
    index_present = wsconfig.search_db(ws).is_file()
    meeting_folders = len(wsconfig.iter_meetings(ws))
    owner = (cfg.get("workspace") or {}).get("owner") or None
    groups = list((cfg.get("groups") or {}).keys())

    out = {
        "ok": True,
        "workspace": str(ws),
        "index_present": index_present,
        "index_path": str(wsconfig.search_db(ws)),
        "search": status if serr is None else None,
        "files": files,
        "meeting_folders": meeting_folders,
        "config_present": (ws / wsconfig.CONFIG_NAME).is_file(),
        "owner": owner,
        "groups": groups,
    }
    if serr is not None:
        out["search_error"] = serr["error"]
        out["hint"] = serr["hint"]
    missing = [name for name, present in files.items() if not present]
    out["summary"] = (
        f"{meeting_folders} meeting folder(s); index "
        + ("present" if index_present else "MISSING (run: whosaid index <ws>)")
        + (f"; missing {', '.join(missing)}" if missing else "; wiki, action items and index rendered")
        + (f"; owner {owner}" if owner else "; no owner configured")
        + "."
    )
    return out


# ---------------------------------------------------------------------------
# 16) whosaid_worklist
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_worklist", description=_DESC_WS_WORKLIST, annotations=_read_only())
def whosaid_worklist(owner: str = "me", workspace: Optional[str] = None) -> dict:
    """Shell `workspace.py worklist <ws> --owner <owner> --json`.

    Returns {"ok", "workspace", "owner", "generated_from": [meetings], "items": [...],
    "count"}, each item {id, source, text, status, tier, score, why, first_seen,
    last_seen, occurrences, requested_by, negative, also}. Reads _commitments.json
    and _action-items.json only (the roll-up writes them), never the index.
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    who = (owner or "").strip() or "me"
    payload, err = _run_ws(WORKSPACE_PY, ["worklist", ws, "--owner", who], ws)
    if err:
        # _run_ws's default hint points at the search index, which this view never reads.
        err["hint"] = (
            f"run: whosaid roll-up {ws} --action-items (builds the corpora); pass owner="
            "<speaker label> when no 'self' role or [workspace] owner is set"
        )
        return err
    data = payload if isinstance(payload, dict) else {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {
        "ok": True,
        "workspace": str(ws),
        "owner": data.get("owner") or who,
        "generated_from": data.get("generated_from") or [],
        "items": items,
        "count": len(items),
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

## Speaker roles and dev-commitments
Roles tag who each speaker IS to the user, so commitments can be ranked:
- Registry entries may carry an optional `"role"` string. Conventional roles:
  `self` (the user's own voice), `boss`, `peer`, `report`, `external` —
  free-form values allowed.
- Set them via whosaid_relabel's `roles` param ({Name: role}), or the CLI:
  `whosaid relabel <base> SPEAKER_XX=Name ... --role NAME=ROLE` (repeatable;
  forwarded to the diarizer as `--save-role`). Roles persist in the registry,
  come back from whosaid_list_speakers and whosaid_transcribe (`roles` key),
  and appear in the diarization sidecar's top-level `"roles": {name: role}`
  (key omitted when empty).
- `.speakers.txt` may carry `# Role: NAME = ROLE` header lines right after
  the `# Speakers (N):` header; speaker cards render `NAME  [role]`.
- **Dev-commitments** are the cross-meeting corpus of commitments the
  `self`-roled speaker made to others/team. Per meeting:
  `python3 lib/workspace.py commitments --transcript T --json-out F
  [--hook CMD] [--roles JSON]` (env `WHOSAID_COMMITMENTS_HOOK`) writes
  `commitments.md` + `commitments.json` next to the json-out. The stdlib
  heuristic (no LLM) picks up first-person cues ("I'll ...", "I will ...",
  "I plan to ..."); with roles present only the `self` speaker's cues count
  (with no roles, every speaker's do — legacy transcripts); a cue requested
  by a different speaker's question/imperative records `requested_by`, and
  boss requests get priority "high". `whosaid ingest --commitments` runs it
  during ingest.
- The workspace roll-up folds each meeting's commitments.json into
  `_COMMITMENTS.md` / `_commitments.json` with stable `CM-NNN` ids (never
  renumbered; difflib dedupe, like the action-items corpus) — boss-requested
  items flagged, boss requests ranking highest for follow-up. Dedupe also
  accepts embedding cosine >= [commitments] embed_threshold (0.90) when a
  loopback Ollama answers and [search] embed is on; otherwise difflib only.
- **Worklist**: whosaid_worklist(owner="me") (CLI: `whosaid commitments <ws>
  [--owner NAME|me] [--all-owners] [--json]`; the roll-up writes the same
  view to `_WORKLIST-<Owner>.md`) ranks the owner's open CM-NNN commitments
  plus the AI-NNN action items they own (name match with '_'/' '
  interchangeable, plus [workspace] aliases; a look-alike action item folds
  into the commitment as "(also AI-NNN)"). Tiers: P1 = boss-requested, a
  blocking/urgency cue, a deadline cue, or 3+ meetings; P2 = 2 meetings,
  requested by anyone, or a strong cue in the latest meeting; P3 = the rest;
  negated items never P1. Score, why strings and the cue lists come from
  whosaid.toml [commitments] (boss, deadline_cues, blocking_cues,
  strong_cues, weak_cues, weights, embed_threshold). Deterministic, no LLM.

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
- `WHOSAID_COMMITMENTS_HOOK` — external commitments extractor hook (replaces
  the stdlib heuristic; receives WHOSAID_ROLES JSON).
- `DIARIZE_EMB_NAME` — speaker-embedding model (default NeMo TitaNet-small,
  English-native; set the zh-cn 3D-Speaker model for Mandarin audio). Voiceprints
  are keyed by this model, so switching it re-enrolls speakers.
- `WHOSAID_REC_DEVICE` — avfoundation audio device index (default 0; CLI record/enroll).
- `WHOSAID_INSTALL_DIR` — command install directory (default `~/.local/bin`).
- `HF_HOME` — Hugging Face cache (Whisper model cache).
- `SHERPA_DIARIZE_CACHE` — sherpa-onnx diarization model cache.

## Meeting workspace (search, graph, resources)
A workspace is a directory of meeting folders, each holding a `*.speakers.txt`
transcript (from `whosaid ingest`) and optionally `action-items.md`. `whosaid
index <ws>` builds `_search.db` (SQLite FTS5 over every turn, optional
`nomic-embed-text` embeddings from a localhost Ollama, and the entity graph:
people, meetings, action items, commitments, PR mentions) plus `_WIKI.md`.
`whosaid roll-up <ws> [--action-items]` renders `_INDEX.md` and `_ACTION-ITEMS.md`.
- Workspace for tools: the `workspace` argument, else `WHOSAID_WORKSPACE`. No
  cwd fallback. Resources read `WHOSAID_WORKSPACE` only.
- whosaid_search -> whosaid_context -> whosaid_items / whosaid_item /
  whosaid_person; whosaid_meetings, whosaid_prs, whosaid_speakers list the graph;
  whosaid_workspace_status reports index + rendered files + config owner.
- Resources: `whosaid://workspace/wiki` (_WIKI.md), `whosaid://workspace/action-items`
  (_ACTION-ITEMS.md), `whosaid://workspace/index` (_INDEX.md),
  `whosaid://workspace/meeting/{folder}/transcript` (the folder's speaker
  transcript(s)), `whosaid://workspace/meeting/{folder}/action-items`.
- Nothing here writes: rebuild with `whosaid index <ws>` (the watcher does it
  after each ingest). A missing index comes back as an error dict with that hint.

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


# ---------------------------------------------------------------------------
# Resources: whosaid://workspace/... (GitHub issue #14)
#
# Resource URIs carry no arguments beyond the {folder} template, so these read
# the workspace from WHOSAID_WORKSPACE only. Every failure (no workspace, no
# such file, bad folder) is returned as a short explanatory text, never raised,
# so a client that lists-then-reads never sees a protocol error.
#
# `@mcp.resource("...{folder}...")` on a function taking `folder` registers a
# resource TEMPLATE under both SDK majors (FastMCP 1.x and MCPServer 2.x); the
# SDK matches the concrete URI and passes the segment in. The 2.x matcher does
# not let "{folder}" span a "/", and _safe_folder rejects "..", so a read can
# never leave the workspace.
# ---------------------------------------------------------------------------
def _resource_ws() -> tuple:
    """(workspace dir, None) or (None, explanatory text). Resources: env only."""
    ws, err = _resolve_ws(None)
    if err:
        return None, (
            f"whosaid: {err['error']}. Resources read the workspace from WHOSAID_WORKSPACE; "
            "launch the server with it set (tools also accept a `workspace` argument)."
        )
    return ws, None


def _workspace_file(name: str, built_by: str) -> str:
    """Text of <ws>/<name>, or a one-line note saying what writes it."""
    ws, note = _resource_ws()
    if note:
        return note
    text = _read_text_or_none(ws / name)
    if text is None:
        return f"{name} is not in {ws} yet; it is written by `{built_by}`."
    return text


def _meeting_dir(folder: str) -> tuple:
    """(meeting dir, None) or (None, explanatory text) for a {folder} template value."""
    ws, note = _resource_ws()
    if note:
        return None, note
    if not _safe_folder(folder):
        return None, (
            f"invalid meeting folder '{folder}': pass one folder name from whosaid_meetings "
            "(no path separators, no '..')."
        )
    meeting = ws / folder.strip()
    if not meeting.is_dir():
        return None, f"no meeting folder '{folder}' in {ws}; list them with whosaid_meetings."
    return meeting, None


@mcp.resource(
    "whosaid://workspace/wiki",
    description="The generated workspace wiki (_WIKI.md) built by `whosaid index <ws>`.",
    mime_type="text/markdown",
)
def workspace_wiki() -> str:
    """_WIKI.md from WHOSAID_WORKSPACE, or a note saying it is not built yet."""
    return _workspace_file("_WIKI.md", "whosaid index <ws>")


@mcp.resource(
    "whosaid://workspace/action-items",
    description="The deduplicated action-item corpus (_ACTION-ITEMS.md) rendered by `whosaid roll-up <ws> --action-items`.",
    mime_type="text/markdown",
)
def workspace_action_items() -> str:
    """_ACTION-ITEMS.md from WHOSAID_WORKSPACE, or a note saying it is not rendered yet."""
    return _workspace_file("_ACTION-ITEMS.md", "whosaid roll-up <ws> --action-items")


@mcp.resource(
    "whosaid://workspace/index",
    description="The meeting index (_INDEX.md: one row per meeting plus the nothing-missing audit) rendered by `whosaid roll-up <ws>`.",
    mime_type="text/markdown",
)
def workspace_index() -> str:
    """_INDEX.md from WHOSAID_WORKSPACE, or a note saying it is not rendered yet."""
    return _workspace_file("_INDEX.md", "whosaid roll-up <ws>")


@mcp.resource(
    "whosaid://workspace/meeting/{folder}/transcript",
    description="One meeting's speaker-labeled transcript (its *.speakers.txt, joined if there are several). Prefer whosaid_context for a slice.",
    mime_type="text/plain",
)
def meeting_transcript(folder: str) -> str:
    """Every *.speakers.txt in <ws>/<folder>, joined with a `# <file>` header each."""
    meeting, note = _meeting_dir(folder)
    if note:
        return note
    files = sorted(p for p in meeting.glob("*.speakers.txt") if p.is_file())
    if not files:
        return f"no *.speakers.txt in {meeting}; it is written by `whosaid ingest`."
    if len(files) == 1:
        return _read_text_or_none(files[0]) or f"{files[0].name} is empty."
    parts = []
    for f in files:
        parts.append(f"# {f.name}\n" + (_read_text_or_none(f) or "(empty)"))
    return "\n\n".join(parts)


@mcp.resource(
    "whosaid://workspace/meeting/{folder}/action-items",
    description="One meeting's action-items.md, written by `whosaid ingest --action-items`.",
    mime_type="text/markdown",
)
def meeting_action_items(folder: str) -> str:
    """<ws>/<folder>/action-items.md, or a note saying it is not written yet."""
    meeting, note = _meeting_dir(folder)
    if note:
        return note
    text = _read_text_or_none(meeting / MEETING_ACTION_ITEMS)
    if text is None:
        return (
            f"no {MEETING_ACTION_ITEMS} in {meeting} yet; it is written by "
            "`whosaid ingest --action-items` (or the summarizer over this folder)."
        )
    return text


if __name__ == "__main__":
    mcp.run()
