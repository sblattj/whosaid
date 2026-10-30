#!/usr/bin/env python3
"""
Offline test for lib/live_insights.py (`whosaid live insights`, `whosaid live ask`):
topics with a revisit, the wordgraph nodes (jibbr keys), 5-minute summaries,
agreed vs suggested next steps, determinism, atomic writes, follow --once against
test/live_replay.py, lexical ask, and the G4 rule: nothing but an explicit
`engine = "claude"` workspace may spawn a process or open a socket.

Run: python3 test/live_insights_test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import live  # noqa: E402
import live_insights as li  # noqa: E402

FIXTURE = ROOT / "test" / "fixtures" / "live" / "call.jsonl"
REPLAY = ROOT / "test" / "live_replay.py"
NODE_KEYS = {"id", "topicId", "parentId", "depth", "label", "description", "source", "evidence"}
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


meta, entries = live.read_entries(FIXTURE)
check(120 <= len(entries) <= 160 and meta.get("ended") is True, f"fixture shape: {len(entries)} entries")
check({e["who"] for e in entries} == {"me", "them"}, "fixture is me/them only")
r = li.build(meta, entries, "call", now=1.0)
last_t = entries[-1]["t"]
check(r["version"] == 1 and r["session"] == "call" and r["t"] == last_t and r["lines"] == len(entries)
      and r["ended"] is True and r["updated"] == 1.0, "top-level fields")

# ---- topics ----
topics = r["topics"]
ids = [t["id"] for t in topics]
check(len(topics) >= 4 and len(set(ids)) == len(ids), f">= 4 distinct topics: {ids}")
check(all(t["rootId"] == t["id"] for t in topics), "rootId = id")
check(sorted(t["rank"] for t in topics) == list(range(1, len(topics) + 1)), "ranks 1..n")
by_rank = sorted(topics, key=lambda t: t["rank"])
check(all(a["speakingMs"] >= b["speakingMs"] for a, b in zip(by_rank, by_rank[1:])), "rank by speakingMs desc")
check(abs(sum(t["share"] for t in topics) - 1.0) < 0.01, "shares sum to 1")
check(not any(t["new"] for t in topics), "ended call: nothing is new")
labels = {t["label"].lower() for t in topics}
check(any("release" in x for x in labels) and any("onboarding" in x for x in labels), f"labels: {labels}")
check(all(t["label"] == t["label"].title() for t in topics), "labels are title case")

parser = next(t for t in topics if "regex" in t["terms"])
check(parser["visits"] >= 2, f"the parser topic comes back: visits {parser['visits']}")
back = next(e["t"] for e in entries if e["t"] > 800 and "dry run over the archive" in e["text"])
before = li.build(meta, [e for e in entries if e["t"] < back], "call", now=1.0)
early = next(t for t in before["topics"] if "regex" in t["terms"])
check(early["id"] == parser["id"] and early["visits"] == 1, f"revisit keeps the id: {early['id']} vs {parser['id']}")
check(all(t["id"] in ids for t in before["topics"]), "earlier topic ids survive later entries")

cost_start = next(e["t"] for e in entries if "cloud bill" in e["text"])
part = li.build(meta, [e for e in entries if e["t"] <= cost_start + 30], "call", now=1.0)
fresh = [t for t in part["topics"] if t["new"]]
check(len(fresh) == 1 and any(w in fresh[0]["terms"] for w in ("cost", "bucket", "spend", "storage")),
      f"new = firstT in the last 60 s: {[(t['id'], t['new']) for t in part['topics']]}")
check(part["ended"] is True and part["t"] <= cost_start + 30, "partial build reports its own t")

# ---- nodes ----
nodes = r["nodes"]
nids = {n["id"] for n in nodes}
check(len(nodes) >= 60 and len(nodes) <= 800, f"node count: {len(nodes)}")
check(len(nids) == len(nodes), "node ids unique")
check(all(set(n) == NODE_KEYS for n in nodes), "every node has exactly the jibbr keys")
check(all(n["source"] in ("topic", "meeting", "related") for n in nodes), "source values")
check(all(n["topicId"] in ids for n in nodes), "every node names a known topic")
byid = {n["id"]: n for n in nodes}
roots = [n for n in nodes if n["parentId"] is None]
check({n["id"] for n in roots} == set(ids) and all(n["depth"] == 0 and n["source"] == "topic" for n in roots),
      "one depth-0 topic node per topic")
check(all(n["parentId"] is None or (n["parentId"] in byid and byid[n["parentId"]]["depth"] == n["depth"] - 1
                                     and n["id"].startswith(n["parentId"] + "/")) for n in nodes),
      "parents exist, depth = parent + 1, ids are paths")
check(max(n["depth"] for n in nodes) == 2, "depth <= 2")
kids = Counter(n["parentId"] for n in nodes if n["parentId"])
check(max(kids.values()) <= 6, f"<= 6 children per parent: {max(kids.values())}")
root = byid[parser["id"]]
check(root["description"].startswith(f"Discussed {parser['visits']} times, ") and ":" in root["description"],
      f"topic description: {root['description']}")
check(all(isinstance(n["evidence"], list) and isinstance(n["description"], str) for n in nodes), "types")

# ---- summaries ----
sums = r["summaries"]
check(len(sums) == math.ceil(last_t / 300) and sums[0]["from"] == 0 and sums[-1]["to"] >= last_t,
      f"summaries cover the call: {[(s['from'], s['to']) for s in sums]}")
check(all(b["from"] == a["to"] and b["to"] - b["from"] == 300 for a, b in zip(sums, sums[1:])), "5-minute windows")
check(all(2 <= len(s["bullets"]) <= 3 and s["text"] and s["topics"] for s in sums), "every window has 2-3 bullets")
check(all(set(s["topics"]) <= set(ids) for s in sums), "summary topics are topic ids")
check(any(parser["id"] in s["topics"] for s in sums[3:]), "the revisit window lists the parser topic")

# ---- next steps ----
ns = r["nextSteps"]
kinds = Counter(n["kind"] for n in ns)
check(kinds["agreed"] >= 3 and kinds["suggested"] >= 3, f"agreed and suggested: {kinds}")
check([n["id"] for n in ns] == [f"ns-{i + 1}" for i in range(len(ns))], "ns ids in order")
check(all(n["topicId"] in ids and n["who"] in ("me", "them") and n["clock"].startswith("09:") for n in ns),
      "next step fields")


def kind_of(prefix):
    return next((n["kind"] for n in ns if n["text"].startswith(prefix)), None)


check(kind_of("I'll write the unit test") == "agreed", "I'll write ... = agreed")
check(kind_of("We could fix the glossary") == "suggested", "we could ... = suggested")
check(kind_of("Let's set the cost alerts") == "agreed", "let's ... = agreed")
check(kind_of("So that's what I'll say.") is None, "I'll with no action is not a next step")
check(kind_of("Then next week we'll polish up the") is None, "a sentence cut off mid-thought is not a next step")
check(kind_of("Hey folks, let's do this.") is None, "let's with no action is not a next step")
check(kind_of("Sounds good, I'll book") == "agreed", "sounds good + action = agreed")
check(kind_of("We might also tag") == "suggested", "might = suggested")
check(li.classify("Let's move on to the budget.") is None, "navigation is not a next step")
check(li.classify("Should we tag the release first?") == "suggested", "should we ...? = suggested")
check(li.classify("I'll send the fixture tonight") == "agreed", "contract example = agreed")
check(li.classify("Thanks, talk tomorrow.") is None, "small talk is not a next step")

# ---- determinism ----
check(li.build(meta, entries, "call", now=1.0) == r, "build is deterministic in-process")
outs = []
with tempfile.TemporaryDirectory() as d:
    for seed in ("1", "7"):
        out = Path(d) / f"o{seed}.json"
        subprocess.run([sys.executable, str(ROOT / "lib" / "live_insights.py"), "build", "--jsonl", str(FIXTURE),
                        "--out", str(out)], check=True, capture_output=True, env={**os.environ, "PYTHONHASHSEED": seed})
        obj = json.loads(out.read_text())
        obj.pop("updated")
        outs.append(obj)
check(outs[0] == outs[1], "same output under different hash seeds")

# ---- atomic write + default out path ----
with tempfile.TemporaryDirectory() as d:
    jl = Path(d) / "20261029T090000.jsonl"
    shutil.copy(FIXTURE, jl)
    out, res = li.build_file(jl)
    check(out == Path(d) / "20261029T090000.insights.json" and out.is_file(), f"default out path: {out}")
    check(not list(Path(d).glob("*.tmp")), "no .tmp left behind")
    check(json.loads(out.read_text())["session"] == "20261029T090000", "session = stem")

# ---- follow --once against a replay ----
with tempfile.TemporaryDirectory() as d:
    fol = subprocess.Popen([sys.executable, str(ROOT / "lib" / "live_insights.py"), "follow", "--into", d,
                            "--once", "--poll", "0.05", "--max-hours", "0.01"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    rep = subprocess.run([sys.executable, str(REPLAY), "--fixture", str(FIXTURE), "--into", d,
                          "--interval", "0.08", "--lines-per-write", "20"], capture_output=True, text=True, timeout=30)
    try:
        rc = fol.wait(timeout=15)
    except subprocess.TimeoutExpired:
        fol.kill()
        rc = None
    lines = rep.stdout.splitlines()
    check(rep.returncode == 0 and len(lines) == math.ceil(len(entries) / 20)
          and lines[-1].startswith(f"write n={len(entries)} wall="), f"replay output: {lines[-2:]}")
    final = Path(d) / live.LIVE_DIR / "call.insights.json"
    check(rc == 0 and final.is_file(), f"follow --once exits 0 after the end: rc={rc} {fol.stderr.read()[-300:]}")
    got = json.loads(final.read_text())
    check(got["ended"] and got["lines"] == len(entries) and got["session"] == "call", "follow wrote the final file")
    got.pop("updated")
    want = dict(r, session="call")
    want.pop("updated")
    check(got == want, "followed result equals a one-shot build")
    check(not list((Path(d) / live.LIVE_DIR).glob("*.tmp")), "follow leaves no .tmp")
sys.path.insert(0, str(ROOT / "test"))
import live_replay  # noqa: E402
check(not live_replay.allowed(Path.home() / "not-a-fixture"), "replay refuses a home folder")
with contextlib.redirect_stderr(io.StringIO()):
    check(live_replay.main(["--fixture", str(FIXTURE), "--into", str(Path.home() / "not-a-fixture")]) == 2,
          "replay exits 2 outside temp")
check(not (Path.home() / "not-a-fixture").exists(), "refusal writes nothing")
check(live_replay.allowed(Path(tempfile.gettempdir()) / "x") and live_replay.allowed(Path("/tmp/x"))
      and live_replay.allowed(Path.home() / "whosaid-app-fixture" / "x"), "replay allows temp and fixture dirs")

# ---- ask (lexical) ----
with tempfile.TemporaryDirectory() as d:
    (Path(d) / live.LIVE_DIR).mkdir()
    shutil.copy(FIXTURE, Path(d) / live.LIVE_DIR / "call.jsonl")
    a = li.ask(Path(d), "How many lines did the parser drop in the archive?")
    check(a["ok"] and a["mode"] == "search" and a["session"] == "call" and 1 <= len(a["matches"]) <= 5,
          f"ask: {a.get('mode')} {len(a['matches'])}")
    check("dropped nine lines" in a["matches"][0]["text"], f"ask top line: {a['matches'][0]}")
    check(set(a["matches"][0]) >= {"t", "clock", "who", "text"} and "nine lines" in a["answer"], "ask fields + answer")
    a = li.ask(Path(d), "what is the retention policy on the storage bucket", minutes=8)
    check(a["matches"] and "retention" in a["matches"][0]["text"].lower()
          and all(m["t"] >= last_t - 480 for m in a["matches"]), "ask --minutes limits the window")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = li.main(["ask", "--into", d, "--question", "who sets up the cost alerts", "--json"])
    j = json.loads(buf.getvalue())
    check(rc == 0 and set(j) >= {"ok", "mode", "answer", "matches", "session"} and "cost alerts" in
          j["matches"][0]["text"], "ask --json")
    with tempfile.TemporaryDirectory() as e:
        check(li.ask(Path(e), "anything")["ok"] is False, "ask with no session: ok false")

    # ---- G4: no process, no socket, unless engine = "claude" ----
    def ws_with(engine):
        w = Path(tempfile.mkdtemp(dir=d))
        if engine is not None:
            (w / "whosaid.toml").write_text(f'[summarizer]\nengine = "{engine}"\n')
        return w

    claude_ws = ws_with("claude")

    def boom(*_a, **_k):
        raise AssertionError("G4: a process or socket was opened")

    class BoomSocket(socket.socket):   # a class, so modules that subclass socket still import
        def __init__(self, *_a, **_k):
            boom()

    class BoomPopen(subprocess.Popen):
        def __init__(self, *_a, **_k):
            boom()

    saved = (subprocess.Popen, subprocess.run, socket.socket, os.system, socket.create_connection)
    old_env = os.environ.get("WHOSAID_WORKSPACE")
    os.environ["WHOSAID_WORKSPACE"] = str(claude_ws)   # must be ignored: only --ws opts in
    subprocess.Popen, socket.socket = BoomPopen, BoomSocket
    subprocess.run = os.system = socket.create_connection = boom
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            g = li.build(meta, entries, "call", now=1.0)
            out, _ = li.build_file(Path(d) / live.LIVE_DIR / "call.jsonl")
            rc = li.follow(Path(d), poll=0.01, once=True, max_hours=0.01)
            modes = [li.ask(Path(d), "parser dry run", ws=None if w == "-" else str(ws_with(w) if w else ws_with(None)))
                     for w in ("-", None, "none", "auto")]
        tripped = 0
        for f in (lambda: socket.socket(), lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"])):
            try:
                f()
            except AssertionError:
                tripped += 1
        check(tripped == 3, "G4: the guards are live")
        check(g == r and out.is_file() and rc == 0, "G4: build + follow --once ran with no process or socket")
        check(all(m["mode"] == "search" and "engine_error" not in m for m in modes), "G4: ask stays local")
    finally:
        subprocess.Popen, subprocess.run, socket.socket, os.system, socket.create_connection = saved
        if old_env is None:
            os.environ.pop("WHOSAID_WORKSPACE", None)
        else:
            os.environ["WHOSAID_WORKSPACE"] = old_env

    # ---- engine = "claude": ClaudeCLI is used, failures fall back ----
    calls = []

    class Stub:
        def chat(self, system, user):
            calls.append((system, user))
            return "The parser dropped nine lines [09:15:10]."

    real = li._claude_client
    li._claude_client = lambda model, timeout, binary="": calls.append((model, timeout)) or Stub()
    a = li.ask(Path(d), "parser dry run", ws=str(claude_ws))
    check(a["mode"] == "claude" and "nine lines" in a["answer"] and len(calls) == 2
          and calls[0] == ("opus", 900) and "Question: parser dry run" in calls[1][1], f"claude mode: {a}")

    def failing(*_a, **_k):
        raise RuntimeError("no claude here")
    li._claude_client = failing
    a = li.ask(Path(d), "parser dry run", ws=str(claude_ws))
    check(a["mode"] == "search" and a["engine_error"] == "no claude here" and a["matches"], "claude failure falls back")
    li._claude_client = real

    # the real factory builds claude_engine.ClaudeCLI from the [summarizer] settings
    import claude_engine  # noqa: E402
    built = []

    class StubCLI:
        def __init__(self, model, *, binary="", timeout=900):
            built.append((model, binary, timeout))

        def chat(self, system, user):
            return "stub answer [09:15:10]"

    real_cli, claude_engine.ClaudeCLI = claude_engine.ClaudeCLI, StubCLI
    try:
        a = li.ask(Path(d), "parser dry run", ws=str(claude_ws))
    finally:
        claude_engine.ClaudeCLI = real_cli
    check(built == [("opus", "", 900)] and a["mode"] == "claude" and a["answer"] == "stub answer [09:15:10]",
          f"engine = claude constructs ClaudeCLI: {built} {a['mode']}")

print(f"PASS: live_insights_test ({CHECKS} checks)")
