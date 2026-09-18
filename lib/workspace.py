#!/usr/bin/env python3
"""
workspace.py: meeting-workspace layer for whosaid (GitHub issues #2, #3).

Turns loose transcription outputs into a dated, auditable meeting workspace:

  meetings/2026-09-16-0703/meeting.m4a
  meetings/2026-09-16-0703/meeting.txt            plain transcript
  meetings/2026-09-16-0703/meeting.speakers.txt   speaker-labeled transcript
  meetings/2026-09-16-0703/action-items.md        per-meeting action items

Meeting folders are YYYY-MM-DD-HHMM, optionally with a numeric collision
suffix the ingest appends when the plain dated folder already holds a
different recording: 2026-09-16-0703-2, -3, ... Both shapes are first-class
dated folders for every subcommand below.

Subcommands (the `whosaid` bash CLI shells out to this module via
`uv run python lib/workspace.py <subcommand> ...`):

  folder-name <audio> [--tz America/Los_Angeles]
      Dated folder name YYYY-MM-DD-HHMM derived from the recording's own
      container creation_time (ffprobe format_tags=creation_time, UTC
      ISO8601), converted to --tz via stdlib zoneinfo; falls back to the
      file's mtime. Prints just the folder name to stdout.

  hash <audio>
      sha256 of the file, streamed — the stable identity behind idempotent
      ingest and the coverage audit.

  action-items --transcript <path.speakers.txt> [--md-out F] [--json-out F] [--hook CMD]
               [--engine auto|ollama|hook|none] [--ws DIR]
      Per-meeting action items. Two engines (issue #14):
        hook    --hook (or WHOSAID_ACTION_ITEMS_HOOK) runs as a shell command
                with the transcript text on stdin plus WHOSAID_TRANSCRIPT_PATH
                and WHOSAID_SPEAKERS in the environment; its stdout is the
                markdown.
        ollama  the built-in summarizer, lib/action_items.py, run in-process:
                a local Ollama model on 127.0.0.1 drafts sectioned bullets
                with verified quotes, configured by <workspace>/whosaid.toml
                (owner, groups, model; see lib/wsconfig.py).
      --engine defaults to [summarizer] engine in whosaid.toml, itself
      defaulting to auto: a hook if one is set, else ollama if it answers,
      else a skeleton. `none` always writes the skeleton. The markdown goes to
      --md-out (default: action-items.md alongside the transcript) and is
      mirrored to --json-out if given. Exits 0 either way, degrading to the
      skeleton with a WARN when an engine fails, so the offline default stays
      intact. --ws names the workspace whose whosaid.toml applies (default:
      the transcript's parent's parent).

  commitments --transcript <path.speakers.txt> --json-out F [--hook CMD] [--roles JSON]
      Per-meeting dev commitments: first-person commitment cues ("I'll ...",
      "I will ...", "I plan to ...") extracted from the speaker-attributed
      transcript by a stdlib heuristic (no LLM). Roles come from '# Role:
      NAME = ROLE' header lines after the '# Speakers (N):' header (self,
      boss, peer, report, external): with roles present only the "self"
      speaker's cues count — the commitments the dev owes; with no roles
      every speaker's first-person cues count (legacy transcripts).
      Negated cues ("I won't", "I can't") record negative:true. A cue whose
      clause is a question is skipped. When the immediately preceding turn
      is a different speaker asking a question or an imperative ("can you",
      "please", ...), the item records requested_by — priority "high" when
      that speaker's role is boss, else "normal". --hook (or
      WHOSAID_COMMITMENTS_HOOK) mirrors the action-items hook contract and
      adds WHOSAID_ROLES (compact JSON {name: role}); its stdout becomes
      commitments.md, written next to --json-out. Exits 0 either way.

  rollup <workspace-dir> [--action-items] [-o INDEX.md] [--action-items-out F]
         [--commitments-out F] [--similarity-threshold F] [--rebuild]
      The workspace aggregate. Two JSON state files live in the workspace dir:
        _workspace.json    manifest: one entry per dated meeting folder
        _action-items.json living deduplicated action-item corpus
      Rendering: _INDEX.md (one row per meeting — date, duration, transcribed?,
      diarized?, action-items? — plus a nothing-missing audit and a recurring-
      topics section) and, with --action-items, _ACTION-ITEMS.md (corpus
      grouped by owner then status, plus a possible-duplicates review section).
      Corpus ids are AI-001... and NEVER renumber; folding is append-only
      unless --rebuild. Items carry an optional free-form type ("leadership
      ask", "peer ask", ...) rendered in parens after the status. Dedupe is
      difflib similarity on normalized text >= --similarity-threshold (0.5–1.0,
      default 0.82, recorded in _action-items.json as similarity_threshold);
      pairs within 0.10 below the threshold are listed under
      "## Possible duplicates (review)" — informational only, never auto-merged.
      _ACTION-ITEMS.md is living: on every non-rebuild run its hand edits are
      folded back into the corpus before extraction — hand-edited statuses,
      types, and titles win over the hook output, "(merged AI-NNN[, ...])"
      annotations fold the merged-away item's occurrences into the survivor
      (the merged item stays, never renumbered, status "merged", rendered
      collapsed as "[merged → AI-XXX]"), and new occurrences keep appending.
      Only edits visible in the md apply — direct JSON edits also survive.
      Incremental by default: re-running with nothing new writes nothing.
      Folding is section-aware: a bullet that sits under a "## N. Heading"
      in its meeting's action-items.md gives a NEW corpus item (or one that
      has no type yet) that heading as its type, minus the "N. " and any
      trailing parenthetical ("Asks from leadership (Bob)" -> "Asks from
      leadership"), so the built-in summarizer's sections carry through to
      _ACTION-ITEMS.md; hand-edited types still win. Bullets inside a
      <details> block (the summarizer's verbatim evidence) and "- none"
      placeholders are not items and are never folded.

      A parallel dev-commitments corpus rides along: each meeting's
      commitments.json (written by the commitments subcommand) folds into
      _commitments.json (ids CM-001..., stable, never renumbered, same 0.82
      dedupe + 0.10 near-miss band, same hand-edit reconcile from
      _COMMITMENTS.md — grouped by status then speaker, **[boss]** marking
      boss-requested items). _workspace.json points at it via
      "commitments_corpus"; _INDEX.md gains a one-line open/total count
      once any exist. Meeting folders without commitments.json roll up
      exactly as before.

Everything stays LOCAL: stdlib only, no third-party imports, no network
(the ollama engine talks only to Ollama on 127.0.0.1).
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".webm"}
# Meeting folder names: YYYY-MM-DD-HHMM, or that plus a numeric collision
# suffix (-2, -3, ...) appended by the bash ingest side when the plain dated
# folder already holds a different recording.
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}(?:-\d+)?$")
# diarize_sherpa.py renders turns as "[HH:MM:SS] Name: text"; the looser
# "Name (MM:SS): text" spelling is accepted too so hand-made notes still parse.
TURN_BRACKET_RE = re.compile(r"^\[\d{1,3}:\d{2}(?::\d{2})?\]\s+([^\n:]+?):\s")
TURN_PAREN_RE = re.compile(r"^([A-Za-z][\w .'/-]*?)\s*\(\d{1,3}:\d{2}(?::\d{2})?\):\s")
SPEAKERS_HEADER_RE = re.compile(r"^#\s+Speakers?\s*\(\d+\)\s*:\s*(.+)$", re.IGNORECASE)
# '# Role: NAME = ROLE' header lines (anywhere after the '# Speakers (N):'
# line) tag speakers with roles: self, boss, peer, report, external.
ROLE_HEADER_RE = re.compile(r"^#\s+Role:\s*(?P<name>.+?)\s*=\s*(?P<role>\S.*?)\s*$")
# Commitment extraction walks the same "[HH:MM:SS] Name: text" turns; this
# variant also captures the timestamp and the text after the speaker colon.
TURN_TIME_RE = re.compile(r"^\[(?P<time>\d{1,3}:\d{2}(?::\d{2})?)\]\s+(?P<name>[^\n:]+?):\s(?P<text>.*)$")
BULLET_RE = re.compile(
    r"^\s*[-*]\s+(?:[-x]\s+)?(?:\*\*(?P<owner>[^*]+?)\s*:?\*\*\s*:?\s+)?(?P<text>\S.*)$"
)
SIMILARITY_THRESHOLD = 0.82
# Pairs scoring within this band below the threshold are surfaced (never
# merged) under "## Possible duplicates (review)".
NEAR_MISS_BAND = 0.10
STATUSES = ("open", "ongoing", "resolved")
# Commitment corpus statuses (rendered _COMMITMENTS.md groups: open, done).
CM_STATUSES = ("open", "done")
# Rendered _ACTION-ITEMS.md item lines: "- **AI-001** [status] (type) span
# (n×): text", optionally carrying "(merged AI-NNN, ...)" hand-merge notes
# (before the ": " or trailing the text) and the collapsed merged rendering
# "- **AI-005** [merged → AI-002] (n×): text".
MD_ITEM_RE = re.compile(r"^- \*\*(?P<id>AI-\d{3,})\*\* \[(?P<status>[^\]]*)\](?P<rest>.*)$")
MD_MERGE_NOTE_RE = re.compile(r"\((?P<ids>merged AI-\d{3,}(?:, ?AI-\d{3,})*)\)")
MD_MERGED_STATUS_RE = re.compile(r"^merged → (AI-\d{3,})$")
MD_COUNT_RE = re.compile(r"^\d+×$")
MD_PAREN_RE = re.compile(r"\(([^()]*)\)")
# Section headings in a per-meeting action-items.md: "## 3. Alice's own
# commitments" -> "Alice's own commitments"; a trailing "(Bob, Carol)" is dropped.
HEADING_RE = re.compile(r"^\s{0,3}(?P<hashes>#{1,6})\s+(?P<text>.*?)\s*#*\s*$")
HEADING_NUM_RE = re.compile(r"^\d+[.)]\s+")
HEADING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
ENGINES = ("auto", "ollama", "hook", "none")
# Rendered _COMMITMENTS.md item lines mirror the _ACTION-ITEMS.md shape with
# CM- ids; the speaker rides in the paren slot the action-item parser reads
# as the type.
MD_CM_ITEM_RE = re.compile(r"^- \*\*(?P<id>CM-\d{3,})\*\* \[(?P<status>[^\]]*)\](?P<rest>.*)$")
MD_CM_MERGE_NOTE_RE = re.compile(r"\((?P<ids>merged CM-\d{3,}(?:, ?CM-\d{3,})*)\)")
MD_CM_MERGED_STATUS_RE = re.compile(r"^merged → (CM-\d{3,})$")
# Per-meeting commitments.md bullets: "- [ ] (SPEAKER) text  — TIME" with an
# HTML-comment metadata marker on the following line.
CM_BULLET_RE = re.compile(
    r"^\s*-\s+\[(?P<box>[ x])\]\s+\((?P<speaker>[^)]*)\)\s+(?P<text>.+?)\s+—\s+"
    r"(?P<time>\d{1,3}:\d{2}(?::\d{2})?)\s*$"
)
CM_META_RE = re.compile(r"^<!--\s*cm:\s*(?P<meta>\{.*\})\s*-->$")

STOPWORDS = frozenset(
    """
    a about above after again all also am an and any are aren't as at be because
    been before being below between both but by can can't cannot could couldn't
    did didn't do does doesn't doing don't down during each few for from further
    had hadn't has hasn't have haven't having he he'd he'll he's her here here's
    hers herself him himself his how how's i i'd i'll i'm i've if in into is
    isn't it it's its itself just let's me more most mustn't my myself no nor
    not of off on once only or other ought our ours ourselves out over own same
    shan't she she'd she'll she's should shouldn't so some such than that that's
    the their theirs them themselves then there there's these they they'd
    they'll they're they've this those through to too under until up very was
    wasn't we we'd we'll we're we've were weren't what what's when when's where
    where's which while who who's whom why why's with won't would wouldn't you
    you'd you'll you're you've your yours yourself yourselves yeah okay ok um
    like know think really going get got go going one two also us will can
    kind sort little bit right well maybe thing things want need let lets say
    said says see look looks course sure mean means anyway actually basically
    """.split()
)


def log(msg: str) -> None:
    print(f"whosaid: {msg}", file=sys.stderr)


# ---- small shared helpers ------------------------------------------------------

def write_if_changed(path: Path, text: str) -> bool:
    """Write only when content differs, so re-runs never touch mtimes. Returns True if written."""
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_creation_time(raw: str) -> datetime | None:
    """Container creation_time (UTC ISO8601, 'Z' or offset, space or 'T' separator) -> aware UTC datetime."""
    s = raw.strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def probe_creation_time(audio: Path) -> datetime | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return parse_creation_time(out.stdout)


def probe_duration_s(audio: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(audio)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return round(float(out.stdout.strip()), 1) if out.returncode == 0 else None
    except ValueError:
        return None


def hms(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    t = int(round(seconds))
    return f"{t // 3600}:{(t % 3600) // 60:02d}:{t % 60:02d}"


# ---- transcript parsing --------------------------------------------------------

def parse_speakers(transcript_text: str) -> list[str]:
    """Distinct speaker names, in first-appearance order, from a speaker-labeled
    transcript ([HH:MM:SS] Name: text turns, 'Name (MM:SS): text' variants, and
    the '# Speakers (N): ...' header the diarizer writes)."""
    seen: dict[str, None] = {}
    for line in transcript_text.splitlines():
        if line.startswith("#"):
            m = SPEAKERS_HEADER_RE.match(line)
            if m:
                for name in m.group(1).split(","):
                    name = name.strip()
                    if name:
                        seen.setdefault(name, None)
            continue
        m = TURN_BRACKET_RE.match(line) or TURN_PAREN_RE.match(line)
        if m:
            seen.setdefault(m.group(1).strip(), None)
    return list(seen)


def parse_roles(transcript_text: str) -> dict[str, str]:
    """'{name: role}' from '# Role: NAME = ROLE' header lines (self, boss,
    peer, report, external). Later lines win; the speakers-header shape is
    untouched."""
    roles: dict[str, str] = {}
    for line in transcript_text.splitlines():
        m = ROLE_HEADER_RE.match(line)
        if m:
            roles[m.group("name").strip()] = m.group("role").strip()
    return roles


def parse_bullets(markdown: str) -> list[tuple[int, str, str]]:
    """Action-item bullets -> [(line_no, owner, text)]. '- **Owner:** text' and
    plain '- text' both parse; owner is '' when absent."""
    out = []
    for n, line in enumerate(markdown.splitlines(), start=1):
        m = BULLET_RE.match(line)
        if m and m.group("text").strip():
            out.append((n, (m.group("owner") or "").strip(), m.group("text").strip()))
    return out


def section_name(heading_text: str) -> str:
    """'3. Asks from leadership (Bob, Carol)' -> 'Asks from leadership'."""
    text = HEADING_NUM_RE.sub("", heading_text.strip(), count=1)
    return HEADING_PAREN_RE.sub("", text).strip()


def parse_bullets_with_sections(markdown: str) -> list[tuple[int, str, str, str]]:
    """Like parse_bullets, plus the '##' section each bullet sits under:
    [(line_no, owner, text, section)]. section is the heading text without
    its leading 'N. ' and trailing parenthetical, '' before any heading (an
    h1 resets it). Bullets inside a <details> block and '- none' placeholders
    are skipped: they are evidence and empty-section markers, not items."""
    out = []
    section = ""
    in_details = False
    for n, line in enumerate(markdown.splitlines(), start=1):
        low = line.strip().lower()
        if in_details:
            if "</details>" in low:
                in_details = False
            continue
        if low.startswith("<details"):
            in_details = "</details>" not in low
            continue
        h = HEADING_RE.match(line)
        if h:
            section = "" if len(h.group("hashes")) == 1 else section_name(h.group("text"))
            continue
        m = BULLET_RE.match(line)
        if not m or not m.group("text").strip():
            continue
        text = m.group("text").strip()
        if not m.group("owner") and normalize_text(text) == "none":
            continue
        out.append((n, (m.group("owner") or "").strip(), text, section))
    return out


def normalize_text(text: str) -> str:
    lowered = text.lower()
    stripped = re.sub(r"[^0-9a-z\s]", " ", lowered)
    return " ".join(stripped.split())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def similar(a: str, b: str, threshold: float = SIMILARITY_THRESHOLD) -> bool:
    return similarity(a, b) >= threshold


# ---- folder-name / hash ----------------------------------------------------------

def cmd_folder_name(args: argparse.Namespace) -> int:
    audio = Path(args.audio)
    if not audio.is_file():
        log(f"folder-name: file not found: {audio}")
        return 1
    try:
        tz = ZoneInfo(args.tz)
    except Exception as e:  # noqa: BLE001
        log(f"folder-name: unknown timezone {args.tz!r} ({e})")
        return 1
    dt = probe_creation_time(audio)
    if dt is not None:
        log(f"creation_time={dt.isoformat()} (container tag)")
    else:
        dt = datetime.fromtimestamp(audio.stat().st_mtime, tz=timezone.utc)
        log("creation_time tag missing/empty; falling back to file mtime")
    local = dt.astimezone(tz)
    print(local.strftime("%Y-%m-%d-%H%M"))
    return 0


def cmd_hash(args: argparse.Namespace) -> int:
    audio = Path(args.audio)
    if not audio.is_file():
        log(f"hash: file not found: {audio}")
        return 1
    print(sha256_file(audio))
    return 0


# ---- action-items ----------------------------------------------------------------

def skeleton_markdown(transcript: Path, speakers: list[str]) -> str:
    lines = [
        f"# Action items — {transcript.parent.name}",
        "",
        "_No summarizer hook is configured and the built-in Ollama engine did not run, "
        "so no action items were extracted._",
        "_Re-run with --engine ollama (a local Ollama model on 127.0.0.1, offline) or pass "
        "--hook CMD (or set WHOSAID_ACTION_ITEMS_HOOK) to generate them._",
        "",
    ]
    if speakers:
        lines.append(f"Speakers in this meeting: {', '.join(speakers)}")
        lines.append("")
    return "\n".join(lines)


def run_hook(hook: str, transcript: Path, text: str, speakers: list[str]) -> str:
    """The --hook engine: shell command, transcript on stdin, markdown on stdout.
    Returns '' (after a WARN) when the hook fails or prints nothing."""
    env = dict(os.environ)
    env["WHOSAID_TRANSCRIPT_PATH"] = str(transcript.resolve())
    env["WHOSAID_SPEAKERS"] = ",".join(speakers)
    try:
        proc = subprocess.run(
            hook, shell=True, input=text, env=env,
            capture_output=True, text=True,
        )
    except OSError as e:  # noqa: BLE001
        log(f"WARN action-items hook failed to start ({e}); writing skeleton")
        return ""
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout
    tail = (proc.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
    log(f"WARN action-items hook exited {proc.returncode}: {tail[0]}; writing skeleton")
    return ""


def run_ollama_engine(text: str, meeting: str, cfg: dict) -> tuple[str, dict]:
    """The built-in engine, in-process. Returns ('', {}) after a WARN on any failure."""
    try:
        import action_items  # lazy: only the ollama engine needs it
        return action_items.draft(text, meeting, cfg)
    except Exception as e:  # noqa: BLE001
        log(f"WARN action-items engine ollama failed ({type(e).__name__}: {e}); writing skeleton")
        return "", {}


def cmd_action_items(args: argparse.Namespace) -> int:
    transcript = Path(args.transcript)
    if not transcript.is_file():
        log(f"action-items: transcript not found: {transcript}")
        return 1
    text = transcript.read_text()
    speakers = parse_speakers(text)

    import wsconfig  # sibling module in lib/; lazy so importers of this file need nothing new
    ws = Path(args.ws).expanduser() if getattr(args, "ws", None) else transcript.resolve().parent.parent
    cfg = wsconfig.load_config(ws)
    engine = getattr(args, "engine", None) or str(cfg["summarizer"].get("engine") or "auto")
    if engine not in ENGINES:
        log(f"action-items: unknown engine {engine!r} (expected one of {', '.join(ENGINES)})")
        return 1
    hook = args.hook or os.environ.get("WHOSAID_ACTION_ITEMS_HOOK", "")
    if engine == "auto":
        if hook:
            engine = "hook"
        elif wsconfig.ollama_up(wsconfig.ollama_url(cfg)):
            engine = "ollama"
        else:
            engine = "none"
            log(f"engine auto: no hook set and Ollama at {wsconfig.ollama_url(cfg)} is not "
                "answering; writing skeleton")

    source = "skeleton"
    markdown = ""
    stats: dict = {}
    if engine == "hook":
        if not hook:
            log("WARN --engine hook but no hook is set (--hook CMD or WHOSAID_ACTION_ITEMS_HOOK); "
                "writing skeleton")
        else:
            markdown = run_hook(hook, transcript, text, speakers)
            if markdown:
                source = "hook"
    elif engine == "ollama":
        markdown, stats = run_ollama_engine(text, transcript.resolve().parent.name, cfg)
        if markdown:
            source = f"ollama:{stats.get('model', '')}"
    if not markdown:
        markdown = skeleton_markdown(transcript, speakers)

    md_out = Path(args.md_out) if args.md_out else transcript.parent / "action-items.md"
    wrote = write_if_changed(md_out, markdown if markdown.endswith("\n") else markdown + "\n")
    log(f"action items ({source}) -> {md_out}" + (" (unchanged)" if not wrote else ""))

    if args.json_out:
        payload = {
            "transcript": str(transcript),
            "md_out": str(md_out),
            "source": source.split(":", 1)[0],
            "engine": source,
            "speakers": speakers,
            "items": [{"line": n, "owner": o, "text": t, "section": s}
                      for n, o, t, s in parse_bullets_with_sections(markdown)],
        }
        if stats:
            payload["stats"] = {k: v for k, v in stats.items() if k not in ("speakers", "meeting")}
        write_if_changed(Path(args.json_out), json.dumps(payload, indent=2) + "\n")
    return 0


# ---- commitments -------------------------------------------------------------------

# First-person commitment cues, longest-first so specific forms win over
# their prefixes. Matched only at clause starts: the start of the turn, or
# right after a comma/semicolon/colon, an em dash, or a coordinating
# conjunction.
COMMITMENT_NEGATIVE_CUES = frozenset(("i won't", "i can't", "i cant"))
COMMITMENT_CUE_RE = re.compile(
    r"(?:^|[,;:]\s*|\s—\s|\b(?:and|but|or|so|then)\s+)"
    r"(?P<cue>i won't|i can't|i cant|i'm going to|i am going to|i plan to|"
    r"i'll follow up|i'll pick up|i'll take|i'll send|i'll get|i'll own|i'll|"
    r"i will|i shall|i can|i could|i promise|count on me|leave it with me|"
    r"let me|i owe)(?![a-z])",
    re.IGNORECASE,
)
# A clause runs from its cue to the next boundary: a comma/semicolon/colon,
# a sentence terminator, or a coordinating conjunction.
CLAUSE_END_RE = re.compile(r"\s+(?:and|but|or|so|then)\s+|[,;:.!?](?:\s|$)")
# Imperative cues in the immediately preceding turn that make the following
# commitment an answer to a request.
REQUEST_CUES = ("can you", "could you", "please", "need you to", "have you", "will you")


def iter_turns(transcript_text: str) -> list[tuple[str, str, str]]:
    """[(time, speaker, text)] for each [HH:MM:SS] Name: text turn.
    Continuation lines (up to the blank line between turns) join the turn's
    text; '#' header lines are skipped."""
    turns: list[tuple[str, str, str]] = []
    cur: list[str] | None = None

    def flush() -> None:
        if cur is not None:
            turns.append((cur[0], cur[1], " ".join(cur[2:])))

    for line in transcript_text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = TURN_TIME_RE.match(line)
        if m:
            flush()
            cur = [m.group("time"), m.group("name").strip(), m.group("text")]
            continue
        if not line.strip():
            flush()
            cur = None
        elif cur is not None:
            cur.append(line.strip())
    flush()
    return turns


def extract_commitments(transcript_text: str, roles: dict | None = None) -> list[dict]:
    """First-person commitments from speaker-attributed turns. With truthy
    roles only the 'self'-role speaker's cues count; without them any
    speaker's do (legacy transcripts). Cues inside question clauses are
    skipped; when the immediately preceding turn is a different speaker
    asking a question or an imperative, the item records requested_by —
    priority 'high' when that speaker's role is boss, else 'normal'."""
    gate = bool(roles)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    prev: tuple[str, str, str] | None = None
    for time, speaker, text in iter_turns(transcript_text):
        role = roles.get(speaker) if gate else None
        if gate and (role or "").strip().lower() != "self":
            prev = (time, speaker, text)
            continue
        for m in COMMITMENT_CUE_RE.finditer(text):
            cue = m.group("cue").lower()
            end_m = CLAUSE_END_RE.search(text, m.start("cue"))
            clause = text[m.start("cue"):end_m.start() if end_m else len(text)].strip()
            if not clause or (end_m and end_m.group(0)[0] == "?"):
                continue  # empty clause, or a question — not a commitment
            key = (speaker, normalize_text(clause))
            if key in seen:
                continue
            seen.add(key)
            requested_by = requested_by_role = ""
            priority = "normal"
            if prev is not None and prev[1] != speaker:
                ptext = prev[2].lower()
                if prev[2].rstrip().endswith("?") or any(c in ptext for c in REQUEST_CUES):
                    requested_by = prev[1]
                    requested_by_role = (roles.get(prev[1]) or "") if gate else ""
                    if requested_by_role.strip().lower() == "boss":
                        priority = "high"
            item = {
                "speaker": speaker,
                "speaker_role": role,
                "text": clause,
                "time": time,
                "cue": cue,
                "negative": cue in COMMITMENT_NEGATIVE_CUES,
                "priority": priority,
            }
            if requested_by:
                item["requested_by"] = requested_by
                item["requested_by_role"] = requested_by_role
            out.append(item)
        prev = (time, speaker, text)
    return out


