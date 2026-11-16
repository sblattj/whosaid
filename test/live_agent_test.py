#!/usr/bin/env python3
"""
Offline test for `whosaid live follow --agent` (issue #217): events for an agent's
background monitor, built from the live .jsonl.

Done-when:
  * killing and restarting `follow --agent --cursor-file F` mid-fixture emits every
    utterance exactly once (same utterances as one uninterrupted run);
  * a topic revisited 20+ minutes later emits `topic_revisit` pointing to the first visit;
  * a turn that addresses the self speaker by name with a question emits `for_me`;
  * `call_ended` is followed by `call_started` when a second session begins, without
    restarting the follower.
Also: utterances join consecutive segments of one voice, a line sorted in late is
emitted once, `decision` cites the proposal and the yes, `alive` heartbeats, the plain
follow resumes from --cursor-file, and a broken cursor file is an error.

The call is the synthetic #213 fixture (test/fixtures/decisions/...), invented people.
Stdlib only; the kill/restart test runs `lib/live.py` as a child process.

Run: python3 test/live_agent_test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
NO_REG = Path(tempfile.mkdtemp(prefix="whosaid-agent-test-")) / "speakers.json"
NO_REG.write_text(json.dumps({"speakers": [{"name": "Alice_Example", "role": "self"}]}))
os.environ["WHOSAID_SPEAKER_DB"] = str(NO_REG)  # before any import reads the registry
import live  # noqa: E402
import live_agent as la  # noqa: E402
import wsconfig  # noqa: E402

FIXTURE = ROOT / "test" / "fixtures" / "decisions" / "2026-03-02-1000" / "call.speakers.txt"
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


VOICE = {"Bob_Example": "v1", "Carol_Example": "v2"}
ASK = (17 * 60 + 30, "Bob_Example", "Alice, can you check whether the billing export ticket already has an owner?")
SPLIT = {11 * 60 + 20: "Let's keep the date parser in the service lib for now,|the service lib already ships with every deploy."}


def fixture_entries(started: float) -> list[dict]:
    """The #213 call as live entries (Alice = me), in the order the writer made them; one
    turn split in two segments (one utterance), plus one question put to Alice."""
    rows = [(float(t.t_sec), t.speaker, t.text.strip()) for t in wsconfig.parse_turns(FIXTURE.read_text())]
    rows.append(ASK)
    rows.sort(key=lambda r: r[0])
    out = []
    for t, who, text in rows:
        parts = SPLIT.get(int(t), text).split("|")
        for k, part in enumerate(parts):
            e = {"t": t + 4.0 * k, "who": "me" if who == "Alice_Example" else "them", "text": part.strip()}
            if who in VOICE:
                e.update(voice=VOICE[who], confidence=0.81)
                if who == "Bob_Example":
                    e["name"] = who
            out.append(e)
    for n, e in enumerate(out):
        e["n"] = n
        e["clock"] = time.strftime("%H:%M:%S", time.localtime(started + e["t"]))
    return out


def write_progressively(into: Path, started: float, entries: list[dict], batch: int = 4, pause: float = 0.12,
                        ended: bool = True) -> None:
    """The writer: the whole file rewritten per batch; Carol's voice gets a name halfway
    (the earlier lines are renamed in place, as voices.annotate does)."""
    md, jl = live.session_paths(into, started)
    live.write_session(md, jl, started, [], ended=False)
    shown: list[dict] = []
    for i in range(0, len(entries), batch):
        shown = [dict(e) for e in entries[: i + batch]]
        if i >= len(entries) // 2:
            for e in shown:
                if e.get("voice") == "v2":
                    e["name"] = "Carol_Example"
        time.sleep(pause)
        live.write_session(md, jl, started, shown, ended=False)
    time.sleep(pause)
    live.write_session(md, jl, started, shown, ended=ended)


def run_agent(into: Path, **kw) -> list[dict]:
    buf = io.StringIO()
    kw.setdefault("once", True)
    kw.setdefault("poll", 0.02)
    kw.setdefault("deadline", time.monotonic() + 20)
    la.follow_agent(into, fmt="jsonl", out=buf, **kw)
    return [json.loads(ln) for ln in buf.getvalue().splitlines()]


def utter(rows):
    return [r for r in rows if r["event"] == "utterance"]


STARTED = time.time() - 3 * 3600
ENTRIES = fixture_entries(STARTED)


# ---------------------------------------------------------------- utterances
def test_utterances():
    with tempfile.TemporaryDirectory() as d:
        md, jl = live.session_paths(Path(d), STARTED)
        live.write_session(md, jl, STARTED, [dict(e) for e in ENTRIES], ended=True)
        rows = run_agent(Path(d), self_names=["Alice_Example"])
    us = utter(rows)
    ns = [n for u in us for n in u["n"]]
    check(sorted(ns) == list(range(len(ENTRIES))) and len(ns) == len(set(ns)), f"every line in exactly one utterance: {ns}")
    joined = [u for u in us if u["t"] == 11 * 60 + 20]
    check(len(joined) == 1 and len(joined[0]["n"]) == 2 and joined[0]["end"] == 11 * 60 + 24
          and joined[0]["text"].startswith("Let's keep the date parser") and joined[0]["text"].endswith("every deploy."),
          f"two segments of one voice: one utterance: {joined}")
    for k in ("t", "end", "speaker", "voice", "confidence", "mixed", "text", "topic", "visit", "session", "cursor"):
        check(all(k in u for u in us), f"every utterance has {k}")
    check(us[0]["speaker"] == "me" and next(u for u in us if u.get("voice") == "v1")["speaker"] == "Bob_Example",
          f"speaker labels: {[u['speaker'] for u in us[:3]]}")
    check(rows[0]["event"] == "call_started" and rows[-1]["event"] == "call_ended", f"lifecycle ends: {rows[0]} {rows[-1]}")
    voices = {v["voice"] for v in rows[-1]["voices"]}
    check({"me", "v1", "v2"} <= voices, f"call_ended lists the voices: {rows[-1]['voices']}")


def test_late_line_once():
    """A line a lagging leg sorts in among lines already emitted: its own utterance, once."""
    with tempfile.TemporaryDirectory() as d:
        md, jl = live.session_paths(Path(d), STARTED)
        a = [{"clock": "10:00:00", "n": 0, "t": 0.0, "who": "them", "voice": "v1", "text": "first point about the build."},
             {"clock": "10:00:01", "n": 1, "t": 30.0, "who": "me", "text": "second point about the deploy."},
             {"clock": "10:00:02", "n": 2, "t": 60.0, "who": "them", "voice": "v1", "text": "third point about the tests."}]
        live.write_session(md, jl, STARTED, [dict(e) for e in a], ended=False)
        cf = Path(d) / "cur.json"
        first = run_agent(Path(d), once=False, deadline=time.monotonic() + 0.3, cursor_file=cf, flush_seconds=1e9)
        late = {"clock": "10:00:03", "n": 3, "t": 20.0, "who": "me", "text": "a late line from my mic."}
        live.write_session(md, jl, STARTED, [dict(e) for e in a] + [late], ended=True)
        second = run_agent(Path(d), cursor_file=cf)
    got = [n for u in utter(first) + utter(second) for n in u["n"]]
    check(sorted(got) == [0, 1, 2, 3] and len(got) == 4, f"the late line is emitted once, nothing twice: {got}")
    check([u["n"] for u in utter(first)] == [[0], [1]], f"the last line waits: another voice may go on: {utter(first)}")
    check([u["n"] for u in utter(second)] == [[3], [2]], f"after the restart: the late line and the tail: {utter(second)}")
    check(not any(r["event"] == "call_started" for r in second), "no second call_started on resume")


# ---------------------------------------------------------------- done-when 1: kill and restart
def agent_cmd(into: Path, cf: Path) -> list[str]:
    return [sys.executable, str(ROOT / "lib" / "live.py"), "follow", "--agent", "--format", "jsonl", "--once",
            "--into", str(into), "--cursor-file", str(cf), "--poll", "0.05", "--self", "Alice_Example"]


def test_kill_restart():
    env = dict(os.environ, WHOSAID_SPEAKER_DB=str(NO_REG))
    with tempfile.TemporaryDirectory() as d:  # uninterrupted run: the reference
        into = Path(d)
        th = threading.Thread(target=write_progressively, args=(into, STARTED, ENTRIES), kwargs={"pause": 0.05})
        th.start()
        ref = run_agent(into, cursor_file=into / "ref-cursor.json", self_names=["Alice_Example"])
        th.join()
    with tempfile.TemporaryDirectory() as d:
        into = Path(d)
        cf = into / "cursor.json"
        th = threading.Thread(target=write_progressively, args=(into, STARTED, ENTRIES), kwargs={"pause": 0.25})
        th.start()
        p = subprocess.Popen(agent_cmd(into, cf), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        before = []
        while len(utter(before)) < 8:
            line = p.stdout.readline()
            check(line, f"the first follower stopped early: {before}")
            before.append(json.loads(line))
        last = utter(before)[-1]["n"]
        for _ in range(200):  # the cursor file holds the last utterance: kill it now, mid-call
            try:
                if set(last) <= la.load_cursor(cf).consumed_set():
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.01)
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        before += [json.loads(ln) for ln in p.stdout.read().splitlines() if ln.strip()]
        p.stdout.close()
        out = subprocess.run(agent_cmd(into, cf), capture_output=True, text=True, env=env, timeout=60)
        th.join()
        after = [json.loads(ln) for ln in out.stdout.splitlines()]
    both = utter(before) + utter(after)
    got = [n for u in both for n in u["n"]]
    check(len(utter(before)) >= 8 and len(utter(after)) >= 3, f"killed mid-call: {len(utter(before))} + {len(utter(after))}")
    check(len(got) == len(set(got)) and sorted(got) == list(range(len(ENTRIES))),
          f"kill + restart: every line exactly once: {got}")
    check([(u["n"], u["text"]) for u in both] == [(u["n"], u["text"]) for u in utter(ref)],
          "kill + restart: the same utterances as one uninterrupted run")
    print(f"  kill/restart: {len(utter(before))} utterances before the kill, {len(utter(after))} after, "
          f"{len(got)} lines, each once")
    for kind in ("call_started", "call_ended", "topic_revisit", "for_me", "decision"):
        n_ref = sum(r["event"] == kind for r in ref)
        n_got = sum(r["event"] == kind for r in before + after)
        check(n_ref == n_got and n_ref >= 1, f"kill + restart: {kind} {n_got} times, as uninterrupted ({n_ref})")


# ---------------------------------------------------------------- done-when 2: topic revisit
def test_topic_revisit():
    with tempfile.TemporaryDirectory() as d:
        md, jl = live.session_paths(Path(d), STARTED)
        live.write_session(md, jl, STARTED, [dict(e) for e in ENTRIES], ended=True)
        rows = run_agent(Path(d))
    revs = [r for r in rows if r["event"] == "topic_revisit"]
    auth = next(i for i, r in enumerate(rows) if r["event"] == "utterance" and "auth client is the other piece" in r["text"])
    rv = [r for r in revs if r["topic"] == rows[auth]["topic"]]
    check(not any("everything, thanks" in r["text"] for r in revs), f"a thin closing chunk is no revisit: {revs}")
    check(len(rv) == 1 and rv[0]["visit"] == 2 and rows[auth]["visit"] == 2, f"the shared libraries topic: visit 2: {revs}")
    rv = rv[0]
    check(rows.index(rv) == auth - 1, "topic_revisit comes just before the utterance that brings the topic back")
    check(rv["first"]["t"] == 10 * 60 + 40 and "shared libraries" in rv["first"]["text"],
          f"points to the first visit's first turn: {rv['first']}")
    check(rv["previous"]["visit"] == 1 and rv["t"] - rv["first"]["t"] >= 20 * 60,
          f"20+ minutes later: {rv['t']} vs {rv['first']['t']}")
    print(f"  topic_revisit: {rv['label']!r} visit {rv['visit']} at {rv['t']:.0f}s -> first turn at {rv['first']['t']:.0f}s")


# ---------------------------------------------------------------- done-when 3: for_me
def test_for_me():
    with tempfile.TemporaryDirectory() as d:
        md, jl = live.session_paths(Path(d), STARTED)
        live.write_session(md, jl, STARTED, [dict(e) for e in ENTRIES], ended=True)
        rows = run_agent(Path(d))  # the self name from the registry (role = self)
        named_off = run_agent(Path(d), self_names=["Zed_Nobody"])
    fm = [r for r in rows if r["event"] == "for_me"]
    check(len(fm) == 1 and fm[0]["text"] == ASK[2] and fm[0]["speaker"] == "Bob_Example",
          f"the question put to Alice by name: {fm}")
    check({"named", "question"} <= set(fm[0]["why"]), f"why: {fm[0]['why']}")
    check([c["t"] for c in fm[0]["before"]] == [16 * 60 + 20, 17 * 60], f"with the 2 turns before it: {fm[0]['before']}")
    check(not any(r["event"] == "for_me" and r["speaker"] == "me" for r in rows), "never for your own lines")
    check(not any(r["event"] == "for_me" and "named" in r["why"] for r in named_off), "another self name: not named")
    # a request with no name, right after you spoke, is for you; a bare statement is not
    s2 = la.ForMe(["Alice_Example"])
    mine = {"who": "me", "speaker": "me", "text": "I looked at the billing export logs.", "t": 1.0}
    req = {"who": "them", "speaker": "them (v3)", "text": "Could you take that one and file it?", "t": 5.0}
    flat = {"who": "them", "speaker": "them (v3)", "text": "The export runs nightly.", "t": 9.0}
    check("request" in (s2.why(req, [mine]) or []), "a request right after you spoke: for_me")
    check(s2.why(flat, [mine, req]) is None, "a statement: not for_me")
    print(f"  for_me: {fm[0]['speaker']}: {fm[0]['text']!r} why={fm[0]['why']}")


# ---------------------------------------------------------------- decision
def test_decision():
    with tempfile.TemporaryDirectory() as d:
        md, jl = live.session_paths(Path(d), STARTED)
        live.write_session(md, jl, STARTED, [dict(e) for e in ENTRIES], ended=True)
        rows = run_agent(Path(d))
    ds = [r for r in rows if r["event"] == "decision"]
    parser = [r for r in ds if "date parser in the service lib" in r["text"].lower()]
    check(len(parser) == 1 and parser[0]["status"] == "agreed", f"the agreed date parser decision, once: {ds}")
    roles = [c["role"] for c in parser[0]["turns"]]
    check(roles[0] == "proposal" and "agree" in roles and parser[0]["turns"][0]["speaker"] != parser[0]["yes"]["speaker"],
          f"cites the proposal and another voice's yes: {parser[0]}")
    check(len({r["id"] for r in ds}) == len(ds), "each decision once")


# ---------------------------------------------------------------- done-when 4: lifecycle
def test_lifecycle():
    with tempfile.TemporaryDirectory() as d:
        into = Path(d)
        s1, s2 = STARTED, STARTED + 3600
        one = [dict(e) for e in ENTRIES[:6]]
        two = [{"clock": "10:00:00", "n": 0, "t": 2.0, "who": "them", "voice": "v1", "text": "Second call, first line about the roadmap."},
               {"clock": "10:00:01", "n": 1, "t": 20.0, "who": "me", "text": "And my reply about the roadmap."}]

        def writer():
            md1, jl1 = live.session_paths(into, s1)
            live.write_session(md1, jl1, s1, one[:4], ended=False)
            time.sleep(0.3)
            live.write_session(md1, jl1, s1, one, ended=False)  # a last write, then the next call starts
            md2, jl2 = live.session_paths(into, s2)
            live.write_session(md2, jl2, s2, two, ended=False)
            time.sleep(0.3)
            live.write_session(md2, jl2, s2, two, ended=True)
        th = threading.Thread(target=writer)
        th.start()
        rows = run_agent(into, once=False, deadline=time.monotonic() + 1.5, flush_seconds=1e9, poll=0.1)
        th.join()
    kinds = [(r["event"], r.get("session")) for r in rows if r["event"] in ("call_started", "call_ended")]
    st1, st2 = live.session_paths(into, s1)[1].stem, live.session_paths(into, s2)[1].stem
    check(kinds == [("call_started", st1), ("call_ended", st1), ("call_started", st2), ("call_ended", st2)],
          f"call_ended, then call_started for the second session, one follower: {kinds}")
    end1 = next(i for i, r in enumerate(rows) if r["event"] == "call_ended")
    got1 = sorted(n for u in utter(rows[:end1]) for n in u["n"])
    check(got1 == list(range(6)), f"the old call is drained before its call_ended: {got1}")
    check(rows[end1].get("superseded") is True, f"ended because a newer call started: {rows[end1]}")
    start2 = rows[end1 + 1]
    check(start2["event"] == "call_started" and {v["voice"] for v in start2["voices"]} == {"v1", "me"},
          f"call_started carries the voices so far: {start2}")
    print(f"  lifecycle: {[k for k, _ in kinds]}")


# ---------------------------------------------------------------- heartbeat
def test_heartbeat():
    with tempfile.TemporaryDirectory() as d:
        md, jl = live.session_paths(Path(d), time.time())
        live.write_session(md, jl, time.time(), [], ended=False)
        clock = {"t": 1000.0}

        def fake_sleep(s):
            clock["t"] += s
        rows = run_agent(Path(d), once=False, heartbeat=10.0, poll=1.0, sleep=fake_sleep,
                         now=lambda: clock["t"], deadline=time.monotonic() + 0.5, max_polls=35)
    alive = [r for r in rows if r["event"] == "alive"]
    check(3 <= len(alive) <= 4 and all("cursor" in r for r in alive), f"one alive per 10 s of quiet: {len(alive)} {alive[:1]}")


# ---------------------------------------------------------------- plain follow resumes too; a bad cursor file
def test_plain_cursor_file():
    with tempfile.TemporaryDirectory() as d:
        into = Path(d)
        md, jl = live.session_paths(into, STARTED)
        live.write_session(md, jl, STARTED, [dict(e) for e in ENTRIES[:5]], ended=False)
        cf = into / "plain.cursor"
        a, b = io.StringIO(), io.StringIO()
        live.follow_stream(into, live.turn_filter(), "jsonl", once=False, poll=0.02,
                           deadline=time.monotonic() + 0.2, out=a, cursor_file=cf)
        live.write_session(md, jl, STARTED, [dict(e) for e in ENTRIES[:8]], ended=True)
        live.follow_stream(into, live.turn_filter(), "jsonl", once=True, poll=0.02, out=b, cursor_file=cf)
        got = [json.loads(x)["n"] for x in (a.getvalue() + b.getvalue()).splitlines() if '"turn"' in x]
        check(got == list(range(8)), f"plain follow + --cursor-file: each line once across a restart: {got}")
        bad = into / "bad.cursor"
        bad.write_text("{not json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = live.main(["follow", "--agent", "--once", "--into", d, "--cursor-file", str(bad)])
        check(rc == 2 and "cursor" in err.getvalue(), f"a broken cursor file is an error, not a replay: {rc} {err.getvalue()}")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print(f"PASS live_agent_test ({CHECKS} checks)")
