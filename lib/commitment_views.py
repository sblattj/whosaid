"""Read-only commitment enrichment shared by graph and ranked views.

The derived corpora remain the extraction authority. The separate tracking
ledger is the evidence authority and survives index rebuilds.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import obligations
import tracking
import wsconfig


def _name(value: str) -> str:
    return re.sub(r"[ _-]+", " ", str(value or "")).strip().casefold()


def identity_aliases(cfg: dict, raw: dict[str, dict]) -> dict[str, list[str]]:
    """Explicit aliases plus first names unique in the known identity roster."""
    labels = {str(r.get("owner") or r.get("speaker") or "") for r in raw.values()}
    labels.update(str(v) for v in cfg.get("teams", {}).get("names", {}).values())
    for members in cfg.get("groups", {}).values():
        labels.update(members)
    labels.add(cfg.get("workspace", {}).get("owner", ""))
    full = {_name(label) for label in labels if len(_name(label).split()) > 1}
    firsts: dict[str, set[str]] = {}
    for label in full:
        firsts.setdefault(label.split()[0], set()).add(label)
    result = {label: [label.split()[0]] for label in full
              if len(firsts[label.split()[0]]) == 1}
    workspace = cfg.get("workspace", {})
    aliases = workspace.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [a.strip() for a in aliases.split(",")]
    owner = _name(workspace.get("owner", ""))
    if owner:
        # Without an owner the aliases belong to nobody, never to unowned items.
        result.setdefault(owner, []).extend(aliases)
    return result


def historical_row(row: dict, as_of: str | None) -> dict:
    """Trim dated source occurrences before classification and repeat scoring."""
    if not as_of:
        return row
    instant = tracking._instant(as_of, end_of_day=True)
    cutoff = instant.date().isoformat()
    def eligible(value: str) -> bool:
        match = re.match(r"\d{4}-\d{2}-\d{2}", str(value or ""))
        return not match or match.group(0) <= cutoff
    result = dict(row)
    if isinstance(row.get("occurrences"), list):
        def visible(occurrence):
            stamp = occurrence.get("occurred_at") or occurrence.get("date")
            if stamp:
                return tracking._instant(stamp) <= instant
            return eligible(occurrence.get("meeting"))
        result["occurrences"] = [o for o in row["occurrences"] if visible(o)]
    if isinstance(row.get("acceptance_evidence"), list):
        result["acceptance_evidence"] = [e for e in row["acceptance_evidence"]
            if e.get("at") and tracking._instant(e["at"]) <= instant]
    if "meetings" in row:
        result["meetings"] = type(row["meetings"])(m for m in row["meetings"] if eligible(m))
        if result["meetings"]:
            result["last_seen"] = max(result["meetings"])
    return result


def corpus_rows(ws: Path) -> dict[str, dict]:
    rows = {}
    for name, source in (("_action-items.json", "action-items"),
                         ("_commitments.json", "commitments")):
        path = Path(ws) / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for item in data.get("items", []):
            if isinstance(item, dict) and item.get("id"):
                rows[item["id"]] = {**item, "source": source}
    return rows


def source_pointer_pairs(raw: dict[str, dict], as_of: str | None = None) -> dict[str, str]:
    """Unique CM-to-AI equivalence from the same action-items bullet.

    A shared meeting alone or transcript line is insufficient. Several CM
    clauses from one bullet, several AI rows at that location, or conflicting
    pairings across meetings are ambiguous and deliberately remain unjoined.
    """
    action_at: dict[tuple[str, int], set[str]] = {}
    commitment_at: dict[tuple[str, int], set[str]] = {}
    for ident, row in raw.items():
        source = row.get("source")
        if source not in {"action-items", "commitments"}:
            continue
        occurrences = historical_row(row, as_of).get("occurrences", [])
        if not isinstance(occurrences, list):
            continue
        for occurrence in occurrences:
            origin = str(occurrence.get("source", "action-items" if source == "action-items" else "")).replace("_", "-")
            line, meeting = occurrence.get("line"), occurrence.get("meeting")
            if origin != "action-items" or not isinstance(line, int) or isinstance(line, bool) or line < 1 or not meeting:
                continue
            target = action_at if source == "action-items" else commitment_at
            target.setdefault((meeting, line), set()).add(ident)
    candidates: dict[str, set[str]] = {}
    reverse: dict[str, set[str]] = {}
    ambiguous: set[str] = set()
    for pointer in action_at.keys() & commitment_at.keys():
        ais, cms = action_at[pointer], commitment_at[pointer]
        if len(ais) != 1 or len(cms) != 1:
            ambiguous.update(ais | cms)
            continue
        ai, cm = next(iter(ais)), next(iter(cms))
        candidates.setdefault(cm, set()).add(ai)
        reverse.setdefault(ai, set()).add(cm)
    return {cm: next(iter(ais)) for cm, ais in candidates.items()
            if cm not in ambiguous and len(ais) == 1 and next(iter(ais)) not in ambiguous
            and len(reverse[next(iter(ais))]) == 1}


def enrich_rows(ws: Path, rows: list[dict], as_of: str | None = None, identities=None) -> list[dict]:
    """Join raw attribution before classifying, then apply evidence history."""
    raw = corpus_rows(ws)
    # Build equivalence before selecting a view so a single AI lookup can see
    # evidence curated under its corresponding CM ID, and vice versa.
    links: dict[str, set[str]] = {}
    for ident, original in raw.items():
        refs = original.get("ai_refs", [])
        if isinstance(refs, str):
            refs = [r.strip() for r in refs.split(",") if r.strip()]
        for other in [*refs, *original.get("also", [])]:
            links.setdefault(ident, set()).add(other)
            links.setdefault(other, set()).add(ident)
    pointer_pairs = source_pointer_pairs(raw, as_of)
    for cm, ai in pointer_pairs.items():
        links.setdefault(cm, set()).add(ai)
        links.setdefault(ai, set()).add(cm)
    aliases = identity_aliases(wsconfig.load_config(Path(ws)), raw)
    if identities is None:
        from message_identity import MessageIndex
        identities = MessageIndex(ws)
    enriched = []
    for row in rows:
        original = raw.get(row.get("id"), {})
        linked, pending = set(), [row.get("id", "")]
        while pending:
            ident = pending.pop()
            if ident not in linked:
                linked.add(ident)
                pending.extend(links.get(ident, set()) - linked)
        # Keep display folding (`also`) separate from persistent equivalence.
        equivalent = (linked - {row.get("id", "")}) | set(row.get("also", []))
        row = {**row, "tracking_aliases": sorted(equivalent)}
        if as_of and str(row.get("meeting", ""))[:10] > tracking._instant(as_of, end_of_day=True).date().isoformat():
            continue
        row = historical_row(row, as_of)
        evidence_row = {**original, **row}
        pointer_ai = raw.get(pointer_pairs.get(row.get("id"), ""), {})
        source_occurrences = historical_row(original, as_of).get("occurrences")
        # Rolled-up corpora may store a count here; only a list of sightings has sources.
        source_occurrences = ([o for o in source_occurrences if isinstance(o, dict)]
                              if isinstance(source_occurrences, list) else [])
        summary_only = bool(source_occurrences) and all(
            str(o.get("source", "")).replace("_", "-") == "action-items" for o in source_occurrences)
        if summary_only:
            # Summary provenance does not depend on finding a unique AI twin.
            # The CM title must not become a first-person transcript assertion
            # merely because an AI pointer is absent or ambiguous.
            evidence_row["source"] = "action-items"
        if pointer_ai and summary_only:
            # A CM extracted from a summary can lose the heading and [inferred]
            # marker. Recover that origin from its uniquely corresponding AI;
            # do not treat the extracted summary as a new spoken promise.
            origin = obligations.classify_item(pointer_ai)["origin_kind"]
            evidence_row["type"] = {"inferred_next_step": "inferred", "standing_directive": "directive",
                                    "request": "request", "explicit_commitment": "commitment"}.get(
                                        origin, pointer_ai.get("type", ""))
        # Graph/worklist occurrence counts must not hide raw source evidence.
        if isinstance(original.get("occurrences"), list):
            evidence_row["occurrences"] = [{**o, "source": o.get("source", original["source"])}
                                            for o in original["occurrences"]]
        evidence_row = historical_row(evidence_row, as_of)
        if as_of and isinstance(evidence_row.get("occurrences"), list):
            visible_occurrences = evidence_row["occurrences"]
            meetings = sorted({o["meeting"] for o in visible_occurrences if o.get("meeting")})
            if meetings:
                row["first_seen"], row["last_seen"] = meetings[0], meetings[-1]
            if isinstance(row.get("occurrences"), int):
                row["occurrences"] = len(visible_occurrences)
            if "span" in row:
                first, last = row.get("first_seen", ""), row.get("last_seen", "")
                row["span"] = first if first == last or not last else f"{first} → {last}"
        resolved, occurrences = [], []
        for occurrence in evidence_row.get("occurrences", []) if isinstance(evidence_row.get("occurrences"), list) else []:
            occurrence = dict(occurrence)
            if hasattr(identities, "resolve_occurrence"):
                message = identities.resolve_occurrence(occurrence, text=evidence_row.get("text", ""))
            else:
                message = identities.resolve(occurrence.get("meeting", ""), t_sec=occurrence.get("t_sec"),
                                             text=occurrence.get("text", evidence_row.get("text", "")))
            if message:
                uncertain = message["attribution_uncertain"] or not message["assertion_text"]
                if occurrence.get("source") == "transcript" and occurrence.get("text") and message["quoted"]:
                    from message_identity import normalized
                    uncertain = uncertain or normalized(occurrence["text"]) not in normalized(message["assertion_text"])
                resolved.append({**message, "attribution_uncertain": uncertain})
                occurrence.update({"attribution_uncertain": uncertain, "message_id": message["message_id"]})
            occurrences.append(occurrence)
        evidence_row["occurrences"] = occurrences
        if resolved and all(m["attribution_uncertain"] or not m["assertion_text"] for m in resolved):
            evidence_row["attribution_uncertain"] = True
        who = evidence_row.get("owner") or evidence_row.get("speaker") or ""
        # A representative corpus title is current state. It may have changed
        # after the cutoff, so only the filtered source history (or dated human
        # confirmation) establishes historical acceptance.
        classified = obligations.classify_item(evidence_row, aliases=aliases.get(_name(who), []),
                                               include_title_evidence=as_of is None)
        enriched.append({**row, **classified,
                         "first_seen": row.get("first_seen") or original.get("first_seen", ""),
                         "ai_refs": row.get("ai_refs") or original.get("ai_refs", []),
                         "message_provenance": [{"message_id": m["message_id"], "provenance": m["provenance"],
                                                 "attribution_uncertain": m["attribution_uncertain"]} for m in resolved]})
    return tracking.reconcile_items(Path(ws), enriched, as_of=as_of)


def filter_rows(rows: list[dict], kind: str | None = None,
                status: str | None = None, accepted_only: bool = False) -> list[dict]:
    if kind:
        if kind not in obligations.KINDS:
            raise ValueError(f"unknown obligation kind: {kind}")
        rows = [r for r in rows if r.get("kind") == kind]
    if status:
        rows = [r for r in rows if status.lower() in str(r.get("status", "")).lower()]
    if accepted_only:
        rows = [r for r in rows if r.get("accepted_owner")]
    return rows
