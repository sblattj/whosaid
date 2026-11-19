#!/usr/bin/env python3
"""
live_items.py: an ended live explainer as rows (`whosaid live export-items`, GitHub
issue #227).

  export-items [STEM] [--since STEM] [--format json|csv]
      Read-only. Every ask, question, suggestion, decision and next step of an ended
      call as one row: a stable id (stem, kind and a hash of the normalized text), the
      section id and time window, the asker, the owner when the text names one, the
      verbatim transcript line it was matched to (clock and speaker) and every source
      path it was merged from (NNN-<slug>.json:asks[i], recap.json:owes[i],
      <stem>.ai.json:nextSteps[i]). STEM alone: that call (refused while it is still
      live). --since S alone: every ended call after S (stems are start times, so they
      sort); both: the calls in (S, STEM]. Calls whose .jsonl meta says ended: false
      are skipped and listed.
  mark-item STEM ID --status open|done [--note N]
      The one writer: sets a row's status in <stem>.explain/items.status.json, the
      overlay export-items merges into its rows (the served page's checkboxes write
      the same file).

Sources: only the top-level arrays of each section (asks, for_you, questions,
suggested), recap.json (decisions, questions, owes) and the AI sidecar's nextSteps;
never revisions[], and an item a correction marks as the mis-heard "before" is
dropped, so a retracted item never resurfaces. low_confidence is not an item.

Dedupe: within one call, items of the same family (ask and next step are one family:
an owe or next step that repeats an ask is that ask) whose content words overlap
(Jaccard >= 0.6, or the normalized texts are >= 0.85 alike) are one row. The row's
kind, text and id come from its first source in a fixed order (sections by id, then
the recap, then the AI next steps), so a recap or next step arriving later never
renames a section's row; it only adds a source.

Recap fallback: an ended call with no recap gets one derived locally (no model call)
from the sections' top-level items plus the agreed decisions of the local decisions
ledger (lib/decisions.py); live_explain.ensure_recap writes it (derived: true) and
export-items computes the same thing in memory without writing. Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import io
import json
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live  # noqa: E402
import live_explain as lx  # noqa: E402

KINDS = ("ask", "question", "suggestion", "decision", "next step")
FAMILY = {"ask": "todo", "next step": "todo", "question": "question", "suggestion": "suggestion",
          "decision": "decision"}
STATUS_FILE = "items.status.json"
STATUSES = ("open", "done")
CSV_COLUMNS = ["id", "stem", "kind", "text", "section", "from", "to", "window", "asker", "owner", "status",
               "quote_t", "quote_clock", "quote_speaker", "quote_text", "sources"]
JACCARD_MIN = 0.6
RATIO_MIN = 0.85
QUOTE_MIN = 0.4          # share of the item's content words a transcript line must carry
QUOTE_PAD = 30.0         # seconds around a section's window searched first
AI_PAD = 90.0            # seconds around an AI next step's t searched first
STOP = frozenset("""a an the and or but nor to of in on at by for with from into onto over under about after
before up down out off so just please let's lets sure yes yeah ok okay also then than as if is are was were be
been being am do does did done can could would will shall should may might must you your yours me my mine i
i'll i'm i've i'd we we'll we're us our it it's its this that these those there here what who whom whose how
when why which where any some all more most very too not no have has had get got go going gonna want wants
like one thing things really maybe let him her his she he they them their""".split())
_STATUS_LOCK = threading.Lock()


class ItemsError(ValueError):
    """Bad input: an unknown stem, row id or status."""


# ---------------------------------------------------------------- text

def norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _stem_word(w: str) -> str:
    if len(w) > 4:
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                return w[: -len(suf)]
    return w


def words(text: str) -> set[str]:
    """Content words, lightly stemmed (expires / expiring -> expir)."""
    out = set()
    for w in re.findall(r"[a-z0-9']+", str(text or "").lower()):
        w = w.strip("'")
        if len(w) >= 2 and w not in STOP:
            out.add(_stem_word(w.replace("'", "")))
    return out


def similar(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    wa, wb = words(a), words(b)
    if wa and wb and len(wa & wb) / len(wa | wb) >= JACCARD_MIN:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= RATIO_MIN


def row_id(stem: str, kind: str, text: str) -> str:
    return f"{stem}-{kind.replace(' ', '-')}-{hashlib.sha1(norm(text).encode()).hexdigest()[:12]}"


# ---------------------------------------------------------------- reading one call

def _load(p: Path) -> dict:
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _paths(d: Path) -> tuple[str, Path, Path]:
    stem = lx.stem_of(d)
    return stem, d.with_name(stem + ".jsonl"), d.with_name(stem + ".ai.json")


def _transcript(jl: Path) -> tuple[dict, list[dict]]:
    if not jl.is_file():
        return {}, []
    try:
        meta, entries = live.read_entries(jl)
    except (OSError, ValueError):
        return {}, []
    return meta, sorted((e for e in entries if str(e.get("text") or "").strip() and "t" in e),
                        key=lambda e: float(e["t"]))


def _retracted(sec: dict, text: str) -> bool:
    """A top-level item a correction names as the mis-heard 'before' (and not the 'after')."""
    for c in sec.get("corrections") or []:
        if similar(text, c.get("before") or "") and not similar(text, c.get("after") or ""):
            return True
    return False


def _cand(kind, text, src, who="", sec=None, t=None, origin="section"):
    return {"kind": kind, "text": lx._text(text), "src": src, "who": lx._text(who), "origin": origin,
            "section": sec["id"] if sec else None, "from": sec.get("from") if sec else None,
            "to": sec.get("to") if sec else None, "t": t}


def section_candidates(d: Path) -> list[dict]:
    """Items from the sections' top-level arrays, in section-id order (never revisions[])."""
    out = []
    for sid, jp in sorted(lx.section_files(d).items()):
        sec = _load(jp)
        if not sec:
            continue
        sec.setdefault("id", sid)
        name = jp.name
        items = []
        for i, a in enumerate(sec.get("asks") or []):
            if isinstance(a, dict):
                items.append(_cand("ask", a.get("text"), f"{name}:asks[{i}]", a.get("who"), sec))
        for i, f in enumerate(sec.get("for_you") or []):
            if isinstance(f, dict):
                items.append(_cand("ask", f.get("ask"), f"{name}:for_you[{i}]", f.get("who"), sec))
        for i, q in enumerate(sec.get("questions") or []):
            items.append(_cand("question", q, f"{name}:questions[{i}]", "", sec))
        for i, s in enumerate(sec.get("suggested") or []):
            items.append(_cand("suggestion", s, f"{name}:suggested[{i}]", "", sec))
        out += [c for c in items if c["text"] and not _retracted(sec, c["text"])]
    return out


