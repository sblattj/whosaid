#!/usr/bin/env python3
"""
Offline test for the `realistic` meeting planner (GitHub issue #80,
test/diarize_eval/realistic_plan.py). No audio is mixed; the pool is a tiny
synthetic one (noise bursts separated by silences, written with numpy + wave).

Covers: phrase_bank splits a 3-burst clip into 3 phrases (and caches it, keyed
by size + mtime); a 40-min / 8-speaker plan hits the issue's turn-length table
(+-6 pp), switches/min, overlap, top-speaker share, one local speaker, gap-free
epochs, determinism, and no same-speaker overlapping turns. Finally plans against
the REAL pool (read-only) for 3 seeds and prints plan_stats (no asserts).

Run:
    uv run --with numpy --with mcp python test/realistic_plan_test.py
"""
import json
import os
import random
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "test" / "diarize_eval"))
import realistic_plan as rp  # noqa: E402

SR = 16000


def write_wav(path, bursts, gap=0.4, seed=0):
    """bursts: list of burst durations (s) separated by `gap` s of digital silence,
    with 0.2 s lead/trail silence."""
    r = np.random.RandomState(seed)
    parts = [np.zeros(int(0.2 * SR))]
    for i, b in enumerate(bursts):
        if i:
            parts.append(np.zeros(int(gap * SR)))
        parts.append(r.randint(-8000, 8000, int(b * SR)).astype(np.int16).astype(np.float64))
    parts.append(np.zeros(int(0.2 * SR)))
    x = np.concatenate(parts).astype(np.int16)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(x.tobytes())
    return len(x) / SR


def build_pool(root, nvoices=12):
    voices = {}
    for i in range(nvoices):
        vid = "v%02d" % i
        clips = []
        specs = ([("short", [0.5 + 0.1 * (i % 3)])] * 2
                 + [("medium", [1.0, 0.9, 1.1, 0.8])] * 3
                 + [("long", [1.2, 0.9, 1.4, 1.0, 1.3, 0.8, 1.1, 1.2, 0.9, 1.0, 1.3, 1.1])] * 3)
        for j, (kind, bursts) in enumerate(specs):
            rel = "clips/%s/L%03d.wav" % (vid, j)
            d = write_wav(os.path.join(root, rel), bursts, seed=i * 100 + j)
            clips.append({"line_id": "L%03d" % j, "kind": kind, "text": "x", "path": rel,
                          "dur": round(d, 2)})
        voices[vid] = {"name": "Voice %d" % i, "vendor": "synthetic", "gender": "male",
                       "accent": "american", "clips": clips}
    return voices


def check_bank(root, voices):
    one = os.path.join(root, "one")
    write_wav(os.path.join(one, "c.wav"), [0.6, 0.7, 0.5], gap=0.4)
    v = {"x": {"clips": [{"line_id": "A", "kind": "medium", "text": "", "path": "c.wav", "dur": 3}]}}
    cache = os.path.join(root, "cache.json")
    b = rp.phrase_bank(one, v, cache)
    ph = b["x"]
    assert len(ph) == 3, ph
    for p, want in zip(ph, [0.6, 0.7, 0.5]):
        assert abs(p["dur"] - want) < 0.15, (p, want)
    assert ph[0]["t0"] < ph[0]["t1"] <= ph[1]["t0"] < ph[1]["t1"] <= ph[2]["t0"], ph
    assert os.path.exists(cache)
    assert rp.phrase_bank(one, v, cache) == b  # cache hit gives the same bank
    # changing the file invalidates the cache entry
    write_wav(os.path.join(one, "c.wav"), [0.6, 0.7], gap=0.4)
    assert len(rp.phrase_bank(one, v, cache)["x"]) == 2
    # an unsplittable clip stays whole
    write_wav(os.path.join(one, "c.wav"), [1.5])
    assert len(rp.phrase_bank(one, v, None)["x"]) == 1
    # a sub-180 ms dip does NOT split
    write_wav(os.path.join(one, "c.wav"), [0.6, 0.6], gap=0.12)
    assert len(rp.phrase_bank(one, v, None)["x"]) == 1
    print("phrase_bank: ok")


