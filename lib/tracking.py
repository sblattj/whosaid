#!/usr/bin/env python3
"""Local evidence ledger for commitments, milestones, priorities, and handoffs.

The derived corpora are inputs, never rewritten here. `_tracking.json` holds
curated events and survives roll-up/reindex. Import is an explicit mutation;
views and suggestions are read-only. Standard library only, no model calls.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, time, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from time import monotonic
from typing import Any

LEDGER_NAME = "_tracking.json"
VERSION = 1
STATUSES = {"open", "in_progress", "delivered", "accepted", "blocked", "superseded", "unknown"}
EVENT_TYPES = {"status", "milestones", "priority", "handoff_requested", "handoff_accepted", "owner"}
EVIDENCE_KINDS = {"transcript", "external", "manual"}
TERMINAL = {"delivered", "accepted", "superseded", "done", "resolved", "closed"}


class TrackingError(ValueError):
    """Invalid or unreadable tracking data. Never discard existing curation."""


def _instant(value: str, end_of_day: bool = False) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TrackingError("occurred_at/as_of must be an ISO date or timestamp")
    try:
        value = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = datetime.strptime(value, "%Y-%m-%d").date()
            return datetime.combine(day, time.max if end_of_day else time.min, timezone.utc)
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise TrackingError("timestamps must include a timezone; dates are also accepted")
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise TrackingError(f"invalid ISO date/timestamp: {value!r}") from exc


def _stamp(value: str) -> str:
    return _instant(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _required(row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TrackingError(f"{key} must be a nonempty string")
    return value.strip()


def _evidence(value: Any) -> dict:
    if not isinstance(value, dict):
        raise TrackingError("every event needs an evidence object")
    out = copy.deepcopy(value)
    if out.get("kind") not in EVIDENCE_KINDS:
        raise TrackingError("evidence.kind must be transcript, external, or manual")
    out["ref"] = _required(out, "ref")
    if "confidence" in out and (isinstance(out["confidence"], bool) or
            not isinstance(out["confidence"], (int, float)) or not 0 <= out["confidence"] <= 1):
        raise TrackingError("evidence.confidence must be between 0 and 1")
    if out["kind"] == "transcript":
        out["meeting"] = _required(out, "meeting")
        if not (isinstance(out.get("line"), int) and not isinstance(out["line"], bool) and out["line"] > 0) and not (
                isinstance(out.get("t_sec"), (int, float)) and not isinstance(out["t_sec"], bool)
                and math.isfinite(out["t_sec"]) and out["t_sec"] >= 0):
            raise TrackingError("transcript evidence needs a positive line or nonnegative t_sec")
    if out.get("merged_at") is not None:
        out["merged_at"] = _stamp(out["merged_at"])
    return out


def validate_event(event: dict) -> dict:
    if not isinstance(event, dict):
        raise TrackingError("event must be an object")
    event = copy.deepcopy(event)
    if event.get("review_required") and event.get("approved") is not True:
        raise TrackingError("suggested events require approved=true before import")
    event.pop("review_required", None)
    event.pop("approved", None)
    event.pop("reason", None)
    event["item_id"] = _required(event, "item_id")
    if event.get("type") not in EVENT_TYPES:
        raise TrackingError(f"unknown event type: {event.get('type')!r}")
    event["occurred_at"] = _stamp(_required(event, "occurred_at"))
    event["evidence"] = _evidence(event.get("evidence"))
    event.setdefault("source", "curated")
    if event["source"] not in {"curated", "imported", "automatic"}:
        raise TrackingError("source must be curated, imported, or automatic")
    supersedes = event.get("supersedes", [])
    if not isinstance(supersedes, list) or not all(isinstance(x, str) and x for x in supersedes):
        raise TrackingError("supersedes must be an array of event IDs")
    event["supersedes"] = sorted(set(supersedes))
    kind = event["type"]
    if kind == "status":
        if event.get("status") not in STATUSES:
            raise TrackingError(f"invalid status: {event.get('status')!r}")
        # A provider's closed state is not proof that a PR was merged.
        if event["evidence"].get("artifact_type") == "pull_request" and event["status"] in {"delivered", "accepted"}:
            state = event["evidence"].get("state", "")
            if not isinstance(state, str):
                raise TrackingError("pull request state must be a string")
            if state.lower() == "closed" and not event["evidence"].get("merged_at"):
                raise TrackingError("closed unmerged pull request does not establish delivery")
            if (event["evidence"].get("merged_at") or "") > event["occurred_at"]:
                raise TrackingError("delivery cannot precede the referenced pull request merge")
    elif kind == "milestones":
        milestones = event.get("milestones")
        if not isinstance(milestones, list) or not milestones:
            raise TrackingError("milestones must be a nonempty array")
        seen = set()
        for milestone in milestones:
            if not isinstance(milestone, dict):
                raise TrackingError("each milestone must be an object")
            ident = _required(milestone, "id")
            _required(milestone, "text")
            _required(milestone, "acceptance")
            if ident == event["item_id"] or ident in seen:
                raise TrackingError("milestone IDs must be unique and differ from the parent")
            if "status" in milestone:
                raise TrackingError("milestone status needs its own evidence-backed status event")
            seen.add(ident)
    elif kind in {"owner", "handoff_requested", "handoff_accepted"}:
        event["owner"] = _required(event, "owner")
        if kind == "handoff_accepted":
            event["request_event_id"] = _required(event, "request_event_id")
    elif kind == "priority":
        event["speaker"] = _required(event, "speaker")
        event["scope"] = _required(event, "scope")
        event["text"] = _required(event, "text")
        event.setdefault("explicit", True)
        if not isinstance(event["explicit"], bool):
            raise TrackingError("explicit must be boolean")
        event.setdefault("state", "active")
        if event["state"] not in {"active", "deferred", "withdrawn"}:
            raise TrackingError("priority state must be active, deferred, or withdrawn")
        rank = event.get("rank")
        if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank < 1):
            raise TrackingError("priority rank must be a positive integer or null")
    content = {k: v for k, v in event.items() if k not in {"id", "recorded_at"}}
    generated_id = "EV-" + hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
    event["id"] = event.get("id") or generated_id
    if not isinstance(event["id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", event["id"]):
        raise TrackingError("invalid event id")
    if "recorded_at" in event:
        event["recorded_at"] = _stamp(event["recorded_at"])
    return event


def _validate_links(events: list[dict]) -> None:
    by_id = {e["id"]: e for e in events}
    if len(by_id) != len(events):
        raise TrackingError("duplicate event IDs")
    milestone_parents: dict[str, str] = {}
    for e in events:
        for other_id in e["supersedes"]:
            other = by_id.get(other_id)
            if not other or other["item_id"] != e["item_id"] or other["type"] != e["type"]:
                raise TrackingError("supersedes must name an existing event on the same item and type")
            if other_id == e["id"] or other["occurred_at"] > e["occurred_at"]:
                raise TrackingError("cannot supersede self or a future event")
        if e["type"] == "handoff_accepted":
            request = by_id.get(e["request_event_id"])
            if not request or request["type"] != "handoff_requested" or request["item_id"] != e["item_id"] or request["owner"] != e["owner"]:
                raise TrackingError("handoff acceptance must reference a matching request")
            if request["occurred_at"] > e["occurred_at"]:
                raise TrackingError("handoff cannot be accepted before it was requested")
        if e["type"] == "milestones":
            for milestone in e["milestones"]:
                parent = milestone_parents.setdefault(milestone["id"], e["item_id"])
                if parent != e["item_id"]:
                    raise TrackingError("a milestone ID cannot belong to multiple parents")
    # Repeated corrections can share a timestamp, so explicitly reject cycles.
    def visit(ident: str, path: set[str]) -> None:
        if ident in path:
            raise TrackingError("supersedes cycle")
        for previous in by_id[ident]["supersedes"]:
            visit(previous, path | {ident})
    for ident in by_id:
        visit(ident, set())
    for child in milestone_parents:
        seen = set()
        node = child
        while node in milestone_parents:
            if node in seen:
                raise TrackingError("milestone parent cycle")
            seen.add(node)
            node = milestone_parents[node]


def load_ledger(ws: str | Path) -> dict:
    path = Path(ws) / LEDGER_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": VERSION, "events": []}
    except (OSError, ValueError) as exc:
        raise TrackingError(f"cannot read tracking ledger: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("events"), list):
        raise TrackingError("invalid or unsupported tracking ledger; existing data was preserved")
    data["events"] = [validate_event(e) for e in data["events"]]
    _validate_links(data["events"])
    return data


def record_events(ws: str | Path, events: list[dict]) -> dict:
    """Atomically append a reviewed batch. Content-identical imports are idempotent.

    Validation is all-or-nothing. flock serializes writers; os.replace ensures
    readers see a complete ledger. No corpus or search index is modified.
    """
    ws = Path(ws)
    if not ws.is_dir():
        raise TrackingError("workspace must exist")
    proposed = [validate_event(e) for e in events]
    with (ws / "_tracking.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = load_ledger(ws)
        by_id = {e["id"]: e for e in ledger["events"]}
        added = []
        for event in proposed:
            previous = by_id.get(event["id"])
            if previous:
                comparable = lambda row: {k: v for k, v in row.items() if k != "recorded_at"}
                if comparable(previous) != comparable(event):
                    raise TrackingError("an existing event ID has different contents; append a correction instead")
                continue
            event.setdefault("recorded_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            ledger["events"].append(event)
            by_id[event["id"]] = event
            added.append(event["id"])
        _validate_links(ledger["events"])
        if added:
            fd, name = tempfile.mkstemp(prefix=".tracking-", suffix=".json", dir=ws)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(ledger, stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, ws / LEDGER_NAME)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return {"added": added, "count": len(ledger["events"])}


def record_event(ws: str | Path, event: dict) -> dict:
    event = validate_event(event)
    record_events(ws, [event])
    return event


# What the app's "Mark done" means: the owner says it is delivered.
STATUS_ALIASES = {"done": "delivered"}


def set_status(ws: str | Path, item_id: str, status: str, note: str,
               occurred_at: str | None = None) -> dict:
    """Append one curated status event for an existing AI-NNN / CM-NNN item.

    `done` means `delivered`. The evidence is manual: `note` says who marked it
    and why (for example "marked done in the whosaid app"). Unknown ids are
    refused so a typo never starts a new item's history.
    """
    import commitment_views  # local: commitment_views imports this module
    status = STATUS_ALIASES.get(status, status)
    if status not in STATUSES:
        raise TrackingError(f"status must be one of {sorted(STATUSES | set(STATUS_ALIASES))}")
    item_id = (item_id or "").strip()
    if item_id not in commitment_views.corpus_rows(Path(ws)):
        raise TrackingError(f"no item {item_id!r} in _action-items.json or _commitments.json")
    when = occurred_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return record_event(ws, {"type": "status", "item_id": item_id, "status": status, "occurred_at": when,
                             "source": "curated", "evidence": {"kind": "manual", "ref": _required({"n": note}, "n")}})


def add_milestones(ws: str | Path, item_id: str, milestones: list[dict], evidence: dict,
                   occurred_at: str) -> dict:
    return record_event(ws, {"type": "milestones", "item_id": item_id,
                             "milestones": milestones, "evidence": evidence,
                             "occurred_at": occurred_at})


def _visible(events: list[dict], as_of: str | None) -> list[dict]:
    cutoff = _instant(as_of, end_of_day=True) if as_of else datetime.now(timezone.utc)
    return sorted((e for e in events if _instant(e["occurred_at"]) <= cutoff),
                  key=lambda e: (e["occurred_at"], e.get("recorded_at", ""), e["id"]))


def _active(events: list[dict]) -> list[dict]:
    superseded = {ident for e in events for ident in e["supersedes"]}
    return [e for e in events if e["id"] not in superseded]


def _aliases(item: dict) -> set[str]:
    values = {str(item.get("id", ""))}
    for key in ("also", "tracking_aliases"):
        for value in item.get(key, []):
            values.add(str(value))
    refs = item.get("ai_refs", "")
    if isinstance(refs, str):
        values.update(re.findall(r"(?:AI|CM)-\d+", refs))
    elif isinstance(refs, (list, tuple, set)):
        values.update(str(value) for value in refs if value)
    return values


def _project(item: dict, events: list[dict], as_of: str | None, stack: set[str]) -> dict:
    out = copy.deepcopy(item)
    aliases = _aliases(item)
    relevant = [e for e in events if e["item_id"] in aliases]
    active = _active(relevant)
    out["tracking_history"] = relevant
    out["evidence"] = []
    out["conflicts"] = []
    out["as_of"] = as_of
    status_events = [e for e in active if e["type"] == "status"]
    if status_events:
        # Curated corrections have priority over automated extraction, not
        # over later explicitly imported evidence. Their sources stay visible.
        authoritative = [e for e in status_events if e["source"] != "automatic"] or status_events
        latest_time = max(e["occurred_at"] for e in authoritative)
        latest = [e for e in authoritative if e["occurred_at"] == latest_time]
        states = {e["status"] for e in latest}
        if len(states) > 1:
            out["status"] = "unknown"
            out["conflicts"].append({"type": "status", "event_ids": [e["id"] for e in latest]})
        else:
            out["status"] = latest[-1]["status"]
        out["evidence"] = [e["evidence"] for e in latest]
        out["status_source"] = "tracking"
    else:
        if as_of:
            # A corpus status is current state, not dated status history.
            out["status"] = "unknown"
            out["status_source"] = "historical_unknown"
        else:
            out.setdefault("status", "unknown")
            out["status_source"] = "corpus"
    ownership = [e for e in active if e["type"] in {"owner", "handoff_accepted"}]
    if ownership:
        latest_time = max(e["occurred_at"] for e in ownership)
        latest_owners = [e for e in ownership if e["occurred_at"] == latest_time]
        if len({e["owner"] for e in latest_owners}) > 1:
            out["owner"] = ""
            out["accepted_owner"] = ""
            out["ownership_conflict"] = True
            out["acceptance_evidence"] = []
            out["obligation_status"] = "uncertain"
            out["actionable"] = False
            out["conflicts"].append({"type": "owner", "event_ids": [e["id"] for e in latest_owners]})
        else:
            chosen = next((e for e in latest_owners if e["type"] == "handoff_accepted"), latest_owners[-1])
            out["owner"] = chosen["owner"]
            out["proposed_owner"] = chosen["owner"]
            out["owner_evidence"] = chosen["evidence"]
            standing = out.get("kind") == "standing_directive" or out.get("origin_kind") == "standing_directive"
            accepted_handoff = chosen["type"] == "handoff_accepted"
            # Assignment identifies responsibility, not the assignee's acceptance.
            out["accepted_owner"] = chosen["owner"] if accepted_handoff and not standing else ""
            out["acceptance_evidence"] = [chosen["evidence"]] if accepted_handoff else []
            if standing:
                out["kind"] = "standing_directive"
                out["obligation_status"] = "standing"
            elif accepted_handoff:
                out["kind"] = "explicit_commitment"
                out["obligation_status"] = "accepted"
                out["classification_reason"] = "evidenced acceptance of requested handoff"
            else:
                out["obligation_status"] = "proposed"
                out["classification_reason"] = "ownership assigned; acceptance not established"
            out["actionable"] = bool(out["accepted_owner"])
    accepted = {e["request_event_id"] for e in active if e["type"] == "handoff_accepted"}
    requests = [e for e in active if e["type"] == "handoff_requested" and e["id"] not in accepted]
    out["pending_handoffs"] = requests
    out["next_action_owner"] = "" if out.get("ownership_conflict") else out.get("owner") or out.get("speaker") or ""
    splits = [e for e in active if e["type"] == "milestones"]
    if splits:
        latest_time = max(e["occurred_at"] for e in splits)
        definitions = [e for e in splits if e["occurred_at"] == latest_time]
        signatures = {json.dumps(sorted(e["milestones"], key=lambda m: m["id"]), sort_keys=True) for e in definitions}
        split_conflict = len(signatures) > 1
        latest = definitions[-1]
        milestones = []
        for child in latest["milestones"]:
            if child["id"] in stack:
                raise TrackingError("milestone cycle")
            child = {"status": "open", "owner": out.get("owner", ""), **child}
            milestones.append(_project(child, events, as_of, stack | {child["id"]}))
        out["milestones"] = milestones
        out["remaining_milestones"] = [m["id"] for m in milestones if m["status"] not in TERMINAL]
        states = [m["status"] for m in milestones]
        complete = [state in TERMINAL for state in states]
        if all(complete):
            derived = "superseded" if all(s == "superseded" for s in states) else (
                "accepted" if all(s == "accepted" for s in states) else "delivered")
        elif any(complete) or any(s in {"in_progress", "partial"} for s in states):
            derived = "partial"
        elif "blocked" in states:
            derived = "blocked"
        elif "unknown" in states:
            derived = "unknown"
        else:
            derived = "open"
        if status_events and out["status"] in TERMINAL and not all(complete):
            out["conflicts"].append({"type": "parent_completion", "message": "parent completion conflicts with unfinished milestones"})
        out["status"] = derived
        out["status_source"] = "milestones"
        if split_conflict:
            # No candidate definition gets to erase the other's unfinished
            # child or replace its acceptance criterion without a correction.
            out["conflicts"].append({"type": "milestones", "event_ids": [e["id"] for e in definitions]})
            out["milestone_definitions"] = definitions
            out["milestones"] = []
            out["remaining_milestones"] = sorted({m["id"] for e in definitions for m in e["milestones"]})
            out["status"] = "unknown"
    out["evidence_available"] = bool(out["evidence"] or any(m.get("evidence_available") for m in out.get("milestones", [])))
    if "actionable" in out:
        out["overdue_eligible"] = bool(out["actionable"] and out.get("kind") == "explicit_commitment"
                                       and out["status"] in {"", "open", "ongoing", "in_progress", "partial", "blocked"})
    out["open"] = out["status"] not in TERMINAL
    return out


def reconcile_items(ws: str | Path, items: list[dict], as_of: str | None = None,
                    ledger: dict | None = None) -> list[dict]:
    """Read-only overlay, preserving caller fields and source corpus statuses.

    `also` and `ai_refs` identify equivalent corpus IDs. A date-only as_of
    includes the whole UTC day. This is event time, not knowledge-at-the-time.
    """
    ledger = ledger if ledger is not None else load_ledger(ws)
    events = _visible(ledger["events"], as_of)
    # The same underlying promise may be queried by its action-item or spoken
    # commitment ID. Build an undirected closure so evidence travels both ways.
    equivalents: dict[str, set[str]] = {}
    for item in items:
        aliases = _aliases(item)
        for ident in aliases:
            equivalents.setdefault(ident, set()).update(aliases)
    def connected(ident: str) -> set[str]:
        seen, todo = set(), [ident]
        while todo:
            current = todo.pop()
            if current not in seen:
                seen.add(current)
                todo.extend(equivalents.get(current, set()) - seen)
        return seen
    visible_items = []
    cutoff = _instant(as_of, end_of_day=True) if as_of else None
    for item in items:
        first_seen = str(item.get("first_seen", ""))
        first = re.match(r"\d{4}-\d{2}-\d{2}", first_seen)
        if cutoff and first:
            try:
                created = _instant(first_seen)
            except TrackingError:
                created = _instant(first.group())  # date-prefixed meeting folder
            if created > cutoff:
                continue
        item = {**item, "tracking_aliases": sorted(connected(str(item.get("id", ""))) - {str(item.get("id", ""))})}
        visible_items.append(_project(item, events, as_of, {str(item.get("id", ""))}))
    return visible_items


def priority_view(ws: str | Path, group: str | None = None, as_of: str | None = None,
                  ledger: dict | None = None) -> dict:
    """Temporal assertions, separate explicit and inferred ranks and conflicts.

    Different speakers' rankings are not silently combined into an official
    ordering. The selected configured group and its members are disclosed.
    """
    import wsconfig
    cfg = wsconfig.load_config(Path(ws))
    groups = cfg.get("groups", {})
    if group is not None and group not in groups:
        raise TrackingError(f"unknown configured group: {group}")
    members = list(groups[group]) if group is not None else []
    events = _visible((ledger or load_ledger(ws))["events"], as_of)
    history = [e for e in events if e["type"] == "priority" and (group is None or e["speaker"] in members)]
    active = _active(history)
    latest: dict[tuple[str, str, str], list[dict]] = {}
    for e in active:
        key = (e["speaker"], e["scope"], e["item_id"])
        if key not in latest or latest[key][0]["occurred_at"] < e["occurred_at"]:
            latest[key] = [e]
        else:
            latest[key].append(e)
    assertions = [e for rows in latest.values() for e in rows]
    conflicts = []
    by_subject: dict[tuple[str, str], list[dict]] = {}
    for e in assertions:
        by_subject.setdefault((e["scope"], e["item_id"]), []).append(e)
    for (scope, ident), rows in by_subject.items():
        if len({(e["state"], e.get("rank")) for e in rows}) > 1:
            conflicts.append({"item_id": ident, "scope": scope, "event_ids": [e["id"] for e in rows]})
    assertions.sort(key=lambda e: (e["state"] != "active", not e["explicit"], e.get("rank") or 10**9, e["scope"], e["item_id"], e["speaker"]))
    return {"group": group, "members": members, "as_of": as_of, "items": assertions,
            "history": history, "conflicts": conflicts, "ranking": "speaker assertions; not an inferred consensus"}


_STOP = set("a an the i ill im will would can could should to of for on in at by with and or it this that be been is are was were my our your we you they he she its from then also have has had do does did just more all some after before today tomorrow now later please going make get work item task says said speaker own commitments".split())
_ACTIONS = {
    "send": ({"send", "share", "post", "publish"}, r"\b(?:sent|shared|posted|published)\b"),
    "merge": ({"merge", "land"}, r"\b(?:merged|landed)\b"),
    "deploy": ({"deploy", "release", "ship"}, r"\b(?:deployed|released|shipped)\b"),
    "open": ({"open", "create", "submit"}, r"\b(?:opened|created|submitted)\b"),
    "demo": ({"demo", "demonstrate", "present"}, r"\b(?:demoed|demonstrated|presented)\b"),
    "finish": ({"finish", "complete", "fix"}, r"\b(?:finished|completed|fixed)\b"),
}
def _action_subject(text: str, past: bool = False) -> tuple[set[str], set[str]]:
    """Identify predicate cues without deleting nouns such as release or post."""
    text = re.sub(r"^\s*\[[^\]]*\]\s*", "", text.lower())
    tokens = list(re.finditer(r"[a-z][a-z0-9_-]*", text))
    actions, positions = set(), set()
    for i, token in enumerate(tokens):
        word = token.group()
        matches = {action for action, (verbs, pattern) in _ACTIONS.items()
                   if (re.fullmatch(pattern, word) if past else word in verbs)}
        if not matches:
            continue
        previous = tokens[i - 1].group() if i else ""
        before_previous = tokens[i - 2].group() if i > 1 else ""
        boundary = bool(i and re.search(r"[.;:!?]", text[tokens[i - 1].end():token.start()]))
        predicate = i == 0 or boundary or previous in {"i", "we", "will", "ll", "shall", "please"}
        if past:
            predicate = predicate or previous in {"have", "has", "had", "was", "were", "is", "been", "just", "already", "now"}
        elif previous == "to" and before_previous in {"commit", "committed", "agree", "agreed", "need", "plan", "going", "promise", "promised"}:
            predicate = True
        if previous in {"and", "then", "also"}:
            # "send the checklist and release plan" contains a noun here.
            following = tokens[i + 1].group() if i + 1 < len(tokens) else ""
            predicate = past or word not in {"release", "post", "share", "open", "demo", "finish", "land", "ship"} or following in {
                "", "the", "a", "an", "it", "this", "that", "our", "my", "your"}
        if predicate:
            actions.update(matches)
            positions.add(i)
    # A word can be both the verb and a noun in one sentence. Retain a noun
    # occurrence for subject matching instead of globally deleting the word.
    subject = {token.group() for i, token in enumerate(tokens)
               if i not in positions and token.group() not in _STOP and len(token.group()) > 1}
    return actions, subject


def _turn_time(turn: dict) -> str | None:
    stamp = turn.get("occurred_at") or turn.get("date")
    if not stamp:
        match = re.match(r"\d{4}-\d{2}-\d{2}", str(turn.get("meeting", "")))
        stamp = match.group() if match else None
    if stamp:
        try:
            return _stamp(stamp)
        except TrackingError:
            pass
    return None


def suggest_evidence(items: list[dict], turns: list[dict], limit: int = 100,
                     *, deadline: float | None = None) -> list[dict]:
    """Conservative offline candidates; never records or changes any status.

    Requires later evidence from the owner, an action-specific past tense cue,
    and at least two shared subject words. Future/negated/quoted assertions are
    excluded. Compound promises are candidates for review, never auto-closed.
    This lexical pass intentionally trades recall for reviewable suggestions.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise TrackingError("suggestion limit must be a positive integer")
    def check_deadline():
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError("completion evidence scan timed out")
    check_deadline()
    prepared = []
    inverted: dict[str, set[int]] = {}
    for turn in turns:
        check_deadline()
        if not isinstance(turn, dict) or turn.get("quoted") or turn.get("is_quote"):
            continue
        text = str(turn.get("text", ""))
        if text.lstrip().startswith(">") or re.search(r"\b(?:not|never|didn't|haven't|hasn't|isn't|wasn't|won't|cannot|can't|will|would|going to)\b", text, re.I):
            continue
        stamp = _turn_time(turn)
        if not stamp:
            continue
        actions, words = _action_subject(text, past=True)
        if not actions:
            continue
        idx = len(prepared)
        prepared.append((turn, stamp, actions, words))
        for word in words:
            inverted.setdefault(word, set()).add(idx)
    result = []
    seen = set()
    for item in items:
        check_deadline()
        if item.get("status") in TERMINAL or not item.get("id"):
            continue
        text = str(item.get("text", ""))
        actions, subject = _action_subject(text)
        if not actions or len(subject) < 2:
            continue
        owner = str(item.get("owner") or item.get("speaker") or "").strip().lower()
        if not owner or owner.startswith("speaker_"):
            continue
        first = item.get("first_seen") or item.get("created_at")
        match = re.match(r"\d{4}-\d{2}-\d{2}", str(first or ""))
        if not match:
            continue
        candidates = set().union(*(inverted.get(w, set()) for w in subject))
        for idx in sorted(candidates):
            check_deadline()
            turn, stamp, completed, turn_words = prepared[idx]
            if str(turn.get("speaker", "")).strip().lower() != owner or stamp[:10] <= match.group():
                continue
            overlap = subject & turn_words
            if len(overlap) < 2 or len(overlap) / min(len(subject), len(turn_words) or 1) < .5 or not (actions & completed):
                continue
            # A compound obligation must be split or reviewed milestone by milestone.
            partial = not actions <= completed
            evidence = {"kind": "transcript", "ref": turn.get("ref") or f"{turn.get('meeting', '')}/{turn.get('file', turn.get('source', 'transcript.speakers.txt'))}",
                        "meeting": str(turn.get("meeting", "")), "quote": str(turn.get("text", "")),
                        "speaker": str(turn.get("speaker", "")), "confidence": .7}
            for key in ("line", "t_sec"):
                if turn.get(key) is not None:
                    evidence[key] = turn[key]
            event = {"item_id": str(item["id"]), "type": "status", "status": "in_progress" if partial else "delivered",
                     "occurred_at": stamp, "source": "imported", "evidence": evidence}
            try:
                event = validate_event(event)
            except TrackingError:
                continue
            # Repeated copies of a message should not create duplicate suggestions.
            fingerprint = (event["item_id"], stamp, evidence.get("t_sec"), evidence["speaker"], evidence["quote"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            event["review_required"] = True
            event["reason"] = ("Only some action milestones have completion cues; split/review the remaining promise."
                               if partial else "Later owner statement shares subject words and an action-specific completion cue; verify the whole deliverable.")
            result.append(event)
            if len(result) >= limit:
                return result
    return result


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="append explicitly reviewed events from a JSON array")
    imp.add_argument("workspace")
    imp.add_argument("events")
    show = sub.add_parser("show", help="overlay tracking history onto JSON items")
    show.add_argument("workspace")
    show.add_argument("items")
    show.add_argument("--as-of")
    priorities = sub.add_parser("priorities", help="read priority history")
    priorities.add_argument("workspace")
    priorities.add_argument("--group")
    priorities.add_argument("--as-of")
    setst = sub.add_parser("set-status", help="mark one AI-NNN / CM-NNN item (done = delivered)")
    setst.add_argument("workspace")
    setst.add_argument("item_id")
    setst.add_argument("status", help=f"one of {', '.join(sorted(STATUSES | set(STATUS_ALIASES)))}")
    setst.add_argument("--note", required=True, help="manual evidence: who marked it and why")
    setst.add_argument("--at", help="when it happened (ISO date or timestamp; default now)")
    suggest = sub.add_parser("suggest", help="generate unapproved evidence candidates")
    suggest.add_argument("items")
    suggest.add_argument("turns")
    suggest.add_argument("--limit", type=int, default=100)
    suggest.add_argument("--timeout", type=float, default=60, help="maximum scan seconds (default: 60)")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            data = _read_json(args.events)
            if isinstance(data, dict):
                data = data.get("events")
            if not isinstance(data, list):
                raise TrackingError("import file must contain an events array")
            result = record_events(args.workspace, data)
        elif args.command == "show":
            data = _read_json(args.items)
            result = reconcile_items(args.workspace, data.get("items", []) if isinstance(data, dict) else data, args.as_of)
        elif args.command == "set-status":
            result = set_status(args.workspace, args.item_id, args.status, args.note, args.at)
        elif args.command == "priorities":
            result = priority_view(args.workspace, args.group, args.as_of)
        else:
            if args.timeout <= 0 or not math.isfinite(args.timeout):
                raise TrackingError("timeout must be a positive finite number of seconds")
            items, turns = _read_json(args.items), _read_json(args.turns)
            result = suggest_evidence(items.get("items", []) if isinstance(items, dict) else items,
                                      turns.get("turns", []) if isinstance(turns, dict) else turns, args.limit,
                                      deadline=monotonic() + args.timeout)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (TrackingError, OSError, ValueError) as exc:
        parser.exit(1, f"tracking: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