def _local_decisions(jl: Path) -> list[str]:
    """Agreed decisions of the local decisions ledger (lib/decisions.py), no model call."""
    if not jl.is_file():
        return []
    try:
        import decisions
        _, res = decisions.live_ledger(jl, "local")
    except Exception as err:  # noqa: BLE001  a ledger failure only costs the decision rows
        lx.log(f"decisions ledger for {jl.name}: {err}")
        return []
    out = []
    for row in res.get("decisions") or []:
        text = lx._text(row.get("text"))
        if row.get("status") == "agreed" and text and not any(similar(text, x) for x in out):
            out.append(text)
    return out


def derive_recap(d: Path) -> dict | None:
    """The fallback recap of a call: section questions, asks and for-you asks (owed by me),
    agreed local-ledger decisions. None when there is nothing to recap. Writes nothing."""
    _, jl, _ = _paths(d)
    questions, owes = [], []
    for c in section_candidates(d):
        if c["kind"] == "question" and not any(similar(c["text"], q) for q in questions):
            questions.append(c["text"])
        elif c["kind"] == "ask" and not any(similar(c["text"], o["text"]) for o in owes):
            # asks are "asked of the user or the user owes" (ENGINE_SYSTEM): the user owes it
            owes.append({"who": "me", "text": c["text"]})
    decisions = _local_decisions(jl)
    if not (decisions or questions or owes):
        return None
    return {"title": "Recap", "decisions": decisions, "questions": questions, "owes": owes, "derived": True}