def check_plan(voices, bank):
    plan = rp.plan_meeting(random.Random(7), voices, bank, minutes=(40, 40), speakers=(8, 8),
                           mid="m7-000")
    st = rp.plan_stats(plan)
    print("synthetic 40-min plan stats:", json.dumps(st, indent=1))
    assert plan["schema"] == 1 and plan["id"] == "m7-000" and plan["seed"] == 7
    assert plan["tags"][:2] == ["realistic", "n8"], plan["tags"]
    assert len(plan["speakers"]) == 8
    assert 38 * 60 < plan["duration"] < 43 * 60, plan["duration"]
    want = dict(zip(["<1", "1-2", "2-4", "4-8", ">8"], rp.BIN_TARGETS))
    for k, w in want.items():
        got = st["turn_table"][k]["turns"]
        assert abs(got - w) <= 0.06, (k, got, w)
    assert 4 <= st["switches_per_min"] <= 7.5, st["switches_per_min"]
    assert 0.01 <= st["overlap_frac"] <= 0.09, st["overlap_frac"]
    assert 0.25 <= st["top_share"] <= 0.75, st["top_share"]
    assert 0.5 <= st["gap_median"] <= 1.0, st["gap_median"]
    locs = [s for s in plan["speakers"] if s["channel"] == "local"]
    assert len(locs) == 1 and all(s["channel"] in ("local", "remote") for s in plan["speakers"])
    # epochs cover [0, duration] per speaker without gaps
    for s in plan["speakers"]:
        eps = plan["epochs"][s["voice"]]
        assert eps[0]["start"] == 0 and abs(eps[-1]["end"] - plan["duration"]) < 1e-6
        for a, b in zip(eps, eps[1:]):
            assert abs(a["end"] - b["start"]) < 1e-9, (a, b)
        for e in eps:
            assert 0.94 <= e["tempo"] <= 1.06 and -3 <= e["gain_db"] <= 3
            assert isinstance(e["drift_seed"], int)
            assert e["end"] > e["start"]
        assert all(300 - 1e-6 <= e["end"] - e["start"] <= 1200 + 300 for e in eps[:-1] + eps[-1:]) \
            or len(eps) == 1
    # turns sorted, in range, pieces consistent, no same-speaker overlap
    turns = plan["turns"]
    assert [t["start"] for t in turns] == sorted(t["start"] for t in turns)
    assert turns[0]["start"] >= 0.5 - 1e-9 and turns[-1]["end"] <= plan["duration"] - 0.5 + 1e-3
    byspk = {}
    for t in turns:
        assert t["kind"] in ("turn", "backchannel") and t["end"] > t["start"] and t["pieces"]
        byspk.setdefault(t["speaker"], []).append(t)
        ep = plan["epochs"][t["speaker"]]
        last = None
        for pc in t["pieces"]:
            assert pc["t1"] > pc["t0"] and pc["at"] >= -1e-9
            tm = t["start"] + pc["at"]
            f = [e for e in ep if e["start"] <= tm < e["end"] + 1e-9][0]["tempo"]
            if last is not None:
                assert pc["at"] >= last - 1e-3, (pc, last)
            last = pc["at"] + (pc["t1"] - pc["t0"]) / f
        assert abs(t["start"] + last - t["end"]) < 0.02, (t["start"] + last, t["end"])
    for v, lst in byspk.items():
        lst.sort(key=lambda t: t["start"])
        for a, b in zip(lst, lst[1:]):
            assert b["start"] >= a["end"] - 1e-9, (v, a["start"], a["end"], b["start"])
    # every piece points at a real bank phrase
    ok = {(p["clip"], p["t0"], p["t1"]) for v in bank for p in bank[v]}
    assert all((pc["clip"], pc["t0"], pc["t1"]) in ok for t in turns for pc in t["pieces"])
    # determinism
    again = rp.plan_meeting(random.Random(7), voices, bank, minutes=(40, 40), speakers=(8, 8),
                            mid="m7-000")
    assert json.dumps(plan, sort_keys=True) == json.dumps(again, sort_keys=True)
    other = rp.plan_meeting(random.Random(8), voices, bank, minutes=(40, 40), speakers=(8, 8),
                            mid="m8-000")
    assert json.dumps(plan, sort_keys=True) != json.dumps(other, sort_keys=True)
    # JSON round-trips
    assert json.loads(json.dumps(plan)) == plan
    print("plan_meeting: ok")


