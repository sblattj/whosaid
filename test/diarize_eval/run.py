#!/usr/bin/env python3
"""Run whosaid over built synthetic meetings in several modes and score the result.

    python3 test/diarize_eval/run.py --meetings DIR --modes blind,hint,refs,refs-subset
        [--whosaid PATH] [--run-name NAME] [--only m1,m2] [--force] [--results DIR] [--pool PATH]
    python3 test/diarize_eval/run.py --report RESULTS.json

Isolation: every whosaid call gets its own WHOSAID_SPEAKER_DB, a private
WHOSAID_VOICE_REFS dir and an unused WHOSAID_WORKSPACE, with WHOSAID_OWNER unset.
The user's real registry (~/.config/whosaid/speakers.json) and the repo voices/
dir are fingerprinted before and after each call; any change aborts the run.

When the pool has a voice_sim.json (voice_sim.py), every meeting is also bucketed
by its closest voice pair: "similar" when two of its voices score >= 0.60 cosine
to whosaid's own embedder, "distinct" otherwise. A similar pair tests the
embedder, not clustering, so the report shows the two buckets apart.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from score import score_meeting, aggregate  # noqa: E402

MODES = ("blind", "hint", "refs", "refs-subset")
REAL_REGISTRY = os.path.expanduser("~/.config/whosaid/speakers.json")
SIMILAR_PAIR = 0.60


def cache_root():
    return os.environ.get("WHOSAID_DIARIZE_EVAL_CACHE") or os.path.expanduser("~/.cache/whosaid/diarize-eval")


def fingerprint(whosaid):
    """sha256 of the real registry + a listing fingerprint of the repo voices/ dir."""
    h = hashlib.sha256()
    try:
        with open(REAL_REGISTRY, "rb") as f:
            reg = hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        reg = "absent"
    vd = os.path.join(os.path.dirname(os.path.abspath(whosaid)), "voices")
    parts = []
    if os.path.isdir(vd):
        for n in sorted(os.listdir(vd)):
            p = os.path.join(vd, n)
            st = os.stat(p)
            parts.append("%s:%d:%d" % (n, st.st_size, st.st_mtime_ns))
    h.update("\n".join(parts).encode())
    return reg, h.hexdigest()


def atomic_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def pick_subset(truth, salt=""):
    """Half the roster (at least one), picked by a stable hash. A non-empty salt picks a
    different half, so the same meetings yield more unenrolled-lookalike cases."""
    names = [s["name"] for s in truth["speakers"]]
    k = max(1, len(names) // 2)
    ranked = sorted(names, key=lambda n: hashlib.md5((salt + truth["id"] + n).encode()).hexdigest())
    return sorted(ranked[:k])


def run_one(whosaid, mdir, truth, mode, work, pool_root, extra=(), salt=""):
    mid = truth["id"]
    odir = os.path.join(work, mid, mode)
    os.makedirs(odir, exist_ok=True)
    refs = os.path.join(work, mid, mode + "-refs")
    shutil.rmtree(refs, ignore_errors=True)
    os.makedirs(refs)
    enrolled = []
    if mode in ("refs", "refs-subset"):
        enrolled = [s["name"] for s in truth["speakers"]] if mode == "refs" else pick_subset(truth, salt)
        for s in truth["speakers"]:
            if s["name"] in enrolled:
                shutil.copyfile(os.path.join(pool_root, s["enrolled_clip"]),
                                os.path.join(refs, s["name"] + ".wav"))
    base = "%s_%s" % (mid, mode)
    audio = os.path.join(mdir, mid, "audio.wav")
    cmd = [whosaid, audio, "-n", base, "-o", odir + "/"]
    if mode == "hint":
        cmd += ["--speakers", str(len(truth["speakers"]))]
    cmd += list(extra)
    env = dict(os.environ)
    env.pop("WHOSAID_OWNER", None)
    env["WHOSAID_SPEAKER_DB"] = os.path.join(odir, "speakers.json")
    env["WHOSAID_VOICE_REFS"] = refs
    env["WHOSAID_WORKSPACE"] = os.path.join(work, "ws-unused")
    row = {"meeting": mid, "mode": mode, "tags": truth["tags"], "enrolled": enrolled,
           "failed": False, "cmd": " ".join(cmd[1:])}
    before = fingerprint(whosaid)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           timeout=max(600, 4 * truth["duration"]))
        rc, out = p.returncode, p.stdout
    except subprocess.TimeoutExpired as e:
        rc, out = "timeout", (e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""))
    row["wall_s"] = round(time.time() - t0, 1)
    row["rc"] = rc
    after = fingerprint(whosaid)
    if before != after:
        raise SystemExit("ISOLATION BREACH: real registry / repo voices changed during %s %s" % (mid, mode))
    with open(os.path.join(odir, "whosaid.log"), "w") as f:
        f.write(out or "")
    side = os.path.join(odir, base + ".diarization.json")
    if rc != 0 or not os.path.exists(side):
        row["failed"] = True
        row["error"] = "rc=%s" % rc if rc != 0 else "no diarization sidecar"
        row["log_tail"] = (out or "")[-600:]
        return row
    try:
        with open(side) as f:
            sc = json.load(f)
        hyp_text = None
        wj = os.path.join(odir, base + ".json")
        if os.path.exists(wj):
            with open(wj) as f:
                hyp_text = json.load(f).get("text")
        # Observed 2026-10-06: segments keep raw SPEAKER_nn labels; the resolved
        # names live in the sidecar's `names` map (spec section 4 was wrong).
        names = sc.get("names") or {}
        segs = [dict(s, speaker=names.get(s["speaker"], s["speaker"])) for s in sc["segments"]]
        res = score_meeting(truth, segs, "blind" if mode in ("blind", "hint") else "named",
                            enrolled=enrolled or None, hyp_text=hyp_text,
                            uncounted=sc.get("uncounted"))
    except Exception as e:  # a scoring/parse bug must not kill the run
        row["failed"] = True
        row["error"] = "score: %r" % (e,)
        return row
    row.update(res)
    for k in ("num_speakers", "uncounted", "detect_mode", "count_warning", "count_estimate"):
        row[k] = sc.get(k)
    return row


def max_voice_sim(truth, voice_sim):
    """Highest enrollment-print cosine between two of the meeting's voices, or None."""
    if not voice_sim:
        return None
    pos = {v: i for i, v in enumerate(voice_sim["ids"])}
    idx = [pos[s["voice"]] for s in truth["speakers"] if s.get("voice") in pos]
    if len(idx) < len(truth["speakers"]) or len(idx) < 2:
        return None
    return max(voice_sim["sim"][a][b] for i, a in enumerate(idx) for b in idx[i + 1:])


