#!/usr/bin/env python3
"""Synthetic release gate: score one whosaid build on a fixed synthetic corpus and hold it to a
committed baseline. The fallback for the by-ear replay when the real meeting corpus is absent.

    python3 test/diarize_eval/release_gate.py --whosaid ~/.wt/whosaid/v1.52.0/whosaid
        [--baseline test/diarize_eval/gate_baseline.json] [--extra "--match-threshold 0.5"]
        [--state DIR] [--skip-live] [--write-baseline FILE]

Exit codes: 0 pass, 1 a regression past the baseline's tolerance (each one is printed),
2 cannot run (no voice pool, a pool or corpus that differs from the baseline's, a failed step).
A run that cannot run never reads as a pass.

What it runs, all against the build under test, isolated from the real registry and workspace:

1. corpus: meetings built from the cached TTS voice pool at fixed seeds (SETS below):
   build.py "mixed" and "hard" batches, plus realistic.py meetings with crosstalk blends.
   Byte-for-byte reproducible from the same pool; the pool and every truth.json are
   fingerprinted into the result, and a different fingerprint than the baseline's exits 2.
2. registry round trip (the replay's path): a throwaway registry learns half the pool's voices
   the way a user teaches it (transcribe a voice's enrollment clip, `relabel SPEAKER_00=Name`).
   Every meeting is transcribed blind first, one stranger cluster gets a transcript-only label
   (`relabel --no-save`), then `whosaid reprocess WS ... --match-threshold 0.7` names them from
   the registry, as replay.sh does. Each cluster is scored like the by-ear replay
   (score.score_clusters): enrolled voices named right, strangers and MIXED blends unnamed, the
   local label carried. Wrong-name time is split into swap / stranger / blend.
3. diarize_eval modes (run.py) on the mixed batch: --speakers N, all enrolled, half enrolled.
4. live: test/live_diarize_eval.py --fast from the build's own tree (live naming, cassette ASR),
   and a `live serve` / `live pull` round trip of a synthetic session over loopback with a token,
   plus a wrong token that must be refused (exit 2, nothing written).

Names are the TTS voice names of the pool plus one made-up guest; no real person, recording or
voiceprint is involved, and nothing leaves the machine.
"""
import argparse
import datetime
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import score as sc  # noqa: E402

SCHEMA = 1
POOL = os.path.join(os.environ.get("WHOSAID_DIARIZE_EVAL_CACHE") or os.path.expanduser("~/.cache/whosaid/diarize-eval"),
                    "pool.json")
REAL_REGISTRY = os.path.expanduser("~/.config/whosaid/speakers.json")
# The fixed corpus. Changing any of this changes the fingerprint, so refresh the baseline with it.
SETS = [
    {"set": "mixed", "tool": "build", "args": ["--seed", "7101", "--count", "8", "--profile", "mixed"]},
    {"set": "hard", "tool": "build", "args": ["--seed", "7102", "--count", "4", "--profile", "hard"]},
    {"set": "blend", "tool": "realistic", "args": ["--seed", "7103", "--count", "2", "--minutes", "6-9",
                                                    "--speakers", "3-5", "--blends", "3"]},
]
KNOWN_SALT = "release-gate-1"   # which half of the pool's voices the registry learns
MATCH_THRESHOLD = "0.7"          # replay.sh's default
GUEST = "Guest Quill"            # made-up transcript-only label
RUN_MODES = "hint,refs,refs-subset"
RUN_SET = "mixed"


def log(msg):
    print("release-gate: " + msg, file=sys.stderr, flush=True)


class CannotRun(Exception):
    pass


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def real_fingerprint():
    try:
        return sha_file(REAL_REGISTRY)
    except FileNotFoundError:
        return "absent"


def run(cmd, env, logf, timeout, cwd=None):
    with open(logf, "a") as out:
        out.write("$ " + " ".join(map(str, cmd)) + "\n")
        out.flush()
        try:
            return subprocess.run([str(c) for c in cmd], env=env, stdin=subprocess.DEVNULL, stdout=out,
                                  stderr=subprocess.STDOUT, timeout=timeout, cwd=cwd).returncode
        except subprocess.TimeoutExpired:
            out.write("TIMEOUT after %ss\n" % timeout)
            return "timeout"