def commitments_markdown(folder: str, items: list[dict]) -> str:
    lines = [f"# Commitments — {folder}", "",
             "_dev-commitments: first-person commitments extracted from the "
             "speaker-labeled transcript (stdlib heuristic, no LLM)._", ""]
    if not items:
        lines += ["_No commitments detected._", ""]
        return "\n".join(lines)
    for it in items:
        text = it["text"] + (" (boss-requested)" if it.get("priority") == "high" else "")
        lines.append(f"- [ ] ({it['speaker']}) {text}  — {it['time']}")
        meta = {k: it[k] for k in ("speaker", "speaker_role", "text", "time", "cue",
                                   "negative", "priority", "requested_by",
                                   "requested_by_role") if k in it}
        lines.append("<!-- cm: " + json.dumps(meta) + " -->")
    lines.append("")
    return "\n".join(lines)


def parse_commitment_bullets(markdown: str) -> list[dict]:
    """Rendered commitments.md bullets -> item dicts. '- [ ] (SPEAKER) text
    — TIME' lines parse; a following '<!-- cm: {...} -->' comment restores
    the full metadata when present."""
    out: list[dict] = []
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        m = CM_BULLET_RE.match(line)
        if not m:
            continue
        item = {"speaker": m.group("speaker").strip(), "speaker_role": None,
                "text": m.group("text").strip(), "time": m.group("time"),
                "cue": "", "negative": False, "priority": "normal"}
        if item["text"].endswith(" (boss-requested)"):
            item["text"] = item["text"][: -len(" (boss-requested)")].rstrip()
            item["priority"] = "high"
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        cm = CM_META_RE.match(nxt)
        if cm:
            try:
                meta = json.loads(cm.group("meta"))
            except ValueError:
                meta = None
            if isinstance(meta, dict):
                item.update(meta)
        out.append(item)
    return out