def sim_bucket(row):
    v = row.get("max_voice_sim")
    return None if v is None else ("similar" if v >= SIMILAR_PAIR else "distinct")


def summarize(rows_by_mm):
    by_mode, by_tag, by_sim = {}, {}, {}
    modes = sorted({m for d in rows_by_mm.values() for m in d})
    for mode in modes:
        rows = [d[mode] for d in rows_by_mm.values() if mode in d]
        ok = [r for r in rows if not r.get("failed")]
        agg = aggregate(ok) if ok else {}
        agg["n_failed"] = len(rows) - len(ok)
        by_mode[mode] = agg
        tags = sorted({t for r in ok for t in r["tags"]})
        by_tag[mode] = {t: aggregate([r for r in ok if t in r["tags"]]) for t in tags}
        buckets = sorted({sim_bucket(r) for r in ok} - {None})
        if buckets:
            by_sim[mode] = {b: aggregate([r for r in ok if sim_bucket(r) == b]) for b in buckets}
    return by_mode, by_tag, by_sim


def f(x, pct=True):
    if x is None:
        return "-"
    return ("%.1f%%" % (100 * x)) if pct else ("%.3f" % x)


def render(res):
    out = ["### %s  (%s, whosaid %s)" % (res["run"], res["date"], res.get("whosaid_version", "?")), "",
           "| mode | n | failed | count acc | DER | turn acc | short-turn acc | WER | named right | named wrong |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for mode, a in res["by_mode"].items():
        out.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            mode, a.get("n_meetings", 0), a.get("n_failed", 0), f(a.get("count_accuracy")),
            f(a.get("der")), f(a.get("turn_acc")), f(a.get("turn_acc_short")), f(a.get("wer")),
            f(a.get("named_correct_time")), f(a.get("named_wrong_time"))))
    if res.get("by_sim"):
        out += ["", "By closest voice pair (similar = cosine >= %.2f):" % SIMILAR_PAIR, "",
                "| mode | voices | n | count acc | DER | turn acc | named wrong |",
                "|---|---|---|---|---|---|---|"]
        for mode, buckets in res["by_sim"].items():
            for b, a in buckets.items():
                out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                    mode, b, a.get("n_meetings", 0), f(a.get("count_accuracy")), f(a.get("der")),
                    f(a.get("turn_acc")), f(a.get("named_wrong_time"))))
    for mode in res["by_mode"]:
        rows = [d[mode] for d in res["per_meeting"].values()
                if mode in d and not d[mode].get("failed")]
        worst = sorted(rows, key=lambda r: -(r.get("der") or 0))[:5]
        if worst:
            out += ["", "Worst 5 by DER, %s:" % mode, ""]
            for r in worst:
                out.append("- %s DER %s n_true=%s n_hyp=%s [%s]" % (
                    r["meeting"], f(r["der"]), r["n_true"], r["n_hyp"], ",".join(r["tags"])))
        fails = [d[mode] for d in res["per_meeting"].values() if mode in d and d[mode].get("failed")]
        for r in fails:
            out.append("- FAILED %s %s: %s" % (r["meeting"], mode, r.get("error")))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--meetings")
    ap.add_argument("--modes", default="blind,hint,refs,refs-subset")
    ap.add_argument("--whosaid", default=os.path.join(REPO, "whosaid"))
    ap.add_argument("--run-name")
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--results", default=os.path.join(HERE, "results"))
    ap.add_argument("--pool", help="pool.json, to resolve enrolled clips (default: index.json pool_root)")
    ap.add_argument("--extra", default="",
                    help="extra whosaid flags for every call (space-separated), e.g. to A/B a threshold")
    ap.add_argument("--subset-salt", default="",
                    help="re-pick the refs-subset half with this salt (default: the original half)")
    ap.add_argument("--report", help="print the markdown table for an existing results file")
    args = ap.parse_args(argv)
    if args.report:
        with open(args.report) as fh:
            print(render(json.load(fh)))
        return 0
    if not args.meetings:
        ap.error("--meetings is required")
    modes = [m for m in args.modes.split(",") if m]
    for m in modes:
        if m not in MODES:
            ap.error("unknown mode %s" % m)
    mdir = os.path.abspath(args.meetings)
    with open(os.path.join(mdir, "index.json")) as fh:
        index = json.load(fh)
    pool_root = os.path.dirname(os.path.abspath(args.pool)) if args.pool else index["pool_root"]
    ids = [e["id"] for e in index["meetings"]]
    if args.only:
        want = set(args.only.split(","))
        ids = [i for i in ids if i in want]
    run_name = args.run_name or datetime.datetime.now().strftime("run-%Y%m%d-%H%M%S")
    work = os.path.join(cache_root(), "runs", run_name)
    whosaid = os.path.abspath(args.whosaid)
    ver = subprocess.run([whosaid, "--version"], capture_output=True, text=True).stdout.strip()
    start_fp = fingerprint(whosaid)
    voice_sim = None
    vs_path = os.path.join(pool_root, "voice_sim.json")
    if os.path.exists(vs_path):
        with open(vs_path) as fh:
            voice_sim = json.load(fh)
    per = {}
    for mid in ids:
        with open(os.path.join(mdir, mid, "truth.json")) as fh:
            truth = json.load(fh)
        closest = max_voice_sim(truth, voice_sim)
        per[mid] = {}
        for mode in modes:
            cached = os.path.join(work, mid, mode, "result.json")
            if os.path.exists(cached) and not args.force:
                with open(cached) as fh:
                    per[mid][mode] = json.load(fh)
                per[mid][mode]["max_voice_sim"] = closest
                print("[cached] %s %s" % (mid, mode), file=sys.stderr)
                continue
            print("[run] %s %s" % (mid, mode), file=sys.stderr, flush=True)
            row = run_one(whosaid, mdir, truth, mode, work, pool_root, args.extra.split(),
                         args.subset_salt)
            row["max_voice_sim"] = closest
            atomic_json(cached, row)
            per[mid][mode] = row
    if fingerprint(whosaid) != start_fp:
        raise SystemExit("ISOLATION BREACH: real registry / repo voices changed during the run")
    by_mode, by_tag, by_sim = summarize(per)
    res = {"schema": 1, "run": run_name, "date": datetime.datetime.now().isoformat(timespec="seconds"),
           "whosaid_version": ver, "extra": args.extra, "per_meeting": per, "by_mode": by_mode, "by_tag": by_tag,
           "by_sim": by_sim}
    path = os.path.join(args.results, run_name + ".json")
    atomic_json(path, res)
    print(render(res))
    print("\nresults: %s" % path)
    print("real registry sha256: %s (unchanged)" % start_fp[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
