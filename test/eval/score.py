#!/usr/bin/env python3
"""
score.py: scores one action-items draft against a fixture's gold.json.

Pure functions, stdlib only, no model and no network. docs/eval.md has the
rules in prose; this file is the definition.

Parsing. A scored bullet is a line matching BULLET_RE, the shape
lib/action_items.py's render_bullet writes:

    - **<label>** [<Requester> HH:MM:SS] <title>. <rest>[ _(⚠ ...)_]

`[inferred]` bullets are counted separately and never scored. A bullet is
`flagged` when its body contains ⚠ (no quote, or a quote that is not verbatim).

Matching, per bullet in document order:
  cands = gold items with the same t whose ANY keyword (normed) is a substring
          of norm(body), whether or not they are matched yet.
  - an unmatched non-optional cand exists -> assign the first one: TP
  - else an unmatched optional cand exists -> assign the first one: ignored
  - else the bullet is a false positive, tagged (first rule that holds):
      distractor  its t is one of gold["distractors"]
      duplicate   its t has gold items and either cands is non-empty (every
                  keyword match is taken) or every gold item at t is taken
      wrong-item  its t has gold items but none matches by keyword
      unlabeled   anything else
Unmatched non-optional gold items are false negatives; unmatched optional
items are ignored.

Ratios (see ratios()): precision = TP/(TP+FP), and when TP+FP == 0 it is 1.0
if there is no scorable gold (TP+FN == 0) else 0.0; recall = TP/(TP+FN), 1.0
when TP+FN == 0; f1 is the harmonic mean, 0.0 when both are 0; flag_rate =
flagged/bullets, 0.0 with no bullets. Ratios are rounded to 4 decimal places.

Aggregation across fixtures is a micro average: aggregate() sums the counts
and computes the ratios from the sums.
"""

from __future__ import annotations

import re

BULLET_RE = re.compile(
    r"^- \*\*(?P<label>.+?)\*\* \[(?P<who>[^\]]*?) (?P<t>\d{2}:\d{2}:\d{2})\] (?P<body>.*)$")
INFERRED_LINE_RE = re.compile(r"^- (?:\*\*.+?\*\* )?\[inferred\] ")
FP_TAGS = ("distractor", "duplicate", "wrong-item", "unlabeled")
COUNT_KEYS = ("tp", "fp", "fn", "bullets", "flagged", "inferred", "model_calls")
RATIO_KEYS = ("precision", "recall", "f1", "flag_rate")
DP = 4


def norm(s: str) -> str:
    """Same as lib/action_items.py norm(): lowercase, whitespace collapsed,
    everything but [a-z0-9 ] stripped. Kept here so scoring has no lib import."""
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", s.lower())).strip()


def canon_t(t: str) -> str:
    """'0:00:05', '00:05' and '00:00:05' -> '00:00:05'."""
    parts = [int(x) for x in str(t).strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    total = h * 3600 + m * 60 + s
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def parse_draft(markdown: str) -> dict:
    """-> {"bullets": [{label, who, t, body, flagged}], "inferred": int}"""
    bullets: list[dict] = []
    inferred = 0
    for line in markdown.splitlines():
        line = line.rstrip()
        if INFERRED_LINE_RE.match(line):
            inferred += 1
            continue
        m = BULLET_RE.match(line)
        if m:
            body = m.group("body")
            bullets.append({"label": m.group("label"), "who": m.group("who"),
                            "t": m.group("t"), "body": body, "flagged": "⚠" in body})
    return {"bullets": bullets, "inferred": inferred}


def ratios(tp: int, fp: int, fn: int, bullets: int, flagged: int) -> dict:
    if tp + fp:
        precision = tp / (tp + fp)
    else:
        precision = 1.0 if tp + fn == 0 else 0.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    flag_rate = flagged / bullets if bullets else 0.0
    return {"precision": round(precision, DP), "recall": round(recall, DP),
            "f1": round(f1, DP), "flag_rate": round(flag_rate, DP)}


def score(markdown: str, gold: dict, *, model_calls: int = 0) -> dict:
    """One draft against one gold.json -> counts, ratios, fp_tags and the
    per-bullet / per-item detail (under "detail", not stored in results)."""
    parsed = parse_draft(markdown)
    items = [dict(it, _t=canon_t(it["t"]), _kw=[norm(k) for k in it.get("keywords") or [] if norm(k)])
             for it in gold.get("items") or []]
    by_t: dict[str, list[dict]] = {}
    for it in items:
        by_t.setdefault(it["_t"], []).append(it)
    distractor_ts = {canon_t(d["t"]) for d in gold.get("distractors") or []}
    matched: set[str] = set()
    tp = fp = 0
    fp_tags = {tag: 0 for tag in FP_TAGS}
    detail: list[dict] = []

    for b in parsed["bullets"]:
        t = canon_t(b["t"])
        nbody = norm(b["body"])
        at_t = by_t.get(t, [])
        cands = [it for it in at_t if any(k in nbody for k in it["_kw"])]
        free = [it for it in cands if it["id"] not in matched]
        pick = next((it for it in free if not it.get("optional")), None) \
            or next((it for it in free if it.get("optional")), None)
        if pick is not None:
            matched.add(pick["id"])
            if pick.get("optional"):
                detail.append({"t": t, "result": "optional", "gold": pick["id"], "body": b["body"]})
            else:
                tp += 1
                detail.append({"t": t, "result": "tp", "gold": pick["id"], "body": b["body"]})
            continue
        fp += 1
        if t in distractor_ts:
            tag = "distractor"
        elif at_t and (cands or all(it["id"] in matched for it in at_t)):
            tag = "duplicate"
        elif at_t:
            tag = "wrong-item"
        else:
            tag = "unlabeled"
        fp_tags[tag] += 1
        detail.append({"t": t, "result": "fp", "tag": tag, "body": b["body"]})

    missed = [it for it in items if not it.get("optional") and it["id"] not in matched]
    fn = len(missed)
    bullets = len(parsed["bullets"])
    flagged = sum(1 for b in parsed["bullets"] if b["flagged"])
    out = {"tp": tp, "fp": fp, "fn": fn, "bullets": bullets, "flagged": flagged,
           "inferred": parsed["inferred"], "model_calls": int(model_calls)}
    out.update(ratios(tp, fp, fn, bullets, flagged))
    out["fp_tags"] = fp_tags
    out["detail"] = {"bullets": detail,
                     "missed": [{"id": it["id"], "t": it["_t"], "desc": it.get("desc", "")}
                                for it in missed]}
    return out


def public(result: dict) -> dict:
    """The stored shape: counts, ratios and fp_tags, without the detail."""
    return {k: result[k] for k in COUNT_KEYS + RATIO_KEYS + ("fp_tags",)}


def aggregate(results: list[dict]) -> dict:
    """Micro average: sum every count and fp tag, then compute the ratios."""
    tot = {k: sum(int(r[k]) for r in results) for k in COUNT_KEYS}
    tot.update(ratios(tot["tp"], tot["fp"], tot["fn"], tot["bullets"], tot["flagged"]))
    tot["fp_tags"] = {tag: sum(int(r["fp_tags"].get(tag, 0)) for r in results) for tag in FP_TAGS}
    return tot
