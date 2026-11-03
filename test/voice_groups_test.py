#!/usr/bin/env python3
"""
Offline test for lib/voice_groups.py: which unnamed clusters are read, average linkage
(never chains), groups across meetings with a registry hint, the cache, and naming a
group (one registry write, first and alone; the rest transcript-only; index once).
`whosaid` is a stub that logs its argv and labels the sidecar; voiceprints are made up.

Run: uv run --with numpy python test/voice_groups_test.py
"""
import json
import os
import stat
import sys
import tempfile
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="voice-groups-test-"))
os.environ["WHOSAID_SPEAKER_DB"] = str(TMP / "speakers.json")
sys.path.insert(0, str(ROOT / "lib"))
import diarize_sherpa as ds  # noqa: E402
import voice_groups as vg  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def unit(*xs):
    v = np.zeros(8, dtype=np.float32)
    v[:len(xs)] = xs
    return v / np.linalg.norm(v)


# average linkage never chains: A~B and B~C close, A~C far, the mean stays under 0.65
V = np.stack([unit(1, 0, 0), unit(0.75, 0.66, 0), unit(0.13, 0.99, 0)])
sims = V @ V.T
check(sims[0, 1] > 0.7 and sims[1, 2] > 0.7 and sims[0, 2] < 0.3, f"fixture: {sims.round(2)}")
check(sorted(map(len, vg.average_linkage(V, 0.65))) == [1, 2], "a chain A~B~C does not become one group")
check(vg.average_linkage(np.zeros((0, 8)), 0.65) == [], "no clusters: no groups")

ws = TMP / "ws"
ADA, GRACE, NEWBIE = unit(1, 0, 0, 0), unit(0, 1, 0, 0), unit(0, 0, 1, 0)


def meeting(folder, clusters, names=None, model=ds.EMB_NAME):
    """clusters: {cluster: (vec, seconds)}."""
    d = ws / folder
    d.mkdir(parents=True)
    (d / "transcript.speakers.txt").write_text("[00:00:01] x: hi\n")
    segs, t = [], 0.0
    for c, (_, secs) in clusters.items():
        segs.append({"start": t, "end": t + secs, "speaker": c})
        t += secs
    (d / "transcript.diarization.json").write_text(json.dumps({
        "base": "transcript", "emb_model": model, "segments": segs,
        "names": names or {c: c for c in clusters},
        "cluster_emb": {c: (v * 3).tolist() for c, (v, _) in clusters.items()}}))


