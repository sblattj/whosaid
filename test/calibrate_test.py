#!/usr/bin/env python3
"""
Offline test for test/diarize_eval/calibrate.py (issue #80 calibration check).

Builds a temp meetings dir + run dir with two hand-computed meetings and checks
every computed number, that real_stats.json carries every key calibrate.py
reads, and the +-20% flag logic. The CLI is run for real (--json) at the end.

Run:
    uv run --with numpy --with mcp python test/calibrate_test.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_DIR / "test" / "diarize_eval"
sys.path.insert(0, str(EVAL_DIR))

import calibrate as cal  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    assert cond, msg
    CHECKS += 1


def near(a, b, msg, tol=1e-6):
    check(a is not None and abs(a - b) <= tol, "%s: got %r want %r" % (msg, a, b))


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def turn(spk, a, b):
    return {"speaker": spk, "start": a, "end": b, "text": "x"}


def seg(spk, a, b):
    return {"start": a, "end": b, "speaker": spk}


def rec(cluster, name, sim, passname, matched, **kw):
    r = {"cluster": cluster, "name": name, "similarity": sim, "threshold": 0.5,
         "matched": matched, "pass": passname}
    r.update(kw)
    return r


def build(tmp):
    meet, run = tmp / "meetings", tmp / "run"
    # m1: 60 s. truth X 0-10, Y 10.5-14, X 14-20, X 20-25, Z 30-36, X 40-44, Y 44-48
    write(meet / "m1" / "truth.json", {"schema": 1, "id": "m1", "duration": 60.0, "turns": [
        turn("X", 0, 10), turn("Y", 10.5, 14), turn("X", 14, 20), turn("X", 20, 25),
        turn("Z", 30, 36), turn("X", 40, 44), turn("Y", 44, 48)]})
    s3_absorb_turns = [{"start": 20, "end": 23, "score": 0.8, "best": "X"},
                       {"start": 23, "end": 24, "score": 0.5, "best": "X"},
                       {"start": 24, "end": 25, "score": 0.9, "best": "X"}]
    write(run / "m1" / "refs" / "m1_refs.diarization.json", {
        "num_speakers": 5, "uncounted": [], "source": {"duration_seconds": 60.0},
        "segments": [seg("S0", 0, 10), seg("S1", 10.5, 14), seg("S0", 13, 20),
                     seg("S3", 20, 25), seg("S2", 30, 36), seg("S4", 40, 48)],
        "registry_matches": [
            rec("S0", "X", 0.85, "ref", True), rec("S0", "Y", 0.20, "ref", False),
            rec("S1", "Y", 0.80, "ref", True), rec("S1", "X", 0.25, "ref", False),
            rec("S3", "X", 0.76, "ref", False),
            rec("S3", "X", 0.78, "absorb", False, purity=0.31, turns=s3_absorb_turns),
            rec("S4", "X", 0.72, "ref", False), rec("S4", "Y", 0.81, "ref", False),
            rec("S2", "X", 0.30, "ref", False), rec("S2", "Y", 0.20, "ref", False)]})
    # m2: 120 s. truth X 0-30 only; enrolment known from the <mode>-refs dir listing
    write(meet / "m2" / "truth.json", {"schema": 1, "id": "m2", "duration": 120.0,
                                       "turns": [turn("X", 0, 30)]})
    write(run / "m2" / "refs" / "m2_refs.diarization.json", {
        "num_speakers": 1, "source": {"duration_seconds": 120.0},
        "segments": [seg("S0", 0, 30)],
        "registry_matches": [rec("S0", "X", 0.90, "registry", True)]})
    (run / "m2" / "refs-refs").mkdir(parents=True)
    (run / "m2" / "refs-refs" / "X.wav").write_bytes(b"")
    return meet, run


def test_stats(meet, run, real):
    ms = cal.load_meetings(str(meet), str(run), "refs")
    check([m[0] for m in ms] == ["m1", "m2"], "loads both meetings")
    check(ms[0][3] == {"X", "Y"}, "m1 enrolled from pass:ref records: %r" % (ms[0][3],))
    check(ms[1][3] == {"X"}, "m2 enrolled from refs-refs dir")
    sd, st, rows = cal.compute(ms, real)

    # --- per-meeting numbers, m1 alone
    edges = real["turn_table"]["edges_s"]
    m1 = cal.seg_stats(ms[0][2]["segments"], 1.0, edges, 5)
    near(m1["switches_per_min"], 5.0, "m1 switches/min")
    check(m1["gaps"] == [0.5, 0.0, 0.0, 5.0, 4.0], "m1 gaps %r" % (m1["gaps"],))
    near(m1["overlap_share"], 1.0 / 38.5, "m1 overlap (1 s of 38.5 s)")
    near(m1["top_share"], 17.0 / 39.5, "m1 top share")
    check(m1["bins_n"] == [0, 0, 1, 3, 2], "m1 turn bins %r" % (m1["bins_n"],))
    near(m1["bins_t"][3], 18.0, "m1 talk in 4-8 s bin (7+5+6)")
    check(m1["under_2min_per_hour"] == 0, "m1 no speaker under 2 min/hour")

    # --- aggregates over both meetings
    near(sd["meeting_minutes"]["value"], 1.5, "minutes median of 1 and 2")
    near(sd["switches_per_min"]["value"], 2.5, "switches median of 5 and 0")
    near(sd["switches_per_min"]["min"], 0.0, "switches min")
    near(sd["switches_per_min"]["max"], 5.0, "switches max")
    near(sd["speakers_per_meeting"]["value"], 3.0, "speakers median of 5 and 1")
    near(sd["top_speaker_share"]["value"], (17.0 / 39.5 + 1.0) / 2, "top share median")
    near(sd["overlap_share"]["max"], 1.0 / 38.5, "overlap max")
    near(sd["overlap_share"]["min"], 0.0, "overlap min")
    near(sd["gap_median_s"]["value"], 0.5, "pooled gap median")
    near(sd["gap_share_under"]["value"], 0.4, "pooled gap share under 0.3 s")
    near(sd["turn_share_2"]["value"], 1 / 7.0, "pooled 2-4 s share of turns")
    near(sd["turn_share_3"]["value"], 3 / 7.0, "pooled 4-8 s share of turns")
    near(sd["turn_share_4"]["value"], 3 / 7.0, "pooled >8 s share of turns")
    near(sd["talk_share_4"]["value"], (18.0 + 30.0) / (39.5 + 30.0), "pooled >8 s share of talk")
    # --- truth side (reference column)
    near(st["switches_per_min"]["min"], 0.0, "truth m2 switches")
    near(st["switches_per_min"]["max"], 5.0, "truth m1 switches")
    near(st["overlap_share"]["max"], 0.0, "truth m1 has no overlap")
    near(st["turn_share_3"]["value"], 5 / 8.0, "truth pooled 4-8 s share of turns")

    # --- embedding categories
    cats = {(r["meeting"], r["cluster"]): r for r in rows}
    check(cats[("m1", "S0")]["category"] == "correct", "S0 correct pass-1")
    check(cats[("m1", "S1")]["category"] == "correct", "S1 correct pass-1")
    check(cats[("m2", "S0")]["category"] == "correct", "registry pass counts as pass-1")
    check(cats[("m1", "S3")]["category"] == "over_split", "S3 is the over-split second cluster")
    check(cats[("m1", "S4")]["category"] == "blend", "S4 is a blend")
    check(cats[("m1", "S2")]["category"] == "unenrolled", "S2 is unenrolled")
    near(cats[("m1", "S0")]["purity_time"], 16.0 / 17.0, "S0 purity_time")
    near(cats[("m1", "S4")]["purity_time"], 0.5, "S4 purity_time")
    near(cats[("m1", "S3")]["similarity"], 0.78, "over-split best = max over records naming X")
    near(cats[("m1", "S3")]["purity"], 0.31, "recorded purity carried")
    near(cats[("m1", "S3")]["purity_tw"], 0.8, "time-weighted purity from turns (4 s of 5 s >= 0.70)")
    check(cats[("m1", "S4")]["candidate"] == "Y", "blend candidate is the best-scoring name")
    near(cats[("m1", "S4")]["similarity"], 0.81, "blend best similarity")
    near(cats[("m1", "S2")]["similarity"], 0.30, "unenrolled best similarity")
    check(len(rows) == 6, "six classified clusters, got %d" % len(rows))
    near(sd["emb_correct_pass1"]["value"], 0.85, "correct median of .85 .80 .90")
    near(sd["emb_correct_pass1"]["min"], 0.80, "correct min")
    near(sd["emb_correct_pass1"]["max"], 0.90, "correct max")
    check(sd["emb_correct_pass1"]["n"] == 3, "correct n")
    near(sd["emb_over_split"]["value"], 0.78, "over-split median")
    near(sd["emb_blend"]["value"], 0.81, "blend median")
    near(sd["emb_unenrolled"]["value"], 0.30, "unenrolled median")
    near(sd["emb_purity_split"]["value"], 0.31, "purity split stat")
    near(sd["emb_purity_tw_split"]["value"], 0.8, "time-weighted purity stat")
    check(sd["emb_purity_blend"]["value"] is None, "no purity recorded on the blend: tolerated")
    return sd, st


def test_no_turns_tolerated(real):
    """Records without `turns`/`purity` must not break classification."""
    truth = {"turns": [turn("X", 0, 10), turn("X", 20, 30)]}
    doc = {"segments": [seg("S0", 0, 10), seg("S1", 20, 30)],
           "registry_matches": [rec("S0", "X", 0.9, "ref", True), rec("S1", "X", 0.7, "absorb", False)]}
    rows = cal.classify_clusters(truth, doc, {"X"})
    check({r["cluster"]: r["category"] for r in rows} == {"S0": "correct", "S1": "over_split"},
          "categories without turns/purity: %r" % rows)
    check(all(r["purity"] is None and r["purity_tw"] is None for r in rows), "no purity invented")


def test_real_stats(real):
    check(real["source"] == "sblattj/whosaid#80 issue body", "source note")
    near(real["tolerance"], 0.2, "tolerance")
    tt = real["turn_table"]
    check(tt["share_of_turns"] == [0.26, 0.18, 0.21, 0.21, 0.14], "turn shares")
    check(tt["share_of_talk"] == [0.04, 0.06, 0.14, 0.29, 0.47], "talk shares")
    near(sum(tt["share_of_turns"]), 1.0, "turn shares sum")
    near(sum(tt["share_of_talk"]), 1.0, "talk shares sum")
    check(real["switches_per_min"] == {"min": 2.2, "max": 8.4, "median": 5.7}, "switches")
    check(real["gap"]["median_s"] == 0.74 and real["gap"]["share_under_0.3s"] == 0.26, "gap")
    check(real["overlap_share"] == {"min": 0.01, "max": 0.08, "median": 0.04}, "overlap")
    check(real["top_speaker_share"] == {"min": 0.25, "max": 0.75, "median": 0.41}, "top share")
    check(real["meeting_minutes"] == {"min": 5, "max": 121, "median": 44}, "minutes")
    check((real["speakers_per_meeting"]["min"], real["speakers_per_meeting"]["max"]) == (6, 10), "speakers")
    e = real["embedding"]
    check((e["correct_pass1"]["min"], e["correct_pass1"]["max"]) == (0.72, 0.89), "pass-1 range")
    check((e["over_split"]["min"], e["over_split"]["max"]) == (0.70, 0.83), "over-split range")
    check(e["blend"]["values"] == [0.75, 0.84], "blend values")
    check((e["unenrolled"]["min"], e["unenrolled"]["max"]) == (0.17, 0.45), "unenrolled range")
    check((e["purity_split"]["min"], e["purity_split"]["max"]) == (0.03, 0.35), "split purity")
    check((e["purity_blend"]["min"], e["purity_blend"]["max"]) == (0.02, 0.09), "blend purity")
    # every metric calibrate.py reports resolves against the file (time-weighted purity has no real value)
    for label, key, kind in cal.metric_list(real):
        r = cal.real_stat(real, key)
        if key.startswith("emb_purity_tw_"):
            check(r is None, key + " has no real counterpart")
        else:
            check(r is not None, "real_stat resolves %s" % key)


def test_flag_logic():
    s = cal.scalar
    check(cal.within(s(1.0), s(1.0)) == "yes", "equal")
    check(cal.within(s(0.8), s(1.0)) == "yes", "-20% edge is inside")
    check(cal.within(s(1.2), s(1.0)) == "yes", "+20% edge is inside")
    check(cal.within(s(0.79), s(1.0)) == "NO", "-21% flagged")
    check(cal.within(s(1.21), s(1.0)) == "NO", "+21% flagged")
    check(cal.within(s(0.05), s(0.04)) == "NO", "25% over a small value flagged")
    check(cal.within(s(0.0), s(0.0)) == "yes", "zero vs zero")
    check(cal.within(s(0.1), s(0.0)) == "NO", "nonzero vs zero")
    rng = {"value": None, "min": 0.72, "max": 0.89, "n": None}
    check(cal.within(s(0.80), rng) == "yes", "inside range-only real")
    check(cal.within(s(0.90), rng) == "NO", "above range-only real")
    check(cal.within(s(0.50), rng) == "NO", "below range-only real")
    withmed = {"value": 5.7, "min": 2.2, "max": 8.4, "n": None}
    check(cal.within(s(6.5), withmed) == "yes", "median compare: 6.5 vs 5.7 within 20%")
    check(cal.within(s(7.0), withmed) == "NO", "median compare: 7.0 vs 5.7 flagged even though in range")
    check(cal.within(s(None), withmed) == "n/a", "no synthetic data")
    check(cal.within(s(1.0), None) == "n/a", "no real counterpart")
    check(cal.within(s(1.0), {"value": None, "min": None, "max": None}) == "n/a", "empty real")


def test_cli(meet, run, tmp):
    out = tmp / "out.json"
    p = subprocess.run([sys.executable, str(EVAL_DIR / "calibrate.py"), "--meetings", str(meet),
                        "--run", str(run), "--mode", "refs", "--json", str(out)],
                       capture_output=True, text=True)
    check(p.returncode == 0, "cli rc=%d %s" % (p.returncode, p.stderr))
    check("| metric | synthetic (diarizer) | synthetic (truth) | real | within +-20%? |" in p.stdout,
          "markdown header")
    check("| speaker switches / min | 2.50 (0.00-5.00) |" in p.stdout, "switches row text: " + p.stdout)
    data = json.loads(out.read_text())
    byk = {r["key"]: r for r in data["rows"]}
    near(byk["switches_per_min"]["synthetic_diarizer"]["value"], 2.5, "json switches median")
    check(byk["switches_per_min"]["within"] == "NO", "2.5 vs real 5.7 flagged in json")
    check(byk["emb_correct_pass1"]["within"] == "yes", "0.85 inside real 0.72-0.89")
    check(data["meetings"] == ["m1", "m2"], "json meetings")
    check(len(data["clusters"]) == 6, "json clusters")
    p2 = subprocess.run([sys.executable, str(EVAL_DIR / "calibrate.py"), "--meetings", str(meet),
                         "--run", str(run), "--mode", "nope"], capture_output=True, text=True)
    check(p2.returncode == 2, "no matching sidecars -> rc 2")


def main():
    real = json.loads((EVAL_DIR / "real_stats.json").read_text())
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        meet, run = build(tmp)
        test_stats(meet, run, real)
        test_no_turns_tolerated(real)
        test_real_stats(real)
        test_flag_logic()
        test_cli(meet, run, tmp)
    print("calibrate_test: %d checks passed" % CHECKS)


if __name__ == "__main__":
    main()