def same_recap(a: dict | None, b: dict | None) -> bool:
    keys = ("decisions", "questions", "owes")
    return bool(a) and bool(b) and all((a.get(k) or []) == (b.get(k) or []) for k in keys)


def recap_for_rows(d: Path, ended: bool) -> tuple[dict | None, str | None]:
    """(recap, file label) to read: a producer's recap.json as is; a derived one on disk
    when it is still what derive_recap gives; else (ended) the derivation in memory,
    with no file label because nothing is on disk."""
    p = d / "recap.json"
    rc = _load(p) if p.is_file() else None
    if rc and not rc.get("derived"):
        return rc, "recap.json"
    if not ended:
        return None, None
    der = derive_recap(d)
    if rc and same_recap(rc, der):
        return rc, "recap.json"
    return der, None


def recap_candidates(rc: dict | None, label: str | None) -> list[dict]:
    if not rc:
        return []
    out = []
    if label:
        for i, x in enumerate(rc.get("decisions") or []):
            out.append(_cand("decision", x, f"{label}:decisions[{i}]", origin="recap"))
        for i, x in enumerate(rc.get("questions") or []):
            out.append(_cand("question", x, f"{label}:questions[{i}]", origin="recap"))
        for i, o in enumerate(rc.get("owes") or []):
            if isinstance(o, dict):
                out.append(_cand("next step", o.get("text"), f"{label}:owes[{i}]", o.get("who"), origin="owes"))
    else:  # derived in memory: its questions and owes ARE the section items; only decisions are new
        for i, x in enumerate(rc.get("decisions") or []):
            out.append(_cand("decision", x, f"derived-recap:decisions[{i}]", origin="recap"))
    return [c for c in out if c["text"]]


def ai_candidates(ai: Path) -> list[dict]:
    data = _load(ai) if ai.is_file() else {}
    out = []
    for i, s in enumerate(data.get("nextSteps") or []):
        if not isinstance(s, dict) or not lx._text(s.get("text")):
            continue
        try:
            t = float(s.get("t")) if s.get("t") is not None else None
        except (TypeError, ValueError):
            t = None
        out.append(_cand("next step", s.get("text"), f"{ai.name}:nextSteps[{i}]",
                         "me" if s.get("who") == "me" else "", t=t, origin="ai"))
    return out


# ---------------------------------------------------------------- matching

def _full_clock(started, t: float) -> str:
    if started:
        return time.strftime("%H:%M:%S", time.localtime(float(started) + float(t)))
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def match_line(text: str, entries: list[dict], started, lo=None, hi=None) -> dict | None:
    """The transcript line that best carries the item's content words (verbatim), searched
    inside [lo, hi] first, then the whole call; ties go to the earliest line. None below
    QUOTE_MIN, never a wrong guess."""
    want = words(text)
    if not want:
        return None
    need = 1 if len(want) <= 2 else 2

    def best(pool):
        top = None
        for e in pool:
            shared = len(want & words(e.get("text")))
            score = shared / len(want)
            if shared >= need and score >= QUOTE_MIN and (top is None or score > top[0]):
                top = (score, e)
        return top[1] if top else None
    e = None
    if lo is not None and hi is not None:
        e = best([x for x in entries if lo <= float(x["t"]) <= hi])
    e = e or best(entries)
    if e is None:
        return None
    return {"t": float(e["t"]), "clock": e.get("clock") or _full_clock(started, e["t"]),
            "speaker": live.speaker(e), "text": e["text"]}


