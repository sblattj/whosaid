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

Everything stays on the machine — audio, text, and voice embeddings never leave. (The one opt-in
exception is outside this server: a workspace whose whosaid.toml sets [summarizer] engine = "claude"
sends transcript text to Anthropic when ingest drafts action items.)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
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
import live as live_mod  # noqa: E402
import live_insights  # noqa: E402

# Single source of truth: lib/__init__.py (pyproject.toml reads it too, via
# hatch's dynamic version). Import both ways so the file works as a package
# module (whosaid.mcp_server in the wheel) and as a script (python lib/...).
try:
    from . import __version__  # type: ignore[attr-defined]
except ImportError:
    from __init__ import __version__  # type: ignore[attr-defined]  # noqa: F401

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


def _suggested_max_speakers(sidecar: dict) -> Optional[int]:
    """The #59 `--max-speakers N` hint from a sidecar, or None.

    The top-level key is authoritative; count_estimate.suggested_max is the same
    number one level down. Anything that is not a positive int reads as None."""
    value = sidecar.get("suggested_max_speakers")
    if value is None:
        value = (sidecar.get("count_estimate") or {}).get("suggested_max")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


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
SERVER_INSTRUCTIONS = """whosaid turns audio into a transcript where every turn is labeled with who spoke. It runs 100% locally on Apple Silicon via the MLX Whisper GPU pipeline plus sherpa-onnx diarization — audio, text, and voice embeddings NEVER leave the machine; there are no API keys and no cloud calls. (One opt-in exception, never made by these tools: a workspace configured with [summarizer] engine = "claude" has its action items drafted by Claude, so ingest sends transcript text to Anthropic; the item notes say so.)

Output-file contract: each transcribe writes, using <base> = the input's basename (or your `name`, with any character outside [A-Za-z0-9_-] mapped to '_'): <base>.txt (plain transcript), <base>.speakers.txt (speaker-labeled), <base>.speaker-cards.txt (one card per speaker: turn count, talk time, sample snippets — to tell who each SPEAKER_NN is), and <base>.diarization.json (a sidecar that whosaid_relabel reuses). Diarization results include `count_before_fold`, `count_after_fold`, and `fold_note`; these are physical speaker-cluster counts before and after the anonymous-phantom check, not identity claims.

Typical workflow: whosaid_transcribe -> read the speaker cards in the result -> whosaid_samples if you want to LISTEN to a clip per cluster before trusting a name -> whosaid_relabel to put real names on the SPEAKER_NN clusters. Relabeling saves each name to a persistent local registry, so the same voice is auto-named in every later transcript. whosaid_list_speakers shows who is already known.

Speakers can carry role tags (self/boss/peer/report/external) set via whosaid_relabel's `roles` param; roles appear in speaker cards and transcripts and tell you whose commitments matter most (boss ranks higher). "self" is the user's own voice.

Dev-commitments = the cross-meeting corpus of commitments the self-roled speaker made to others/team. Extracted per-meeting into commitments.md/commitments.json (via `python3 lib/workspace.py commitments --transcript T --json-out F`, or `whosaid ingest --commitments`), then rolled up into _COMMITMENTS.md/_commitments.json with stable CM-NNN ids. whosaid_worklist(owner="me") ranks the owner's commitments plus the action items they own with P1/P2/P3, a score and why strings. These are heuristic ranks, distinct from leadership's stated priorities. Filter by kind, status or accepted_only before paging; an omitted limit caps every page at 50. If it times out, read the error's category/phase/hint; a compatible cached current result may be attached with stale and generated_at. Historical as_of queries cannot use a current cache.

Commitment tracking: kind distinguishes explicit promises, requests, standing directives, suggestions and inferred next steps. An extracted owner does not establish acceptance; accepted_owner and acceptance_evidence make that distinction. Items and worklists include completed history, evidence and remaining milestones. as_of reconciles recorded event time, not what was known at that time. whosaid_priorities(group="leadership") reads the configured group's recorded assertions, priority changes and conflicts. whosaid_tracking_suggestions(owner="me") finds later indexed statements that could show completion; every suggestion requires review and none changes the tracking ledger. Use whosaid_item for AI-NNN or CM-NNN evidence detail.

Not exposed here: `enroll` and `record` are interactive microphone operations that need a live terminal. Run them from the `whosaid` CLI. The first transcribe downloads ~1.5 GB of models; run whosaid_doctor to check readiness. Full flag/env/long-audio reference: read the whosaid://guide resource.

Meeting workspace (search across many transcripts): point the server at a workspace directory by launching it with WHOSAID_WORKSPACE=<dir>, or pass `workspace` on each call; there is no cwd fallback. Flow: whosaid_search (turn-level hits with meeting folder + timestamp) -> whosaid_context (the verbatim minute around one hit; do not read whole transcripts) -> whosaid_items / whosaid_item / whosaid_person for action items and per-person commitments; whosaid_worklist for one person's ranked P1/P2/P3 worklist across both corpora. whosaid_meetings, whosaid_prs and whosaid_speakers list what the graph knows; whosaid_workspace_status says whether the index exists. The index (_search.db, _WIKI.md) is built by `whosaid index <ws>` from the CLI or the watcher, never from here: every workspace tool is read-only except whosaid_set_status, which appends one status event to the tracking ledger (_tracking.json) when the user marks an item. Resources: whosaid://workspace/wiki, whosaid://workspace/action-items, whosaid://workspace/index, whosaid://workspace/meeting/{folder}/transcript and whosaid://workspace/meeting/{folder}/action-items. Everything stays local (SQLite FTS5 plus optional embeddings from a localhost Ollama)."""


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
    "role tags (self/boss/…) come back under `roles`. If diarization fails the result is "
    "ok=false, partial=true with `diarization_error`; the plain transcript is still written. "
    "Keywords: "
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
    "sidecar, which folds phantom cluster splits into their real speaker. With auto=true, pass "
    "fold_unknown=true to repair anonymous phantom clusters in a cached sidecar. auto=true also "
    "matches the enrollment clips in voices/ and drops auto-assigned names whose registry entry "
    "and clip are gone; pass forget=['Name'] (with auto=true) to clear a name from this "
    "meeting's clusters first (it can come back if still enrolled and matching). Optionally pass "
    "`roles` ({Name: role} — self/boss/peer/report/external) to role-tag speakers in the "
    "registry, cards, and transcripts. Pass no_save=true for a transcript-only label: sidecar "
    "+ outputs are renamed but the registry is untouched (composes with auto=true); note adds "
    "a provenance string stored with each local label; force=true overrides the guard that "
    "refuses replacing a registry print whose similarity to the cluster is below "
    "match_threshold; blend=true averages the cluster into the existing registry print instead "
    "of replacing it (same guard), to strengthen a voiceprint from a new recording. Keywords: rename speaker, label speaker, assign name, identify voice, "
    "who is SPEAKER_00, correct labels."
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


