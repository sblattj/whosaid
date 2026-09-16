#!/usr/bin/env python3
"""
workspace.py: meeting-workspace layer for whosaid (GitHub issue #2).

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
      Per-meeting action items. Pluggable: --hook (or WHOSAID_ACTION_ITEMS_HOOK)
      runs as a shell command with the transcript text on stdin plus
      WHOSAID_TRANSCRIPT_PATH and WHOSAID_SPEAKERS in the environment; its
      stdout is the markdown, written to --md-out (default: action-items.md
      alongside the transcript) and mirrored to --json-out if given. With no
      hook a skeleton is emitted instead. Exits 0 either way — the offline
      default stays intact.

  rollup <workspace-dir> [--action-items] [-o INDEX.md] [--action-items-out F] [--rebuild]
      The workspace aggregate. Two JSON state files live in the workspace dir:
        _workspace.json    manifest: one entry per dated meeting folder
        _action-items.json living deduplicated action-item corpus
      Rendering: _INDEX.md (one row per meeting — date, duration, transcribed?,
      diarized?, action-items? — plus a nothing-missing audit and a recurring-
      topics section) and, with --action-items, _ACTION-ITEMS.md (corpus
      grouped by owner then status). Corpus ids are AI-001... and NEVER
      renumber; dedupe is difflib similarity >= 0.82 on normalized text;
      statuses survive re-runs; folding is append-only unless --rebuild.
      Incremental by default: re-running with nothing new writes nothing.

Everything stays LOCAL: stdlib only, no third-party imports, no network.
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
BULLET_RE = re.compile(
    r"^\s*[-*]\s+(?:[-x]\s+)?(?:\*\*(?P<owner>[^*]+?)\s*:?\*\*\s*:?\s+)?(?P<text>\S.*)$"
)
SIMILARITY_THRESHOLD = 0.82
STATUSES = ("open", "ongoing", "resolved")

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


def parse_bullets(markdown: str) -> list[tuple[int, str, str]]:
    """Action-item bullets -> [(line_no, owner, text)]. '- **Owner:** text' and
    plain '- text' both parse; owner is '' when absent."""
    out = []
    for n, line in enumerate(markdown.splitlines(), start=1):
        m = BULLET_RE.match(line)
        if m and m.group("text").strip():
            out.append((n, (m.group("owner") or "").strip(), m.group("text").strip()))
    return out


def normalize_text(text: str) -> str:
    lowered = text.lower()
    stripped = re.sub(r"[^0-9a-z\s]", " ", lowered)
    return " ".join(stripped.split())


def similar(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD


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
        "_No summarizer hook is configured, so no action items were extracted._",
        "_Pass --hook CMD (or set WHOSAID_ACTION_ITEMS_HOOK) to generate them offline._",
        "",
    ]
    if speakers:
        lines.append(f"Speakers in this meeting: {', '.join(speakers)}")
        lines.append("")
    return "\n".join(lines)


def cmd_action_items(args: argparse.Namespace) -> int:
    transcript = Path(args.transcript)
    if not transcript.is_file():
        log(f"action-items: transcript not found: {transcript}")
        return 1
    text = transcript.read_text()
    speakers = parse_speakers(text)

    hook = args.hook or os.environ.get("WHOSAID_ACTION_ITEMS_HOOK", "")
    source = "skeleton"
    markdown = ""
    if hook:
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
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            source = "hook"
            markdown = proc.stdout
        elif proc is not None:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["(no stderr)"]
            log(f"WARN action-items hook exited {proc.returncode}: {tail[0]}; writing skeleton")
    if source == "skeleton":
        markdown = skeleton_markdown(transcript, speakers)

    md_out = Path(args.md_out) if args.md_out else transcript.parent / "action-items.md"
    wrote = write_if_changed(md_out, markdown if markdown.endswith("\n") else markdown + "\n")
    log(f"action items ({source}) -> {md_out}" + (" (unchanged)" if not wrote else ""))

    if args.json_out:
        payload = {
            "transcript": str(transcript),
            "md_out": str(md_out),
            "source": source,
            "speakers": speakers,
            "items": [{"line": n, "owner": o, "text": t} for n, o, t in parse_bullets(markdown)],
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
                 topics: list[tuple[str, int, int]]) -> str:
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
    first_seen: str = ""
    last_seen: str = ""
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
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"WARN _action-items.json unreadable ({e}); starting a fresh corpus")
    return {"next_id": 1, "folded_meetings": [], "items": []}


def corpus_to_items(data: dict) -> list[ActionItem]:
    return [ActionItem(**item) for item in data.get("items", [])]


def fold_meeting(meeting_folder: str, bullets: list[tuple[int, str, str]],
                 items: list[ActionItem], next_id: list[int]) -> list[ActionItem]:
    for line_no, owner, text in bullets:
        norm = normalize_text(text)
        if not norm:
            continue
        match = next(
            (it for it in items
             if similar(norm, normalize_text(it.text))),
            None,
        )
        if match is None:
            item = ActionItem(
                id=f"AI-{next_id[0]:03d}", text=text, owner=owner,
                first_seen=meeting_folder, last_seen=meeting_folder,
                occurrences=[Occurrence(meeting=meeting_folder, line=line_no)],
            )
            next_id[0] += 1
            items.append(item)
            log(f"  + {item.id} (new): {text}")
        else:
            if not match.owner and owner:
                match.owner = owner
            match.last_seen = max(match.last_seen, meeting_folder)
            if not any(o.meeting == meeting_folder for o in match.occurrences):
                match.occurrences.append(Occurrence(meeting=meeting_folder, line=line_no))
            log(f"  = {match.id} (dedup, {len(match.occurrences)}×): {text}")
    return items


def render_action_items_md(ws: Path, items: list[ActionItem]) -> str:
    lines = [f"# Action items — {ws.resolve()}", "",
             "Living corpus, deduplicated across meetings. Ids are stable and never "
             "renumber; status survives re-runs. Grouped by owner, then status.", ""]
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
                span = it.first_seen if it.first_seen == it.last_seen else f"{it.first_seen} → {it.last_seen}"
                lines.append(f"- **{it.id}** [{it.status}] {span} ({len(it.occurrences)}×): {it.text}")
            lines.append("")
    return "\n".join(lines)


def cmd_rollup(args: argparse.Namespace) -> int:
    ws = Path(args.workspace_dir)
    if not ws.is_dir():
        log(f"rollup: workspace dir not found: {ws}")
        return 1

    if args.rebuild:
        log("--rebuild: resetting manifest + corpus and rebuilding from folders")
        manifest_data = {"meetings": []}
        corpus_data = {"next_id": 1, "folded_meetings": [], "items": []}
    else:
        manifest_data = load_manifest(ws)
        corpus_data = load_corpus(ws)

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
        ]
    }

    items = corpus_to_items(corpus_data)
    next_id = [int(corpus_data.get("next_id", 1))]
    folded: set[str] = set(corpus_data.get("folded_meetings", []))

    if args.action_items:
        for folder in dated:
            md_path = folder / "action-items.md"
            if not md_path.is_file():
                continue
            if folder.name in folded:
                log(f"  {folder.name}: action items already folded (skipping)")
                continue
            bullets = parse_bullets(md_path.read_text())
            log(f"folding {folder.name}/action-items.md ({len(bullets)} item(s))")
            fold_meeting(folder.name, bullets, items, next_id)
            folded.add(folder.name)
    corpus_data = {
        "next_id": next_id[0],
        "folded_meetings": sorted(folded),
        "items": [
            {
                "id": it.id, "text": it.text, "owner": it.owner, "status": it.status,
                "first_seen": it.first_seen, "last_seen": it.last_seen,
                "occurrences": [vars(o) for o in it.occurrences],
            }
            for it in items
        ],
    }

    speakers_texts = {
        m.folder: "\n".join(
            p.read_text() for p in sorted((ws / m.folder).glob("*.speakers.txt"))
        )
        for m in meetings if m.has_speakers
    }
    index_md = render_index(ws, meetings, orphans, stale, recurring_topics(speakers_texts))

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
        ai_out = Path(args.action_items_out) if args.action_items_out else ws / "_ACTION-ITEMS.md"
        changed_ai = write_if_changed(ai_out, render_action_items_md(ws, items))
    else:
        changed_corpus = changed_ai = False

    log(f"index -> {index_out}" + ("" if changed else " (unchanged)"))
    log(f"manifest ({len(meetings)} meetings) -> {manifest_out}"
        + ("" if changed_manifest else " (unchanged)"))
    if changed_corpus or changed_ai:
        log(f"corpus ({len(items)} items, next id AI-{next_id[0]:03d}) -> {corpus_out}")
    elif args.action_items:
        log(f"corpus unchanged ({len(items)} items)")
    if stale:
        log(f"NOTE {len(stale)} stale manifest entr(ies); re-run with --rebuild to drop them")
    return 0


# ---- CLI --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workspace.py",
        description="whosaid meeting-workspace layer: dated folders, action items, "
                    "and the coverage/corpus roll-up (issue #2).",
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
                          "(default: $WHOSAID_ACTION_ITEMS_HOOK, else skeleton)")
    pai.set_defaults(func=cmd_action_items)

    pr = sub.add_parser("rollup", help="aggregate a meeting workspace: coverage index, "
                                       "audit, recurring topics, action-item corpus")
    pr.add_argument("workspace_dir", help="workspace directory of dated meeting folders")
    pr.add_argument("--action-items", action="store_true",
                    help="fold each meeting's action-items.md into the deduplicated corpus")
    pr.add_argument("-o", "--out", default=None,
                    help="index markdown path (default: <workspace>/_INDEX.md)")
    pr.add_argument("--action-items-out", default=None,
                    help="corpus markdown path (default: <workspace>/_ACTION-ITEMS.md)")
    pr.add_argument("--rebuild", action="store_true",
                    help="reset manifest + corpus and rebuild from folders")
    pr.set_defaults(func=cmd_rollup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