_FIRST_PERSON = re.compile(r"^\W*(i|i'll|i will|i'm going to|i am going to|i can|i owe|me)\b", re.I)
_YOU = re.compile(r"\b(you|your)\b", re.I)


def owner_of(members: list[dict], people: set[str]) -> str:
    """Who owns the item when the text (or the owe line) names one, else ''."""
    for m in members:
        if m["origin"] == "owes" and m["who"]:
            return m["who"]
    text = members[0]["text"]
    for name in sorted(people, key=len, reverse=True):
        if re.match(rf"^\W*{re.escape(name)}\b\s*(?::|(?:will|to|owes|should|needs to|is going to|'ll|can|must)\b)",
                    text, re.I):
            return name
    for m in members:
        if m["kind"] == "ask" and m["origin"] == "section" and _YOU.search(m["text"]):
            return "me"  # an ask is asked of the user ('can you ...')
        if _FIRST_PERSON.match(m["text"]) and m["who"]:
            return m["who"]
    for m in members:
        if m["origin"] == "ai" and m["who"] == "me":
            return "me"
    return ""


# ---------------------------------------------------------------- rows

def load_status(d: Path) -> dict:
    data = _load(d / STATUS_FILE)
    items = data.get("items")
    return items if isinstance(items, dict) else {}


def rows_for(d: Path, ended: bool | None = None) -> list[dict]:
    """Every item row of one call (merged, matched, with the overlay status)."""
    stem, jl, ai = _paths(d)
    meta, entries = _transcript(jl)
    if ended is None:
        ended = bool(meta.get("ended"))
    started = meta.get("started")
    sections = {}
    for sid, jp in lx.section_files(d).items():
        s = _load(jp)
        sections[sid] = (s.get("from"), s.get("to"))
    rc, label = recap_for_rows(d, ended)
    cands = section_candidates(d) + recap_candidates(rc, label) + ai_candidates(ai)

    groups: list[list[dict]] = []
    for c in cands:
        for g in groups:
            if FAMILY[g[0]["kind"]] == FAMILY[c["kind"]] and any(similar(m["text"], c["text"]) for m in g):
                g.append(c)
                break
        else:
            groups.append([c])

    people = {lx._text(e.get("name")) for e in entries if e.get("name")}
    people |= {c["who"] for c in cands if c["who"]}
    people -= {"", "me", "them"}
    status = load_status(d)
    rows = []
    for g in groups:
        first = g[0]
        kind, text = first["kind"], first["text"]
        placed = next((m for m in g if m["section"]), None)
        quote = None
        if kind != "suggestion":  # a suggestion is the explainer's own idea, never said on the call
            if placed and placed["from"] is not None and placed["to"] is not None:
                quote = match_line(text, entries, started, float(placed["from"]) - QUOTE_PAD,
                                   float(placed["to"]) + QUOTE_PAD)
            else:
                at = next((m["t"] for m in g if m["t"] is not None), None)
                quote = match_line(text, entries, started, *((at - AI_PAD, at + AI_PAD) if at is not None
                                                             else (None, None)))
        section, t0, t1 = (placed["section"], placed["from"], placed["to"]) if placed else (None, None, None)
        if not placed:
            at = quote["t"] if quote else next((m["t"] for m in g if m["t"] is not None), None)
            hit = next((sid for sid, (a, b) in sorted(sections.items())
                        if at is not None and a is not None and b is not None and a <= at <= b), None)
            if hit:
                section, (t0, t1) = hit, sections[hit]
            else:
                section = "recap" if first["origin"] in ("recap", "owes") else "ai"
                t0 = t1 = at
        if t0 is not None and t1 is not None and t0 != t1:
            window = f"{lx.clock(started, t0)}–{lx.clock(started, t1)}"
        else:
            window = lx.clock(started, t0) if t0 is not None else ""
        asker = ""
        if kind in ("ask", "question"):
            asker = next((m["who"] for m in g if m["who"] and m["origin"] == "section"), "") or \
                (quote["speaker"] if quote else "")
        rid = row_id(stem, kind, text)
        st = status.get(rid) if isinstance(status.get(rid), dict) else {}
        rows.append({"id": rid, "stem": stem, "kind": kind, "text": text, "section": section,
                     "from": t0, "to": t1, "window": window, "asker": asker, "owner": owner_of(g, people),
                     "quote": quote, "sources": [m["src"] for m in g],
                     "status": st.get("status") if st.get("status") in STATUSES else "open"})
    return rows