def iso_env(work, db):
    env = dict(os.environ)
    env.pop("WHOSAID_OWNER", None)
    env["WHOSAID_SPEAKER_DB"] = db
    env["WHOSAID_VOICE_REFS"] = os.path.join(work, "refs-empty")
    env["WHOSAID_WORKSPACE"] = os.path.join(work, "ws-unused")
    os.makedirs(env["WHOSAID_VOICE_REFS"], exist_ok=True)
    return env


# ---------------------------------------------------------------- corpus

def build_corpus(state, pool):
    pool_fp = sha_file(pool)
    root = os.path.join(state, "corpus", pool_fp[:12])
    for s in SETS:
        out = os.path.join(root, s["set"])
        if os.path.exists(os.path.join(out, "index.json")):
            continue
        tmp = out + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        if s["tool"] == "build":
            cmd = [sys.executable, os.path.join(HERE, "build.py")]
        else:
            cmd = ["uv", "run", "-q", "--with", "numpy", "python", os.path.join(HERE, "realistic.py")]
        log("building the %s set" % s["set"])
        p = subprocess.run(cmd + ["--pool", pool, "--out", tmp] + s["args"], capture_output=True, text=True,
                           timeout=1800)
        if p.returncode != 0:
            raise CannotRun("building the %s set failed: %s" % (s["set"], (p.stderr or p.stdout)[-400:]))
        os.replace(tmp, out)
    meetings, h = [], hashlib.sha256()
    for s in SETS:
        with open(os.path.join(root, s["set"], "index.json")) as f:
            idx = json.load(f)
        for e in idx["meetings"]:
            d = os.path.join(root, s["set"], e["id"])
            with open(os.path.join(d, "truth.json"), "rb") as f:
                raw = f.read()
            h.update(raw)
            h.update(str(os.path.getsize(os.path.join(d, "audio.wav"))).encode())
            meetings.append({"id": e["id"], "set": s["set"], "dir": d, "truth": json.loads(raw)})
    return root, pool_fp, h.hexdigest(), meetings


