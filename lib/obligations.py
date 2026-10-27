"""Conservative, offline obligation classification for corpus views (#109).

This module enriches a view; it never rewrites the source corpus or asserts that
an assigned owner accepted a request. Headings explain what sort of item was
extracted. Attributed first-person turns or explicit human confirmations establish
acceptance. All original fields remain available to callers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
import re
from typing import Any


KINDS = (
    "explicit_commitment", "request", "standing_directive", "suggestion",
    "inferred_next_step",
)
_UNRESOLVED = re.compile(r"^(?:speaker[ _-]*\d+|unknown|unidentified|unassigned|someone|anyone|team|everyone)$", re.I)
_BRACKET = re.compile(r"^\s*\[([^\]]+)\]\s*")
_TIME = re.compile(r"(?:~?\d{1,2}:\d{2}(?::\d{2})?)")
_QUOTES = re.compile(r'"([^"\n]+)"|“([^”\n]+)”')
_ACCEPT = re.compile(
    r"(?:^|[.!?;]\s*|\b(?:yes|yeah|okay|ok|sure|agreed|and|then|so|but)[,.:!]?\s+)"
    r"(?:i\s+will\b(?!\s+(?:not|never)\b)|i['’]ll\b(?!\s+(?:not|never)\b)|"
    r"i\s+(?:agree|commit|promise)\s+to\b|i\s+accept\b|i['’]m\s+going\s+to\b|"
    r"i\s+am\s+going\s+to\b|i\s+can\s+take\s+(?:this|that|it)\b)", re.I,
)
_UNCERTAIN = re.compile(
    r"\b(?:if|unless|might|maybe|perhaps|possibly|could|would|not\s+sure|"
    r"i\s+(?:won't|will\s+not|cannot|can't)|i['’]m\s+not\s+going|"
    # A refusal can put an adverb between will and not: "I will definitely not".
    r"i(?:\s+will|['’]ll)\s+(?:\w+\s+){0,2}(?:not|never))\b", re.I,
)
_OPEN_REQUEST = re.compile(r"\b(?:can|could|would|will)\s+(?:someone|anyone|somebody|anybody)\b", re.I)
_REQUEST = re.compile(r"\b(?:can|could|would|will)\s+(?:you|someone|anyone|somebody|anybody)\b|\bplease\b", re.I)
_STANDING = re.compile(r"\b(?:always|every\s+(?:time|pull\s+request|pr|release)|never)\b", re.I)
_SUGGESTION = re.compile(r"\b(?:could|might|maybe|perhaps|suggest|recommend|should|consider)\b", re.I)
_ACTIVE = {"", "open", "ongoing", "in_progress", "partial", "blocked"}


def _dict(value: Any) -> dict:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping) or hasattr(value, "keys"):
        return dict(value)
    raise TypeError("an obligation must be a mapping or dataclass")


def _name(value: Any) -> str:
    return re.sub(r"[\s_-]+", " ", str(value or "").strip()).casefold()


def _known(value: Any) -> bool:
    return bool(_name(value)) and not _UNRESOLVED.fullmatch(str(value).strip())


def _same(left: Any, right: Any, aliases: Iterable[str]) -> bool:
    if not _known(left) or not _known(right):
        return False
    names = {_name(right), *(_name(a) for a in aliases)}
    return _name(left) in names


def _bracket(text: str) -> tuple[str, str | None, bool]:
    match = _BRACKET.match(text)
    if not match:
        return "", None, False
    label = match.group(1)
    if label.strip().casefold() == "inferred":
        return "", None, True
    stamp = _TIME.search(label)
    speaker = label[:stamp.start()].strip() if stamp else label.strip()
    return speaker, stamp.group(0) if stamp else None, False


def _accepts(text: str) -> bool:
    """Only an unconditional first-person assertion, not reported speech."""
    text = str(text or "").strip()
    if _UNCERTAIN.search(text):
        return False
    return bool(_ACCEPT.search(text))


def _origin(row: dict, source: str, text: str) -> tuple[str, str]:
    heading = str(row.get("type") or "").casefold()
    _, _, inferred = _bracket(text)
    if inferred or "inferred" in heading:
        return "inferred_next_step", "source marks this as inferred"
    if "directive" in heading or _STANDING.search(text):
        return "standing_directive", "standing instruction, not a one-time deliverable"
    if "ask" in heading or "request" in heading or _REQUEST.search(text):
        return "request", "request does not establish acceptance"
    if "commitment" in heading or source in {"commitments", "transcript"} or _ACCEPT.search(text):
        return "explicit_commitment", "commitment candidate requires attributed acceptance"
    if _SUGGESTION.search(text):
        return "suggestion", "tentative or suggested action"
    return "suggestion", "no explicit commitment or acceptance established"


def _candidates(row: dict, source: str, include_title_evidence: bool = True) -> list[dict]:
    candidates = []
    occurrences = row.get("occurrences", [])
    if not isinstance(occurrences, (list, tuple)):
        occurrences = []
    for raw in occurrences:
        if not (is_dataclass(raw) or isinstance(raw, Mapping)):
            continue
        occurrence = _dict(raw)
        text = str(occurrence.get("text") or "")
        if occurrence.get("negative") or occurrence.get("attribution_uncertain") or occurrence.get("is_quote"):
            continue
        if occurrence.get("source", "transcript") == "transcript":
            # Text in quotation marks belongs to quoted speech, not this turn's
            # speaker. Keep source metadata even when the accepted clause is short.
            own_text = _QUOTES.sub("", text)
            if _accepts(own_text):
                candidates.append({
                    **occurrence, "kind": "acceptance",
                    "speaker": occurrence.get("speaker") or occurrence.get("owner") or row.get("speaker"),
                    "quote": own_text.strip(),
                })
        else:
            candidates.extend(_summary_acceptance(text, occurrence))

    text = str(row.get("text") or "")
    if include_title_evidence and not row.get("negative") and not row.get("attribution_uncertain") and not row.get("is_quote"):
        candidates.extend(_summary_acceptance(text, row))
        if source in {"transcript", "commitments"} and not _BRACKET.match(text):
            own_text = _QUOTES.sub("", text)
            if _accepts(own_text):
                candidates.append({
                    "kind": "acceptance", "source": source,
                    "speaker": row.get("speaker") or row.get("owner"),
                    "meeting": row.get("meeting") or row.get("first_seen"),
                    "t_sec": row.get("t_sec"), "quote": own_text.strip(),
                })
    return candidates


def _summary_acceptance(text: str, metadata: dict) -> list[dict]:
    speaker, stamp, inferred = _bracket(text)
    if inferred or not speaker or "not verbatim" in text.casefold():
        return []
    found = []
    for match in _QUOTES.finditer(text):
        quote = match.group(1) or match.group(2)
        if _accepts(quote):
            found.append({
                "kind": "acceptance", "source": "action-items",
                "speaker": speaker, "meeting": metadata.get("meeting") or metadata.get("first_seen"),
                "t_str": stamp, "t_sec": metadata.get("t_sec"),
                "quote": quote, "evidence_quality": "summary_quote",
            })
    return found


def classify_item(item: Any, *, source: str | None = None, owner: str | None = None,
                  aliases: Iterable[str] = (), evidence: Iterable[dict] | None = None,
                  include_title_evidence: bool = True) -> dict:
    """Return additive classification fields; never mutate ``item``.

    ``evidence`` is scoped by the caller to this commitment. Human acceptance is
    explicit: ``{kind: 'user_confirmation', confirmed: True, speaker: <owner>,
    at: <timestamp>, note: <nonempty explanation>}``. An attributed acceptance
    turn requires ``kind: 'acceptance'``, ``speaker`` and ``quote``. Summary quotes
    remain labeled as such. Aliases must be supplied, never guessed from names.
    Historical callers set ``include_title_evidence=False`` because a corpus
    title can be rewritten later. Dated source occurrences and supplied evidence
    can still establish acceptance after the caller filters them to its cutoff.
    """
    row = _dict(item)
    source = source or str(row.get("source") or ("commitments" if "speaker" in row else "action-items"))
    text = str(row.get("text") or "")
    proposed = str(owner if owner is not None else row.get("owner") or row.get("speaker") or "").strip()
    bracket_speaker, _, _ = _bracket(text)
    requester = str(row.get("requester") or row.get("requested_by") or "").strip()
    origin, reason = _origin(row, source, text)
    if not requester and origin == "request" and _known(bracket_speaker):
        requester = bracket_speaker
    if _OPEN_REQUEST.search(text):
        proposed = ""
    if not _known(proposed):
        proposed = ""

    accepted = []
    unresolved = False
    all_candidates = _candidates(row, source, include_title_evidence=include_title_evidence)
    # Persisted acceptance fields may have been supplied by a human correction
    # or the tracking ledger. They go through the same validation as fresh input.
    supplied = evidence if evidence is not None else row.get("acceptance_evidence", [])
    supplied_list = ([dict(e) for e in supplied if isinstance(e, Mapping)]
                     if supplied is not None and not isinstance(supplied, (str, bytes, Mapping)) else [])
    all_candidates.extend(supplied_list)
    if origin == "inferred_next_step":
        # Generated text cannot be its own evidence. Only a separate source
        # or an explicit human confirmation can accept an inferred task.
        all_candidates = supplied_list
    for candidate in all_candidates:
        speaker = candidate.get("speaker") or candidate.get("accepted_owner") or candidate.get("owner")
        if not _known(speaker):
            unresolved = True
            continue
        human = (candidate.get("kind") == "user_confirmation" and candidate.get("confirmed") is True
                 and bool(candidate.get("at")) and bool(str(candidate.get("note") or "").strip()))
        spoken = candidate.get("kind") == "acceptance" and _accepts(str(candidate.get("quote") or ""))
        if not (human or spoken):
            continue
        if not proposed:
            # An open request can acquire an owner from a later explicit response.
            # A row with no proposed owner may also be a direct attributed promise.
            proposed = str(speaker)
        if _same(speaker, proposed, aliases):
            if candidate not in accepted:
                accepted.append(candidate)

    kind = "explicit_commitment" if accepted and origin != "standing_directive" else origin
    accepted_owner = proposed if accepted and kind == "explicit_commitment" else None
    if kind == "standing_directive":
        state = "standing"
    elif kind == "inferred_next_step":
        state = "inferred"
    elif accepted_owner:
        state = "accepted"
        reason = "attributed acceptance evidence" if not any(e.get("kind") == "user_confirmation" for e in accepted) else "explicit human confirmation"
    elif unresolved or kind == "explicit_commitment":
        state = "uncertain"
    else:
        state = "proposed" if proposed else "unassigned"
    actionable = bool(accepted_owner and kind == "explicit_commitment")
    return {
        "kind": kind, "origin_kind": origin, "requester": requester or None,
        "proposed_owner": proposed or None, "accepted_owner": accepted_owner,
        "acceptance_evidence": accepted, "obligation_status": state,
        "classification_reason": reason, "actionable": actionable,
        "overdue_eligible": actionable and str(row.get("status") or "").casefold() in _ACTIVE,
    }


def enrich_item(item: Any, **kwargs: Any) -> dict:
    """Copy a mapping/dataclass and add obligation metadata."""
    return {**_dict(item), **classify_item(item, **kwargs)}


def kind_counts(items: Iterable[Any]) -> dict[str, int]:
    """Counts include zero categories so callers can render a stable legend."""
    counts = Counter()
    for item in items:
        row = _dict(item)
        kind = row.get("kind") if row.get("kind") in KINDS else classify_item(row)["kind"]
        counts[kind] += 1
    return {kind: counts[kind] for kind in KINDS}


def filter_items(items: Iterable[Any], *, kind: str | None = None,
                 accepted_only: bool = False) -> list[dict]:
    """Filter enriched items. Validate kind rather than silently returning none."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"invalid obligation kind {kind!r}; choose one of {', '.join(KINDS)}")
    out = []
    for item in items:
        row = _dict(item)
        if row.get("kind") not in KINDS:
            row = enrich_item(row)
        if kind is not None and row["kind"] != kind:
            continue
        if accepted_only and not row.get("accepted_owner"):
            continue
        out.append(row)
    return out