def known_stems(into: Path) -> list[str]:
    ld = Path(into).expanduser() / live.LIVE_DIR
    if not ld.is_dir():
        return []
    out = set()
    for p in ld.iterdir():
        for suf in (".jsonl", lx.SUFFIX, ".ai.json"):
            if p.name.endswith(suf) and re.fullmatch(r"[\w.-]+", p.name[: -len(suf)]):
                out.add(p.name[: -len(suf)])
    return sorted(out)


def _clean_stem(s: str) -> str:
    return lx.stem_of(lx.store_dir(Path("."), s))


def ended_reason(into: Path, stem: str) -> str | None:
    """None when the call has ended, else why it is skipped."""
    d = lx.store_dir(into, stem)
    jl = d.with_name(stem + ".jsonl")
    if jl.is_file():
        meta, _ = _transcript(jl)
        return None if meta.get("ended") else "not ended"
    return None if (d / "recap.json").is_file() else "no transcript"


def export(into: Path, stem: str | None = None, since: str | None = None) -> dict:
    """Rows of one ended call (stem), every ended call after `since`, or the calls in
    (since, stem]. Read-only."""
    into = Path(into).expanduser()
    if not stem and not since:
        return {"ok": False, "error": "give a stem or --since",
                "hint": "a stem is the session's start time, e.g. 20261116T100000 (see `whosaid live status`)"}
    try:
        stem = _clean_stem(stem) if stem else None
        since = _clean_stem(since) if since else None
    except lx.ExplainError as err:
        return {"ok": False, "error": str(err), "hint": "a stem looks like 20261116T100000"}
    have = known_stems(into)
    if stem and not since:
        if stem not in have:
            return {"ok": False, "error": f"no live session {stem!r} under {into / live.LIVE_DIR}",
                    "hint": "pass --into the recordings folder, or a stem from `whosaid live status`"}
        why = ended_reason(into, stem)
        if why:
            return {"ok": False, "error": f"{stem}: {why} (its .jsonl meta says ended: false)" if why == "not ended"
                    else f"{stem}: {why}", "hint": "export-items reads a call once it has ended"}
        picked = [stem]
    else:
        picked = [s for s in have if s > since and (stem is None or s <= stem)]
    stems, skipped, rows = [], [], []
    for s in picked:
        why = ended_reason(into, s)
        if why:
            skipped.append({"stem": s, "reason": why})
            continue
        stems.append(s)
        rows += rows_for(lx.store_dir(into, s), ended=True)
    return {"ok": True, "stems": stems, "skipped": skipped, "count": len(rows), "rows": rows}


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        q = r.get("quote") or {}
        w.writerow({**{k: r.get(k) for k in CSV_COLUMNS if k in r},
                    "from": "" if r.get("from") is None else r["from"],
                    "to": "" if r.get("to") is None else r["to"],
                    "quote_t": q.get("t", ""), "quote_clock": q.get("clock", ""),
                    "quote_speaker": q.get("speaker", ""), "quote_text": q.get("text", ""),
                    "sources": " | ".join(r.get("sources") or [])})
    return buf.getvalue()


# ---------------------------------------------------------------- the overlay (the one writer)