def cmd_commitments(args: argparse.Namespace) -> int:
    transcript = Path(args.transcript)
    if not transcript.is_file():
        log(f"commitments: transcript not found: {transcript}")
        return 1
    text = transcript.read_text()
    speakers = parse_speakers(text)

    roles = None
    if args.roles:
        try:
            raw = Path(args.roles[1:]).read_text() if args.roles.startswith("@") else args.roles
            roles = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            log(f"WARN commitments: --roles unreadable ({e}); using '# Role:' headers")
            roles = None
    if roles is None:
        roles = parse_roles(text)

    hook = args.hook or os.environ.get("WHOSAID_COMMITMENTS_HOOK", "")
    source = "heuristic"
    markdown = ""
    items: list[dict] = []
    if hook:
        env = dict(os.environ)
        env["WHOSAID_TRANSCRIPT_PATH"] = str(transcript.resolve())
        env["WHOSAID_SPEAKERS"] = ",".join(speakers)
        env["WHOSAID_ROLES"] = json.dumps(roles, separators=(",", ":"))
        try:
            proc = subprocess.run(
                hook, shell=True, input=text, env=env,
                capture_output=True, text=True,
            )
        except OSError as e:  # noqa: BLE001
            log(f"WARN commitments hook failed to start ({e}); using heuristic")
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            source = "hook"
            markdown = proc.stdout
        elif proc is not None:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
            log(f"WARN commitments hook exited {proc.returncode}: {tail[0]}; using heuristic")
    if source == "heuristic":
        items = extract_commitments(text, roles)
        markdown = commitments_markdown(transcript.parent.name, items)
    else:
        items = parse_commitment_bullets(markdown)

    md_out = Path(args.json_out).parent / "commitments.md"
    wrote = write_if_changed(md_out, markdown if markdown.endswith("\n") else markdown + "\n")
    log(f"commitments ({source}) -> {md_out}" + (" (unchanged)" if not wrote else ""))

    payload = {
        "transcript": str(transcript),
        "md_out": str(md_out),
        "source": source,
        "speakers": speakers,
        "roles": roles,
        "items": items,
    }
    write_if_changed(Path(args.json_out), json.dumps(payload, indent=2) + "\n")
    return 0