def check_voice_choice(voices, bank):
    # roster preference + capped n
    v2 = {k: dict(v) for k, v in voices.items()}
    for k in ("v00", "v01", "v02"):
        v2[k]["roster"] = "teamA"
    p = rp.plan_meeting(random.Random(1), v2, bank, minutes=(5, 5), speakers=(6, 6), roster="teamA",
                        mid="m1-000")
    got = {s["voice"] for s in p["speakers"]}
    assert {"v00", "v01", "v02"} <= got and len(got) == 6, got
    try:  # a roster tag no voice carries is an error, not a plain meeting
        rp.plan_meeting(random.Random(1), v2, bank, minutes=(5, 5), speakers=(6, 6),
                        roster="teamZ", mid="m1-002")
        raise AssertionError("unknown roster was accepted")
    except ValueError as e:
        assert "teamZ" in str(e), e
    p = rp.plan_meeting(random.Random(1), voices, bank, minutes=(5, 5), speakers=(20, 20),
                        mid="m1-001")
    assert len(p["speakers"]) == 12 and "capped-n" in p["tags"] and "n12" in p["tags"], p["tags"]
    print("voice choice: ok")


def check_blends(voices, bank):
    """--blends: crosstalk episodes appear, overlap, are tagged, and blends=0 is unchanged."""
    kw = dict(minutes=(10, 10), speakers=(4, 4), mid="m5-000")
    base = rp.plan_meeting(random.Random(5), voices, bank, **kw)
    zero = rp.plan_meeting(random.Random(5), voices, bank, blends=0, **kw)
    assert json.dumps(base, sort_keys=True) == json.dumps(zero, sort_keys=True), "blends=0 changed the plan"
    assert not any(t["kind"] == "crosstalk" for t in base["turns"])
    p = rp.plan_meeting(random.Random(5), voices, bank, blends=2, **kw)
    ct = [t for t in p["turns"] if t["kind"] == "crosstalk"]
    assert "blend2" in p["tags"], p["tags"]
    assert len({t["speaker"] for t in ct}) >= 2, ct[:3]
    assert len(ct) >= 10, len(ct)
    assert all(t["end"] - t["start"] <= 2.6 + 1e-6 for t in ct), max(t["end"] - t["start"] for t in ct)
    ov = sum(1 for a, b in zip(ct, ct[1:]) if b["speaker"] != a["speaker"] and b["start"] < a["end"])
    assert ov >= len(ct) // 2, (ov, len(ct))
    for v in {t["speaker"] for t in p["turns"]}:  # a voice never overlaps itself
        mine = sorted((t["start"], t["end"]) for t in p["turns"] if t["speaker"] == v)
        assert all(b[0] >= a[1] for a, b in zip(mine, mine[1:])), v
    print("  blends: %d crosstalk turns, %d overlapping hand-offs" % (len(ct), ov))


def real_pool():
    pj = os.path.expanduser("~/.cache/whosaid/diarize-eval/pool.json")
    if not os.path.exists(pj):
        print("real pool not found; skipped")
        return
    voices = json.load(open(pj))["voices"]
    root = os.path.dirname(pj)
    cache = os.path.join(tempfile.gettempdir(), "realistic_plan_test_bank_cache.json")
    bank = rp.phrase_bank(root, voices, cache)
    print("real pool: %d voices, %d phrases" % (len(bank), sum(len(b) for b in bank.values())))
    for seed in (1, 2, 3):
        plan = rp.plan_meeting(random.Random(seed), voices, bank, mid="m%d-000" % seed)
        st = rp.plan_stats(plan)
        tt = " ".join("%s:%.0f%%" % (k, 100 * v["turns"]) for k, v in st["turn_table"].items())
        print("real seed %d: n=%d %.0f min turns=%d | %s | sw/min=%.2f gap_med=%.2f gap<.3=%.0f%% "
              "overlap=%.1f%% top=%.0f%% under2min/h=%d" % (
                  seed, st["n_speakers"], st["minutes"], st["n_turns"], tt, st["switches_per_min"],
                  st["gap_median"], 100 * st["gap_lt_0_3"], 100 * st["overlap_frac"],
                  100 * st["top_share"], st["under_2min_per_hour"]))


def main():
    with tempfile.TemporaryDirectory() as td:
        check_bank(td, None)
        pool = os.path.join(td, "pool")
        voices = build_pool(pool)
        bank = rp.phrase_bank(pool, voices, os.path.join(td, "bank.json"))
        assert all(len(b) >= 20 for b in bank.values()), {k: len(v) for k, v in bank.items()}
        check_plan(voices, bank)
        check_voice_choice(voices, bank)
        check_blends(voices, bank)
    real_pool()
    print("realistic_plan_test: ALL PASS")


if __name__ == "__main__":
    main()