def known_voices(pool):
    """Half the pool's voices, picked by a stable hash: the ones the registry learns."""
    ids = sorted(pool["voices"])
    ranked = sorted(ids, key=lambda v: hashlib.md5((KNOWN_SALT + v).encode()).hexdigest())
    return sorted(ranked[: len(ids) // 2])


# ---------------------------------------------------------------- registry round trip

def eff_names(side):
    names = dict(side.get("names") or {})
    for c, lab in (side.get("local_labels") or {}).items():
        lab = lab.get("name") if isinstance(lab, dict) else lab
        if lab:
            names[c] = lab
    return names


def majority_cluster(side, spans):
    cov = {}
    for s in side.get("segments", []):
        for a, b in spans:
            ov = min(b, s["end"]) - max(a, s["start"])
            if ov > 0:
                cov[s["speaker"]] = cov.get(s["speaker"], 0) + ov
    return max(cov, key=cov.get) if cov else None


def registry_round_trip(whosaid, work, pool, pool_root, meetings, extra, res):
    db = os.path.join(work, "registry.json")
    lg = os.path.join(work, "registry.log")
    env = iso_env(work, db)
    known = known_voices(pool)
    vname = {v: pool["voices"][v]["name"] for v in pool["voices"]}
    if len(set(vname.values())) != len(vname):
        raise CannotRun("pool voice names are not unique")
    res["registry_voices"] = len(known)
    log("teaching the registry %d voices" % len(known))
    for v in known:
        d = os.path.join(work, "enroll", v)
        os.makedirs(d, exist_ok=True)
        clip = os.path.join(pool_root, "enroll", v + ".wav")
        rc = run([whosaid, clip, "-o", d + "/", "-n", "e", "--speakers", "1"], env, lg, 600)
        if rc != 0:
            raise CannotRun("enrolling %s: transcribe exited %s (see %s)" % (v, rc, lg))
        rc = run([whosaid, "relabel", os.path.join(d, "e"), "SPEAKER_00=" + vname[v]], env, lg, 300)
        if rc != 0:
            raise CannotRun("enrolling %s: relabel exited %s" % (v, rc))
    with open(db) as f:
        learned = {s["name"] for s in json.load(f)["speakers"]}
    if learned != {vname[v] for v in known}:
        raise CannotRun("the throwaway registry holds %d names, expected %d" % (len(learned), len(known)))

    ws = os.path.join(work, "ws")
    os.makedirs(ws, exist_ok=True)
    empty = iso_env(work, os.path.join(work, "registry-empty.json"))
    log("transcribing %d meetings blind (the pre-label sidecars)" % len(meetings))
    for m in meetings:
        d = os.path.join(ws, m["id"])
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, "audio.wav")
        if not os.path.exists(dst):
            os.link(os.path.join(m["dir"], "audio.wav"), dst)
        rc = run([whosaid, dst, "-o", d + "/", "-n", "transcript"], empty, lg,
                 max(600, int(4 * m["truth"]["duration"])))
        if rc != 0:
            raise CannotRun("pre pass on %s exited %s (see %s)" % (m["id"], rc, lg))

    # One transcript-only label on the longest stranger cluster of the first meeting that has a
    # stranger: reprocess must carry it onto the new clusters (lib/reprocess.py map_local_labels).
    known_names = {vname[v] for v in known}
    guest = None
    for m in meetings:
        strangers = [s["name"] for s in m["truth"]["speakers"] if s["name"] not in known_names]
        if not strangers:
            continue
        with open(os.path.join(ws, m["id"], "transcript.diarization.json")) as f:
            pre = json.load(f)
        r = sc.score_clusters(m["truth"], pre["segments"], enrolled=())
        cands = [c for c in r["clusters"] if c["truth"] in strangers]
        if cands:
            c = max(cands, key=lambda c: c["secs"])
            rc = run([whosaid, "relabel", os.path.join(ws, m["id"], "transcript"),
                      "%s=%s" % (c["cluster"], GUEST), "--no-save"], env, lg, 300)
            if rc != 0:
                raise CannotRun("relabel --no-save exited %s" % rc)
            spans = [(s["start"], s["end"]) for s in pre["segments"] if s["speaker"] == c["cluster"]]
            guest = {"meeting": m["id"], "cluster": c["cluster"], "truth": c["truth"], "spans": spans}
            break
    with open(db) as f:
        if GUEST in {s["name"] for s in json.load(f)["speakers"]}:
            raise CannotRun("relabel --no-save wrote the registry")

    log("reprocessing with the registry (match threshold %s%s)" % (MATCH_THRESHOLD, " " + " ".join(extra) if extra else ""))
    rc = run([whosaid, "reprocess", ws] + [m["id"] for m in meetings]
             + ["--no-items", "--match-threshold", MATCH_THRESHOLD] + list(extra), env, lg, 7200)
    if rc != 0:
        raise CannotRun("reprocess exited %s (see %s)" % (rc, lg))

    tot, per = {}, {}
    for m in meetings:
        with open(os.path.join(ws, m["id"], "transcript.diarization.json")) as f:
            side = json.load(f)
        enrolled = [s["name"] for s in m["truth"]["speakers"] if s["name"] in known_names]
        expect = {}
        if guest and guest["meeting"] == m["id"]:
            c = majority_cluster(side, guest["spans"])
            expect = {c: GUEST}
            shown = eff_names(side).get(c, c)
            res["checks"]["local_label_carried"] = shown == GUEST
            log("local label: %s now %s -> %s" % (guest["cluster"], c, shown))
        r = sc.score_clusters(m["truth"], side["segments"], side.get("names"), enrolled=enrolled,
                              local=side.get("local_labels"), expect=expect)
        segs = [dict(s, speaker=eff_names(side).get(s["speaker"], s["speaker"])) for s in side["segments"]]
        f = sc.score_meeting(m["truth"], segs, "named", enrolled=enrolled, uncounted=side.get("uncounted"))
        per[m["id"]] = {k: r[k] for k in r if k != "clusters"}
        per[m["id"]].update(set=m["set"], der=f["der"], count_ok=f["count_ok"], clusters=r["clusters"])
        for k, v in r.items():
            if k != "clusters":
                tot[k] = tot.get(k, 0) + v
    if guest is None:
        res["checks"]["local_label_carried"] = None
    talk = tot["talk_s"] or 1
    res["registry"] = {"per_meeting": per, "totals": {k: round(v, 2) for k, v in tot.items()}}
    met = res["metrics"]
    met["reg_named_ok_pct"] = round(100 * tot["ok_named_s"] / talk, 2)
    met["reg_wrong_pct"] = round(100 * tot["wrong_s"] / talk, 2)
    met["reg_swap_pct"] = round(100 * tot["swap_s"] / talk, 2)
    met["reg_stranger_pct"] = round(100 * tot["stranger_s"] / talk, 2)
    met["reg_blend_named_pct"] = round(100 * tot["blend_s"] / talk, 2)
    met["reg_missed_pct"] = round(100 * tot["missed_s"] / talk, 2)
    met["reg_clusters_ok_pct"] = round(100 * tot["n_ok"] / max(1, tot["n_clusters"]), 2)
    met["reg_blends_unnamed_pct"] = (round(100 * tot["n_blend_unnamed"] / tot["n_blend"], 2)
                                     if tot["n_blend"] else 100.0)
    met["reg_count_acc_pct"] = round(100 * sum(p["count_ok"] for p in per.values()) / len(per), 2)
    w = sum(p["talk_s"] for p in per.values()) or 1
    met["reg_der_pct"] = round(100 * sum(p["der"] * p["talk_s"] for p in per.values()) / w, 2)


# ---------------------------------------------------------------- diarize_eval modes

def run_modes(whosaid, work, root, name, extra, res):
    results = os.path.join(work, "run-results")
    lg = os.path.join(work, "run.log")
    cmd = [sys.executable, os.path.join(HERE, "run.py"), "--meetings", os.path.join(root, RUN_SET),
           "--modes", RUN_MODES, "--whosaid", whosaid, "--run-name", name, "--results", results, "--force"]
    if extra:
        cmd += ["--extra", " ".join(extra)]
    log("diarize_eval modes %s on the %s set" % (RUN_MODES, RUN_SET))
    rc = run(cmd, dict(os.environ), lg, 7200)
    if rc != 0:
        raise CannotRun("run.py exited %s (see %s)" % (rc, lg))
    with open(os.path.join(results, name + ".json")) as f:
        r = json.load(f)
    met = res["metrics"]
    for mode, a in r["by_mode"].items():
        if a.get("n_failed"):
            raise CannotRun("run.py: %d %s run(s) failed (see %s)" % (a["n_failed"], mode, lg))
        k = mode.replace("-", "_")
        met[k + "_der_pct"] = round(100 * a["der"], 2)
        met[k + "_count_acc_pct"] = round(100 * a["count_accuracy"], 2)
        met[k + "_turn_acc_pct"] = round(100 * a["turn_acc"], 2)
        if a.get("named_wrong_time") is not None:
            met[k + "_named_right_pct"] = round(100 * a["named_correct_time"], 2)
            met[k + "_named_wrong_pct"] = round(100 * a["named_wrong_time"], 2)
    res["run_py"] = {"by_mode": r["by_mode"], "results": os.path.join(results, name + ".json")}


# ---------------------------------------------------------------- live

def live_eval(tree, work, res):
    script = os.path.join(tree, "test", "live_diarize_eval.py")
    if not os.path.exists(script):
        log("live: %s has no test/live_diarize_eval.py" % tree)
        return
    out = os.path.join(work, "live-eval.json")
    log("live naming eval (--fast)")
    rc = run([sys.executable, script, "--fast", "--out", out], dict(os.environ), os.path.join(work, "live.log"),
             1800, cwd=tree)
    if rc != 0:
        raise CannotRun("live_diarize_eval.py --fast exited %s" % rc)
    with open(out) as f:
        live = json.load(f)["suite"]["live"]
    for k in ("name_acc", "unnamed", "guest_named", "split_voice", "handoff_acc"):
        res["metrics"]["live_" + k + "_pct"] = round(100 * live[k], 2)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def serve_pull(whosaid, tree, work, meeting, res):
    if not os.path.exists(os.path.join(tree, "lib", "live_remote.py")):
        log("live: %s has no live serve/pull (before v1.52.0)" % tree)
        return
    ck = res["checks"]
    lg = os.path.join(work, "serve.log")
    src, dst, bad = (os.path.join(work, x) for x in ("live-src", "live-dst", "live-bad"))
    stem = "2026-01-02_10-00-00"
    started = time.time() - 600
    turns = meeting["truth"]["turns"][:40]
    entries = [{"n": i + 1, "t": round(t["start"], 2), "who": "them", "text": t["text"] or "...",
                "name": t["speaker"], "clock": time.strftime("%H:%M:%S", time.localtime(started + t["start"]))}
               for i, t in enumerate(turns)]
    code = ("import json,sys,pathlib;sys.path.insert(0,sys.argv[1]);import live;"
            "d=pathlib.Path(sys.argv[2])/live.LIVE_DIR;e=json.load(open(sys.argv[4]));"
            "live.write_session(d/(sys.argv[3]+'.md'),d/(sys.argv[3]+'.jsonl'),float(sys.argv[5]),e,True)")
    ef = os.path.join(work, "live-entries.json")
    with open(ef, "w") as f:
        json.dump(entries, f)
    subprocess.run([sys.executable, "-c", code, os.path.join(tree, "lib"), src, stem, ef, str(started)], check=True)
    tok, wrong = os.path.join(work, "token"), os.path.join(work, "token-wrong")
    for p, v in ((tok, secrets.token_urlsafe(24)), (wrong, secrets.token_urlsafe(24))):
        with open(p, "w") as f:
            f.write(v + "\n")
        os.chmod(p, 0o600)
    port = free_port()
    env = dict(os.environ)
    env.pop("WHOSAID_LIVE_TOKEN_FILE", None)
    srv = subprocess.Popen([whosaid, "live", "serve", "--serve-only", "--bind", "127.0.0.1", "--port", str(port),
                            "--token-file", tok, "--into", src], stdin=subprocess.DEVNULL,
                           stdout=open(lg, "a"), stderr=subprocess.STDOUT, env=env)
    try:
        url = "http://127.0.0.1:%d/sessions" % port
        up, code401 = False, None
        for _ in range(100):
            try:
                urllib.request.urlopen(url, timeout=2)
            except urllib.error.HTTPError as e:
                up, code401 = True, e.code
                break
            except OSError:
                time.sleep(0.1)
        ck["serve_requires_token"] = up and code401 == 401
        rc = run([whosaid, "live", "pull", "--from", "127.0.0.1:%d" % port, "--token-file", wrong,
                  "--session", stem, "--once", "--into", bad], env, lg, 120)
        ck["pull_wrong_token_refused"] = rc == 2 and not os.path.exists(os.path.join(bad, ".live", stem + ".jsonl"))
        rc = run([whosaid, "live", "pull", "--from", "127.0.0.1:%d" % port, "--token-file", tok,
                  "--session", stem, "--once", "--into", dst], env, lg, 120)
        got, meta = [], {}
        p = os.path.join(dst, ".live", stem + ".jsonl")
        if os.path.exists(p):
            with open(p) as f:
                lines = [json.loads(x) for x in f if x.strip()]
            meta, got = lines[0].get("meta", {}), lines[1:]
        key = lambda e: (e["n"], e["who"], e["text"], e.get("name"))  # noqa: E731
        ck["pull_mirrors_session"] = (rc == 0 and [key(e) for e in got] == [key(e) for e in entries]
                                      and str(meta.get("source", "")).startswith("remote:"))
        log("serve/pull: token required %s, wrong token refused %s, mirrored %d/%d lines (pull rc %s)"
            % (ck["serve_requires_token"], ck["pull_wrong_token_refused"], len(got), len(entries), rc))
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()


# ---------------------------------------------------------------- main

def rules_for(baseline, skip_live):
    rules = dict(baseline["rules"])
    if skip_live:
        rules = {k: v for k, v in rules.items() if not (k.startswith("live_") or k.startswith("pull_")
                                                         or k.startswith("serve_"))}
    return rules


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--whosaid", required=True, help="the build under test (a frozen tag worktree's whosaid)")
    ap.add_argument("--baseline", default=os.path.join(HERE, "gate_baseline.json"))
    ap.add_argument("--state", default=os.path.join(os.path.dirname(POOL), "release-gate"))
    ap.add_argument("--pool", default=POOL)
    ap.add_argument("--extra", default="", help="extra flags for every transcribe/reprocess (a red-test knob)")
    ap.add_argument("--name", help="run name (default: version + timestamp)")
    ap.add_argument("--skip-live", action="store_true", help="skip the live checks (a build before v1.52.0)")
    ap.add_argument("--write-baseline", metavar="FILE",
                    help="write this run's metrics as a new baseline (keeps the old file's rules)")
    a = ap.parse_args(argv)
    whosaid = os.path.abspath(os.path.expanduser(a.whosaid))
    tree = os.path.dirname(whosaid)
    extra = a.extra.split()
    if not os.path.exists(a.pool):
        print("GATE CANNOT RUN: no voice pool at %s. Render it once with test/diarize_eval/render.py "
              "(docs/diarize-eval.md), or run the by-ear replay." % a.pool)
        return 2
    with open(a.baseline) as f:
        baseline = json.load(f)
    ver = subprocess.run([whosaid, "--version"], capture_output=True, text=True).stdout.strip()
    name = a.name or "%s-%s" % (ver.split()[1] if len(ver.split()) > 1 else "x",
                                datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    work = os.path.join(a.state, "runs", name)
    if os.path.exists(work):
        print("GATE CANNOT RUN: %s exists; pick another --name" % work)
        return 2
    os.makedirs(work)
    res = {"schema": SCHEMA, "run": name, "whosaid": ver, "extra": a.extra, "date":
           datetime.datetime.now().isoformat(timespec="seconds"), "metrics": {}, "checks": {}}
    before = real_fingerprint()
    t0 = time.time()
    try:
        with open(a.pool) as f:
            pool = json.load(f)
        root, pool_fp, corpus_fp, meetings = build_corpus(a.state, a.pool)
        res.update(pool_fp=pool_fp, corpus_fp=corpus_fp, meetings=[m["id"] for m in meetings])
        if not a.write_baseline and (baseline.get("pool_fp"), baseline.get("corpus_fp")) != (pool_fp, corpus_fp):
            raise CannotRun("this pool or corpus differs from the baseline's (pool %s vs %s, corpus %s vs %s); "
                            "refresh the baseline from the last release on this pool first"
                            % (pool_fp[:12], str(baseline.get("pool_fp"))[:12], corpus_fp[:12],
                               str(baseline.get("corpus_fp"))[:12]))
        registry_round_trip(whosaid, work, pool, os.path.dirname(os.path.abspath(a.pool)), meetings, extra, res)
        run_modes(whosaid, work, root, name, extra, res)
        if not a.skip_live:
            live_eval(tree, work, res)
            serve_pull(whosaid, tree, work, meetings[0], res)
    except CannotRun as e:
        print("GATE CANNOT RUN: %s" % e)
        return 2
    finally:
        res["checks"]["real_registry_unchanged"] = real_fingerprint() == before
    res["wall_s"] = round(time.time() - t0)
    met = dict(res["metrics"], **res["checks"])
    with open(os.path.join(work, "result.json"), "w") as f:
        json.dump(res, f, indent=1)
    for k in sorted(met):
        old = baseline.get("metrics", {}).get(k)
        print("  %-32s %-8s baseline %s" % (k, met[k], old))
    if a.write_baseline:
        out = {"schema": SCHEMA, "whosaid": ver, "date": res["date"], "pool_fp": pool_fp, "corpus_fp": corpus_fp,
               "sets": SETS, "known_salt": KNOWN_SALT, "match_threshold": MATCH_THRESHOLD,
               "meetings": res["meetings"], "rules": baseline["rules"], "metrics": met}
        with open(a.write_baseline, "w") as f:
            json.dump(out, f, indent=1)
            f.write("\n")
        print("wrote baseline %s from %s (no compare: a new baseline is the reference)" % (a.write_baseline, ver))
        return 0
    bad = sc.gate_compare(met, baseline.get("metrics", {}), rules_for(baseline, a.skip_live))
    print("result: %s" % os.path.join(work, "result.json"))
    if bad:
        for b in bad:
            print("GATE FAIL " + b)
        return 1
    print("GATE PASS %s on %d synthetic meetings vs the baseline from %s (%ss)"
          % (ver, len(meetings), baseline.get("whosaid"), res["wall_s"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