# ---- rollup: manifest -------------------------------------------------------------

@dataclass
class Meeting:
    folder: str
    source_name: str | None = None
    source_sha256: str | None = None
    created: str | None = None
    duration_s: float | None = None
    has_txt: bool = False
    has_json: bool = False
    has_speakers: bool = False
    has_action_items: bool = False
    has_commitments: bool = False
    ingested_at: str = ""


def find_source_audio(folder: Path) -> Path | None:
    candidates = sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS),
        key=lambda p: (-p.stat().st_size, p.name),
    )
    return candidates[0] if candidates else None


def scan_meeting(folder: Path, previous: Meeting | None) -> Meeting:
    prev = previous or Meeting(folder=folder.name)
    m = Meeting(folder=folder.name, ingested_at=prev.ingested_at)
    audio = find_source_audio(folder)
    if audio:
        m.source_name = audio.name
        m.source_sha256 = sha256_file(audio)
        dur = probe_duration_s(audio)
        if dur is not None:
            m.duration_s = dur
    created = probe_creation_time(audio) if audio else None
    if created is None:
        m.created = folder.name
    else:
        m.created = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not m.ingested_at:
        m.ingested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = [p.name for p in folder.iterdir() if p.is_file()]
    m.has_txt = any(
        n.endswith(".txt") and not n.endswith((".speakers.txt", ".speaker-cards.txt"))
        for n in files
    )
    m.has_json = any(n.endswith(".json") for n in files)
    m.has_speakers = any(n.endswith(".speakers.txt") for n in files)
    m.has_action_items = any(n == "action-items.md" for n in files)
    m.has_commitments = any(n == "commitments.json" for n in files)
    return m


def load_manifest(ws: Path) -> dict:
    try:
        data = json.loads((ws / "_workspace.json").read_text())
        if isinstance(data, dict) and isinstance(data.get("meetings"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"WARN _workspace.json unreadable ({e}); starting a fresh manifest")
    return {"meetings": []}


def manifest_to_meetings(data: dict) -> dict[str, Meeting]:
    out = {}
    for entry in data.get("meetings", []):
        try:
            m = Meeting(**entry)
        except TypeError:
            continue
        out[m.folder] = m
    return out


# ---- rollup: recurring topics -------------------------------------------------------

def tokenize_line(line: str) -> list[str]:
    line = TURN_BRACKET_RE.sub(" ", line)
    words = re.sub(r"[^0-9A-Za-z\s]", " ", line.lower()).split()
    return [w for w in words if w.isalpha() and len(w) >= 3 and w not in STOPWORDS]


def recurring_topics(speakers_texts: dict[str, str]) -> list[tuple[str, int, int]]:
    """Top unigrams/bigrams appearing in >= 2 meetings: [(term, total, meetings)], best first."""
    counts: dict[str, int] = {}
    docs: dict[str, set[str]] = {}
    for meeting, text in speakers_texts.items():
        seen_here: set[str] = set()
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            toks = tokenize_line(line)
            terms = toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]
            for term in terms:
                counts[term] = counts.get(term, 0) + 1
                seen_here.add(term)
        for term in seen_here:
            docs.setdefault(term, set()).add(meeting)
    scored = [(t, c, len(docs[t])) for t, c in counts.items() if len(docs[t]) >= 2]
    scored.sort(key=lambda x: (-x[1], -x[2], x[0]))
    return scored[:16]