def _diarization_reason(proc) -> str:
    """The most specific failure line from the CLI's output, for diarization_error."""
    lines = [ln.strip() for ln in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines() if ln.strip()]
    for pat in ("Error:", "Exception", "[E:", "ERROR", "error:", "failed"):
        hits = [ln for ln in lines if pat in ln and "plain transcript" not in ln]
        if hits:
            return hits[-1][-500:]
    return f"no speaker output from this run (exit {proc.returncode})"


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
    accuracy: Literal["default", "fast", "accurate"] = "default",
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
    # "default" passes nothing, so the CLI default (large-v3, or WHOSAID_MODEL) applies;
    # "fast" must ask for turbo explicitly now that the default is large-v3.
    if accuracy == "accurate":
        args.append("--accurate")
    elif accuracy == "fast":
        args.append("--fast")
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
    # No speaker hint passed: an output folder inside a workspace takes that
    # workspace's whosaid.toml [diarize] hints, exactly as `whosaid ingest` does.
    speaker_hints_from: Optional[str] = None
    if diarize and not any(a in wsconfig.SPEAKER_FLAGS for a in args):
        ws = wsconfig.find_workspace(Path(out))
        if ws is not None:
            ws_args, ws_err = wsconfig.workspace_speaker_args(ws)
            if ws_err:
                return {
                    "ok": False,
                    "error": f"{ws / wsconfig.CONFIG_NAME}: {ws_err}",
                    "fix": "fix the [diarize] section, or pass speakers/min_speakers/max_speakers explicitly",
                }
            if ws_args:
                args += ws_args
                speaker_hints_from = f"{ws / wsconfig.CONFIG_NAME} [diarize]: {' '.join(ws_args)}"
    if not diarize:
        args.append("--no-diarize")
    if match_threshold is not None:
        args += ["--match-threshold", str(match_threshold)]

    started = time.time()
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

    # Diarization failure (GitHub issue #56): the CLI keeps the plain transcript and
    # exits 3, or exits 0 when a previous run's speaker files are still on disk. Either
    # way this run wrote no fresh sidecar, so nothing speaker-related below is trusted.
    diarization_error: Optional[str] = None
    if diarize:
        fresh = sidecar_path.exists() and sidecar_path.stat().st_mtime >= started - 1.0
        if proc.returncode == 3 or not fresh:
            diarization_error = _diarization_reason(proc)

    if diarization_error:
        stale = [str(p) for p in (speakers_txt, speaker_cards_path, sidecar_path) if p.exists()]
        return {
            "ok": False,
            "partial": True,
            "base": base,
            "outdir": str(out_dir),
            "transcript_txt": str(transcript_txt),
            "diarization_error": diarization_error,
            "stale_speaker_files": stale,
            "error": f"diarization failed; only the plain transcript was written: {diarization_error}",
            "fix": "run whosaid_doctor; if the error names an ONNX/embedding failure, update whosaid "
                   "and retry, or retry with diarize=false for a plain transcript",
            "next_step": ("Speaker labels are missing for this run. Do not relabel"
                          + (" (the speaker files on disk are from an earlier run)" if stale else "")
                          + "; fix the error and transcribe again."),
            "summary": f"Transcribed {base}: plain transcript only; diarization FAILED.",
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
    suggested_max_speakers: Optional[int] = None
    count_before_fold: Optional[int] = None
    count_after_fold: Optional[int] = None
    fold_note: Optional[str] = None
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
            suggested_max_speakers = _suggested_max_speakers(data)
            count_before_fold = data.get("count_before_fold")
            count_after_fold = data.get("count_after_fold")
            fold_note = data.get("fold_note")
            roles = data.get("roles")
            if num_speakers is None:
                skip = set(data.get("uncounted") or [])
                num_speakers = len({s["speaker"] for s in data.get("segments", [])} - skip) or None
        except Exception:  # noqa: BLE001
            names = {}
    if not names:
        clusters = sorted(set(re.findall(r"SPEAKER_\d+", speakers_text or "")))
        names = {c: c for c in clusters}
        if num_speakers is None:
            hidden = set(re.findall(r"SPEAKER_\d+", "\n".join(
                ln for ln in (speakers_text or "").splitlines() if ln.startswith("# Uncounted"))))
            num_speakers = len(set(clusters) - hidden) or None

    # Older sidecars predate the fold metadata. Their rendered count is the
    # only available observation, and so is both the before and after count.
    if count_before_fold is None:
        count_before_fold = num_speakers
    if count_after_fold is None:
        count_after_fold = num_speakers

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
        "suggested_max_speakers": suggested_max_speakers,
        "count_before_fold": count_before_fold,
        "count_after_fold": count_after_fold,
        "fold_note": fold_note,
        "named_speakers": named_speakers,
        "unnamed_clusters": unnamed_clusters,
        "speaker_cards": speaker_cards,
        "speaker_hints_from": speaker_hints_from,
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
    no_save: bool = False,
    force: bool = False,
    note: Optional[str] = None,
    fold_unknown: bool = False,
    blend: bool = False,
    forget: Optional[list[str]] = None,
) -> dict:
    """Name SPEAKER_NN clusters and persist them, by shelling `whosaid relabel`.

    With auto=True the assignments map may be empty: whosaid re-applies registry
    matching + the absorb pass over the cached sidecar (no re-diarization),
    merging phantom cluster splits into their real speaker. auto=True also matches
    the enrollment clips in voices/ and drops auto-assigned names whose registry
    entry and clip are both gone. Set fold_unknown=True only with auto=True to
    repair anonymous phantom clusters in a cached sidecar. `forget` (only with
    auto=True) is a list of names to clear from this meeting's clusters before
    re-naming; the registry is untouched, so a name that is still enrolled and
    still matches can come back.

    `roles` optionally maps Name -> role (conventional: self, boss, peer,
    report, external; free-form allowed) and is passed through as repeatable
    `--role NAME=ROLE` flags; roles are saved to the registry and rendered in
    speaker cards and transcripts.

    With no_save=True the labels are transcript-only: the sidecar and outputs
    are renamed but the registry is untouched (composes with auto=True). note
    is a provenance string stored with each local label. force=True overrides
    the guard that refuses replacing a registry print when the cluster's
    similarity to it is below the match threshold. blend=True averages each
    assigned cluster into the existing print instead of replacing it.
    """
    if not isinstance(assignments, dict) or (not assignments and not auto):
        return {
            "ok": False,
            "error": "assignments must be a non-empty map of SPEAKER_NN -> Name (or pass auto=true)",
            "fix": "pass at least one entry, e.g. {'SPEAKER_00':'Jane'}, or auto=true with {}",
        }
    if fold_unknown and not auto:
        return {
            "ok": False,
            "error": "fold_unknown requires auto=true",
            "fix": "pass auto=true to repair anonymous phantom clusters in a cached sidecar",
        }
    forget_names: list = []
    if forget:
        if not auto:
            return {
                "ok": False,
                "error": "forget requires auto=true",
                "fix": "pass auto=true: forget clears a name, then re-runs the automatic naming",
            }
        if not isinstance(forget, list) or not all(
                isinstance(n, str) and re.fullmatch(r"[A-Za-z0-9_-]+", n) for n in forget):
            return {
                "ok": False,
                "error": "forget must be a list of names matching [A-Za-z0-9_-]+",
                "fix": "pass e.g. forget=['Jane'] with auto=true",
            }
        forget_names = list(forget)
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
    if fold_unknown:
        args.append("--fold-unknown")
    for name in forget_names:
        args += ["--forget", name]
    if match_threshold is not None:
        args += ["--match-threshold", str(match_threshold)]
    for name, role in role_specs:
        args += ["--role", f"{name}={role}"]
    if no_save:
        args.append("--no-save")
    if force:
        args.append("--force")
    if blend:
        args.append("--blend")
    if note:
        args += ["--note", note]
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
    num_speakers: Optional[int] = None
    count_before_fold: Optional[int] = None
    count_after_fold: Optional[int] = None
    fold_note: Optional[str] = None
    count_warning: Optional[str] = None
    count_estimate: Optional[dict] = None
    suggested_max_speakers: Optional[int] = None
    if sidecar and sidecar.exists():
        try:
            d = json.loads(sidecar.read_text())
            data_base = d.get("base", data_base)
            num_speakers = d.get("num_speakers")
            if num_speakers is None:
                skip = set(d.get("uncounted") or [])
                num_speakers = len({s.get("speaker") for s in d.get("segments", [])
                                    if s.get("speaker") and s.get("speaker") not in skip}) or None
            count_before_fold = d.get("count_before_fold", num_speakers)
            count_after_fold = d.get("count_after_fold", num_speakers)
            fold_note = d.get("fold_note")
            count_warning = d.get("count_warning")
            count_estimate = d.get("count_estimate")
            suggested_max_speakers = _suggested_max_speakers(d)
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
        "num_speakers": num_speakers,
        "count_before_fold": count_before_fold,
        "count_after_fold": count_after_fold,
        "fold_note": fold_note,
        "count_warning": count_warning,
        "count_estimate": count_estimate,
        "suggested_max_speakers": suggested_max_speakers,
        "registry_path": str(_speaker_db()),
        "summary": (
            (f"Re-applied registry + absorb naming from the sidecar"
             + (" and checked anonymous clusters for repair" if fold_unknown else "")
             + (f" plus {len(assignments)} explicit assignment(s)" if assignments else "")
             + (f", {len(role_specs)} role(s) applied" if role_specs else "")
             + (f": {fold_note}" if fold_unknown and fold_note else "")
             + "." + (" Transcript-only: registry untouched." if no_save else ""))
            if auto else
            (f"Renamed {len(assignments)} cluster(s)"
             + (f", tagged {len(role_specs)} role(s)" if role_specs else "")
             + (" transcript-only — registry untouched"
                if no_save else
                "; saved to the local speaker registry so they auto-name in future transcripts")
             + ".")
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
_MAX_K = 100
_MAX_WINDOW = 3600  # seconds of context either side; more than that is "read the transcript"
_WORKLIST_TIERS = ("P1", "P2", "P3")
# GitHub issue #30: one real worklist ran ~61k chars and blew the MCP tool-result
# token limit, so omitting limit caps every page at N, including when offset is set,
# reporting the rest via "omitted"/"hint" instead of dumping everything.
_WORKLIST_DEFAULT_LIMIT = 50
_WORKLIST_COMPACT_KEYS = ("id", "source", "text", "status", "kind", "accepted_owner",
                          "evidence_available", "remaining_milestones", "tier", "score", "why")
# GitHub issue #90: a ~300-entry workspace made whosaid_meetings ~82k chars, past the
# same limit, so the list tools page the same way (newest meetings first).
_LIST_DEFAULT_LIMIT = 50
_MEETINGS_COMPACT_KEYS = ("folder", "kind", "created", "minutes")
# Extraction type, obligation kind, and evidence availability are distinct fields.
_ITEMS_COMPACT_KEYS = ("id", "text", "type", "owner", "status", "kind", "accepted_owner",
                       "evidence_available", "occurrences")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _read_only() -> ToolAnnotations:
    """Annotations shared by every workspace tool: read-only, idempotent, local."""
    return ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )


def _ws_error(error: str, hint: str, **fields) -> dict:
    """{"ok": False, "error", "hint"} plus any keyword fields given (GitHub
    issue #113 adds category, elapsed_s, phase); none given = the old shape."""
    out = {"ok": False, "error": error, "hint": hint}
    out.update(fields)
    return out


# _run_ws error categories (GitHub issue #113): missing_script, spawn_failed,
# timeout (with elapsed_s and phase), exit_<code>, bad_json. whosaid_worklist
# adds missing_corpora and no_cache.
_PHASE_RE = re.compile(r"\bphase:\s*(.+?)\s*$")


