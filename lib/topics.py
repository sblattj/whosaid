#!/usr/bin/env python3
"""
topics.py: the topics library across meetings (`whosaid topics build`; the macOS
app's Topics page reads it).

  build [WS] [--out F] [--chats]
      Run the live_insights topic clustering over each meeting's speaker
      transcript (wsconfig.iter_meetings, the same folders the index sees), then
      roll the meeting topics up across meetings into <WS>/_topics.json
      (tmp + os.replace). Teams chats and mail are left out unless --chats.

Rollup: meetings are taken in folder order (dated folders sort by time), so
earlier meetings mint topics first and a new meeting only extends the library.
A meeting topic joins the library topic with the same label slug, else the one
whose term vector it matches best (cosine over tf-idf, idf across meeting
topics) at >= LINK_MIN, else it starts a new one. A library topic's id is the
slug of its first meeting topic's label (an ordinal only on a clash), so ids stay
put as meetings are added. Its label is the one most of its meetings gave it (a
tie goes to the most talk time).
Topics with under MIN_TOPIC_MS of talk in a meeting are left out.

The file is a superset of the live insights schema (`topics` + `nodes`), so the
wordgraph page can draw it as is: per topic id, rootId, label, rank, share,
speakingMs, visits (= meetings), terms, firstDate, lastDate and meetings
[{folder, date, label, speakingMs, firstT}], newest first. Ranked by number of
meetings, then the latest meeting, then talk time. Each topic gets its own
wordgraph (topic -> top terms -> bigrams / co-occurring terms). No speaker
names are written. Stdlib only, no network, no model.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_insights as li  # noqa: E402
import wsconfig  # noqa: E402

VERSION = 1
OUT_NAME = "_topics.json"
LINK_MIN = 0.35          # cosine to join an existing library topic
MIN_TOPIC_MS = 45_000    # less talk than this in a meeting: not a topic of that meeting
MIN_TOPIC_LINES = 3
LIBRARY_TERMS = 8
NODES_PER_TOPIC = 43     # root + 6 terms + 6 children each
CHAT_KINDS = {"teams-chat", "outlook-mail"}
DATED_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(?:-\d+)?$")


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} topics: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- meetings

def manifest(ws: Path) -> dict[str, dict]:
    try:
        data = json.loads((ws / "_workspace.json").read_text())
        return {m["folder"]: m for m in data.get("meetings", []) if isinstance(m, dict) and m.get("folder")}
    except (OSError, ValueError, TypeError):
        return {}


def meeting_date(folder: str, entry: dict | None) -> str | None:
    """'YYYY-MM-DDTHH:MM': the dated folder name (local time, as the user sees it),
    else the manifest's created time."""
    if m := DATED_RE.match(folder):
        y, mo, d, h, mi = m.groups()
        return f"{y}-{mo}-{d}T{h}:{mi}"
    created = (entry or {}).get("created")
    if isinstance(created, str) and re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", created):
        return created[:16]
    return None


def meeting_entries(files: list[Path]) -> list[dict]:
    """Turns of the meeting's largest speaker transcript as live-style entries
    (t + text only: the library never carries who said it)."""
    best = max(files, key=lambda p: (p.stat().st_size, p.name))
    turns = wsconfig.parse_turns(best.read_text(errors="replace"))
    return [{"t": float(t.t_sec), "text": t.text} for t in turns if t.text.strip()]


def meeting_topics(entries: list[dict]) -> list[dict]:
    """live_insights topics for one meeting, finished, with tiny ones dropped."""
    if not entries:
        return []
    entries.sort(key=lambda e: e["t"])
    chunks = li.chunk(entries)
    topics, _ = li.assign(entries, chunks)
    li.finish(entries, chunks, topics, li.durations(entries))
    return [tp for tp in topics if tp["ms"] >= MIN_TOPIC_MS and len(tp["idx"]) >= MIN_TOPIC_LINES]


# ---------------------------------------------------------------- rollup

