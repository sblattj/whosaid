"""Local source-message identity and attribution for imported text (#110).

No transcript is rewritten. Provenance points to every original turn, while a
message identity lets consumers count overlapping imports once. Audio turns and
ambiguous matches remain separate; text similarity alone never establishes identity.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from wsconfig import iter_meetings, load_config, parse_turns

TEXT_KINDS = {"teams-chat", "outlook-mail"}


def normalized(text: str) -> str:
    # Importers disagree about spaces after punctuation when flattening DOM
    # paragraphs. Preserve words/case, normalize only that formatting difference.
    return re.sub(r"\s*([,.;:!?])\s*", r"\1", " ".join(str(text).split()))


def _digest(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _context(source: dict) -> list[str]:
    context = source.get("chat_id") or source.get("conversation_id")
    return ["id", str(context)] if context else ["name", normalized(source.get("chat_name", ""))]


def source_message_id(source: dict, segment: dict) -> str | None:
    """Prefer a real source ID, else exact context + clock + speaker + text.

    Old imports synthesized ``mid`` from epoch_ms. That is a timestamp, not a
    globally meaningful ID. Missing source context or clock means no cross-file
    deduplication. Conversation names are a conservative legacy fallback.
    """
    kind = source.get("kind")
    if kind not in TEXT_KINDS:
        return None
    context = _context(source)
    if not context[1]:
        return None
    epoch = segment.get("epoch_ms")
    explicit = segment.get("message_id") or segment.get("source_message_id")
    mid = segment.get("mid")
    if not explicit and mid is not None and str(mid) != str(epoch):
        explicit = mid
    if explicit:
        return "msg:" + _digest([kind, context, str(explicit)])
    clock = epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else None
    if clock is None and source.get("day") and segment.get("start") is not None:
        clock = [source["day"], segment["start"]]
    if clock is None:
        return None
    return "msg:" + _digest([kind, context, clock, normalized(segment.get("speaker", "")),
                            normalized(segment.get("text", ""))])


_REPLY_HEADER = re.compile(
    r"^[A-Z][\w.'-]*(?:,?\s+(?:[A-Z][\w.'-]*|\([^()]{1,40}\))){0,4}"
    r"\s+\d{1,2}/\d{1,2}/\d{4},?\s+\d{1,2}:\d{2}\s*(?i:AM|PM)\b")


def assertion_parts(text: str, metadata: dict | None = None) -> dict:
    """Separate explicit quoted text; abstain on flattened reply headers.

    Search always retains verbatim text. Consumers inferring promises should use
    assertion_text and skip attribution_uncertain. We do not guess where a
    flattened quoted passage ends and a new author's response begins.
    """
    meta = metadata or {}
    if isinstance(meta.get("assertion_text"), str):
        assertion = meta["assertion_text"]
        return {"assertion_text": assertion, "quoted": bool(meta.get("quoted") or meta.get("quoted_text") or meta.get("quotes")),
                "attribution_uncertain": bool(meta.get("attribution_uncertain"))}
    if meta.get("is_quote") is True:
        return {"assertion_text": "", "quoted": True, "attribution_uncertain": False}
    quoted = meta.get("quoted_text")
    if isinstance(quoted, str) and quoted.strip() and quoted in text:
        return {"assertion_text": text.replace(quoted, "", 1).strip(), "quoted": True,
                "attribution_uncertain": False}
    lines = text.splitlines()
    if any(line.lstrip().startswith(">") for line in lines):
        return {"assertion_text": "\n".join(line for line in lines if not line.lstrip().startswith(">")),
                "quoted": True, "attribution_uncertain": False}
    # Common flattened reply-attribution header: display name, date, wall clock. The
    # prefix must look like a display name (capitalized words, "Last, First", a
    # parenthesized tag), so a sentence that merely mentions a date keeps its text.
    if _REPLY_HEADER.match(text):
        return {"assertion_text": "", "quoted": True, "attribution_uncertain": True}
    return {"assertion_text": text, "quoted": False, "attribution_uncertain": False}


class MessageIndex:
    """Read-only identity index shared by search and commitment recurrence.

    resolve accepts an exact transcript line or a unique timestamp. A summarized
    action-item line is not a transcript line: pass its bracket timestamp instead.
    No ambiguous timestamp is guessed. ``occurrence_keys`` is safe for legacy
    items without turn pointers: they retain their independent meeting key.
    """

    def __init__(self, workspace: Path | str, conversation_aliases: dict | None = None):
        self.workspace = Path(workspace)
        cfg = load_config(self.workspace)
        self.aliases = conversation_aliases if conversation_aliases is not None else (cfg.get("messages") or {}).get("conversation_aliases", {})
        if not isinstance(self.aliases, dict):
            self.aliases = {}
        self.by_meeting = defaultdict(list)
        self.by_id = defaultdict(list)
        possible = defaultdict(list)
        possible_contexts = defaultdict(set)
        for meeting, files in iter_meetings(self.workspace):
            for path in files:
                try:
                    turns = parse_turns(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                sidecar_path = path.with_name(path.name.replace(".speakers.txt", ".diarization.json"))
                try:
                    sidecar = json.loads(sidecar_path.read_text())
                except (OSError, ValueError):
                    sidecar = {}
                if not isinstance(sidecar, dict):
                    sidecar = {}
                meta = sidecar.get("source") or {}
                if not isinstance(meta, dict):
                    meta = {}
                meta = dict(meta)
                alias = self.aliases.get(meta.get("chat_name"))
                if alias and not meta.get("chat_id") and not meta.get("conversation_id"):
                    meta["chat_name"] = str(alias)
                segments = [s for s in (sidecar.get("segments") or []) if isinstance(s, dict)]
                by_turn = defaultdict(list)
                for segment in segments:
                    by_turn[(segment.get("speaker"), segment.get("start"),
                             normalized(segment.get("text", "")))].append(segment)
                # A complete sidecar/transcript sequence preserves individual
                # IDs even for simultaneous identical bodies. Partial or edited
                # transcripts still require an unambiguous content/time match.
                ordered = len(turns) == len(segments) and all(
                    (s.get("speaker"), s.get("start"), normalized(s.get("text", ""))) ==
                    (t.speaker, t.t_sec, normalized(t.text)) for s, t in zip(segments, turns))
                for position, turn in enumerate(turns):
                    matches = by_turn.get((turn.speaker, turn.t_sec, normalized(turn.text)), [])
                    identities = {source_message_id(meta, s) for s in matches}
                    segment = segments[position] if ordered else (matches[0] if len(identities) == 1 and matches else {})
                    key = source_message_id(meta, segment) if segment else None
                    pointer = {"meeting": meeting, "file": path.name, "line": turn.line,
                               "t_sec": turn.t_sec, "t_str": turn.t_str}
                    key = key or "turn:" + _digest([meeting, path.name, turn.line, turn.t_str, turn.speaker, turn.text])
                    row = {**pointer, "message_id": key, "source_kind": meta.get("kind"),
                           "speaker": turn.speaker, "text": turn.text,
                           **assertion_parts(turn.text, segment)}
                    self.by_meeting[meeting].append(row)
                    self.by_id[key].append(pointer)
                    if meta.get("kind") in TEXT_KINDS and segment.get("epoch_ms") is not None and len(normalized(turn.text)) >= 80:
                        candidate = _digest([meta["kind"], segment["epoch_ms"], turn.speaker, normalized(turn.text)])
                        possible[candidate].append(row)
                        possible_contexts[candidate].add(tuple(_context(meta)))
        self.possible_duplicates = [
            {"reason": "same_exact_clock_speaker_and_long_text_across_contexts", "confirmed": False,
             "message_ids": sorted({r["message_id"] for r in rows}),
             "provenance": [{k: r[k] for k in ("meeting", "file", "line", "t_sec")} for r in rows]}
            for candidate, rows in possible.items()
            if len(possible_contexts[candidate]) > 1 and len({r["message_id"] for r in rows}) > 1
        ]

    def resolve(self, meeting: str, t_sec=None, text: str = "", line=None, source=None) -> dict | None:
        rows = self.by_meeting.get(meeting, [])
        exact_line = False
        if source and source.endswith(".speakers.txt"):
            rows = [r for r in rows if r["file"] == source]
        if line is not None:
            exact = [r for r in rows if r["line"] == line and (t_sec is None or r["t_sec"] == t_sec)]
            if exact:
                rows = exact
                exact_line = True
        if t_sec is not None:
            rows = [r for r in rows if r["t_sec"] == t_sec]
        elif text and not exact_line:
            rows = [r for r in rows if normalized(r["text"]) == normalized(text)]
        elif not exact_line:
            return None
        if len({r["message_id"] for r in rows}) != 1:
            return None
        row = rows[0]
        return {**row, "provenance": list(self.by_id[row["message_id"]])}

    def occurrence_keys(self, occurrences, text: str = "") -> set[str]:
        """Count imported messages once; audio recurrence stays meeting-based."""
        keys = set()
        for occ in occurrences:
            get = occ.get if isinstance(occ, dict) else lambda key, default=None: getattr(occ, key, default)
            meeting = get("meeting", "")
            row = self.resolve_occurrence(occ, text)
            keys.add(row["message_id"] if row and row["source_kind"] in TEXT_KINDS
                     else "meeting:" + str(meeting))
        return keys

    def resolve_occurrence(self, occurrence, text: str = "") -> dict | None:
        """Resolve corpus pointers without confusing document and transcript lines.

        Summaries often retain a precise bracket timestamp but no t_sec. Only an
        exact second that identifies one source message is usable, including a
        bracket marked ``~``. No nearest-time window is searched. Multiple
        timestamps can resolve only when they all identify the same message.
        """
        get = occurrence.get if isinstance(occurrence, dict) else lambda key, default=None: getattr(occurrence, key, default)
        meeting, source = get("meeting", ""), get("source")
        body = get("text") or text
        # CmOccurrence source="transcript" stores a commitments.json entry
        # ordinal, not a speakers-file line. Only an explicit source filename
        # establishes the latter. Source clock/author/text may disambiguate it.
        line = get("line") if str(source or "").endswith(".speakers.txt") else None
        seconds = get("t_sec")
        if source == "transcript" and seconds is not None:
            rows = [r for r in self.by_meeting.get(meeting, []) if r["t_sec"] == seconds]
            speaker = get("speaker") or get("owner")
            if speaker:
                name = lambda value: re.sub(r"[\s_-]+", " ", str(value)).strip().casefold()
                rows = [r for r in rows if name(r["speaker"]) == name(speaker)]
            if len({r["message_id"] for r in rows}) > 1 and body:
                exact = [r for r in rows if normalized(r["text"]) == normalized(body)]
                rows = exact or [r for r in rows if normalized(body) in normalized(r["text"])]
            if len({r["message_id"] for r in rows}) == 1:
                row = rows[0]
                return {**row, "provenance": list(self.by_id[row["message_id"]])}
            return None
        if seconds is not None:
            return self.resolve(meeting, t_sec=seconds, text=body, line=line, source=source)
        stamps = set()
        for bracket in re.findall(r"\[[^\]\n]+\]", body):
            for hours, minutes, seconds in re.findall(r"(?<!\d)(\d{1,3}):([0-5]\d):([0-5]\d)(?!\d)", bracket):
                stamps.add(int(hours) * 3600 + int(minutes) * 60 + int(seconds))
        if stamps:
            rows = [self.resolve(meeting, t_sec=stamp, source=source) for stamp in stamps]
            if all(rows) and len({r["message_id"] for r in rows}) == 1:
                return rows[0]
            return None
        return self.resolve(meeting, text=body, line=line, source=source)

    def provenance(self, message_id: str) -> list[dict]:
        return list(self.by_id.get(message_id, []))
