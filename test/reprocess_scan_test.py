#!/usr/bin/env python3
"""
Offline test for lib/reprocess.py: the pre-1.10 detector (scan_sidecar,
scan_workspace, the `scan` CLI) and the label carry-over (map_local_labels,
the `map-labels` CLI). Synthetic sidecar dicts only; no models, no audio.

Run:
    uv run --quiet python test/reprocess_scan_test.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))
import reprocess  # noqa: E402

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def seg(start, end, sp):
    return {"start": start, "end": end, "speaker": sp}


def sidecar(mode="auto-detected", n=2, dur=1800.0, ce="none", segments=None, matches=None):
    if ce == "none":
        ce = {"method": "gap", "k": n, "raw_k": n, "saturated": False}
    return {"detect_mode": mode, "num_speakers": n, "names": {}, "local_labels": {},
            "registry_matches": matches or [], "source": {"path": "x.wav",
            "duration_seconds": dur, "creation_time": None},
            "count_estimate": ce, "segments": segments or [seg(0, 30, "SPEAKER_00"),
                                                           seg(30, 60, "SPEAKER_01")]}


def codes(data):
    return [f["code"] for f in reprocess.scan_sidecar(data)]


# ---- brief-fragments
frag = [seg(0, 30, "SPEAKER_00"), seg(30, 60, "SPEAKER_01"),
        seg(60, 61, "SPEAKER_02"), seg(70, 71.5, "SPEAKER_02")]
d = sidecar(n=3, segments=frag)
fs = reprocess.scan_sidecar(d)
check([f["code"] for f in fs] == ["brief-fragments"], f"brief fires: {fs}")
check(fs[0]["confidence"] == "likely", "brief is likely")
check("SPEAKER_02" in fs[0]["reason"] and "2.5s" in fs[0]["reason"], fs[0]["reason"])
d = sidecar(n=3, segments=frag, ce={"method": "gap", "k": 3, "brief_fold": {"folded": []}})
check(codes(d) == [], "brief_fold present (v1.10) -> no brief-fragments")
d = sidecar(n=3, segments=frag, mode="as hinted (--num-speakers 3)")
check(codes(d) == [], "hinted count -> no brief-fragments")
d = sidecar(n=3, segments=frag[:3] + [seg(60, 63, "SPEAKER_02")])
check(codes(d) == [], "cluster with a 3s turn is substantive")
d = sidecar(n=3, segments=frag + [seg(80, 83, "SPEAKER_02")])
check(codes(d) == [], "one long turn rescues the cluster (ALL turns must be brief)")

# ---- short-hinted-undercount
d = sidecar(mode="as hinted (--num-speakers 4)", n=3, dur=300, ce=None)
fs = reprocess.scan_sidecar(d)
check([f["code"] for f in fs] == ["short-hinted-undercount"], f"hinted undercount: {fs}")
check(fs[0]["confidence"] == "likely" and "hinted 4" in fs[0]["reason"]
      and "got 3" in fs[0]["reason"], fs[0]["reason"])
check(codes(sidecar(mode="as hinted (--num-speakers 3)", n=3, dur=300, ce=None)) == [],
      "hinted N met -> nothing")
check(codes(sidecar(mode="as hinted (--num-speakers 4)", n=3, dur=3000, ce=None)) == [],
      "long audio hinted -> nothing")
check(codes(sidecar(mode="as hinted (--num-speakers 4), registry-anchored", n=3, dur=300,
                    ce=None)) == ["short-hinted-undercount"], "suffix tolerated")

# ---- short-whole-file
fs = reprocess.scan_sidecar(sidecar(dur=300, ce=None))
check([f["code"] for f in fs] == ["short-whole-file"], f"whole-file: {fs}")
check(fs[0]["confidence"] == "possible" and "--no-chunk" in fs[0]["reason"], fs[0]["reason"])
check(codes(sidecar(dur=3000, ce=None)) == [], "long audio, null estimate -> nothing")
check(codes(sidecar(dur=300)) == [], "short audio with estimate -> nothing")
check(codes(sidecar(dur=899.9, ce=None)) == ["short-whole-file"], "899.9 s is short")
check(codes(sidecar(dur=900.0, ce=None)) == [], "900 s is not short")

# ---- ref-order
def rm(cluster, name, matched, which="ref"):
    return {"cluster": cluster, "name": name, "similarity": 0.8, "threshold": 0.5,
            "matched": matched, "pass": which}


two_refs = [rm("SPEAKER_01", "Jessica", True), rm("SPEAKER_00", "Marin", True)]
fs = reprocess.scan_sidecar(sidecar(matches=two_refs))
check([f["code"] for f in fs] == ["ref-order"] and fs[0]["confidence"] == "possible",
      f"ref-order: {fs}")
check(codes(sidecar(matches=[rm("SPEAKER_01", "Jessica", True)])) == [], "one ref -> nothing")
check(codes(sidecar(matches=[rm("SPEAKER_01", "Jessica", False), rm("SPEAKER_00", "Marin", False)]))
      == [], "no ref matched -> nothing")
check(codes(sidecar(matches=[rm("SPEAKER_01", "Jessica", True, "registry"),
                             rm("SPEAKER_00", "Marin", True, "registry")])) == [],
      "registry pass is not a --ref pass")
check(codes(sidecar(matches=two_refs, ce={"k": 2, "brief_fold": {}})) == [],
      "v1.10 sidecar (brief_fold) -> no ref-order")

# ---- singular "turn"
one = sidecar(n=3, segments=[seg(0, 30, "SPEAKER_00"), seg(30, 60, "SPEAKER_01"),
                             seg(60, 61, "SPEAKER_02")])
r = reprocess.scan_sidecar(one)[0]["reason"]
check("over 1 turn)" in r and "1 turns" not in r, f"singular turn: {r}")

# ---- version stamp: >= 1.11.0 means current, whatever the shape
def stamped(v, **kw):
    d = sidecar(**kw)
    d["whosaid_version"] = v
    return d


flagged = dict(n=3, segments=frag)
check(codes(stamped("1.11.0", **flagged)) == [], "1.11.0 stamp -> no findings")
check(codes(stamped("1.12.3", **flagged)) == [], "1.12.3 stamp -> no findings")
check(codes(stamped("2.0.0", **flagged)) == [], "2.0.0 stamp -> no findings")
check(codes(stamped("v1.11", **flagged)) == [], "v1.11 (no patch) -> no findings")
check(codes(stamped("1.10.0", **flagged)) == ["brief-fragments"], "1.10.0 stamp still scanned")
check(codes(stamped("1.9.9", **flagged)) == ["brief-fragments"], "1.9.9 stamp still scanned")
check(codes(stamped("1.10.99", **flagged)) == ["brief-fragments"], "1.10.99 < 1.11.0")
for junk in (None, "", "dev", 11, ["1.11.0"], {"v": "1.11.0"}):
    check(codes(stamped(junk, **flagged)) == ["brief-fragments"], f"junk stamp {junk!r} ignored")
check(codes(stamped("1.11.0", dur=300, ce=None)) == [], "stamped short-whole-file -> none")
check(codes(stamped("1.11.0", matches=two_refs)) == [], "stamped ref-order -> none")

# ---- bound-pinned (#75): a --max-speakers bound set the count, not capped it
pinned_ce = {"method": "agglomerative", "k": 8, "raw_k": 57, "saturated": True,
             "fallback": None, "brief_fold": {"folded": []}}
pinned = dict(mode="auto-detected, bounded 1-8", n=8, ce=pinned_ce)
fs = reprocess.scan_sidecar(sidecar(**pinned))
check([f["code"] for f in fs] == ["bound-pinned"] and fs[0]["confidence"] == "likely"
      and "bound of 8" in fs[0]["reason"] and "57" in fs[0]["reason"], f"bound-pinned: {fs}")
check(codes(stamped("1.11.1", **pinned)) == ["bound-pinned"], "1.11.1 stamp still scanned for #75")
check(codes(stamped("1.11.0", **pinned)) == ["bound-pinned"], "1.11.0 stamp still scanned for #75")
check(codes(stamped("1.11.2", **pinned)) == [], "1.11.2 stamp -> current")
check(codes(sidecar(**dict(pinned, n=5))) == [], "count below the bound -> nothing")
check(codes(sidecar(**dict(pinned, ce=dict(pinned_ce, saturated=False)))) == [],
      "unsaturated -> nothing")
check(codes(sidecar(**dict(pinned, ce=dict(pinned_ce, fallback={"method": "x", "k": 8})))) == [],
      "a recovery fallback ran -> nothing")
check(codes(sidecar(**dict(pinned, mode="auto-detected"))) == [], "no bound -> nothing")
check(codes(stamped("1.11.0", **dict(pinned, n=3, segments=frag))) == [],
      "1.11.0 stamp: only bound-pinned is checked, not brief-fragments")

# ---- malformed / odd shapes never crash
for bad in (None, [], "x", 3, {}, {"detect_mode": 5}, {"source": "x", "segments": 7},
            {"detect_mode": "auto-detected", "count_estimate": {}, "segments": [None, 1, {}]},
            {"detect_mode": "as hinted (--num-speakers x)", "source": {"duration_seconds": "a"}},
            {"detect_mode": "auto-detected", "registry_matches": [None, 3, {"pass": "ref"}]},
            {"detect_mode": "3 speakers (relabel --auto; fold 2 -> 1)", "num_speakers": 3,
             "source": {"duration_seconds": 100}, "count_estimate": None, "segments": []}):
    reprocess.scan_sidecar(bad)
check(codes({"detect_mode": "3 speakers (relabel --auto; fold 2 -> 1)", "num_speakers": 3,
             "source": {"duration_seconds": 100}, "count_estimate": None}) == [],
      "relabel --fold-unknown shape -> no findings")

# ---- scan_workspace
with tempfile.TemporaryDirectory() as tmp:
    ws = Path(tmp)
    def put(name, data, fname="transcript.diarization.json", raw=None):
        (ws / name).mkdir()
        if data is not None or raw is not None:
            (ws / name / fname).write_text(raw if raw is not None else json.dumps(data))
    put("2026-01-01-a", sidecar(n=3, segments=frag))
    put("2026-01-02-b", sidecar())
    put("2026-01-03-none", None)
    put("2026-01-04-corrupt", None, raw="{not json")
    put("_x", sidecar(n=3, segments=frag))
    put(".y", sidecar(n=3, segments=frag))
    (ws / "loose.txt").write_text("hi")
    rows = reprocess.scan_workspace(ws, None)
    names = [r["meeting"] for r in rows]
    check(names == ["2026-01-01-a", "2026-01-02-b", "2026-01-03-none", "2026-01-04-corrupt"],
          f"_x/.y/files skipped: {names}")
    check(rows[0]["findings"][0]["code"] == "brief-fragments", "folder a flagged")
    check(rows[1]["findings"] == [], "folder b clean")
    check(rows[2].get("skipped") == "no diarization", "no-sidecar folder skipped")
    check(rows[3]["findings"] == [], "corrupt sidecar -> no findings, no crash")
    only = reprocess.scan_workspace(ws, ["2026-01-02-b"])
    check([r["meeting"] for r in only] == ["2026-01-02-b"], "meeting filter")

    def cli(*a):
        return subprocess.run([sys.executable, str(REPO_DIR / "lib" / "reprocess.py"), *a],
                              capture_output=True, text=True)
    p = cli("scan", str(ws))
    check(p.returncode == 0, p.stderr)
    check(p.stdout.strip().splitlines()[-1] == "1 of 4 meetings may change (1 likely)", p.stdout)
    check("2026-01-01-a" in p.stdout and "2026-01-02-b" not in p.stdout, p.stdout)
    p = cli("scan", str(ws), "--json")
    j = json.loads(p.stdout)
    check(p.returncode == 0 and j[0]["findings"][0]["code"] == "brief-fragments", p.stdout)
    check(cli("scan", str(ws / "nope")).returncode == 2, "bad workspace -> exit 2")

    # map-labels CLI
    old = {"local_labels": {"SPEAKER_00": {"name": "Ann", "note": "n"}},
           "segments": [seg(0, 10, "SPEAKER_00")]}
    new = {"segments": [seg(0, 10, "SPEAKER_01")]}
    (ws / "old.json").write_text(json.dumps(old))
    (ws / "new.json").write_text(json.dumps(new))
    p = cli("map-labels", str(ws / "old.json"), str(ws / "new.json"))
    check(p.returncode == 0 and json.loads(p.stdout) == {"SPEAKER_01": {"name": "Ann",
          "note": "n"}}, p.stdout + p.stderr)

# ---- map_local_labels
ll = {"SPEAKER_00": {"name": "Ann", "note": "host"}, "SPEAKER_01": {"name": "Bob", "note": None}}
old = {"local_labels": ll, "segments": [seg(0, 10, "SPEAKER_00"), seg(10, 20, "SPEAKER_01"),
                                         seg(20, 30, "SPEAKER_00")]}
swap = {"segments": [seg(0, 10, "SPEAKER_01"), seg(10, 20, "SPEAKER_00"),
                     seg(20, 30, "SPEAKER_01")]}
m, w = reprocess.map_local_labels(old, swap)
check(m == {"SPEAKER_01": {"name": "Ann", "note": "host"},
            "SPEAKER_00": {"name": "Bob", "note": None}} and w == [], f"swap: {m} {w}")

# partial overlap above threshold still maps
shift = {"segments": [seg(0, 11, "SPEAKER_00"), seg(11, 30, "SPEAKER_01")]}
m, w = reprocess.map_local_labels(old, shift)
check(m["SPEAKER_00"]["name"] == "Ann" and m["SPEAKER_01"]["name"] == "Bob" and w == [],
      f"shifted: {m} {w}")

# below threshold: Ann's 20s is split 8/8 across two new clusters plus 4 uncovered
split = {"segments": [seg(0, 8, "SPEAKER_00"), seg(8, 10, "SPEAKER_02"),
                      seg(10, 20, "SPEAKER_01"), seg(20, 28, "SPEAKER_03"),
                      seg(28, 30, "SPEAKER_02")]}
m, w = reprocess.map_local_labels(old, split)
check(any("SPEAKER_00 (Ann)" in x and "under 50%" in x for x in w), f"below-threshold warn: {w}")
check(m.get("SPEAKER_01", {}).get("name") == "Bob", f"Bob still carried: {m}")
check(all(v["name"] != "Ann" for v in m.values()), f"Ann not carried: {m}")

# collision: both old labels land mostly on the one new cluster
merged = {"segments": [seg(0, 30, "SPEAKER_00")]}
m, w = reprocess.map_local_labels(old, merged)
check(m == {"SPEAKER_00": {"name": "Ann", "note": "host"}}, f"collision keeps larger: {m}")
check(len(w) == 1 and "dropped SPEAKER_01" in w[0], f"collision warn: {w}")

# purity: inputs untouched; empty / missing -> empty
import copy
snap = copy.deepcopy(old)
reprocess.map_local_labels(old, swap)
check(old == snap, "inputs not mutated")
check(reprocess.map_local_labels({}, {}) == ({}, []), "empty inputs")
check(reprocess.map_local_labels({"local_labels": {"S": {"name": "A"}}}, swap)[1] != [],
      "label with no old segments warns")

print(f"reprocess_scan_test: {CHECKS} checks passed")