def render_index(ws: Path, meetings: list[Meeting], orphans: list[str], stale: list[str],
                 topics: list[tuple[str, int, int]],
                 commitments: tuple[int, int] | None = None) -> str:
    lines = [f"# Meeting workspace index — {ws.resolve()}", "",
             f"{len(meetings)} meeting(s).", "", "## Meetings", "",
             "| Meeting | Created | Duration | Transcribed | Diarized | Action items |",
             "|---|---|---|---|---|---|"]
    for m in meetings:
        lines.append(
            f"| {m.folder} | {m.created or '—'} | {hms(m.duration_s)} | "
            f"{'yes' if m.has_txt else 'NO'} | {'yes' if m.has_speakers else 'NO'} | "
            f"{'yes' if m.has_action_items else '—'} |"
        )
    lines += ["", "## Audit", ""]
    problems = 0
    for m in meetings:
        missing = [name for name, ok in (
            ("transcript .txt", m.has_txt), ("speakers .speakers.txt", m.has_speakers),
            ("action-items.md", m.has_action_items),
        ) if not ok]
        if m.source_sha256:
            lines.append(
                f"- {m.folder}: source={m.source_name} sha256={m.source_sha256[:16]}…"
                + (f"  MISSING: {', '.join(missing)}" if missing else "  ok")
            )
        else:
            lines.append(f"- {m.folder}: NO SOURCE AUDIO in folder" +
                         (f"; also missing {', '.join(missing)}" if missing else ""))
        problems += bool(missing) + (0 if m.source_sha256 else 1)
    for folder in orphans:
        lines.append(f"- ORPHAN {folder}/: present in workspace but not a dated meeting folder "
                     "(YYYY-MM-DD-HHMM[-N]); ignored by the manifest")
        problems += 1
    for folder in stale:
        lines.append(f"- STALE {folder}/: in manifest but folder no longer exists (run with --rebuild to drop)")
        problems += 1
    if problems == 0:
        lines.append("- nothing missing: every dated folder has a manifest entry, source, "
                     "transcript, speaker labels, and action items.")
    if commitments and commitments[1]:
        open_cm, total_cm = commitments
        lines += ["", "## Commitments", "",
                  f"dev-commitments: {open_cm} open of {total_cm} total "
                  "(see _COMMITMENTS.md)."]
    lines += ["", "## Recurring topics", ""]
    if topics:
        lines.append("_Terms appearing in the speaker-labeled transcripts of ≥2 meetings._")
        lines.append("")
        for term, total, nmeet in topics:
            lines.append(f"- **{term}** — {total}× across {nmeet} meetings")
    else:
        lines.append("_No term recurs across ≥2 meetings yet (needs diarized transcripts)._")
    lines.append("")
    return "\n".join(lines)


# ---- rollup: action-item corpus ------------------------------------------------------

@dataclass
class Occurrence:
    meeting: str
    line: int


@dataclass
class ActionItem:
    id: str
    text: str
    owner: str = ""
    status: str = "open"
    type: str = ""
    first_seen: str = ""
    last_seen: str = ""
    merged_into: str = ""
    md_status: str = ""
    md_text: str = ""
    md_type: str = ""
    occurrences: list[Occurrence] = field(default_factory=list)


def load_corpus(ws: Path) -> dict:
    try:
        data = json.loads((ws / "_action-items.json").read_text())
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data.setdefault("next_id", 1)
            data.setdefault("folded_meetings", [])
            for item in data["items"]:
                item["occurrences"] = [Occurrence(**o) for o in item.get("occurrences", [])]
                item.setdefault("status", "open")
                for key in ("type", "merged_into", "md_status", "md_text", "md_type"):
                    item.setdefault(key, "")
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"WARN _action-items.json unreadable ({e}); starting a fresh corpus")
    return {"next_id": 1, "folded_meetings": [], "items": []}


def corpus_to_items(data: dict) -> list[ActionItem]:
    return [ActionItem(**item) for item in data.get("items", [])]


def parse_action_items_md(md_text: str) -> dict[str, dict]:
    """Rendered _ACTION-ITEMS.md item lines -> {id: {status, type, text,
    merged_into, merged_ids}}. Count/span parens are skipped; hand-merge
    notes '(merged AI-NNN, ...)' are honored both before the ': ' and
    trailing the item text."""
    out: dict[str, dict] = {}
    for line in md_text.splitlines():
        m = MD_ITEM_RE.match(line)
        if not m:
            continue
        meta, _, text = m.group("rest").partition(": ")
        merged_ids: list[str] = []
        note = MD_MERGE_NOTE_RE.search(text)
        if note and not text[note.end():].strip():
            merged_ids = re.findall(r"AI-\d{3,}", note.group("ids"))
            text = text[:note.start()].rstrip()
        item_type = ""
        for group in MD_PAREN_RE.findall(meta):
            if MD_COUNT_RE.match(group):
                continue
            if MD_MERGE_NOTE_RE.fullmatch(f"({group})"):
                merged_ids = re.findall(r"AI-\d{3,}", group) + merged_ids
                continue
            item_type = group
        status = m.group("status")
        merged_into = ""
        ms = MD_MERGED_STATUS_RE.match(status)
        if ms:
            status, merged_into = "merged", ms.group(1)
        out[m.group("id")] = {"status": status, "type": item_type, "text": text,
                              "merged_into": merged_into, "merged_ids": merged_ids}
    return out


def reconcile_from_md(items: list[ActionItem], md_text: str) -> None:
    """Fold hand edits from the rendered _ACTION-ITEMS.md back onto the corpus:
    statuses, types, retitles, and '(merged AI-NNN)' merge notes win over the
    extracted fields. Each curated field is applied only when it changed in
    the md since the last render (item.md_*), so direct JSON edits — and the
    collapsed '[merged → AI-XXX]' re-render — reconcile to no-ops."""
    parsed = parse_action_items_md(md_text)
    by_id = {it.id: it for it in items}

    def merge_into(survivor: ActionItem | None, merged_id: str) -> None:
        merged = by_id.get(merged_id)
        if merged is None:
            log(f"WARN _ACTION-ITEMS.md names unknown id {merged_id}; ignored")
            return
        if survivor is None or merged is survivor:
            return
        if merged.status == "merged":
            if merged.merged_into != survivor.id:
                log(f"WARN {merged_id} already merged into {merged.merged_into}; "
                    f"not re-merging into {survivor.id}")
            return
        have = {(o.meeting, o.line) for o in survivor.occurrences}
        survivor.occurrences += [o for o in merged.occurrences if (o.meeting, o.line) not in have]
        survivor.first_seen = min(survivor.first_seen, merged.first_seen)
        survivor.last_seen = max(survivor.last_seen, merged.last_seen)
        merged.status, merged.merged_into = "merged", survivor.id
        log(f"  ~ {merged_id} merged into {survivor.id} (hand edit)")

    for line_id in sorted(parsed):
        entry = parsed[line_id]
        it = by_id.get(line_id)
        if it is None:
            log(f"WARN _ACTION-ITEMS.md lists unknown id {line_id}; ignored")
            continue
        for merged_id in entry["merged_ids"]:
            merge_into(it, merged_id)
        if entry["merged_into"]:
            merge_into(by_id.get(entry["merged_into"]), line_id)

    for it in items:
        entry = parsed.get(it.id)
        if entry is None:
            log(f"NOTE {it.id} absent from _ACTION-ITEMS.md; kept")
            continue
        if it.status != "merged" and entry["status"] and entry["status"] != it.status \
                and entry["status"] != it.md_status:
            log(f"  ~ {it.id} status {it.status!r} -> {entry['status']!r} (hand edit)")
            it.status = entry["status"]
        if entry["text"] and entry["text"] != it.text and entry["text"] != it.md_text:
            log(f"  ~ {it.id} retitled (hand edit): {entry['text']}")
            it.text = entry["text"]
        if it.status != "merged" and entry["type"] != it.type and entry["type"] != it.md_type:
            log(f"  ~ {it.id} type -> {entry['type']!r} (hand edit)")
            it.type = entry["type"]