def _last_phase(stderr) -> Optional[str]:
    """The last 'phase: <name>' line a (possibly killed) CLI wrote to stderr,
    or None. TimeoutExpired carries partial stderr as bytes, str or None."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    phase = None
    for line in (stderr or "").splitlines():
        m = _PHASE_RE.search(line)
        if m:
            phase = m.group(1)
    return phase


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
    or, for exit 2, an argument problem. Every error also carries a "category"
    (missing_script, spawn_failed, timeout, exit_<code>, bad_json); a timeout
    adds "elapsed_s" and "phase", the last 'phase: <name>' stderr line the
    killed CLI wrote (None when it wrote none).
    """
    index_hint = f"run: whosaid index {ws}"
    if not script.is_file():
        return None, _ws_error(
            f"{script.name} is missing from {LIB_DIR}",
            "this whosaid checkout lacks the workspace search stack; reinstall or update it",
            category="missing_script",
        )
    cmd = [sys.executable, str(script), *[str(a) for a in args], "--json"]
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True,
            timeout=WS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        return None, _ws_error(
            f"{script.name} {args[0] if args else ''} timed out after {WS_TIMEOUT:g}s".replace("  ", " "),
            "retry with a narrower query, or check that the local Ollama is reachable "
            "if meaning search is enabled",
            category="timeout",
            elapsed_s=round(time.perf_counter() - started, 2),
            phase=_last_phase(exc.stderr),
        )
    except OSError as exc:
        return None, _ws_error(f"could not start {script.name}: {exc}", "run whosaid_doctor",
                               category="spawn_failed")
    if proc.returncode != 0:
        lines = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
        last = lines[-1] if lines else (proc.stdout or "").strip()[-500:]
        if proc.returncode == 2:
            hint = "check the arguments (usage error)"
        elif "whosaid index" in last or not wsconfig.search_db(ws).is_file():
            hint = index_hint
        else:  # the index exists and the CLI did not ask for a rebuild: an argument problem
            hint = f"the index exists; check the arguments (rebuild if stale with: whosaid index {ws})"
        return None, _ws_error(last or f"{script.name} exited {proc.returncode}", hint,
                               category=f"exit_{proc.returncode}")
    try:
        return json.loads(proc.stdout or "null"), None
    except json.JSONDecodeError as exc:
        return None, _ws_error(f"{script.name} returned unparsable JSON: {exc}", index_hint,
                               category="bad_json")


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
    "`status`, extraction `type`, obligation `kind`, or `accepted_only`. Kinds: "
    "explicit_commitment, request, standing_directive, suggestion, inferred_next_step. "
    "as_of accepts an ISO date/time to reconcile historical evidence. Each item carries "
    "AI-NNN id, text, status, attribution and evidence; whosaid_item has full history. "
    "Sorted by numeric id; filters and kind/status counts apply before paging. An omitted "
    "limit always caps at 50, including later pages; follow next_offset. compact=true "
    "keeps id, text, type, owner, status, kind, accepted_owner, evidence_available and "
    "occurrences. Responses carry index_mtime and snapshot_token; restart from offset 0 "
    "if snapshot_token changes between pages (index, corpora, tracking or config changed). "
    "Built by `whosaid index`; "
    "read-only. Keywords: action items, todos, commitments, who owes what, open items, "
    "follow-ups."
)

_DESC_WS_ITEM = (
    "One action item or commitment by id (AI-NNN or CM-NNN) with its tracking history, "
    "evidence and milestones. as_of accepts an ISO date/time. Action items also include the "
    "timestamped turns where it was discussed, commitments and status changes, related PRs. "
    "Use whosaid_context on those timestamps for the verbatim discussion. Read-only."
)

_DESC_WS_PERSON = (
    "Per-person commitments view (GitHub issue #13): what `name` owns, what they asked "
    "others for, deadlines they gave, and the meetings they were in. Omit `name` to list "
    "every known person with counts. Names are speaker labels exactly as they appear in the "
    "transcripts (see whosaid_speakers). With name, as_of accepts an ISO date/time for "
    "historical reconciliation. Read-only. Keywords: my action items, what does X "
    "owe, commitments by person, who promised what."
)

_DESC_WS_MEETINGS = (
    "List the meetings in the workspace as the graph knows them: folder, date, duration, "
    "attendees (speaker labels), action-item count, and kind (source origin: \"teams-chat\" "
    "for a Microsoft Teams chat folder, \"outlook-mail\" for an Outlook mail thread, "
    "\"audio\" for a recorded meeting; pass kind to filter). "
    "The folder names are what whosaid_search "
    "hits carry, what whosaid_context takes, and what the "
    "whosaid://workspace/meeting/{folder}/... resources use. Newest first. Filter with kind, "
    "since/until (YYYY-MM-DD, inclusive, on the meeting date) and query (case-insensitive "
    "substring of folder or source name), then page with offset (skip N) and limit (top N); "
    "compact=true keeps only folder, kind, created and minutes. An omitted limit always "
    "caps the page at 50 plus total/omitted and a paging hint. Read-only."
)

_DESC_WS_PRS = (
    "List pull-request mentions found in the transcripts: PR numbers or links, who mentioned "
    "them, in which meeting and when. Read-only. Keywords: PR, pull request, code review, "
    "merged, deployed. Page with offset/limit; an omitted limit caps every page at 50 plus "
    "total/omitted and a paging hint."
)

_DESC_WS_SPEAKERS = (
    "List every speaker label in the search index with turn counts and the meetings they "
    "appear in. Use the exact labels as the `speaker` filter of whosaid_search or the `name` "
    "of whosaid_person. Busiest first; page with offset/limit; an omitted limit always caps "
    "the page at 50 plus total/omitted and a paging hint. Read-only."
)

