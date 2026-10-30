#!/usr/bin/env python3
"""
Offline test for lib/topics.py (`whosaid topics build`): a synthetic workspace cut
from the live fixture call (three meetings and one Teams chat), the rollup across
meetings, ranking, stable ids as a meeting is added, the per-topic wordgraph, no
speaker names in the file, --out and --chats, and the G4 rule: no process and no
socket while it builds.

Run: python3 test/topics_test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import topics  # noqa: E402

FIXTURE = ROOT / "test" / "fixtures" / "live" / "call.jsonl"
NAMES = {"me": "Alice_Example", "them": "Bob_Example"}
LINES = [json.loads(l) for l in FIXTURE.read_text().splitlines()[1:]]
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def meeting(ws: Path, folder: str, lo: float, hi: float) -> None:
    d = ws / folder
    d.mkdir(parents=True, exist_ok=True)
    rows = [e for e in LINES if lo <= e["t"] < hi]
    turns = []
    for e in rows:
        s = int(e["t"])
        turns.append(f"[{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}] {NAMES[e['who']]}: {e['text']}")
    (d / "meeting.speakers.txt").write_text("# Speakers: Alice_Example, Bob_Example\n\n" + "\n\n".join(turns) + "\n")


def workspace(root: Path) -> Path:
    ws = root / "ws"
    meeting(ws, "2026-09-01-0900", 0, 700)
    meeting(ws, "2026-09-08-0900", 700, 1600)
    meeting(ws, "2026-09-15-0900", 0, 1600)
    meeting(ws, "2026-09-16-1000", 0, 400)          # a Teams chat
    meeting(ws, "standup notes", 0, 300)            # hand-named: its date comes from the manifest
    (ws / "_workspace.json").write_text(json.dumps({"meetings": [
        {"folder": "2026-09-16-1000", "kind": "teams-chat"},
        {"folder": "standup notes", "created": "2026-08-20T16:30:00Z", "kind": "audio"}]}))
    return ws


tmp = Path(tempfile.mkdtemp(prefix="whosaid-topics-"))
try:
    ws = workspace(tmp)
    r = topics.build(ws, now=1.0)
    T = r["topics"]
    ids = [t["id"] for t in T]

    # ---- shape ----
    check(r["version"] == 1 and r["built"] == "1970-01-01T00:00:01Z", "version + built")
    check(r["meetings"] == 4 and r["skippedChats"] == 1, f"4 meetings, the chat skipped: {r['meetings']}, {r['skippedChats']}")
    check(len(T) >= 4 and len(set(ids)) == len(ids), f">= 4 distinct topics: {ids}")
    keys = {"id", "rootId", "label", "rank", "share", "speakingMs", "visits", "terms", "firstDate", "lastDate", "meetings"}
    check(all(set(t) == keys for t in T), "topic keys")
    check(all(t["rootId"] == t["id"] and t["visits"] == len(t["meetings"]) for t in T), "rootId = id, visits = meetings")
    check(abs(sum(t["share"] for t in T) - 1) < 0.01, "shares add up to 1")

    # ---- rollup + ranking ----
    multi = [t for t in T if t["visits"] >= 2]
    check(len(multi) >= 3, f">= 3 topics span meetings: {[(t['id'], t['visits']) for t in T]}")
    check(any(t["visits"] >= 3 for t in T), "one topic is in three meetings")
    check([t["rank"] for t in T] == list(range(1, len(T) + 1)), "ranks 1..n in file order")
    key = [(-t["visits"], t["lastDate"]) for t in T]
    check(all(a[0] < b[0] or (a[0] == b[0] and a[1] >= b[1]) for a, b in zip(key, key[1:])),
          "ranked by meetings, then the latest meeting")
    for t in T:
        dates = [m["date"] for m in t["meetings"]]
        check(dates == sorted(dates, reverse=True) and t["lastDate"] == dates[0] and t["firstDate"] == dates[-1],
              f"{t['id']}: meetings newest first, first/last date")
        check(all(set(m) == {"folder", "date", "label", "speakingMs", "firstT"} and m["speakingMs"] >= topics.MIN_TOPIC_MS
                  and m["firstT"] >= 0 for m in t["meetings"]), f"{t['id']}: meeting refs")
        check(len({m["folder"] for m in t["meetings"]}) == len(t["meetings"]), f"{t['id']}: one ref per meeting")
        check(t["terms"] and not set(t["terms"]) & set(t["label"].lower().split()), f"{t['id']}: terms skip the label")
    folders = {m["folder"] for t in T for m in t["meetings"]}
    check("2026-09-16-1000" not in folders, "the chat is not in any topic")
    hand = [m for t in T for m in t["meetings"] if m["folder"] == "standup notes"]
    check(hand and all(m["date"] == "2026-08-20T16:30" for m in hand), "a hand-named folder dates from the manifest")
    check(all(m["date"] == m["folder"][:10] + "T09:00" for t in T for m in t["meetings"] if m["folder"].startswith("2026-09")),
          "a dated folder dates from its name")

    # ---- wordgraph ----
    N = r["nodes"]
    check({n["topicId"] for n in N} == set(ids), "every topic has nodes, no stray topicId")
    for t in T:
        mine = [n for n in N if n["topicId"] == t["id"]]
        roots = [n for n in mine if n["depth"] == 0]
        check(len(roots) == 1 and roots[0]["id"] == t["id"] and roots[0]["label"] == t["label"], f"{t['id']}: one root")
        want = f"{t['visits']} meeting{'s' if t['visits'] != 1 else ''}, last {t['lastDate'][:10]}"
        check(roots[0]["description"] == want, f"{t['id']}: root says {roots[0]['description']!r}")
        check(len(mine) <= topics.NODES_PER_TOPIC and any(n["depth"] == 2 for n in mine), f"{t['id']}: capped, has depth 2")
        nid = {n["id"] for n in mine}
        check(all(n["parentId"] is None or n["parentId"] in nid for n in mine), f"{t['id']}: parents resolve")
    check(len({n["id"] for n in N}) == len(N), "node ids unique")

    # ---- no names ----
    text = json.dumps(r)
    check("Alice_Example" not in text and "Bob_Example" not in text, "no speaker names in the file")

    # ---- deterministic, stable ids ----
    check(topics.build(ws, now=1.0) == r, "same workspace, same file")
    meeting(ws, "2026-09-22-0900", 0, 700)
    r2 = topics.build(ws, now=2.0)
    ids2 = [t["id"] for t in r2["topics"]]
    check(set(ids) <= set(ids2), f"adding a meeting keeps every id: {ids} -> {ids2}")
    grew = [t for t in r2["topics"] if any(m["folder"] == "2026-09-22-0900" for m in t["meetings"])]
    check(grew and all(t["lastDate"] == "2026-09-22T09:00" and t["meetings"][0]["folder"] == "2026-09-22-0900" for t in grew),
          "the new meeting is the newest ref of the topics it joined")
    old = {t["id"]: t["visits"] for t in T}
    check(all(t["visits"] == old.get(t["id"], 0) + 1 for t in grew), "each joined topic gained one meeting")
    check(topics.build(ws, chats=True, now=2.0)["meetings"] == 6, "--chats takes the chat too")

    # ---- CLI: file, --out, empty ----
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = topics.main(["build", str(ws)])
    f = ws / "_topics.json"
    check(rc == 0 and f.is_file() and "topics" in out.getvalue() and str(f) in out.getvalue(), "build writes <ws>/_topics.json")
    check(not list(ws.glob("._topics.json.tmp")), "no temp file left")
    check(json.loads(f.read_text())["meetings"] == 5, "the file holds the build")
    f.unlink()
    alt = tmp / "elsewhere.json"
    with contextlib.redirect_stdout(io.StringIO()):
        rc = topics.main(["build", str(ws), "--out", str(alt)])
    check(rc == 0 and alt.is_file() and not f.exists(), "--out writes there, not into the workspace")
    empty = tmp / "empty"
    empty.mkdir()
    e = topics.build(empty, now=1.0)
    check(e["topics"] == [] and e["nodes"] == [] and e["meetings"] == 0, "an empty workspace has no topics")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        check(topics.main(["build", str(tmp / "missing")]) == 2, "a missing workspace exits 2")
    cli = subprocess.run([str(ROOT / "whosaid"), "topics", "build", str(ws), "--out", str(alt)],
                         capture_output=True, text=True, timeout=60)
    check(cli.returncode == 0 and "from 5 meetings" in cli.stdout, f"whosaid topics build: {cli.stdout}{cli.stderr}")
    h = subprocess.run([str(ROOT / "whosaid"), "topics", "--help"], capture_output=True, text=True, timeout=30)
    check(h.returncode == 0 and h.stdout.startswith("TOPICS") and "--chats" in h.stdout, "whosaid topics --help")

    # ---- G4: no process, no socket ----
    tripped = 0

    def boom(*a, **k):
        raise AssertionError("G4: a process or socket was opened")

    class BoomSocket(socket.socket):
        def __init__(self, *a, **k):
            boom()

    class BoomPopen(subprocess.Popen):
        def __init__(self, *a, **k):
            boom()

    saved = (subprocess.Popen, subprocess.run, socket.socket, os.system, socket.create_connection)
    subprocess.Popen, socket.socket = BoomPopen, BoomSocket
    subprocess.run = os.system = socket.create_connection = boom
    try:
        for fn in (lambda: socket.socket(), lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"])):
            try:
                fn()
            except AssertionError:
                tripped += 1
        check(tripped == 3, "G4: the guards are live")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = topics.main(["build", str(ws)])
        check(rc == 0 and f.is_file(), "G4: build ran with no process or socket")
    finally:
        subprocess.Popen, subprocess.run, socket.socket, os.system, socket.create_connection = saved
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"PASS: topics_test ({CHECKS} checks)")