def build(ws: Path, chats: bool = False, now: float | None = None) -> dict:
    ws = Path(ws)
    man = manifest(ws)
    meetings = []    # (folder, date, entries, topics)
    skipped = 0
    for folder, files in wsconfig.iter_meetings(ws):
        entry = man.get(folder)
        if not chats and (entry or {}).get("kind") in CHAT_KINDS:
            skipped += 1
            continue
        entries = meeting_entries(files)
        meetings.append((folder, meeting_date(folder, entry), entries, meeting_topics(entries)))

    # idf over meeting topics: a term in every meeting's topics says little
    df = Counter()
    n_mt = 0
    for _, _, _, mts in meetings:
        for tp in mts:
            df.update(set(tp["terms"]))
            n_mt += 1
    idf = {k: math.log(1 + n_mt / df[k]) for k in df}

    lib: list[dict] = []
    used_ids: set[str] = set()
    for mi, (folder, date, entries, mts) in enumerate(meetings):
        for tp in mts:
            key = li.slug(tp["label"])
            pick = next((L for L in lib if L["slug"] == key), None)
            if pick is None:
                scored = sorted(((li.cosine(tp["terms"], L["terms"], idf), -k, k) for k, L in enumerate(lib)),
                                reverse=True)
                if scored and scored[0][0] >= LINK_MIN:
                    pick = lib[scored[0][2]]
            if pick is None:
                tid, n = key, 1
                while tid in used_ids:
                    n += 1
                    tid = f"{key}-{n}"
                used_ids.add(tid)
                pick = {"id": tid, "slug": key, "terms": Counter(), "labels": Counter(), "labelMs": Counter(),
                        "members": []}
                lib.append(pick)
            pick["terms"].update(tp["terms"])
            pick["labels"][tp["label"]] += 1
            pick["labelMs"][tp["label"]] += tp["ms"]
            pick["members"].append((mi, tp))

    out_topics, nodes = [], []
    total_ms = sum(tp["ms"] for L in lib for _, tp in L["members"]) or 1
    for L in lib:
        per: dict[int, dict] = {}
        for mi, tp in L["members"]:
            folder, date = meetings[mi][0], meetings[mi][1]
            m = per.setdefault(mi, {"folder": folder, "date": date, "label": tp["label"],
                                    "speakingMs": 0, "firstT": tp["firstT"]})
            m["speakingMs"] += tp["ms"]
            m["firstT"] = min(m["firstT"], tp["firstT"])
        L["label"] = sorted(L["labels"], key=lambda s: (-L["labels"][s], -L["labelMs"][s], s))[0]
        L["ms"] = sum(tp["ms"] for _, tp in L["members"])
        L["meetings"] = sorted(per.values(), key=lambda m: (m["date"] or "", m["folder"]), reverse=True)
        dates = [m["date"] for m in L["meetings"] if m["date"]]
        L["first"], L["last"] = (min(dates), max(dates)) if dates else (None, None)

    order = sorted(lib, key=lambda L: (-len(L["meetings"]), _neg(L["last"]), -L["ms"], L["id"]))
    for rank, L in enumerate(order, 1):
        graph = topic_graph(L, meetings)
        out_topics.append({
            "id": L["id"], "rootId": L["id"], "label": L["label"], "rank": rank,
            "share": round(L["ms"] / total_ms, 4), "speakingMs": L["ms"], "visits": len(L["meetings"]),
            "terms": graph["terms"], "firstDate": L["first"], "lastDate": L["last"],
            "meetings": L["meetings"]})
        nodes.extend(graph["nodes"])

    built = time.time() if now is None else now
    return {"version": VERSION,
            "built": datetime.fromtimestamp(built, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "meetings": len(meetings), "skippedChats": skipped,
            "topics": out_topics, "nodes": nodes}


def _neg(date: str | None) -> tuple:
    """Sort key that puts the newest date first and undated last."""
    return (date is None, tuple(-ord(c) for c in (date or "")))


def topic_graph(L: dict, meetings: list) -> dict:
    """The wordgraph of one library topic: its members' turns side by side."""
    entries, idx, bg = [], [], Counter()
    for mi, tp in L["members"]:
        src = meetings[mi][2]
        for i in tp["idx"]:
            idx.append(len(entries))
            entries.append(src[i])
        bg.update(tp["bigrams"])
    nt = max(1, len(L["members"]))
    weight = {w: c * (1 + 1 / nt) for w, c in L["terms"].items()}
    top = sorted(weight, key=lambda w: (-weight[w], w))
    n = len(L["meetings"])
    tp = {"id": L["id"], "label": L["label"], "idx": idx, "bigrams": bg, "weight": weight, "top": top,
          "visits": n, "ms": L["ms"]}
    nodes = li.wordgraph(entries, [tp], [0])[:NODES_PER_TOPIC]
    if nodes:
        last = (L["last"] or "")[:10]
        nodes[0]["description"] = f"{n} meeting{'s' if n != 1 else ''}" + (f", last {last}" if last else "")
    label_words = set(L["label"].lower().split())
    return {"nodes": nodes, "terms": [w for w in top if w not in label_words][:LIBRARY_TERMS]}


def write_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="whosaid topics", description="Topics across meetings.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="roll every meeting's topics up into <ws>/_topics.json")
    b.add_argument("ws", nargs="?", help="workspace (default: $WHOSAID_WORKSPACE)")
    b.add_argument("--out", help=f"write here instead of <ws>/{OUT_NAME}")
    b.add_argument("--chats", action="store_true", help="include Teams chats and mail")
    b.add_argument("--json", action="store_true", help="print the result instead of a one-line summary")
    a = ap.parse_args(argv)
    ws = wsconfig.resolve_workspace(a.ws)
    if not ws.is_dir():
        log(f"no workspace at {ws}")
        return 2
    t0 = time.monotonic()
    r = build(ws, chats=a.chats)
    out = Path(a.out).expanduser() if a.out else ws / OUT_NAME
    write_json(out, r)
    if a.json:
        print(json.dumps(r, indent=1, ensure_ascii=False))
    else:
        multi = sum(1 for t in r["topics"] if t["visits"] > 1)
        print(f"{len(r['topics'])} topics ({multi} in more than one meeting) from {r['meetings']} meetings"
              f" in {time.monotonic() - t0:.1f}s -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