_DESC_WS_WORKLIST = (
    "Ranked personal worklist from commitment CM-NNN and action-item AI-NNN corpora, "
    "including completed items and proposed requests. owner is a speaker label or 'me' "
    "(self role, else whosaid.toml owner). Distinguish obligations with kind "
    "(explicit_commitment, request, standing_directive, suggestion, inferred_next_step) "
    "and accepted_only. Filter by status and tier (P1/P2/P3) before paging; kind/status "
    "counts describe that filtered set. as_of accepts an ISO date/time. Offset skips rows; "
    "omitted limit always caps at 50. compact=true keeps id, source, text, status, kind, "
    "accepted_owner, evidence_available, remaining_milestones, tier, score and why. "
    "Deterministic, no LLM. Ranking is a heuristic; leadership assertions are available "
    "through whosaid_priorities. Errors have categories: missing_corpora, timeout "
    "(elapsed_s and phase), exit_<code>, bad_json, no_cache, incompatible_cache. "
    "use_cache=true reads _WORKLIST-<Owner>.json with generated_at, source=cache, stale "
    "and snapshot_token; freshness includes corpus, tracking and config changes. Historical "
    "as_of queries require live reconciliation and reject cached results. Timeouts may "
    "attach a compatible current cache. Read-only. Keywords: my worklist, what did I "
    "promise, my commitments, completed work, ranked priorities."
)
_DESC_WS_PRIORITIES = (
    "Read priority assertions and their changes from the local tracking ledger. Filter "
    "by a configured group such as leadership and/or ISO date/time as_of. Returns group "
    "members, current assertions, history, evidence and conflicts. Explicit priorities "
    "remain separate from inferred ranks and differing speakers' assertions; no official "
    "consensus is invented. Read-only; no priority is inferred from role or meeting count."
)
_DESC_WS_TRACKING_SUGGESTIONS = (
    "Find possible completion evidence for an owner's open commitments in the local "
    "transcript index. owner defaults to me; limit defaults to 50 and is capped at 1000. "
    "Uses later owner statements with matching subjects and completed-action wording; "
    "skips quoted or uncertain attribution and repeated indexed messages. Returns evidence "
    "candidates marked review_required, counts and limitations. Suggestions are not verified "
    "completion and never update status or the tracking ledger. Read-only; no LLM or external calls."
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
def _item_num(item_id) -> int:
    """Numeric suffix of an AI-NNN id for sorting; ids without digits sort last."""
    m = re.search(r"(\d+)\s*$", str(item_id or ""))
    return int(m.group(1)) if m else sys.maxsize


def _tracking_args(kind: Optional[str] = None, as_of: Optional[str] = None) -> Optional[dict]:
    """Reject unsupported filters before a subprocess or ledger read."""
    import obligations
    import tracking
    if kind is not None and kind not in obligations.KINDS:
        return _ws_error(f"unknown obligation kind: {kind}",
                         "use one of: " + ", ".join(obligations.KINDS), category="invalid_arguments")
    if as_of is not None:
        try:
            tracking._instant(as_of, end_of_day=True)
        except (ValueError, TypeError) as exc:
            return _ws_error(str(exc), "pass as_of as an ISO date or timestamp",
                             category="invalid_arguments")
    return None


def _item_counts(items: list[dict]) -> dict:
    """Counts cover the filtered set, before pagination."""
    from collections import Counter
    return {
        "kind_counts": dict(Counter(i.get("kind") or "unknown" for i in items)),
        "status_counts": dict(Counter(i.get("status") or "unknown" for i in items)),
        "accepted_count": sum(bool(i.get("accepted_owner")) for i in items),
        "evidence_available_count": sum(bool(i.get("evidence_available")) for i in items),
    }


def _snapshot_token(ws: Path) -> str:
    """Identify the index and all inputs to the read-only tracking overlays."""
    import workspace as wsmod
    try:
        inputs = wsmod.corpora_digests(ws)
        db = wsconfig.search_db(ws)
        for path in (db, Path(str(db) + "-wal")):
            if path.is_file():
                stat = path.stat()
                inputs[path.name] = f"{stat.st_size}:{stat.st_mtime_ns}"
        return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    except OSError:
        # A transient file replacement must not turn a successful query into a crash.
        # Never null: a fresh token on every failed read makes pagers restart, so the
        # input change behind the failure is never mistaken for a stable snapshot.
        return "unreadable:" + secrets.token_hex(8)


@mcp.tool(name="whosaid_items", description=_DESC_WS_ITEMS, annotations=_read_only())
def whosaid_items(
    owner: Optional[str] = None,
    requester: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 - the MCP-facing name; mirrors `graph.py items --type`
    workspace: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    compact: bool = False,
    kind: Optional[str] = None,
    accepted_only: bool = False,
    as_of: Optional[str] = None,
) -> dict:
    """Filter through graph.py, count matches, sort numeric IDs and page them.

    Omitted limits cap every page at 50. snapshot_token covers the index and
    corpus/tracking/config inputs; consumers restart paging if that token changes.
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    limit, offset, err = _page_args(limit, offset)
    if err:
        return err
    err = _tracking_args(kind, as_of)
    if err:
        return err
    args = ["items", ws]
    for flag, value in (("--owner", owner), ("--requester", requester), ("--status", status),
                        ("--type", type), ("--kind", kind), ("--as-of", as_of)):
        if value is not None and str(value).strip():
            args += [flag, str(value).strip()]
    if accepted_only:
        args += ["--accepted-only"]
    payload, err = _run_ws(GRAPH_PY, args, ws)
    if err:
        return err
    items = [i for i in _as_list(payload, "items") if isinstance(i, dict)]
    # graph.py orders by id as text (AI-1000 before AI-999); re-sort numerically, id as tie-breaker
    items.sort(key=lambda i: (_item_num(i.get("id")), str(i.get("id") or "")))
    try:
        mtime = datetime.fromtimestamp(wsconfig.search_db(ws).stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        mtime = None
    out = _paged({"ok": True, "workspace": str(ws), "index_mtime": mtime,
                  "snapshot_token": _snapshot_token(ws), "as_of": as_of,
                  **_item_counts(items)}, "items", items, limit, offset)
    if compact:
        out["items"] = [{k: i.get(k) for k in _ITEMS_COMPACT_KEYS} for i in out["items"]]
        out["compact"] = True
    return out


# ---------------------------------------------------------------------------
# 10) whosaid_item
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_item", description=_DESC_WS_ITEM, annotations=_read_only())
def whosaid_item(
    id: str,  # noqa: A002 - the MCP-facing name; the AI-NNN id from whosaid_items
    workspace: Optional[str] = None,
    as_of: Optional[str] = None,
) -> dict:
    """Read AI detail from graph.py or CM detail through the shared corpus projection."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    item_id = (id or "").strip()
    if not re.fullmatch(r"(?:AI|CM)-\d{3,}", item_id):
        return _ws_error(f"invalid item id '{id}'", "pass AI-NNN or CM-NNN from items or worklist",
                         category="invalid_arguments")
    err = _tracking_args(as_of=as_of)
    if err:
        return err
    if item_id.startswith("CM-"):
        import commitment_views
        try:
            row = commitment_views.corpus_rows(ws).get(item_id)
            rows = commitment_views.enrich_rows(ws, [row], as_of=as_of) if row else []
        except (OSError, ValueError, TypeError) as exc:
            return _ws_error(str(exc), "check the commitment corpus and tracking ledger",
                             category="tracking_error")
        if not rows:
            return _ws_error(f"item {item_id} not found for the selected date",
                             "pass an item id returned by whosaid_worklist for that date",
                             category="not_found")
        payload = rows[0]
    else:
        args = ["item", ws, item_id] + (["--as-of", as_of] if as_of else [])
        payload, err = _run_ws(GRAPH_PY, args, ws)
        if err:
            return err
    return {"ok": True, "workspace": str(ws), "id": item_id, "item": payload, "as_of": as_of}


# ---------------------------------------------------------------------------
# 11) whosaid_person
# ---------------------------------------------------------------------------
@mcp.tool(name="whosaid_person", description=_DESC_WS_PERSON, annotations=_read_only())
def whosaid_person(
    name: Optional[str] = None,
    workspace: Optional[str] = None,
    as_of: Optional[str] = None,
) -> dict:
    """Shell `graph.py person <ws> [Name] --json`.

    With `name`: {"ok", "name", "person": {...}} (that person's commitments view).
    Without: {"ok", "people": [...], "count"} (everyone the graph knows).
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    who = (name or "").strip()
    err = _tracking_args(as_of=as_of)
    if err:
        return err
    if as_of and not who:
        return _ws_error("historical person view requires a name", "pass name=<speaker label> with as_of",
                         category="invalid_arguments")
    args = ["person", ws] + ([who] if who else [])
    if as_of:
        args += ["--as-of", as_of]
    payload, err = _run_ws(GRAPH_PY, args, ws)
    if err:
        return err
    if who:
        return {"ok": True, "workspace": str(ws), "name": who, "person": payload, "as_of": as_of}
    people = _as_list(payload, "people")
    return {"ok": True, "workspace": str(ws), "people": people, "count": len(people)}


# ---------------------------------------------------------------------------
# 12) whosaid_meetings, 13) whosaid_prs, 14) whosaid_speakers
# ---------------------------------------------------------------------------
def _page_args(limit, offset) -> tuple[Optional[int], int, Optional[dict]]:
    """Validate limit/offset the way whosaid_worklist does; returns (limit, offset, error)."""
    try:
        limit = None if limit is None else int(limit)
    except (TypeError, ValueError):
        return None, 0, _ws_error(f"invalid limit '{limit}'", "pass a whole number >= 1")
    if limit is not None and limit < 1:
        return None, 0, _ws_error(f"limit must be >= 1, got {limit}", "pass limit=N to cap the page size")
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        return None, 0, _ws_error(f"invalid offset '{offset}'", "pass a whole number >= 0")
    if offset < 0:
        return None, 0, _ws_error(f"offset must be >= 0, got {offset}", "offset is a skip count; 0 is the first page")
    return limit, offset, None


def _paged(out: dict, key: str, rows: list, limit: Optional[int], offset: int) -> dict:
    """Slice rows into out[key] with count/total, self-capping at _LIST_DEFAULT_LIMIT when
    limit was omitted, and add omitted/hint when rows were held back."""
    total = len(rows)
    if limit is None:
        limit = _LIST_DEFAULT_LIMIT
    rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    out.update({key: rows, "count": len(rows), "total": total})
    if limit is not None:
        out["limit"] = limit
    if offset:
        out["offset"] = offset
    omitted = total - (offset + len(rows))
    if omitted > 0:
        out["omitted"] = omitted
        out["next_offset"] = offset + len(rows)
        out["hint"] = f"more remain: pass offset={offset + len(rows)} (or limit) to page"
    return out


def _meeting_day(m: dict) -> str:
    """YYYY-MM-DD for a meeting row: its created stamp, else a dated folder name, else ''."""
    for v in (m.get("created"), m.get("folder")):
        day = str(v or "")[:10]
        if _DATE_RE.match(day):
            return day
    return ""


@mcp.tool(name="whosaid_meetings", description=_DESC_WS_MEETINGS, annotations=_read_only())
def whosaid_meetings(
    workspace: Optional[str] = None,
    kind: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    query: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    compact: bool = False,
) -> dict:
    """Shell `graph.py meetings <ws> [--kind K] --json` and return {"ok", "meetings": [...],
    "count", "total"} plus "omitted"/"hint" when rows were held back. Rows are sorted newest
    first (created, else a dated folder name; undated rows last), filtered by since/until
    (inclusive, on that day; undated rows drop out) and query, then offset skips and limit
    caps; an omitted limit caps every page at _LIST_DEFAULT_LIMIT (GitHub issue #90).
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    for name, val in (("since", since), ("until", until)):
        if val is not None and not _DATE_RE.match(str(val).strip()):
            return _ws_error(f"invalid {name} '{val}'", "pass a date as YYYY-MM-DD")
    since = since.strip() if since else None
    until = until.strip() if until else None
    limit, offset, err = _page_args(limit, offset)
    if err:
        return err
    payload, err = _run_ws(GRAPH_PY, ["meetings", ws] + (["--kind", kind] if kind else []), ws)
    if err:
        return err
    meetings = [m for m in _as_list(payload, "meetings") if isinstance(m, dict)]
    meetings.sort(key=lambda m: (_meeting_day(m) != "", str(m.get("created") or _meeting_day(m)),
                                  str(m.get("folder") or "")), reverse=True)
    if since or until:
        meetings = [m for m in meetings if _meeting_day(m)
                    and (not since or _meeting_day(m) >= since) and (not until or _meeting_day(m) <= until)]
    q = (query or "").strip().lower()
    if q:
        meetings = [m for m in meetings
                    if q in str(m.get("folder") or "").lower() or q in str(m.get("source_name") or "").lower()]
    out = _paged({"ok": True, "workspace": str(ws)}, "meetings", meetings, limit, offset)
    if compact:
        out["meetings"] = [{k: m.get(k) for k in _MEETINGS_COMPACT_KEYS} for m in out["meetings"]]
        out["compact"] = True
    return out


@mcp.tool(name="whosaid_prs", description=_DESC_WS_PRS, annotations=_read_only())
def whosaid_prs(workspace: Optional[str] = None, limit: Optional[int] = None, offset: int = 0) -> dict:
    """Shell `graph.py prs <ws> --json` and return {"ok", "prs": [...], "count", "total"},
    paged like whosaid_meetings (GitHub issue #90)."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    limit, offset, err = _page_args(limit, offset)
    if err:
        return err
    payload, err = _run_ws(GRAPH_PY, ["prs", ws], ws)
    if err:
        return err
    return _paged({"ok": True, "workspace": str(ws)}, "prs", _as_list(payload, "prs"), limit, offset)


@mcp.tool(name="whosaid_speakers", description=_DESC_WS_SPEAKERS, annotations=_read_only())
def whosaid_speakers(workspace: Optional[str] = None, limit: Optional[int] = None, offset: int = 0) -> dict:
    """Shell `search.py speakers <ws> --json` and return {"ok", "speakers": [...], "count",
    "total"}, paged like whosaid_meetings (GitHub issue #90)."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    limit, offset, err = _page_args(limit, offset)
    if err:
        return err
    payload, err = _run_ws(SEARCH_PY, ["speakers", ws], ws)
    if err:
        return err
    return _paged({"ok": True, "workspace": str(ws)}, "speakers", _as_list(payload, "speakers"), limit, offset)


# ---------------------------------------------------------------------------
# 15) whosaid_workspace_status
# ---------------------------------------------------------------------------
_DESC_LIVE = (
    "Read the live transcript of the call being recorded right now (`whosaid live`, #131). "
    "Lines look like '[HH:MM:SS] me: ...' (the user's mic) or '[HH:MM:SS] them: ...' (everyone "
    "on the meeting app's side), rough and without names. `active` is true while the call is "
    "still going; `ended` once it is over. Pass `minutes` for only the last N minutes. Use it to "
    "answer questions about the meeting in progress: what was just asked, a catch-up, a reply "
    "draft. Reads local files only; returns ok=false with a hint when `whosaid live` has not "
    "written anything. `recordings` defaults to ~/Recordings."
)


@mcp.tool(name="whosaid_live", description=_DESC_LIVE, annotations=_read_only())
def whosaid_live(minutes: Optional[float] = None, recordings: Optional[str] = None) -> dict:
    """The newest <recordings>/.live/*.jsonl as text, plus whether the call is live."""
    into = Path(recordings).expanduser() if recordings else live_mod.DEFAULT_INTO
    if minutes is not None and minutes <= 0:
        return {"ok": False, "error": "minutes must be > 0", "hint": "omit it for the whole call"}
    return live_mod.latest_session(into, minutes)



_DESC_LIVE_INSIGHTS = (
    "What the call being recorded right now is about: topics (label, share of talk time, top "
    "terms, first/last clock), rolling summaries per few minutes, and next steps tagged agreed or "
    "suggested with who said them ('me' = the user's mic, 'them' = the other side). Built fresh "
    "from the newest live transcript, the same data as the app's Live page. Pair it with "
    "whosaid_live for the verbatim lines and whosaid_live_ask to find where something was said. "
    "Local files only; ok=false with a hint when no live transcript exists."
)


def _clock(started: Optional[float], t: float) -> str:
    return datetime.fromtimestamp(started + t).strftime("%H:%M:%S") if started else ""


@mcp.tool(name="whosaid_live_insights", description=_DESC_LIVE_INSIGHTS, annotations=_read_only())
def whosaid_live_insights(recordings: Optional[str] = None) -> dict:
    """Topics, summaries and next steps for the newest live session (no wordgraph nodes)."""
    into = Path(recordings).expanduser() if recordings else live_mod.DEFAULT_INTO
    head = live_mod.latest_session(into)
    head.pop("transcript", None)
    if not head["ok"]:
        return head
    jl = Path(head["jsonl"])
    meta, entries = live_mod.read_entries(jl)
    res = live_insights.build(meta, entries, session=jl.name[: -len(".jsonl")])
    started = float(meta["started"]) if meta.get("started") else None
    topics = [{"label": t["label"], "rank": t["rank"], "share": t["share"], "terms": t["terms"],
               "first": _clock(started, t["firstT"]), "last": _clock(started, t["lastT"]),
               "new": t["new"]} for t in res["topics"]]
    label = {t["id"]: t["label"] for t in res["topics"]}
    summaries = [{"from": _clock(started, s["from"]), "to": _clock(started, s["to"]),
                  "bullets": s["bullets"], "topics": [label.get(i, i) for i in s["topics"]]}
                 for s in res["summaries"]]
    steps = [{"kind": n["kind"], "who": n["who"], "clock": n["clock"], "text": n["text"],
              "topic": label.get(n["topicId"], n["topicId"])} for n in res["nextSteps"]]
    return dict(head, topics=topics, summaries=summaries, next_steps=steps)


_DESC_LIVE_ASK = (
    "Find where something was said in the call being recorded right now: ranks live transcript "
    "lines against `question` (term overlap, no model call) and returns the best matches with "
    "clock, who ('me'/'them') and text, so you can answer from them and cite the times. Pass "
    "`minutes` to search only the last N minutes. Local files only; ok=false with a hint when no "
    "live transcript exists."
)


@mcp.tool(name="whosaid_live_ask", description=_DESC_LIVE_ASK, annotations=_read_only())
def whosaid_live_ask(question: str, minutes: Optional[float] = None, recordings: Optional[str] = None) -> dict:
    """Lexical matches for `question` in the newest live session; never calls Claude."""
    if not (question or "").strip():
        return {"ok": False, "error": "question is empty", "hint": "pass what to look for"}
    if minutes is not None and minutes <= 0:
        return {"ok": False, "error": "minutes must be > 0", "hint": "omit it for the whole call"}
    into = Path(recordings).expanduser() if recordings else live_mod.DEFAULT_INTO
    res = live_insights.ask(into, question, ws=None, minutes=minutes)
    if not res["ok"]:
        res["hint"] = "start one with `whosaid live` while a call is being recorded"
    res.pop("mode", None)
    for m in res.get("matches", []):
        m.pop("t", None)
    return res

_DESC_SET_STATUS = (
    "Mark one action item or commitment (AI-NNN or CM-NNN) with a status, for example done when "
    "the user says it is finished. Appends ONE curated status event to the workspace's tracking "
    "ledger (_tracking.json, the same ledger `whosaid tracking import` writes); nothing else "
    "changes and no corpus or index is rewritten. status: open, in_progress, delivered, accepted, "
    "blocked, superseded, unknown, or done (= delivered). `note` is required manual evidence: who "
    "marked it and why. Unknown ids are refused. Only call it when the user asked to change the "
    "status; whosaid_tracking_suggestions only proposes. Local files only."
)


@mcp.tool(
    name="whosaid_set_status",
    description=_DESC_SET_STATUS,
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def whosaid_set_status(item_id: str, status: str, note: str, workspace: Optional[str] = None) -> dict:
    """Append one curated status event via tracking.set_status; ok=false with a hint on refusal."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    import tracking
    try:
        event = tracking.set_status(ws, item_id, status, note)
    except (tracking.TrackingError, OSError, ValueError) as exc:
        return _ws_error(str(exc), "check the id with whosaid_item and the status list in this tool's description",
                         category="tracking_error")
    return {"ok": True, "workspace": str(ws), "event": event}


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
_WORKLIST_CORPORA = ("_commitments.json", "_action-items.json")


def _iso_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()


def _worklist_view(ws: Path, who: str, data: dict, tier: Optional[str], limit: Optional[int],
                   offset: int, compact: bool, kind: Optional[str] = None,
                   status: Optional[str] = None, accepted_only: bool = False,
                   as_of: Optional[str] = None) -> dict:
    """One worklist payload filtered and paged the way the tool documents:
    filters first, then offset, then limit (default cap whenever omitted),
    then compact. Shared by the live, cached and use_cache answers."""
    import commitment_views
    items = [i for i in data.get("items", []) if isinstance(i, dict)] if isinstance(data.get("items"), list) else []
    if tier is not None:
        items = [it for it in items if str(it.get("tier", "")).upper() == tier]
    items = commitment_views.filter_rows(items, kind=kind, status=status, accepted_only=accepted_only)
    out = _paged({
        "ok": True,
        "workspace": str(ws),
        "owner": data.get("owner") or who,
        "generated_from": data.get("generated_from") or [],
        "as_of": as_of,
        "snapshot_token": _snapshot_token(ws),
        **_item_counts(items),
    }, "items", items, _WORKLIST_DEFAULT_LIMIT if limit is None else limit, offset)
    for key, value in (("tier", tier), ("kind", kind), ("status", status)):
        if value is not None:
            out[key] = value
    if accepted_only:
        out["accepted_only"] = True
    if compact:
        out["items"] = [{k: it.get(k) for k in _WORKLIST_COMPACT_KEYS} for it in out["items"]]
        out["compact"] = True
    return out


def _cache_compatibility(data: dict, kind: Optional[str], status: Optional[str],
                         accepted_only: bool, as_of: Optional[str]) -> Optional[dict]:
    """Old caches cannot silently answer new acceptance or historical filters."""
    rows = [i for i in data.get("items", []) if isinstance(i, dict)] if isinstance(data.get("items"), list) else []
    historical = as_of or data.get("as_of") or any(i.get("as_of") for i in rows)
    required = ({"kind"} if kind else set()) | ({"status"} if status else set())
    if accepted_only:
        required.add("accepted_owner")
    if historical or any(not required <= row.keys() for row in rows):
        return _ws_error("cached worklist cannot answer these filters",
                         "call with use_cache=false for live evidence reconciliation",
                         category="incompatible_cache")
    return None


def _worklist_cache(ws: Path, who: str) -> tuple:
    """(cache payload with freshness fields, None) or (None, error dict).

    Read-only: finds the _WORKLIST-<Owner>.json the roll-up writes next to
    _WORKLIST-<Owner>.md (GitHub issue #113). stale = the corpus/config/source
    fingerprints differ from the "corpora" digests the cache records. Legacy
    caches without complete source coverage remain readable but stale.
    'me' resolves the way the CLI
    does (workspace.resolve_owner: the self-roled speaker, else [workspace]
    owner), which only reads small per-meeting files, never the corpora."""
    import workspace as wsmod  # lazy: keeps this module's import cheap

    owner = wsmod.resolve_owner(ws, wsconfig.load_config(ws), who)
    if not owner:
        return None, _ws_error(
            "no cached worklist: owner 'me' does not resolve (no 'self' role, no [workspace] owner)",
            "pass owner=<speaker label>", category="no_cache")
    path = ws / wsmod.worklist_cache_filename(owner)
    try:
        data = json.loads(path.read_text())
        cache_mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None, _ws_error(
            f"no cached worklist {path.name} in {ws}",
            f"the roll-up writes it (whosaid roll-up {ws}); until then call without use_cache",
            category="no_cache")
    except (OSError, ValueError) as exc:
        return None, _ws_error(f"cached worklist {path.name} is unreadable: {exc}",
                               "call without use_cache; the next roll-up rewrites it", category="no_cache")
    if not isinstance(data, dict):
        return None, _ws_error(f"cached worklist {path.name} is not a JSON object",
                               "call without use_cache; the next roll-up rewrites it", category="no_cache")
    corpora = [(ws / n).stat().st_mtime for n in wsmod.WORKLIST_CORPORA if (ws / n).is_file()]
    newest = max(corpora) if corpora else 0.0
    recorded = data.get("corpora")
    if isinstance(recorded, dict):
        # The roll-up recorded what it built the cache from: compare content.
        try:
            stale = recorded != wsmod.corpora_digests(ws)
        except OSError:
            stale = True  # unreadable inputs cannot prove freshness
    else:
        stale = True  # mtimes cannot establish the legacy cache's source coverage
    data["owner"] = data.get("owner") or owner
    meta = {
        "cache_file": path.name,
        "generated_at": data.get("generated_at") or _iso_mtime(cache_mtime),
        "cache_mtime": _iso_mtime(cache_mtime),
        "corpora_mtime": _iso_mtime(newest) if corpora else None,
        "stale": stale,
        "snapshot_token": hashlib.sha256(json.dumps(
            {"cache": data, "current": _snapshot_token(ws)}, sort_keys=True).encode()).hexdigest(),
    }
    return (data, meta), None


def _worklist_timeout_hint(phase: Optional[str]) -> str:
    """What a timed-out worklist phase implies (GitHub issue #113)."""
    name = (phase or "").split(" ", 1)[0]
    if name in ("matcher", "prime"):
        return ("timed out preparing source identity reconciliation: inspect the corpus and "
                "message metadata, or use the cached result (use_cache=true). "
                "Live worklists do not call an embedding model")
    if name in ("rank", "match", "score"):
        return ("timed out ranking: the corpora are large for a live query. Use the cached "
                "result (use_cache=true), or pass owner=<speaker label>")
    if name == "load":
        return "timed out reading _commitments.json/_action-items.json: check their size and the disk"
    if name == "write":
        return "timed out writing the result: retry, or use the cached result (use_cache=true)"
    return "timed out before the CLI reported a phase: run whosaid_doctor, or use the cached result (use_cache=true)"


@mcp.tool(name="whosaid_worklist", description=_DESC_WS_WORKLIST, annotations=_read_only())
def whosaid_worklist(
    owner: str = "me",
    tier: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    compact: bool = False,
    use_cache: bool = False,
    workspace: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    accepted_only: bool = False,
    as_of: Optional[str] = None,
) -> dict:
    """Read the shared ranked/reconciled worklist, filter and page with counts.

    Current caches report freshness across corpora, ledger and config. Historical
    queries require live reconciliation; a timeout never substitutes current state.
    Old caches missing requested classification fields are explicitly incompatible.
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    if tier is not None:
        raw_tier = (tier or "").strip()
        tier = raw_tier.upper()
        if tier not in _WORKLIST_TIERS:
            return _ws_error(f"invalid tier '{raw_tier}'", "use one of: P1, P2, P3 (case-insensitive)")
    limit, offset, err = _page_args(limit, offset)
    if err:
        return err
    err = _tracking_args(kind, as_of)
    if err:
        return err
    status = (str(status).strip() or None) if status is not None else None
    who = (owner or "").strip() or "me"
    if not any((ws / n).is_file() for n in _WORKLIST_CORPORA):
        return _ws_error(
            f"no worklist corpora in {ws}: neither _commitments.json nor _action-items.json exists",
            f"run: whosaid roll-up {ws} --action-items (builds the corpora); pass owner="
            "<speaker label> when no 'self' role or [workspace] owner is set",
            category="missing_corpora",
        )
    if use_cache:
        err = _cache_compatibility({}, kind, status, accepted_only, as_of)
        if err:
            return err
        cached, err = _worklist_cache(ws, who)
        if err:
            return err
        data, meta = cached
        err = _cache_compatibility(data, kind, status, accepted_only, as_of)
        if err:
            return err
        out = _worklist_view(ws, who, data, tier, limit, offset, compact, kind, status, accepted_only, as_of)
        out.update(source="cache", **meta)
        return out
    args = ["worklist", ws, "--owner", who]
    for flag, value in (("--kind", kind), ("--status", status), ("--as-of", as_of)):
        if value is not None and str(value).strip():
            args += [flag, str(value).strip()]
    if accepted_only:
        args += ["--accepted-only"]
    payload, err = _run_ws(WORKSPACE_PY, args, ws)
    if err:
        if err.get("category") == "timeout":
            hint = _worklist_timeout_hint(err.get("phase"))
            cached, _ = _worklist_cache(ws, who) if as_of is None else (None, None)
            cache_error = _cache_compatibility(cached[0], kind, status, accepted_only, as_of) if cached else None
            if cache_error:
                cached = None
            if cached:
                data, meta = cached
                view = _worklist_view(ws, who, data, tier, limit, offset, compact, kind, status, accepted_only, as_of)
                view.pop("ok", None)
                view.pop("workspace", None)
                view.update(meta)
                err["cached"] = view
                hint += (f". A cached worklist generated {meta['generated_at']} is attached as "
                         f"'cached' (stale={str(meta['stale']).lower()}); use_cache=true returns it "
                         "without re-running this query")
            elif as_of or cache_error:
                hint = (f"timed out during {err.get('phase') or 'worklist reconciliation'}; "
                        "cached results cannot answer the requested historical or acceptance filters. "
                        "Use live reconciliation for these filters")
                err["cache_error"] = cache_error or _cache_compatibility({}, kind, status, accepted_only, as_of)
            else:
                hint += ". There is no cached worklist (_WORKLIST-<Owner>.json) yet; the next roll-up writes one"
            err["hint"] = hint
        else:
            # The corpora exist, so a rebuild is not the fix; _run_ws's default hint
            # points at the search index, which this view never reads.
            err["hint"] = ("the corpora exist; pass owner=<speaker label> when no 'self' role or "
                           "[workspace] owner is set, else see the error (whosaid_doctor)")
        return err
    data = payload if isinstance(payload, dict) else {}
    out = _worklist_view(ws, who, data, tier, limit, offset, compact, kind, status, accepted_only, as_of)
    out["source"] = "live"
    return out


@mcp.tool(name="whosaid_priorities", description=_DESC_WS_PRIORITIES, annotations=_read_only())
def whosaid_priorities(
    group: Optional[str] = None,
    as_of: Optional[str] = None,
    workspace: Optional[str] = None,
) -> dict:
    """Read recorded priority assertions using the shared ledger projection."""
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    err = _tracking_args(as_of=as_of)
    if err:
        return err
    import tracking
    try:
        view = tracking.priority_view(ws, group=group, as_of=as_of)
    except (OSError, ValueError, TypeError) as exc:
        return _ws_error(str(exc), "check the configured group and local tracking ledger",
                         category="tracking_error")
    return {"ok": True, "workspace": str(ws), **view,
            "count": len(view["items"]), "snapshot_token": _snapshot_token(ws)}


@mcp.tool(name="whosaid_tracking_suggestions", description=_DESC_WS_TRACKING_SUGGESTIONS,
          annotations=_read_only())
def whosaid_tracking_suggestions(
    owner: str = "me",
    limit: int = 50,
    workspace: Optional[str] = None,
) -> dict:
    """Suggest evidence from a bounded read-only index scan, never import it."""
    import sqlite3
    import commitment_views
    import message_identity
    import tracking
    import workspace as wsmod

    ws, err = _resolve_ws(workspace)
    if err:
        return err
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        return _ws_error("limit must be a whole number from 1 to 1000",
                         "omit limit for 50 review candidates", category="invalid_arguments")
    db = wsconfig.search_db(ws)
    if not db.is_file():
        return _ws_error("completion evidence needs the transcript index",
                         f"run: whosaid index {ws}", category="missing_index")
    if not any((ws / n).is_file() for n in _WORKLIST_CORPORA):
        return _ws_error("no commitment or action-item corpus exists",
                         f"run: whosaid roll-up {ws} --action-items", category="missing_corpora")
    started = time.monotonic()
    deadline = started + WS_TIMEOUT
    try:
        cfg = wsconfig.load_config(ws)
        who = wsmod.resolve_owner(ws, cfg, owner)
        if not who:
            return _ws_error("owner 'me' could not be resolved", "pass owner=<speaker label>",
                             category="invalid_arguments")
        raw = commitment_views.corpus_rows(ws)
        aliases = commitment_views.identity_aliases(cfg, raw)
        owner_key = commitment_views._name(who)
        matching = [key for key, values in aliases.items()
                    if owner_key == key or owner_key in {commitment_views._name(v) for v in values}]
        if len(matching) == 1:
            owner_key = matching[0]
        labels = {owner_key, *(commitment_views._name(v) for v in aliases.get(owner_key, []))}
        items = commitment_views.enrich_rows(ws, list(raw.values()))
        items = [item for item in items if commitment_views._name(
            item.get("accepted_owner") or item.get("owner") or item.get("speaker")) in labels
            and item.get("status") not in tracking.TERMINAL
            and item.get("kind") in {"explicit_commitment", "request"}]
        if len(items) > 10000:
            return _ws_error("too many owner items for a bounded evidence scan",
                             "review the owner corpus before requesting suggestions", category="scope_too_large")
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            has_attribution = "seg_message" in tables
            fields = "s.rowid, s.meeting, s.source, s.speaker, s.t_sec, s.line, s.text"
            join = ""
            if has_attribution:
                fields += ", m.message_id, m.attribution"
                join = " LEFT JOIN seg_message m ON m.rowid=s.rowid"
            sql = (f"SELECT {fields} FROM seg s{join} WHERE "
                   "lower(replace(replace(s.speaker, '_', ' '), '-', ' ')) IN ("
                   + ",".join("?" for _ in labels) + ") ORDER BY s.meeting, s.t_sec, s.rowid LIMIT 100001")
            indexed = [dict(row) for row in conn.execute(sql, sorted(labels))]
        finally:
            conn.close()
        if len(indexed) > 100000:
            return _ws_error("owner has more than 100000 indexed turns",
                             "use a smaller workspace for a bounded evidence scan", category="scope_too_large")
        grouped: dict[str, list[dict]] = {}
        seen = set()
        skipped = 0
        missing_attribution = False
        for turn in indexed:
            if time.monotonic() >= deadline:
                return _ws_error("completion evidence scan timed out", "use a smaller workspace",
                                 category="timeout", phase="suggestions", elapsed_s=round(time.monotonic() - started, 3))
            raw_attribution = turn.get("attribution")
            if raw_attribution:
                attribution = json.loads(raw_attribution)
                if not isinstance(attribution, dict):
                    raise ValueError("indexed message attribution must be a JSON object")
            else:
                missing_attribution = True
                attribution = message_identity.assertion_parts(turn["text"])
            if attribution.get("quoted") or attribution.get("attribution_uncertain"):
                skipped += 1
                continue
            key = turn.get("message_id") or (turn["meeting"], turn["source"], turn["line"])
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            turn["text"] = attribution.get("assertion_text") or turn["text"]
            for key in ("line", "t_sec"):
                if turn.get(key) is not None:
                    turn[key] = int(turn[key])
            grouped.setdefault(turn["speaker"], []).append(turn)
        candidates = []
        for speaker, turns in grouped.items():
            # Keep the original indexed speaker on the evidence; aliases only select
            # which owner's items are compared with that speaker's own assertions.
            matched = [{**item, "owner": speaker} for item in items]
            candidates.extend(tracking.suggest_evidence(matched, turns, limit=limit + 1, deadline=deadline))
            if time.monotonic() >= deadline:
                return _ws_error("completion evidence matching timed out", "use a smaller workspace",
                                 category="timeout", phase="suggestions", elapsed_s=round(time.monotonic() - started, 3))
        candidates.sort(key=lambda e: (e["occurred_at"], e["item_id"], e["id"]), reverse=True)
        candidates = list({e["id"]: e for e in candidates}.values())
        limitations = ["Lexical candidates require review; no status or ledger is changed.",
                       "Quoted and uncertain turns are excluded, including messages that also contain a new assertion.",
                       "External artifacts are not checked; absence of a suggestion does not mean unfinished.",
                       "Dates use indexed meeting folders; verify original message dates for imported chat or mail."]
        if missing_attribution:
            limitations.append("Some indexed turns lack message attribution metadata; only text-level quote checks were available.")
        return {"ok": True, "workspace": str(ws), "owner": who, "items": candidates[:limit],
                "count": min(len(candidates), limit), "limit": limit, "has_more": len(candidates) > limit,
                "review_required": True, "items_scanned": len(items), "turns_scanned": len(indexed),
                "turns_excluded": skipped, "limitations": limitations, "snapshot_token": _snapshot_token(ws)}
    except TimeoutError:
        return _ws_error("completion evidence matching timed out", "use a smaller workspace",
                         category="timeout", phase="suggestions", elapsed_s=round(time.monotonic() - started, 3))
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        if time.monotonic() >= deadline:
            return _ws_error("completion evidence scan timed out", "use a smaller workspace",
                             category="timeout", phase="suggestions", elapsed_s=round(time.monotonic() - started, 3))
        return _ws_error(str(exc), "check the local corpora, tracking ledger and transcript index",
                         category="tracking_error")


# ---------------------------------------------------------------------------
# 17) whosaid_reprocess (mutating; dry run by default)
# ---------------------------------------------------------------------------
REPROCESS_TIMEOUT = 3600.0  # seconds: re-diarizing many meetings takes minutes each

_DESC_WS_REPROCESS = (
    "Re-diarize meetings in a workspace whose speaker sidecars predate the v1.10 diarizer fixes "
    "(wraps `whosaid reprocess`). DEFAULT IS A DRY RUN (dry_run=true): it prints the scan report "
    "and the plan and changes nothing; pass dry_run=false to apply. When applied it rewrites each "
    "meeting's speaker-labeled transcript and diarization sidecar from the existing Whisper "
    "<base>.json, so ASR is NOT re-run, carries over transcript-only (no_save) labels, "
    "regenerates per-meeting action items and commitments (skip with no_items=true), then re-runs "
    "the workspace roll-up so corpus item ids and hand edits survive. Every touched meeting is "
    "first backed up to <workspace>/_reprocess-backups/<meeting>/<UTC timestamp>/. With no "
    "`meetings` and all_meetings=false it takes only the meetings the detector flags; "
    "all_meetings=true takes every meeting; `meetings` is a list of folder names. "
    "speakers/min_speakers/max_speakers/engine/hook override the diarization settings. "
    "expected_speakers is a list of known voice names (the roster, sent as one comma list) that "
    "anchors diarization to those enrolled voices. match_threshold (above 0, at most 1) is the cosine a known "
    "voice must reach to claim a cluster. "
    "Not read-only and slow when applied (minutes per meeting); run the dry run first. "
    "Workspace = the `workspace` argument or WHOSAID_WORKSPACE. Keywords: reprocess, "
    "re-diarize, redo speakers, fix speaker labels, old meetings, backfill."
)


@mcp.tool(
    name="whosaid_reprocess",
    description=_DESC_WS_REPROCESS,
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def whosaid_reprocess(
    workspace: Optional[str] = None,
    meetings: Optional[list[str]] = None,
    dry_run: bool = True,
    all_meetings: bool = False,
    no_items: bool = False,
    speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
    expected_speakers: Optional[list[str]] = None,
    match_threshold: Optional[float] = None,
    engine: Optional[str] = None,
    hook: Optional[str] = None,
) -> dict:
    """Shell `whosaid reprocess <ws> [meetings...] [flags]`; dry_run (default) adds --dry-run.

    Returns {"ok", "returncode", "dry_run", "workspace", "stdout", "stderr"} where stdout
    is the CLI's report/summary lines and stderr is its last lines. Validation failures
    come back as the usual {"ok": False, "error", "hint"} before anything is shelled.
    """
    ws, err = _resolve_ws(workspace)
    if err:
        return err
    folders = [str(m).strip() for m in (meetings or [])]
    for m in folders:
        if not _safe_folder(m):
            return _ws_error(
                f"invalid meeting folder '{m}'",
                "pass bare meeting folder names (no slashes, no '..', not hidden)",
            )
    counts = {"speakers": speakers, "min_speakers": min_speakers, "max_speakers": max_speakers}
    for key, val in counts.items():
        if val is None:
            continue
        try:
            n = int(val)
        except (TypeError, ValueError):
            return _ws_error(f"invalid {key} '{val}'", "pass a whole number >= 1")
        if n < 1:
            return _ws_error(f"{key} must be >= 1, got {n}", "pass a whole number >= 1")
        counts[key] = n
    roster: list[str] = []
    for name in (expected_speakers or []):
        nm = str(name).strip()
        if not nm or "," in nm:
            return _ws_error(
                f"invalid expected_speakers entry '{name}'",
                "pass one known voice name per list item (non-empty, no commas)",
            )
        roster.append(nm)
    threshold: Optional[float] = None
    if match_threshold is not None:
        try:
            threshold = float(match_threshold)
        except (TypeError, ValueError):
            return _ws_error(f"invalid match_threshold '{match_threshold}'",
                             "pass a number greater than 0 and at most 1 (default 0.50)")
        if not (0.0 < threshold <= 1.0):
            return _ws_error(f"match_threshold must be in (0, 1], got {threshold}",
                             "pass a number greater than 0 and at most 1 (default 0.50)")
    args: list = ["reprocess", str(ws), *folders]
    if dry_run:
        args.append("--dry-run")
    if all_meetings:
        args.append("--all")
    if no_items:
        args.append("--no-items")
    for key, flag in (("speakers", "--speakers"), ("min_speakers", "--min-speakers"),
                      ("max_speakers", "--max-speakers")):
        if counts[key] is not None:
            args += [flag, str(counts[key])]
    if roster:
        args += ["--expected-speakers", ",".join(roster)]
    if threshold is not None:
        args += ["--match-threshold", str(threshold)]
    if engine and engine.strip():
        args += ["--engine", engine.strip()]
    if hook and hook.strip():
        args += ["--hook", hook.strip()]
    try:
        proc = _run_cli(args, timeout=REPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "dry_run": bool(dry_run),
            "workspace": str(ws),
            "error": f"whosaid reprocess timed out after {REPROCESS_TIMEOUT:.0f}s",
            "hint": "meetings already finished keep their new output and a backup; "
                    "re-run on the remaining meetings (name them in `meetings`)",
        }
    except OSError as exc:
        return _ws_error(f"could not start whosaid: {exc}", "run whosaid_doctor")
    err_lines = (proc.stderr or "").strip().splitlines()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "dry_run": bool(dry_run),
        "workspace": str(ws),
        "stdout": (proc.stdout or "").strip(),
        "stderr": "\n".join(err_lines[-20:]),
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
- `accuracy` → `"default"` (the default) passes nothing, so the CLI default model
  applies: full **large-v3**, or whatever `WHOSAID_MODEL` names. `"fast"` passes `--fast` = **turbo**
  (`mlx-community/whisper-large-v3-turbo`, ~1.5 GB, about half the time, less
  stable on hard audio); `"accurate"` passes `--accurate` = full **large-v3**
  (`mlx-community/whisper-large-v3-mlx`, the whosaid CLI default, best fidelity on
  names/numbers).
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
- `whosaid_relabel(auto=True, fold_unknown=True)` → `whosaid relabel <base> --auto
  --fold-unknown`: repair confident anonymous phantom clusters in a cached sidecar,
  without re-transcribing or assigning a speaker identity. `fold_unknown` requires
  `auto=True`. Results expose `count_before_fold`, `count_after_fold`, and `fold_note`;
  equal counts with a null note mean no fold ran.

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
  negated items never P1. A cue right after a negator ("non-urgent", "not
  blocking") does not count, and a relative deadline ("today", "by friday")
  that has passed since the meeting it was said in shows as
  overdue=YYYY-MM-DD and no longer earns P1. Score, why strings and the cue
  lists come from whosaid.toml [commitments] (boss, deadline_cues,
  blocking_cues, negators, strong_cues, weak_cues, weights,
  embed_threshold). Deterministic, no LLM. The MCP tool filters and pages this
  view (GitHub issue #30): tier (P1/P2/P3, case-insensitive) first, then offset
  skips, then limit caps; compact=true keeps only id, source, text, tier, score
  and why per item. Omitting limit caps every page at 50, even with offset;
  set limit for another size. Replies include total/omitted/next_offset and a paging hint.

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
- `WHOSAID_ABSORB_THRESHOLD` — absorb-pass cosine threshold (default `0.85`), for voices
  not placed in the meeting.
- `WHOSAID_PLACED_ABSORB_THRESHOLD` — lower absorb threshold (default `0.70`) for a voice
  already placed in the meeting; applies only when most of the cluster's turns agree
  (purity gate, transcribe only; `relabel --auto` keeps the `0.85` bar).
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
- whosaid_worklist and whosaid_items filter kind/status/accepted_only before
  paging, with an automatic 50-row limit on every page. Compare snapshot_token
  between pages and restart if the index, corpora, ledger or config changed.
- whosaid_item accepts AI-NNN and CM-NNN; as_of reconciles dated tracking
  evidence for items, named people and worklists. Current caches cannot answer
  historical queries. Completed milestones and remaining work stay separate.
- whosaid_priorities reads configured group assertions and their history;
  whosaid_tracking_suggestions proposes later completion evidence from the local
  index. Suggestions require review and never write statuses or ledger events.
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


# ---------------------------------------------------------------------------
# 18) whosaid_record_browser (start / stop / status; records one browser tab)
# ---------------------------------------------------------------------------
_DESC_RECORD_BROWSER = (
    "Record a browser meeting tab (Teams web, Google Meet, Zoom web) to an .m4a, in the "
    "background, through the Chrome DevTools protocol (wraps `whosaid record --browser`). "
    "Needs a Chromium-family browser that was started with --remote-debugging-port (default "
    "9222; set cdp_port to match). Records only that tab: the page's meeting audio on the right "
    "channel and your mic on the left. No Screen & System Audio Recording permission is needed, "
    "so it works from an agent or a headless session. "
    "action=\"start\" launches the recorder and returns at once with {status:\"recording\", pid, "
    "log}; it refuses while a recording is already running. If more than one tab is open, pass "
    "target (a targetId or a URL substring). The recorder stops by itself when the call ends "
    "(the hang-up control disappears; override with leave_selector) or after max_seconds "
    "(default 10800); leave=true also clicks hang-up at max_seconds; no_mic=true records the "
    "tab only. With into=<dir> the file is saved there and NOT transcribed, so `whosaid watch` "
    "can ingest it; without into it is saved under recordings/ and transcribed. "
    "action=\"stop\" sends Ctrl-C, waits up to 60 seconds for the file to be finalized, and "
    "returns {status, output, log_tail}. action=\"status\" returns {running, pid, started, "
    "log_tail}. Not read-only. Keywords: record meeting, browser, CDP, Teams, Meet, Zoom, "
    "no permissions, auto-stop."
)

RECORD_BROWSER_STOP_WAIT = 60.0  # seconds to wait for the recorder to finalize after SIGINT
_RB_CAPTURED_RE = re.compile(r"captured\s+\S+s\s+->\s+(.+?)\s*$")


def _rb_state_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "whosaid" / "record-browser.json"


def _rb_pid_alive(pid: int) -> bool:
    """True while pid is a running process. Reaps our own finished child and treats a
    zombie as dead (os.kill(pid, 0) alone succeeds on a zombie)."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass  # not our child (server restarted since start): fall through to ps
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return True
    return bool(stat) and not stat.startswith("Z")


def _rb_read_state(state_path: Path) -> Optional[dict]:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None


def _rb_log_tail(log: Optional[str], n: int = 20) -> list:
    if not log:
        return []
    try:
        return Path(log).read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _rb_output_from_log(log: Optional[str]) -> Optional[str]:
    """Output path from the CLI's `captured <dur>s -> PATH` log line (also logged with --into)."""
    found = None
    for line in _rb_log_tail(log, 400):
        m = _RB_CAPTURED_RE.search(line)
        if m:
            found = m.group(1)
    return found


def _rb_start(argv: list, meta: dict, state_path: Path) -> dict:
    """Spawn argv detached (own session, output to a log file) and record its state.
    Refuses while the pid in state_path is still alive. argv is injectable for tests."""
    prior = _rb_read_state(state_path)
    if prior and _rb_pid_alive(prior["pid"]):
        return {"ok": False, "status": "already_recording", "pid": prior["pid"],
                "log": prior.get("log"),
                "hint": "call action=\"stop\" first (or action=\"status\")"}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = state_path.parent / f"record-browser-{stamp}.log"
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen([str(a) for a in argv], cwd=str(REPO_DIR), stdin=subprocess.DEVNULL,
                                stdout=logf, stderr=logf, start_new_session=True)
    state = {"pid": proc.pid, "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "log": str(log_path), "args": meta}
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, state_path)
    return {"ok": True, "status": "recording", "pid": proc.pid, "log": str(log_path)}


def _rb_status(state_path: Path) -> dict:
    st = _rb_read_state(state_path)
    if not st:
        return {"ok": True, "running": False, "pid": None, "started": None, "log_tail": []}
    return {"ok": True, "running": _rb_pid_alive(st["pid"]), "pid": st["pid"],
            "started": st.get("started"), "log": st.get("log"), "log_tail": _rb_log_tail(st.get("log"))}


def _rb_stop(state_path: Path, wait: float = RECORD_BROWSER_STOP_WAIT) -> dict:
    st = _rb_read_state(state_path)
    if not st:
        return {"ok": True, "status": "not_recording", "output": None, "log_tail": []}
    pid, log = st["pid"], st.get("log")
    if _rb_pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGINT)  # start_new_session: the pid leads its own group
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            return {"ok": False, "status": "error", "error": str(exc), "log_tail": _rb_log_tail(log)}
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and _rb_pid_alive(pid):
            time.sleep(0.25)
    if _rb_pid_alive(pid):
        return {"ok": False, "status": "still_running", "pid": pid, "output": None,
                "hint": f"the recorder did not finish within {int(wait)}s; call stop again or check the log",
                "log_tail": _rb_log_tail(log)}
    try:
        state_path.unlink()
    except OSError:
        pass
    output = _rb_output_from_log(log)
    return {"ok": output is not None, "status": "stopped" if output else "stopped_no_output",
            "output": output, "log": log, "log_tail": _rb_log_tail(log)}


@mcp.tool(
    name="whosaid_record_browser",
    description=_DESC_RECORD_BROWSER,
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def whosaid_record_browser(
    action: Literal["start", "stop", "status"],
    cdp_port: Optional[int] = None,
    target: Optional[str] = None,
    leave_selector: Optional[str] = None,
    max_seconds: Optional[int] = None,
    no_mic: bool = False,
    leave: bool = False,
    into: Optional[str] = None,
    label: Optional[str] = None,
) -> dict:
    """start / stop / status for a background `whosaid record --browser` run.

    start returns {"status": "recording", "pid", "log"}; stop returns {"status", "output",
    "log_tail"}; status returns {"running", "pid", "started", "log_tail"}.
    """
    state_path = _rb_state_path()
    if action == "status":
        return _rb_status(state_path)
    if action == "stop":
        return _rb_stop(state_path)
    for key, val in (("cdp_port", cdp_port), ("max_seconds", max_seconds)):
        if val is not None and (isinstance(val, bool) or not isinstance(val, int) or val < 1):
            return _ws_error(f"invalid {key} '{val}'", "pass a whole number >= 1")
    argv = [str(WHOSAID), "record", "--browser"]
    for flag, val in (("--cdp-port", cdp_port), ("--target", target), ("--leave-selector", leave_selector),
                      ("--max-seconds", max_seconds), ("--into", into), ("--label", label)):
        if val is not None and str(val) != "":
            argv += [flag, str(val)]
    if no_mic:
        argv.append("--no-mic")
    if leave:
        argv.append("--leave")
    return _rb_start(argv, {"argv": argv[1:]}, state_path)


def main() -> None:
    """Run the whosaid MCP server over stdio (console-script entry point)."""
    mcp.run()


if __name__ == "__main__":
    main()