def set_status(d: Path, rid: str, status: str, note: str = "", by: str = "cli") -> dict:
    """Mark one row open or done in <stem>.explain/items.status.json (keyed by row id)."""
    if status not in STATUSES:
        return {"ok": False, "error": f"status must be one of {', '.join(STATUSES)}, not {status!r}"}
    with _STATUS_LOCK:
        ids = {r["id"] for r in rows_for(d)}
        if rid not in ids:
            return {"ok": False, "error": f"no item {rid!r} in {lx.stem_of(d)}",
                    "hint": "take an id from `whosaid live export-items`"}
        p = d / STATUS_FILE
        data = _load(p)
        items = data.get("items") if isinstance(data.get("items"), dict) else {}
        items[rid] = {"status": status, "updated": time.time(), "by": lx._text(by)[:40],
                      "note": lx._text(note)[:500]}
        d.mkdir(parents=True, exist_ok=True)
        lx.write_text(p, json.dumps({"version": 1, "items": items}, indent=1, ensure_ascii=False))
    return {"ok": True, "id": rid, "status": status, "file": str(p)}


_KEY = {"asks": "a", "for_you": "f", "questions": "q"}


def page_keys(d: Path) -> dict:
    """The page's checkbox keys (001-a0, 002-f1, recap-q0, recap-o0) -> {id, status}."""
    out = {}
    for r in rows_for(d):
        for src in r["sources"]:
            m = re.fullmatch(r"(\d{3})-[^:]*\.json:(asks|for_you|questions)\[(\d+)\]", src)
            if m:
                out[f"{m.group(1)}-{_KEY[m.group(2)]}{m.group(3)}"] = {"id": r["id"], "status": r["status"]}
                continue
            m = re.fullmatch(r"recap\.json:(questions|owes)\[(\d+)\]", src)
            if m:
                out[f"recap-{'q' if m.group(1) == 'questions' else 'o'}{m.group(2)}"] = {"id": r["id"],
                                                                                         "status": r["status"]}
    return out


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whosaid live", description="Rows of ended live explainer calls (#227).")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export-items", help="read-only: an ended call's asks, questions, decisions as rows")
    e.add_argument("stem", nargs="?", help="the session stem (its start time, e.g. 20261116T100000)")
    e.add_argument("--since", help="every ended call after this stem (with STEM: the calls in (SINCE, STEM])")
    e.add_argument("--format", choices=("json", "csv"), default="json")
    e.add_argument("--into", default=str(live.DEFAULT_INTO), help="recordings folder (default ~/Recordings)")
    m = sub.add_parser("mark-item", help=f"set a row's status in <stem>.explain/{STATUS_FILE}")
    m.add_argument("stem")
    m.add_argument("id", help="the row id from export-items")
    m.add_argument("--status", required=True, choices=STATUSES)
    m.add_argument("--note", default="", help="why (kept with the status)")
    m.add_argument("--into", default=str(live.DEFAULT_INTO), help="recordings folder (default ~/Recordings)")
    a = p.parse_args(argv)
    into = Path(a.into).expanduser()
    if a.cmd == "export-items":
        if not a.stem and not a.since:
            p.error("export-items needs a STEM or --since STEM")
        res = export(into, a.stem, a.since)
        if a.format == "csv" and res.get("ok"):
            sys.stdout.write(to_csv(res["rows"]))
            for s in res["skipped"]:
                print(f"whosaid live export-items: skipped {s['stem']}: {s['reason']}", file=sys.stderr)
            return 0
        if a.format == "csv":
            print(f"whosaid live export-items: {res['error']}", file=sys.stderr)
            return 1
        print(json.dumps(res, indent=1, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    try:
        d = lx.store_dir(into, a.stem)
    except lx.ExplainError as err:
        print(json.dumps({"ok": False, "error": str(err)}))
        return 1
    res = set_status(d, a.id, a.status, a.note, by="cli")
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