near = lambda v, e: unit(*(v[:4] + e))  # noqa: E731
meeting("2026-03-01-0900", {"SPEAKER_00": (near(NEWBIE, [0.1, 0, 0, 0]), 120), "SPEAKER_01": (GRACE, 300)},
        names={"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "Grace_Hopper"})
meeting("2026-03-08-0900", {"SPEAKER_00": (near(NEWBIE, [0, 0.1, 0, 0]), 40), "SPEAKER_02": (near(ADA, [0, 0, 0.2, 0]), 60)})
meeting("2026-03-10-0900", {"SPEAKER_03": (near(NEWBIE, [0, 0, 0, 0.1]), 200), "SPEAKER_04": (GRACE, 3)})
meeting("2026-03-11-0900", {"SPEAKER_00": (unit(0, 0, 0, 1), 90)}, model="some_other_model.onnx")
meeting("_drafts", {"SPEAKER_00": (NEWBIE, 500)})
ds.save_registry({"speakers": [
    {"name": "Ada_Lovelace", "model": ds.EMB_NAME, "embedding": ADA.tolist(), "role": "boss"},
    {"name": "Grace_Hopper", "model": ds.EMB_NAME, "embedding": GRACE.tolist()}]})

r = vg.build(ws, today=date(2026, 3, 20))
check(r["ok"] and r["clusters"] == 4 and r["short_skipped"] == 1,
      f"named, other-model, _folder and short clusters are left out: {r['clusters']}, {r['short_skipped']}")
gs = r["groups"]
check([g["meetings"] for g in gs] == [3, 1], f"the new voice is one group over three meetings: {[g['meetings'] for g in gs]}")
big, ada = gs
check(big["members"][0]["seconds"] == 200 and big["first"] == "2026-03-01" and big["last"] == "2026-03-10"
      and big["seconds"] == 360, f"members largest first, dates and talk time: {big}")
check(big["recent"] and big["hint"] is None, "heard within 14 days; nobody known is near it")
check(ada["hint"]["name"] == "Ada_Lovelace" and ada["hint"]["role"] == "boss" and ada["hint"]["next"]["name"] == "Grace_Hopper",
      f"a second print of a known voice gets a hint: {ada['hint']}")
check(not vg.build(ws, today=date(2026, 4, 30))["groups"][0]["recent"], "older than 14 days: not recent")
check(big["id"] == vg.build(ws, today=date(2026, 3, 20))["groups"][0]["id"], "group ids are stable")

# the cache: reused until a sidecar changes
first = vg.groups(ws)
check((ws / vg.CACHE).exists() and vg.groups(ws) == first, "cached result reused")

# naming: a stub whosaid logs argv; relabel writes the name into the sidecar like the real one
bin_dir = TMP / "repo"
bin_dir.mkdir()
log = TMP / "argv.log"
stub = bin_dir / "whosaid"
stub.write_text(f"""#!/usr/bin/env python3
import json, sys
open({str(log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "relabel":
    p = sys.argv[2]
    d = json.load(open(p))
    c, n = sys.argv[3].split("=", 1)
    if c == "SPEAKER_99":
        sys.exit("diarize: FATAL relabel: unknown cluster")
    d["names"][c] = n
    if "--no-save" in sys.argv:
        d.setdefault("local_labels", {{}})[c] = {{"name": n}}
    json.dump(d, open(p, "w"))
""")
stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
vg.REPO = bin_dir


def calls():
    out = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    log.unlink(missing_ok=True)
    return out


gid = first["groups"][0]["id"]
res = vg.name(ws, gid, "Margaret_Hamilton", role="peer")
argv = calls()
relabels = [a for a in argv if a[0] == "relabel"]
check(res["ok"] and res["enrolled"] and len(res["named"]) == 3, f"new name: all three labeled: {res}")
check(relabels[0][2] == "SPEAKER_03=Margaret_Hamilton" and "--no-save" not in relabels[0]
      and relabels[0][-2:] == ["--role", "Margaret_Hamilton=peer"], f"the largest member saves the print, with the role: {relabels[0]}")
check(all("--no-save" in a and "--role" not in a for a in relabels[1:]) and len(relabels) == 3,
      f"the rest are transcript-only, no registry writes: {relabels[1:]}")
check([a[0] for a in argv].count("index") == 1 and argv[-1][0] == "index", "index once, at the end")
check(gid not in {g["id"] for g in vg.groups(ws)["groups"]}, "a named group drops out (the cache sees the sidecars change)")

# a name already known: every member transcript-only, the role only on the first call
ada_id = vg.groups(ws)["groups"][0]["id"]
res = vg.name(ws, ada_id, "Ada_Lovelace", role="boss", index=False)
argv = calls()
check(res["ok"] and not res["enrolled"] and all("--no-save" in a for a in argv), f"known name: registry print untouched: {argv}")
check(not any(a[0] == "index" for a in argv), "index=False skips the index")
check(vg.groups(ws)["groups"] == [], "everything named: no groups left")

# refusals and a failing relabel
check(not vg.name(ws, "vg-0000000000", "X")["ok"], "unknown group id refused")
check(not vg.name(ws, gid, "SPEAKER_07")["ok"], "SPEAKER_NN is not a name")
check(not vg.name(ws, gid, "Ada Lovelace")["ok"], "a name relabel would refuse (a space) is refused first")
meeting("2026-03-15-0900", {"SPEAKER_99": (NEWBIE, 60)})
bad = vg.groups(ws)["groups"][0]["id"]
res = vg.name(ws, bad, "Someone_New")
check(not res["ok"] and "FATAL" in res["failed"][0]["error"] and not any(a[0] == "index" for a in calls()),
      f"a failed relabel is reported and nothing is indexed: {res}")

# the CLI front: whosaid voices is wired
check("cmd_voices" in (ROOT / "whosaid").read_text() and 'lib/voice_groups.py' in (ROOT / "whosaid").read_text(),
      "whosaid voices runs lib/voice_groups.py")

print(f"PASS: voice_groups_test ({CHECKS} checks)")