def fold_meeting(meeting_folder: str, bullets: list[tuple],
                 items: list[ActionItem], next_id: list[int],
                 threshold: float = SIMILARITY_THRESHOLD,
                 near_misses: list[tuple[str, str, float]] | None = None) -> list[ActionItem]:
    """Bullets are (line, owner, text) or (line, owner, text, section); a
    section becomes the type of a new item, or of a matched item that has
    none yet (hand-set types were reconciled before folding and win)."""
    for entry in bullets:
        line_no, owner, text = entry[:3]
        section = str(entry[3]) if len(entry) > 3 else ""
        norm = normalize_text(text)
        if not norm:
            continue
        match: ActionItem | None = None
        best_below: tuple[float, ActionItem] | None = None
        for it in items:
            ratio = similarity(norm, normalize_text(it.text))
            if ratio >= threshold and match is None:
                match = it
            elif it.status != "merged" and threshold - NEAR_MISS_BAND <= ratio < threshold:
                if best_below is None or ratio > best_below[0]:
                    best_below = (ratio, it)
        target = match
        if target is None:
            item = ActionItem(
                id=f"AI-{next_id[0]:03d}", text=text, owner=owner, type=section,
                first_seen=meeting_folder, last_seen=meeting_folder,
                occurrences=[Occurrence(meeting=meeting_folder, line=line_no)],
            )
            next_id[0] += 1
            items.append(item)
            target = item
            log(f"  + {item.id} (new{', ' + section if section else ''}): {text}")
        else:
            if not target.owner and owner:
                target.owner = owner
            if not target.type and section and target.status != "merged":
                target.type = section
            target.last_seen = max(target.last_seen, meeting_folder)
            if not any(o.meeting == meeting_folder for o in target.occurrences):
                target.occurrences.append(Occurrence(meeting=meeting_folder, line=line_no))
            log(f"  = {target.id} (dedup, {len(target.occurrences)}×): {text}")
        if best_below is not None and near_misses is not None:
            near_misses.append((target.id, best_below[1].id, best_below[0]))
    return items


def possible_duplicates(items: list[ActionItem],
                        near_misses: list[tuple[str, str, float]],
                        threshold: float) -> list[tuple[str, str, float]]:
    """Distinct corpus-item pairs scoring in [threshold - 0.10, threshold),
    deduped by id pair, most similar first. Merged items are skipped."""
    best: dict[tuple[str, str], float] = {}

    def add(id_a: str, id_b: str, ratio: float) -> None:
        key = tuple(sorted((id_a, id_b)))
        best[key] = max(best.get(key, 0.0), ratio)

    for id_a, id_b, ratio in near_misses:
        add(id_a, id_b, ratio)
    live = [it for it in items if it.status != "merged"]
    norms = [normalize_text(it.text) for it in live]
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            ratio = similarity(norms[i], norms[j])
            if threshold - NEAR_MISS_BAND <= ratio < threshold:
                add(live[i].id, live[j].id, ratio)
    return sorted(((a, b, r) for (a, b), r in best.items()),
                  key=lambda x: (-x[2], x[0], x[1]))


def render_action_items_md(ws: Path, items: list[ActionItem],
                           dupes: list[tuple[str, str, float]] | None = None) -> str:
    lines = [f"# Action items — {ws.resolve()}", "",
             "Living corpus, deduplicated across meetings. Ids are stable and never "
             "renumber; hand edits (status, type, retitle, merges) survive re-runs. "
             "Grouped by owner, then status.", ""]
    if not items:
        lines += ["_No action items yet._", ""]
        return "\n".join(lines)
    groups: dict[str, dict[str, list[ActionItem]]] = {}
    for it in items:
        groups.setdefault(it.owner or "(unassigned)", {}).setdefault(it.status, []).append(it)
    for owner in sorted(groups):
        lines.append(f"## {owner}")
        lines.append("")
        for status in STATUSES + tuple(s for s in groups[owner] if s not in STATUSES):
            bucket = groups[owner].get(status)
            if not bucket:
                continue
            lines.append(f"### {status}")
            lines.append("")
            for it in bucket:
                if it.status == "merged":
                    lines.append(f"- **{it.id}** [merged → {it.merged_into}] "
                                 f"({len(it.occurrences)}×): {it.text}")
                else:
                    span = it.first_seen if it.first_seen == it.last_seen else f"{it.first_seen} → {it.last_seen}"
                    type_part = f" ({it.type})" if it.type else ""
                    lines.append(f"- **{it.id}** [{it.status}]{type_part} {span} "
                                 f"({len(it.occurrences)}×): {it.text}")
            lines.append("")
    if dupes:
        texts = {it.id: it.text for it in items}
        lines += ["## Possible duplicates (review)", "",
                  "_Pairs scoring within 0.10 below the similarity threshold — "
                  "merge by hand if truly alike._", ""]
        for a, b, ratio in dupes:
            lines.append(f"- {a} ↔ {b} ({ratio:.2f}): \"{texts[b]}\"")
        lines.append("")
    return "\n".join(lines)


# ---- rollup: commitment corpus ------------------------------------------------------

@dataclass
class CommitmentItem:
    id: str
    text: str
    speaker: str = ""
    status: str = "open"
    priority: str = "normal"
    requested_by: str = ""
    first_seen: str = ""
    last_seen: str = ""
    merged_into: str = ""
    md_status: str = ""
    md_text: str = ""
    md_speaker: str = ""
    occurrences: list[Occurrence] = field(default_factory=list)


def load_commitments_corpus(ws: Path) -> dict:
    try:
        data = json.loads((ws / "_commitments.json").read_text())
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data.setdefault("next_id", 1)
            data.setdefault("folded_meetings", [])
            for item in data["items"]:
                item["occurrences"] = [Occurrence(**o) for o in item.get("occurrences", [])]
                item.setdefault("status", "open")
                for key in ("speaker", "priority", "requested_by", "merged_into",
                            "md_status", "md_text", "md_speaker"):
                    item.setdefault(key, "")
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"WARN _commitments.json unreadable ({e}); starting a fresh corpus")
    return {"next_id": 1, "folded_meetings": [], "items": []}


def commitments_to_items(data: dict) -> list[CommitmentItem]:
    return [CommitmentItem(**item) for item in data.get("items", [])]


def parse_commitments_md(md_text: str) -> dict[str, dict]:
    """Rendered _COMMITMENTS.md item lines -> {id: {status, speaker, text,
    merged_into, merged_ids}}. Mirrors parse_action_items_md: count parens
    are skipped, hand-merge notes '(merged CM-NNN, ...)' are honored both
    before the ': ' and trailing the item text, and the remaining paren
    group is the speaker."""
    out: dict[str, dict] = {}
    for line in md_text.splitlines():
        m = MD_CM_ITEM_RE.match(line)
        if not m:
            continue
        meta, _, text = m.group("rest").partition(": ")
        merged_ids: list[str] = []
        note = MD_CM_MERGE_NOTE_RE.search(text)
        if note and not text[note.end():].strip():
            merged_ids = re.findall(r"CM-\d{3,}", note.group("ids"))
            text = text[:note.start()].rstrip()
        speaker = ""
        for group in MD_PAREN_RE.findall(meta):
            if MD_COUNT_RE.match(group):
                continue
            if MD_CM_MERGE_NOTE_RE.fullmatch(f"({group})"):
                merged_ids = re.findall(r"CM-\d{3,}", group) + merged_ids
                continue
            speaker = group
        status = m.group("status")
        merged_into = ""
        ms = MD_CM_MERGED_STATUS_RE.match(status)
        if ms:
            status, merged_into = "merged", ms.group(1)
        out[m.group("id")] = {"status": status, "speaker": speaker, "text": text,
                              "merged_into": merged_into, "merged_ids": merged_ids}
    return out


def reconcile_commitments_from_md(items: list[CommitmentItem], md_text: str) -> None:
    """Fold hand edits from the rendered _COMMITMENTS.md back onto the
    corpus, mirroring reconcile_from_md: statuses, speakers, retitles, and
    '(merged CM-NNN)' merge notes win over the extracted fields; each
    curated field applies only when it changed in the md since the last
    render (item.md_*)."""
    parsed = parse_commitments_md(md_text)
    by_id = {it.id: it for it in items}

    def merge_into(survivor: CommitmentItem | None, merged_id: str) -> None:
        merged = by_id.get(merged_id)
        if merged is None:
            log(f"WARN _COMMITMENTS.md names unknown id {merged_id}; ignored")
            return
        if survivor is None or merged is survivor:
            return
        if merged.status == "merged":
            if merged.merged_into != survivor.id:
                log(f"WARN {merged_id} already merged into {merged.merged_into}; "
                    f"not re-merging into {survivor.id}")
            return
        have = {(o.meeting, o.line) for o in survivor.occurrences}
        survivor.occurrences += [o for o in merged.occurrences if (o.meeting, o.line) not in have]
        survivor.first_seen = min(survivor.first_seen, merged.first_seen)
        survivor.last_seen = max(survivor.last_seen, merged.last_seen)
        merged.status, merged.merged_into = "merged", survivor.id
        log(f"  ~ {merged_id} merged into {survivor.id} (hand edit)")

    for line_id in sorted(parsed):
        entry = parsed[line_id]
        it = by_id.get(line_id)
        if it is None:
            log(f"WARN _COMMITMENTS.md lists unknown id {line_id}; ignored")
            continue
        for merged_id in entry["merged_ids"]:
            merge_into(it, merged_id)
        if entry["merged_into"]:
            merge_into(by_id.get(entry["merged_into"]), line_id)

    for it in items:
        entry = parsed.get(it.id)
        if entry is None:
            log(f"NOTE {it.id} absent from _COMMITMENTS.md; kept")
            continue
        if it.status != "merged" and entry["status"] and entry["status"] != it.status \
                and entry["status"] != it.md_status:
            log(f"  ~ {it.id} status {it.status!r} -> {entry['status']!r} (hand edit)")
            it.status = entry["status"]
        if entry["text"] and entry["text"] != it.text and entry["text"] != it.md_text:
            log(f"  ~ {it.id} retitled (hand edit): {entry['text']}")
            it.text = entry["text"]
        if it.status != "merged" and entry["speaker"] != it.speaker \
                and entry["speaker"] != it.md_speaker:
            log(f"  ~ {it.id} speaker -> {entry['speaker']!r} (hand edit)")
            it.speaker = entry["speaker"]


