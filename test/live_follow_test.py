#!/usr/bin/env python3
"""
Offline test for `whosaid live follow` (issue #187): a cursor that survives the whole-file
rewrite, same-second turns, a call crossing midnight, the --once `ended` line, speaker
filters that keep (and mark) unnamed turns, the `v9 may be B` hint, and a speaker change
inside one Whisper segment (split into two turns, or one turn flagged mixed).
Fake embeddings and a stand-in transcriber; no model, no audio playback.

Run: uv run --with numpy python test/live_follow_test.py
"""
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import live  # noqa: E402
import live_voices as lv  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def turn(t, who, text, started, n=None, **kw):
    e = {"t": t, "who": who, "text": text,
         "clock": datetime.fromtimestamp(started + t).strftime("%H:%M:%S"), **kw}
    if n is not None:
        e["n"] = n
    return e


def run_follow(argv):
    """`whosaid live follow ...` in process: (exit code, stdout lines)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = live.main(["follow", *argv])
    return rc, buf.getvalue().splitlines()


# ---------------------------------------------------------------- cursor: no replay after a rewrite
def test_no_replay_after_rewrite():
    started = time.time() - 300
    for seq in (True, False):  # the writer's sequence number, and an older file without it
        ents = [turn(i * 10.0, "them", f"line {i}", started, n=i if seq else None, voice="v1") for i in range(3)]
        new, cur, reset = live.turns_after(ents, "S1", None)
        check([e["text"] for e, _ in new] == ["line 0", "line 1", "line 2"] and not reset, f"first read: all ({seq})")
        # the rewrite: an earlier line gets a name from voices.annotate, one new line arrives
        ents2 = [dict(e, name="Bob") for e in ents] + [turn(35.0, "them", "line 3", started, n=3 if seq else None)]
        new, cur2, _ = live.turns_after(ents2, "S1", live.parse_cursor(cur))
        check([e["text"] for e, _ in new] == ["line 3"], f"after a rewrite only the new line ({seq}): {new}")
        new, cur3, _ = live.turns_after(ents2, "S1", live.parse_cursor(cur2))
        check(new == [] and cur3 == cur2, f"nothing new: nothing emitted, cursor kept ({seq}): {cur2} {cur3}")
        new, _, reset = live.turns_after(ents2, "S2", live.parse_cursor(cur2))
        check(reset and len(new) == 4, f"a cursor from another session restarts at the new call ({seq})")
    # a late line from a stalled leg sorts in before the newest one: the sequence cursor still emits it
    ents = [turn(0.0, "them", "a", started, n=0), turn(20.0, "them", "b", started, n=1)]
    _, cur, _ = live.turns_after(ents, "S1", None)
    ents = [ents[0], turn(12.0, "me", "late", started, n=2), ents[1]]
    new, _, _ = live.turns_after(ents, "S1", live.parse_cursor(cur))
    check([e["text"] for e, _ in new] == ["late"], f"a line inserted mid-file is emitted once: {new}")
    check(str(cur).startswith("S1:n") and live.parse_cursor(cur) == {"stem": "S1", "n": 1}, f"cursor format: {cur}")
    for bad in ("", "S1:x", "S1:n", "nope:t:1"):
        try:
            live.parse_cursor(bad)
            check(False, f"bad cursor accepted: {bad!r}")
        except ValueError:
            check(True, "bad cursor refused")
    check(live.parse_cursor("12.5") == {"t": 12.5, "k": None}, "a bare t value is a cursor too")


def test_empty_session_starts_on_sequence():
    """`live run` writes an empty session before its first line (live.follow): the cursor
    taken then must still be the sequence one, so a late mid-file line is caught."""
    started = time.time() - 60
    new, cur, _ = live.turns_after([], "S1", None)
    check(new == [] and cur == "S1:n-1" and live.parse_cursor(cur) == {"stem": "S1", "n": -1},
          f"empty session: a sequence cursor: {cur}")
    ents = [turn(0.0, "them", "a", started, n=0), turn(20.0, "them", "b", started, n=1)]
    new, cur, _ = live.turns_after(ents, "S1", live.parse_cursor(cur))
    check([e["text"] for e, _ in new] == ["a", "b"] and cur == "S1:n1", f"first lines: {new} {cur}")
    ents = [ents[0], turn(12.0, "me", "late", started, n=2), ents[1]]
    new, cur, _ = live.turns_after(ents, "S1", live.parse_cursor(cur))
    check([e["text"] for e, _ in new] == ["late"], f"from an empty start, a line sorted in mid-file is caught: {new}")
    new, cur, reset = live.turns_after([turn(1.0, "them", "old", started)], "S1", live.parse_cursor("S1:n-1"))
    check(len(new) == 1 and reset and cur.startswith("S1:t"), "an older writer (no n): falls back to t, all lines")
    with tempfile.TemporaryDirectory() as d:  # through follow_stream, the way it runs beside `live run`
        md, jl = live.session_paths(Path(d), started)
        live.write_session(md, jl, started, [], ended=False)
        rows = []

        def writer():
            time.sleep(0.1)
            live.write_session(md, jl, started, [turn(0.0, "them", "a", started, n=0),
                                                 turn(20.0, "them", "b", started, n=1)], ended=False)
            time.sleep(0.1)
            live.write_session(md, jl, started, [turn(0.0, "them", "a", started, n=0),
                                                 turn(12.0, "me", "late", started, n=2),
                                                 turn(20.0, "them", "b", started, n=1)], ended=True)
        th = threading.Thread(target=writer)
        th.start()
        buf = io.StringIO()
        live.follow_stream(Path(d), live.turn_filter(), "jsonl", once=True, poll=0.02, out=buf)
        th.join()
        rows = [json.loads(ln) for ln in buf.getvalue().splitlines()]
        check([r["text"] for r in rows if r["event"] == "turn"] == ["a", "b", "late"] and rows[-1]["event"] == "ended",
              f"follow from an empty session: every line once, the late one too: {rows}")
        check(rows[0]["cursor"].startswith(jl.stem + ":n"), f"sequence cursor from the start: {rows[0]}")


def test_waits_for_the_next_call():
    """Without --once: `ended`, then the next call; a call that never said it ended is
    closed when a newer one starts."""
    with tempfile.TemporaryDirectory() as d:
        into = Path(d)
        s1 = time.time() - 120
        md1, jl1 = live.session_paths(into, s1)
        live.write_session(md1, jl1, s1, [turn(1.0, "them", "call one", s1, n=0)], ended=True)
        s2 = s1 + 30
        md2, jl2 = live.session_paths(into, s2)
        s3 = s1 + 60
        md3, jl3 = live.session_paths(into, s3)

        def writer():
            time.sleep(0.15)
            live.write_session(md2, jl2, s2, [turn(1.0, "them", "call two", s2, n=0)], ended=False)
            time.sleep(0.15)
            live.write_session(md3, jl3, s3, [turn(1.0, "them", "call three", s3, n=0)], ended=True)
        th = threading.Thread(target=writer)
        th.start()
        buf = io.StringIO()
        rc = live.follow_stream(into, live.turn_filter(), "text", once=False, poll=0.02,
                                deadline=time.monotonic() + 0.8, out=buf)
        th.join()
        out = buf.getvalue().splitlines()
        check(rc == 0 and [ln.split(": ", 1)[1] for ln in out if ln.startswith("[")] == ["call one", "call two", "call three"],
              f"each call in turn: {out}")
        check(out[1] == f"ended {jl1.stem}" and out[3] == f"ended {jl2.stem} (a newer call started)"
              and out[5] == f"ended {jl3.stem}", f"an ended line per call: {out}")


# ---------------------------------------------------------------- same second, midnight
def test_same_second_all_emitted():
    started = time.time() - 300
    for seq in (True, False):
        ents = [turn(7.0, "them", w, started, n=i if seq else None) for i, w in enumerate(("one", "two", "three"))]
        new, _, _ = live.turns_after(ents, "S", None)
        check(len(new) == 3 and len({e["clock"] for e, _ in new}) == 1, f"three lines in one second, all out ({seq})")
        _, after_first = new[0]
        ents.append(turn(7.0, "me", "four", started, n=3 if seq else None))
        rest, _, _ = live.turns_after(ents, "S", live.parse_cursor(after_first))
        check([e["text"] for e, _ in rest] == ["two", "three", "four"],
              f"a cursor inside a second keeps the rest of that second ({seq}): {rest}")
    # bare t: strictly after it
    rest, _, _ = live.turns_after(ents, "S", live.parse_cursor("7"))
    check(rest == [], "since=t: lines at exactly t were already seen")


def test_midnight():
    with tempfile.TemporaryDirectory() as d:
        into = Path(d)
        started = datetime(2026, 11, 8, 23, 59, 50).timestamp()
        md, jl = live.session_paths(into, started)
        ents = [turn(t, "them", f"m{t:g}", started, n=i, voice="v1", name="Bob")
                for i, t in enumerate((4.0, 9.5, 10.0, 10.0, 70.0))]
        live.write_session(md, jl, started, ents, ended=True)
        clocks = [e["clock"] for e in ents]
        check(clocks[0] == "23:59:54" and clocks[-1] == "00:01:00", f"fixture crosses midnight: {clocks}")
        rc, out = run_follow(["--into", d, "--once", "--poll", "0.01"])
        body = [ln for ln in out if ln.startswith("[")]
        check(rc == 0 and [ln.split("] ")[0][1:] for ln in body] == clocks,
              f"every line, in call order, across midnight: {out}")
        check(out[-1].startswith("ended "), f"ended marker: {out}")


# ---------------------------------------------------------------- --once on a call being rewritten
def test_once_ended_marker():
    with tempfile.TemporaryDirectory() as d:
        into = Path(d)
        started = time.time() - 60
        md, jl = live.session_paths(into, started)
        ents = [turn(1.0, "them", "Alice opens", started, n=0, voice="v1", name="Alice"),
                turn(3.0, "them", "Dmitri aside", started, n=1, voice="v3", name="Dmitri"),
                turn(3.0, "me", "my reply", started, n=2, name="Me")]
        live.write_session(md, jl, started, list(ents), ended=False)

        def writer():
            time.sleep(0.15)  # rewrite: a name moves onto line 1, two new lines (one in the same second)
            ents[1]["name"] = "Dmitri"
            ents.append(turn(5.0, "them", "Bob answers", started, n=3, voice="v2", name="Bob"))
            ents.append(turn(5.0, "them", "Alice again", started, n=4, voice="v1", name="Alice"))
            live.write_session(md, jl, started, list(ents), ended=False)
            time.sleep(0.15)
            live.write_session(md, jl, started, list(ents), ended=False)  # rewrite with nothing new
            time.sleep(0.15)
            ents.append(turn(9.0, "them", "Bob closes", started, n=5, voice="v2", name="Bob"))
            live.write_session(md, jl, started, list(ents), ended=True)

        th = threading.Thread(target=writer)
        th.start()
        rc, out = run_follow(["--into", d, "--once", "--poll", "0.02", "--speaker", "A|B", "--exclude-self"])
        th.join()
        texts = [ln.split(": ", 1)[1] for ln in out if ln.startswith("[")]
        check(rc == 0, f"--once exits 0: {rc}")
        check(texts == ["Alice opens", "Bob answers", "Alice again", "Bob closes"],
              f"each matching line exactly once, in order, no replay: {out}")
        check(out[-1].startswith("ended ") and jl.stem in out[-1], f"last line is the ended marker: {out}")
        rc, out = run_follow(["--into", d, "--once", "--poll", "0.01", "--format", "jsonl"])
        rows = [json.loads(ln) for ln in out]
        check(all(r["event"] in ("turn", "ended", "hint", "renamed") for r in rows), f"jsonl events: {rows}")
        check([r["text"] for r in rows if r["event"] == "turn"] ==
              ["Alice opens", "Dmitri aside", "my reply", "Bob answers", "Alice again", "Bob closes"], "jsonl: all six")
        check(rows[-1]["event"] == "ended" and rows[-1]["session"] == jl.stem and rows[-1]["cursor"], f"{rows[-1]}")
        check(all(r.get("cursor") for r in rows), "every jsonl row carries a cursor")


def test_waits_for_a_session_then_ends_stale():
    with tempfile.TemporaryDirectory() as d:
        into = Path(d)
        started = time.time() - 30
        md, jl = live.session_paths(into, started)

        def later():
            time.sleep(0.1)
            live.write_session(md, jl, started, [turn(1.0, "them", "hello there", started, n=0)], ended=False)
        th = threading.Thread(target=later)
        th.start()
        rc, out = run_follow(["--into", d, "--once", "--poll", "0.02", "--stale-seconds", "0.3"])
        th.join()
        check(rc == 0 and out[0].endswith("them: hello there"), f"no session yet: waits for one: {out}")
        check(out[-1].startswith("ended ") and "stale" in out[-1], f"a writer that died ends as stale: {out}")


# ---------------------------------------------------------------- split voice: named + unnamed clusters
def test_split_voice_named_only():
    def unit(*xs):
        v = np.array(xs, dtype=np.float32)
        return v / np.linalg.norm(v)
    known = [("Bob_Builder", "", unit(1, 0, 0)), ("Alice_Ng", "", unit(0, 1, 0)), ("Me", "self", unit(0, 0, 1))]
    v = lv.Voices(known, lambda s: unit(1, 0, 0), me=("Me", "self"))
    # v2 also sounds like Bob (0.74), but not like v1 (0.67, under SIBLING): v1 owns the name
    v.sums = [unit(1, -0.1, 0) * 5, unit(1, 0.9, 0) * 5]
    started = time.time() - 120
    ents = [turn(1.0, "them", "Bob opening point", started, n=0, voice="v1"),
            turn(4.0, "them", "the point I care about", started, n=1, voice="v2"),
            turn(6.0, "me", "my question", started, n=2),
            turn(8.0, "them", "Bob again", started, n=3, voice="v1"),
            turn(9.0, "them", "more from the second cluster", started, n=4, voice="v2"),
            turn(9.5, "them", "too short to place", started, n=5)]
    v.annotate(ents)
    check(ents[0]["name"] == "Bob Builder" and ents[0]["confidence"] >= 0.99, f"named line: confidence: {ents[0]}")
    check("name" not in ents[1] and ents[1]["nearest"] == "Bob Builder" and 0.7 < ents[1]["confidence"] < 0.8,
          f"unnamed line: nearest name and score: {ents[1]}")
    check("confidence" not in ents[2] and ents[2]["name"] == "Me", "me lines: the self name, no score")
    with tempfile.TemporaryDirectory() as d:
        md, jl = live.session_paths(Path(d), started)
        live.write_session(md, jl, started, ents, ended=True)
        rc, out = run_follow(["--into", d, "--once", "--poll", "0.01", "--speaker", "Bob", "--exclude-self"])
        body = [ln for ln in out if ln.startswith("[")]
        check(rc == 0 and len(body) == 5, f"--speaker Bob keeps the unnamed lines: {out}")
        check(sum("? (v2, nearest: Bob Builder 0.74): " in ln for ln in body) == 2,
              f"unnamed lines are marked with voice and nearest name: {body}")
        check(any("? (them): too short to place" in ln for ln in body), f"no voice at all: marked too: {body}")
        hints = [ln for ln in out if "may be" in ln]
        check(hints == ["# v2 may be Bob Builder (0.74)"], f"one hint, once: {out}")
        rc, out = run_follow(["--into", d, "--once", "--poll", "0.01", "--speaker", "Bob", "--named-only"])
        body = [ln for ln in out if ln.startswith("[")]
        check([ln.split(": ", 1)[1] for ln in body] == ["Bob opening point", "Bob again"],
              f"--named-only drops the unnamed lines (and me, unmatched): {body}")
        rc, out = run_follow(["--into", d, "--once", "--poll", "0.01", "--speaker", "Alice"])
        check(not any("may be" in ln for ln in out), "a hint about someone you aren't following is not shown")


def test_rename_note_only_for_lines_shown():
    e = {"t": 1.0, "who": "them", "voice": "v3", "text": "x"}
    hidden = live.Notes(live.turn_filter(named_only=True))
    hidden.see(e, shown=False)
    check(hidden.scan([dict(e, name="Bob")]) == [], "a voice whose lines were filtered out: no `is now` note")
    seen = live.Notes(live.turn_filter())
    seen.see(e, shown=True)
    check(seen.scan([dict(e, name="Bob")]) == [{"event": "renamed", "voice": "v3", "name": "Bob"}],
          "a voice shown as ? that gets a name: one `v3 is now Bob` note")
    check(seen.scan([dict(e, name="Bob")]) == [], "...only once")
    with tempfile.TemporaryDirectory() as d:  # its cursor is the one before the new lines
        started = time.time() - 60
        md, jl = live.session_paths(Path(d), started)
        live.write_session(md, jl, started, [turn(1.0, "them", "x", started, n=0, voice="v3")], ended=False)

        def writer():
            time.sleep(0.1)
            live.write_session(md, jl, started, [turn(1.0, "them", "x", started, n=0, voice="v3", name="Bob"),
                                                 turn(2.0, "them", "y", started, n=1, voice="v3", name="Bob")], True)
        th = threading.Thread(target=writer)
        th.start()
        buf = io.StringIO()
        live.follow_stream(Path(d), live.turn_filter(), "jsonl", once=True, poll=0.02, out=buf)
        th.join()
        rows = [json.loads(ln) for ln in buf.getvalue().splitlines()]
        ren = [r for r in rows if r["event"] == "renamed"]
        check(len(ren) == 1 and ren[0]["cursor"] == f"{jl.stem}:n0" and rows.index(ren[0]) == 1,
              f"the rename note comes before the new line, with the cursor before it: {rows}")


# ---------------------------------------------------------------- handoff inside one segment
def test_handoff_in_one_segment():
    def unit(*xs):
        v = np.array(xs, dtype=np.float32)
        return v / np.linalg.norm(v)
    dirs = {1: unit(1, 0, 0), 2: unit(0, 1, 0)}

    def embed(samples):  # the voice most of the window carries
        return dirs[int(round(float(np.mean(samples))))]

    known = [("Alice_Ng", "", unit(1, 0.05, 0)), ("Bob_Builder", "", unit(0.05, 1, 0))]
    rate = lv.RATE
    samples = np.concatenate([np.full(3 * rate, 1.0, np.float32), np.full(3 * rate, 2.0, np.float32)])
    words = [{"start": 0.0, "end": 1.0, "word": " so"}, {"start": 1.0, "end": 2.0, "word": " that's"},
             {"start": 2.0, "end": 3.0, "word": " mine."}, {"start": 3.1, "end": 4.5, "word": " Right,"},
             {"start": 4.5, "end": 6.0, "word": " agreed."}]
    seg = {"start": 0.0, "end": 6.0, "text": " so that's mine. Right, agreed."}

    v = lv.Voices(known, embed)
    got = live.transcribe_chunks({"them": (0, samples)}, lambda s: [dict(seg, words=words)], [], v)
    v.annotate(got)
    check(len(got) == 2, f"a speaker change inside a segment with word times: two lines: {got}")
    check([e["text"] for e in got] == ["so that's mine.", "Right, agreed."], f"split at the change: {got}")
    check([e.get("name") for e in got] == ["Alice Ng", "Bob Builder"], f"each half gets its own voice: {got}")
    check(got[1]["t"] == 3.1, f"the second line starts at its first word: {got[1]}")

    v = lv.Voices(known, embed)
    got = live.transcribe_chunks({"them": (0, samples)}, lambda s: [dict(seg)], [], v)
    v.annotate(got)
    check(len(got) == 1 and got[0]["mixed"] is True and got[0]["change_at"] == 3.0,
          f"no word times: one line, flagged mixed, with the change point: {got}")
    with tempfile.TemporaryDirectory() as d:
        started = time.time() - 60
        got[0]["clock"] = datetime.fromtimestamp(started).strftime("%H:%M:%S")
        got[0]["n"] = 0
        md, jl = live.session_paths(Path(d), started)
        live.write_session(md, jl, started, got, ended=True)
        _, out = run_follow(["--into", d, "--once", "--poll", "0.01"])
        change = datetime.fromtimestamp(started + 3).strftime("%H:%M:%S")
        check(f"(mixed, change at {change})" in out[0], f"the text line shows mixed + change point: {out}")

    # one voice throughout: no change, one line, not mixed
    v = lv.Voices(known, embed)
    same = np.full(6 * rate, 1.0, np.float32)
    got = live.transcribe_chunks({"them": (0, same)}, lambda s: [dict(seg, words=words)], [], v)
    v.annotate(got)
    check(len(got) == 1 and not got[0].get("mixed") and got[0]["name"] == "Alice Ng", f"one voice: one line: {got}")

    # a change the windows see but both halves join the same running voice: kept whole, not mixed
    class SameVoice(lv.Voices):
        def change_point(self, samples):
            return 3.0
    v = SameVoice(known, embed)
    got = live.transcribe_chunks({"them": (0, same)}, lambda s: [dict(seg, words=words)], [], v)
    v.annotate(got)
    check(len(got) == 1 and got[0]["text"] == seg["text"].strip() and not got[0].get("mixed"),
          f"a false change (one voice both sides) is merged back: {got}")


for name, fn in list(globals().items()):
    if name.startswith("test_") and callable(fn):
        fn()
print(f"PASS: live_follow_test ({CHECKS} checks)")