def fold_commitments(meeting_folder: str, entries: list[dict],
                     items: list[CommitmentItem], next_id: list[int],
                     threshold: float = SIMILARITY_THRESHOLD,
                     near_misses: list[tuple[str, str, float]] | None = None) -> list[CommitmentItem]:
    for n, entry in enumerate(entries, start=1):
        text = str(entry.get("text", ""))
        norm = normalize_text(text)
        if not norm:
            continue
        speaker = str(entry.get("speaker", ""))
        match: CommitmentItem | None = None
        best_below: tuple[float, CommitmentItem] | None = None
        for it in items:
            ratio = similarity(norm, normalize_text(it.text))
            if ratio >= threshold and match is None:
                match = it
            elif it.status != "merged" and threshold - NEAR_MISS_BAND <= ratio < threshold:
                if best_below is None or ratio > best_below[0]:
                    best_below = (ratio, it)
        target = match
        if target is None:
            item = CommitmentItem(
                id=f"CM-{next_id[0]:03d}", text=text, speaker=speaker,
                priority=entry.get("priority") or "normal",
                requested_by=entry.get("requested_by", "") or "",
                first_seen=meeting_folder, last_seen=meeting_folder,
                occurrences=[Occurrence(meeting=meeting_folder, line=int(entry.get("line", n)))],
            )
            next_id[0] += 1
            items.append(item)
            target = item
            log(f"  + {item.id} (new): {text}")
        else:
            if not target.speaker and speaker:
                target.speaker = speaker
            if entry.get("priority") == "high":
                target.priority = "high"
            target.last_seen = max(target.last_seen, meeting_folder)
            if not any(o.meeting == meeting_folder for o in target.occurrences):
                target.occurrences.append(Occurrence(meeting=meeting_folder, line=int(entry.get("line", n))))
            log(f"  = {target.id} (dedup, {len(target.occurrences)}×): {text}")
        if best_below is not None and near_misses is not None:
            near_misses.append((target.id, best_below[1].id, best_below[0]))
    return items


def render_commitments_md(ws: Path, items: list[CommitmentItem],
                          dupes: list[tuple[str, str, float]] | None = None) -> str:
    lines = [f"# Commitments — {ws.resolve()}", "",
             "dev-commitments: commitments self made across meetings. Living "
             "corpus, deduplicated across meetings. Ids are stable and never "
             "renumbered; hand edits (status, speaker, retitle, merges) survive "
             "re-runs. Grouped by status, then speaker; **[boss]** marks "
             "boss-requested items.", ""]
    if not items:
        lines += ["_No commitments yet._", ""]
        return "\n".join(lines)
    groups: dict[str, dict[str, list[CommitmentItem]]] = {}
    for it in items:
        groups.setdefault(it.status, {}).setdefault(it.speaker or "(unattributed)", []).append(it)
    for status in CM_STATUSES + tuple(s for s in groups if s not in CM_STATUSES):
        if status not in groups:
            continue
        lines.append(f"## {status}")
        lines.append("")
        for speaker in sorted(groups[status]):
            lines.append(f"### {speaker}")
            lines.append("")
            for it in groups[status][speaker]:
                if it.status == "merged":
                    lines.append(f"- **{it.id}** [merged → {it.merged_into}] "
                                 f"({len(it.occurrences)}×): {it.text}")
                else:
                    span = it.first_seen if it.first_seen == it.last_seen else f"{it.first_seen} → {it.last_seen}"
                    boss = "**[boss]** " if it.priority == "high" else ""
                    lines.append(f"- **{it.id}** [{it.status}] ({it.speaker}) {span} "
                                 f"({len(it.occurrences)}×): {boss}{it.text}")
            lines.append("")
    if dupes:
        texts = {it.id: it.text for it in items}
        lines += ["## Possible duplicates (review)", "",
                  "_Pairs scoring within 0.10 below the similarity threshold — "
                  "merge by hand if truly alike._", ""]
        for a, b, ratio in dupes:
            lines.append(f"- {a} ↔ {b} ({ratio:.2f}): \"{texts[b]}\"")
        lines.append("")
    return "\n".join(lines)


def cmd_rollup(args: argparse.Namespace) -> int:
    if not 0.5 <= args.similarity_threshold <= 1.0:
        log(f"rollup: --similarity-threshold must be between 0.5 and 1.0 "
            f"(got {args.similarity_threshold})")
        return 1
    ws = Path(args.workspace_dir)
    if not ws.is_dir():
        log(f"rollup: workspace dir not found: {ws}")
        return 1

    if args.rebuild:
        log("--rebuild: resetting manifest + corpus and rebuilding from folders")
        manifest_data = {"meetings": []}
        corpus_data = {"next_id": 1, "folded_meetings": [], "items": []}
        commitments_data = {"next_id": 1, "folded_meetings": [], "items": []}
    else:
        manifest_data = load_manifest(ws)
        corpus_data = load_corpus(ws)
        commitments_data = load_commitments_corpus(ws)

    prev_meetings = manifest_to_meetings(manifest_data)
    dated: list[Path] = []
    orphans: list[str] = []
    for entry in sorted(ws.iterdir()):
        if entry.name.startswith(("_", ".")):
            continue
        if entry.is_dir() and DATE_DIR_RE.match(entry.name):
            dated.append(entry)
        elif entry.is_dir():
            orphans.append(entry.name)
    stale = [f for f in prev_meetings if f not in {p.name for p in dated}]

    meetings = [scan_meeting(folder, prev_meetings.get(folder.name)) for folder in dated]
    manifest_data = {
        "meetings": [
            {k: v for k, v in vars(m).items() if v is not None} for m in meetings
        ],
        "commitments_corpus": "_commitments.json",
    }

    items = corpus_to_items(corpus_data)
    next_id = [int(corpus_data.get("next_id", 1))]
    folded: set[str] = set(corpus_data.get("folded_meetings", []))
    ai_out = Path(args.action_items_out) if args.action_items_out else ws / "_ACTION-ITEMS.md"

    if not args.rebuild and ai_out.is_file():
        log(f"reconciling hand edits from {ai_out.name}")
        reconcile_from_md(items, ai_out.read_text())

    near_misses: list[tuple[str, str, float]] = []
    if args.action_items:
        for folder in dated:
            md_path = folder / "action-items.md"
            if not md_path.is_file():
                continue
            if folder.name in folded:
                log(f"  {folder.name}: action items already folded (skipping)")
                continue
            bullets = parse_bullets_with_sections(md_path.read_text())
            log(f"folding {folder.name}/action-items.md ({len(bullets)} item(s))")
            fold_meeting(folder.name, bullets, items, next_id,
                         args.similarity_threshold, near_misses)
            folded.add(folder.name)
    dupes = possible_duplicates(items, near_misses, args.similarity_threshold)
    ai_md = render_action_items_md(ws, items, dupes)
    for it in items:
        it.md_status, it.md_text = it.status, it.text
        it.md_type = it.type if it.status != "merged" else ""
    corpus_data = {
        "next_id": next_id[0],
        "similarity_threshold": args.similarity_threshold,
        "folded_meetings": sorted(folded),
        "items": [
            {
                "id": it.id, "text": it.text, "type": it.type, "owner": it.owner,
                "status": it.status, "first_seen": it.first_seen, "last_seen": it.last_seen,
                "merged_into": it.merged_into,
                "occurrences": [vars(o) for o in it.occurrences],
                "md_status": it.md_status, "md_text": it.md_text, "md_type": it.md_type,
            }
            for it in items
        ],
    }

    cm_items = commitments_to_items(commitments_data)
    cm_next_id = [int(commitments_data.get("next_id", 1))]
    cm_folded: set[str] = set(commitments_data.get("folded_meetings", []))
    cm_out = Path(args.commitments_out) if args.commitments_out else ws / "_COMMITMENTS.md"

    if not args.rebuild and cm_out.is_file():
        log(f"reconciling hand edits from {cm_out.name}")
        reconcile_commitments_from_md(cm_items, cm_out.read_text())

    cm_near_misses: list[tuple[str, str, float]] = []
    for folder in dated:
        cj_path = folder / "commitments.json"
        if not cj_path.is_file():
            continue
        if folder.name in cm_folded:
            log(f"  {folder.name}: commitments already folded (skipping)")
            continue
        try:
            cj_data = json.loads(cj_path.read_text())
            entries = cj_data.get("items", []) if isinstance(cj_data, dict) else []
        except Exception as e:  # noqa: BLE001
            log(f"WARN {folder.name}/commitments.json unreadable ({e}); skipped")
            continue
        log(f"folding {folder.name}/commitments.json ({len(entries)} item(s))")
        fold_commitments(folder.name, entries, cm_items, cm_next_id,
                         args.similarity_threshold, cm_near_misses)
        cm_folded.add(folder.name)
    cm_dupes = possible_duplicates(cm_items, cm_near_misses, args.similarity_threshold)
    cm_md = render_commitments_md(ws, cm_items, cm_dupes)
    for it in cm_items:
        it.md_status, it.md_text = it.status, it.text
        if it.priority == "high" and it.status != "merged":
            it.md_text = f"**[boss]** {it.text}"
        it.md_speaker = it.speaker if it.status != "merged" else ""
    commitments_data = {
        "next_id": cm_next_id[0],
        "similarity_threshold": args.similarity_threshold,
        "folded_meetings": sorted(cm_folded),
        "items": [
            {
                "id": it.id, "text": it.text, "speaker": it.speaker,
                "priority": it.priority, "requested_by": it.requested_by,
                "status": it.status, "first_seen": it.first_seen, "last_seen": it.last_seen,
                "merged_into": it.merged_into,
                "occurrences": [vars(o) for o in it.occurrences],
                "md_status": it.md_status, "md_text": it.md_text, "md_speaker": it.md_speaker,
            }
            for it in cm_items
        ],
    }

    speakers_texts = {
        m.folder: "\n".join(
            p.read_text() for p in sorted((ws / m.folder).glob("*.speakers.txt"))
        )
        for m in meetings if m.has_speakers
    }
    open_cm = sum(1 for it in cm_items if it.status == "open")
    index_md = render_index(ws, meetings, orphans, stale, recurring_topics(speakers_texts),
                            commitments=(open_cm, len(cm_items)))

    index_out = Path(args.out) if args.out else ws / "_INDEX.md"
    changed = write_if_changed(index_out, index_md)
    manifest_out = ws / "_workspace.json"
    changed_manifest = write_if_changed(
        manifest_out, json.dumps(manifest_data, indent=2) + "\n"
    )
    corpus_out = ws / "_action-items.json"
    if args.action_items or items or (ws / "_action-items.json").exists():
        changed_corpus = write_if_changed(
            corpus_out, json.dumps(corpus_data, indent=2) + "\n"
        )
        changed_ai = write_if_changed(ai_out, ai_md)
    else:
        changed_corpus = changed_ai = False
    cm_corpus_out = ws / "_commitments.json"
    if cm_items or cm_corpus_out.exists():
        changed_cm_corpus = write_if_changed(
            cm_corpus_out, json.dumps(commitments_data, indent=2) + "\n"
        )
        changed_cm = write_if_changed(cm_out, cm_md)
    else:
        changed_cm_corpus = changed_cm = False

    log(f"index -> {index_out}" + ("" if changed else " (unchanged)"))
    log(f"manifest ({len(meetings)} meetings) -> {manifest_out}"
        + ("" if changed_manifest else " (unchanged)"))
    if changed_corpus or changed_ai:
        log(f"corpus ({len(items)} items, next id AI-{next_id[0]:03d}) -> {corpus_out}")
    elif args.action_items:
        log(f"corpus unchanged ({len(items)} items)")
    if changed_cm_corpus or changed_cm:
        log(f"commitments ({len(cm_items)} items, next id CM-{cm_next_id[0]:03d}) -> {cm_corpus_out}")
    if stale:
        log(f"NOTE {len(stale)} stale manifest entr(ies); re-run with --rebuild to drop them")
    return 0


# ---- CLI --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workspace.py",
        description="whosaid meeting-workspace layer: dated folders, action items, "
                    "and the coverage/corpus roll-up (issues #2, #3).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pfn = sub.add_parser(
        "folder-name", help="dated folder name YYYY-MM-DD-HHMM from the recording's "
                            "own creation_time (mtime fallback)")
    pfn.add_argument("audio", help="audio file to read the timestamp from")
    pfn.add_argument("--tz", default="UTC",
                     help="IANA timezone to render the local time in (default: UTC)")
    pfn.set_defaults(func=cmd_folder_name)

    ph = sub.add_parser("hash", help="sha256 of an audio file (stable ingest identity)")
    ph.add_argument("audio")
    ph.set_defaults(func=cmd_hash)

    pai = sub.add_parser(
        "action-items", help="per-meeting action items; pluggable hook gets the "
                             "transcript on stdin and returns markdown")
    pai.add_argument("--transcript", required=True,
                     help="speaker-labeled transcript (*.speakers.txt)")
    pai.add_argument("--md-out", default=None,
                     help="markdown output path (default: action-items.md next to the transcript)")
    pai.add_argument("--json-out", default=None, help="also write parsed items as JSON")
    pai.add_argument("--hook", default=None,
                     help="shell command producing markdown from stdin "
                          "(default: $WHOSAID_ACTION_ITEMS_HOOK)")
    pai.add_argument("--engine", choices=ENGINES, default=None,
                     help="auto: hook if set, else ollama if it answers, else skeleton; "
                          "ollama: built-in local-model summarizer (lib/action_items.py); "
                          "hook: the --hook command; none: skeleton "
                          "(default: [summarizer] engine in whosaid.toml, else auto)")
    pai.add_argument("--ws", default=None,
                     help="workspace dir whose whosaid.toml configures the summarizer "
                          "(default: the transcript's parent's parent)")
    pai.set_defaults(func=cmd_action_items)

    pcm = sub.add_parser(
        "commitments", help="per-meeting dev commitments (first-person cues from "
                            "the self-role speaker); pluggable hook like action-items")
    pcm.add_argument("--transcript", required=True,
                     help="speaker-labeled transcript (*.speakers.txt)")
    pcm.add_argument("--json-out", required=True,
                     help="parsed commitments JSON output path (commitments.md is "
                          "written alongside it)")
    pcm.add_argument("--hook", default=None,
                     help="shell command producing markdown from stdin "
                          "(default: $WHOSAID_COMMITMENTS_HOOK, else stdlib heuristic)")
    pcm.add_argument("--roles", default=None,
                     help="roles JSON '{name: role}' string or @path "
                          "(default: '# Role:' headers in the transcript)")
    pcm.set_defaults(func=cmd_commitments)

    pr = sub.add_parser("rollup", help="aggregate a meeting workspace: coverage index, "
                                       "audit, recurring topics, action-item corpus")
    pr.add_argument("workspace_dir", help="workspace directory of dated meeting folders")
    pr.add_argument("--action-items", action="store_true",
                    help="fold each meeting's action-items.md into the deduplicated corpus")
    pr.add_argument("-o", "--out", default=None,
                    help="index markdown path (default: <workspace>/_INDEX.md)")
    pr.add_argument("--action-items-out", default=None,
                    help="corpus markdown path (default: <workspace>/_ACTION-ITEMS.md)")
    pr.add_argument("--commitments-out", default=None,
                    help="commitments corpus markdown path (default: <workspace>/_COMMITMENTS.md)")
    pr.add_argument("--similarity-threshold", type=float, default=SIMILARITY_THRESHOLD,
                    help="difflib ratio at or above which two items fold together "
                         "(0.5-1.0, default: %(default)s); pairs within 0.10 below it "
                         "are listed as possible duplicates")
    pr.add_argument("--rebuild", action="store_true",
                    help="reset manifest + corpus and rebuild from folders")
    pr.set_defaults(func=cmd_rollup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
