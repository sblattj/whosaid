#!/usr/bin/env python3
"""Calibration check for the synthetic diarization eval (issue #80, stdlib only).

Reports the stats the issue lists, for a synthetic run, side by side with the
real-corpus numbers in real_stats.json, and flags every synthetic value that is
outside roughly +-20% of the real one.

    python3 test/diarize_eval/calibrate.py --meetings DIR --run RUNDIR [--mode refs] [--json OUT]

Inputs
    DIR/<id>/truth.json                                  (docs/diarize-eval.md, "Meeting format")
    RUNDIR/<id>/<mode>/<id>_<mode>.diarization.json      (whosaid sidecar)

Real stats were measured on the DIARIZER's output, so the headline column
("synthetic (diarizer)") is computed from the sidecar `segments` with the same
definitions; "synthetic (truth)" is the same stat on the truth turns, for
reference only.

Definitions
    turn        one segment (diarizer) or one truth turn. Bins: <1, 1-2, 2-4, 4-8, >8 s.
    switch      consecutive segments (by start) with different speaker labels;
                switches/min = switches / meeting minutes.
    gap         next.start - prev.end between consecutive different-speaker
                segments, clamped at 0 (an overlapping pair is a 0 s gap).
    overlap     seconds covered by >= 2 segments / seconds covered by >= 1.
    top share   talk of the largest label / total talk (sum of segment lengths).
    under 2min/h  labels whose talk, scaled to one hour of meeting, is < 120 s.
    minutes     sidecar source.duration_seconds (truth: truth duration) / 60,
                falling back to the last segment end.
    speakers    sidecar num_speakers (truth: distinct truth names).
Per-meeting stats are aggregated as median (+ min-max) across meetings; the turn
table and the gap stats are pooled over all turns / gaps.

Embedding stats come from `registry_matches`. Each cluster maps to the truth
speaker covering most of its time (purity_time = that share of the cluster's
time). Categories, tried in this order per cluster:
    unenrolled   majority speaker has no reference in this run (best similarity
                 over all the cluster's records).
    correct      a matched record of pass ref/registry naming the majority speaker.
    over-split   purity_time >= 0.7, not correct, and ANOTHER cluster of the same
                 majority speaker holds that name (a matched record, preferring a
                 pass-1 one); best similarity over the cluster's records naming
                 the speaker, plus the recorded `purity` when present.
    blend        purity_time < 0.7: best similarity over all records and its candidate.
If a record carries per-turn `turns` [{start,end,score,best}], time-weighted
purity = seconds of turns with score >= 0.70 / seconds of all turns.
Enrolled names for a meeting = stems of RUNDIR/<id>/<mode>-refs/*.wav plus
every name in a `pass: ref` record of the meeting's sidecar; when neither exists
every truth speaker counts as enrolled.
"""

import argparse
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import score as sc  # noqa: E402

REAL_PATH = os.path.join(HERE, "real_stats.json")
PURITY_BAR = 0.7        # purity_time below this = blend
TURN_SCORE_BAR = 0.70   # per-turn score that counts toward time-weighted purity
PASS1 = ("ref", "registry")


# --------------------------------------------------------------------------
# generic helpers
# --------------------------------------------------------------------------

def median(xs):
    return statistics.median(xs) if xs else None


def stat(values):
    """Per-meeting aggregate: median + range + n."""
    vs = [v for v in values if v is not None]
    if not vs:
        return {"value": None, "min": None, "max": None, "n": 0}
    return {"value": median(vs), "min": min(vs), "max": max(vs), "n": len(vs)}


def scalar(v, n=None):
    return {"value": v, "min": None, "max": None, "n": n}


def within(syn, real, tol=0.2):
    """'yes' / 'NO' / 'n/a'. Real with a median/value: relative +-tol on the
    synthetic median. Range-only real: synthetic median inside the range."""
    if syn is None or syn.get("value") is None or real is None:
        return "n/a"
    v = syn["value"]
    if real.get("value") is not None:
        r = real["value"]
        if r == 0:
            return "yes" if abs(v) < 1e-9 else "NO"
        return "yes" if abs(v - r) <= tol * abs(r) + 1e-9 else "NO"
    lo, hi = real.get("min"), real.get("max")
    if lo is None or hi is None:
        return "n/a"
    return "yes" if lo - 1e-9 <= v <= hi + 1e-9 else "NO"


# --------------------------------------------------------------------------
# segment statistics
# --------------------------------------------------------------------------

def bin_index(length, edges):
    for i, e in enumerate(edges):
        if length < e:
            return i
    return len(edges)


def overlap_share(segs):
    ev = {}
    for s in segs:
        a, b = float(s["start"]), float(s["end"])
        if b > a:
            ev[a] = ev.get(a, 0) + 1
            ev[b] = ev.get(b, 0) - 1
    pts = sorted(ev)
    cur = any1 = any2 = 0.0
    for i, p in enumerate(pts[:-1]):
        cur += ev[p]
        d = pts[i + 1] - p
        if cur >= 1:
            any1 += d
        if cur >= 2:
            any2 += d
    return any2 / any1 if any1 else None


def seg_stats(segs, minutes, edges, speakers=None):
    """Per-meeting stats for a list of {start,end,speaker}."""
    segs = sorted((s for s in segs if float(s["end"]) > float(s["start"])),
                  key=lambda s: (float(s["start"]), float(s["end"])))
    lens = [float(s["end"]) - float(s["start"]) for s in segs]
    if minutes is None:
        minutes = max((float(s["end"]) for s in segs), default=0.0) / 60.0
    talk = {}
    for s, L in zip(segs, lens):
        talk[str(s["speaker"])] = talk.get(str(s["speaker"]), 0.0) + L
    total = sum(lens)
    bins_n = [0] * (len(edges) + 1)
    bins_t = [0.0] * (len(edges) + 1)
    for L in lens:
        i = bin_index(L, edges)
        bins_n[i] += 1
        bins_t[i] += L
    switches = 0
    gaps = []
    for a, b in zip(segs, segs[1:]):
        if str(a["speaker"]) != str(b["speaker"]):
            switches += 1
            gaps.append(max(0.0, float(b["start"]) - float(a["end"])))
    hours = minutes / 60.0
    under = None
    if hours > 0:
        under = sum(1 for t in talk.values() if t / hours < 120.0)
    return {
        "minutes": minutes,
        "n_speakers": speakers if speakers is not None else len(talk),
        "n_turns": len(segs),
        "bins_n": bins_n,
        "bins_t": bins_t,
        "talk": total,
        "switches_per_min": switches / minutes if minutes else None,
        "gaps": gaps,
        "overlap_share": overlap_share(segs),
        "top_share": (max(talk.values()) / total) if total else None,
        "under_2min_per_hour": under,
    }


def aggregate_side(per_meeting, edges, gap_threshold):
    """Aggregate a list of seg_stats dicts into {metric: stat-object}."""
    out = {
        "meeting_minutes": stat([m["minutes"] for m in per_meeting]),
        "speakers_per_meeting": stat([m["n_speakers"] for m in per_meeting]),
        "speakers_under_2min_per_hour": stat([m["under_2min_per_hour"] for m in per_meeting]),
        "top_speaker_share": stat([m["top_share"] for m in per_meeting]),
        "switches_per_min": stat([m["switches_per_min"] for m in per_meeting]),
        "overlap_share": stat([m["overlap_share"] for m in per_meeting]),
    }
    gaps = [g for m in per_meeting for g in m["gaps"]]
    out["gap_median_s"] = scalar(median(gaps), len(gaps))
    out["gap_share_under"] = scalar(
        (sum(1 for g in gaps if g < gap_threshold) / float(len(gaps))) if gaps else None, len(gaps))
    nb = len(edges) + 1
    tn = sum(sum(m["bins_n"]) for m in per_meeting)
    tt = sum(sum(m["bins_t"]) for m in per_meeting)
    for i in range(nb):
        out["turn_share_%d" % i] = scalar(
            sum(m["bins_n"][i] for m in per_meeting) / float(tn) if tn else None, tn)
        out["talk_share_%d" % i] = scalar(
            sum(m["bins_t"][i] for m in per_meeting) / tt if tt else None, tn)
    return out


# --------------------------------------------------------------------------
# embedding statistics
# --------------------------------------------------------------------------

def cluster_truth_map(truth, segs):
    """{label: (majority truth speaker | None, purity_time, label_seconds)}."""
    turns = truth.get("turns", [])
    t_iv = [(sc._fr(t["start"]), sc._fr(t["end"]), t["speaker"]) for t in turns]
    t_iv = [x for x in t_iv if x[1] > x[0]]
    h_iv = [(sc._fr(s["start"]), sc._fr(s["end"]), str(s["speaker"])) for s in segs]
    h_iv = [x for x in h_iv if x[1] > x[0]]
    ov, tot = {}, {}
    for n, t, h in sc._intervals(t_iv, h_iv):
        for hl in h:
            tot[hl] = tot.get(hl, 0) + n
            for tn in t:
                ov[(hl, tn)] = ov.get((hl, tn), 0) + n
    out = {}
    for hl, total in tot.items():
        cands = sorted(((v, tn) for (h, tn), v in ov.items() if h == hl),
                       key=lambda x: (-x[0], x[1]))
        if cands:
            out[hl] = (cands[0][1], cands[0][0] / float(total), total / float(sc.FRAME))
        else:
            out[hl] = (None, 0.0, total / float(sc.FRAME))
    return out


def time_purity(rec):
    turns = rec.get("turns")
    if not turns:
        return None
    tot = good = 0.0
    for t in turns:
        d = float(t["end"]) - float(t["start"])
        if d <= 0:
            continue
        tot += d
        if float(t.get("score", 0)) >= TURN_SCORE_BAR:
            good += d
    return good / tot if tot else None


def enrolled_names(rundir, mid, mode, records):
    names = set()
    d = os.path.join(rundir, mid, mode + "-refs")
    if os.path.isdir(d):
        names |= {os.path.splitext(f)[0] for f in os.listdir(d) if f.lower().endswith(".wav")}
    names |= {r["name"] for r in records if r.get("pass") == "ref" and r.get("name")}
    return names or None


def classify_clusters(truth, doc, enrolled):
    """-> list of {meeting-less} dicts: category, cluster, speaker, purity_time,
    similarity, candidate, purity, purity_time_weighted."""
    segs = doc.get("segments", [])
    recs = doc.get("registry_matches", [])
    cmap = cluster_truth_map(truth, segs)
    by_cluster = {}
    for r in recs:
        by_cluster.setdefault(r["cluster"], []).append(r)

    def matched_for(cluster, name, pass1_only=False):
        return [r for r in by_cluster.get(cluster, [])
                if r.get("matched") and r.get("name") == name
                and (not pass1_only or r.get("pass") in PASS1)]

    def best(rs):
        return max(rs, key=lambda r: r["similarity"]) if rs else None

    def holder(spk, exclude):
        """Another cluster whose majority speaker is spk and which holds the name."""
        for pass1 in (True, False):
            for c, (m, _p, _s) in sorted(cmap.items()):
                if c != exclude and m == spk and matched_for(c, spk, pass1):
                    return c
        return None

    out = []
    for c in sorted(by_cluster):
        spk, pur, _secs = cmap.get(c, (None, 0.0, 0.0))
        if spk is None:
            continue
        recs_c = by_cluster[c]
        row = {"cluster": c, "speaker": spk, "purity_time": pur, "category": None,
               "similarity": None, "candidate": None, "purity": None, "purity_tw": None}
        if enrolled is not None and spk not in enrolled:
            b = best(recs_c)
            row.update(category="unenrolled", similarity=b["similarity"], candidate=b["name"])
        elif matched_for(c, spk, True):
            b = best(matched_for(c, spk, True))
            row.update(category="correct", similarity=b["similarity"], candidate=spk)
        elif pur >= PURITY_BAR and holder(spk, c):
            named = [r for r in recs_c if r.get("name") == spk]
            if not named:
                continue
            b = best(named)
            row.update(category="over_split", similarity=b["similarity"], candidate=spk,
                       purity=b.get("purity"), purity_tw=time_purity(b))
        elif pur < PURITY_BAR:
            b = best(recs_c)
            row.update(category="blend", similarity=b["similarity"], candidate=b["name"],
                       purity=b.get("purity"), purity_tw=time_purity(b))
        else:
            continue
        out.append(row)
    return out


def embedding_stats(rows):
    def sims(cat):
        return [r["similarity"] for r in rows if r["category"] == cat]

    def pur(cat, key):
        return [r[key] for r in rows if r["category"] == cat and r[key] is not None]

    return {
        "emb_correct_pass1": stat(sims("correct")),
        "emb_over_split": stat(sims("over_split")),
        "emb_blend": stat(sims("blend")),
        "emb_unenrolled": stat(sims("unenrolled")),
        "emb_purity_split": stat(pur("over_split", "purity")),
        "emb_purity_blend": stat(pur("blend", "purity")),
        "emb_purity_tw_split": stat(pur("over_split", "purity_tw")),
        "emb_purity_tw_blend": stat(pur("blend", "purity_tw")),
    }


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load_meetings(meetings_dir, run_dir, mode):
    """-> list of (id, truth, sidecar-doc, enrolled) for ids present in both."""
    out = []
    for mid in sorted(os.listdir(meetings_dir)):
        tp = os.path.join(meetings_dir, mid, "truth.json")
        hp = os.path.join(run_dir, mid, mode, "%s_%s.diarization.json" % (mid, mode))
        if not (os.path.isfile(tp) and os.path.isfile(hp)):
            continue
        truth = sc.load_truth(tp)
        with open(hp) as f:
            doc = json.load(f)
        out.append((mid, truth, doc, enrolled_names(run_dir, mid, mode, doc.get("registry_matches", []))))
    return out


def truth_segments(truth):
    return [{"start": t["start"], "end": t["end"], "speaker": t["speaker"]}
            for t in truth.get("turns", [])]


def doc_speakers(doc, segs):
    if doc.get("num_speakers") is not None:
        return doc["num_speakers"]
    return len({str(s["speaker"]) for s in segs})


def compute(meetings, real):
    edges = real["turn_table"]["edges_s"]
    gap_thr = real["gap"]["threshold_s"]
    diar, tru, emb_rows = [], [], []
    for mid, truth, doc, enrolled in meetings:
        segs = doc.get("segments", [])
        d_min = (doc.get("source") or {}).get("duration_seconds")
        d_min = d_min / 60.0 if d_min else None
        diar.append(seg_stats(segs, d_min, edges, doc_speakers(doc, segs)))
        tsegs = truth_segments(truth)
        t_min = truth.get("duration")
        tru.append(seg_stats(tsegs, t_min / 60.0 if t_min else None, edges,
                             len({t["speaker"] for t in tsegs})))
        for r in classify_clusters(truth, doc, enrolled):
            r["meeting"] = mid
            emb_rows.append(r)
    sd = aggregate_side(diar, edges, gap_thr)
    st = aggregate_side(tru, edges, gap_thr)
    sd.update(embedding_stats(emb_rows))
    return sd, st, emb_rows


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def real_stat(real, key):
    """real_stats.json -> stat-object for a metric key (None when not given)."""
    def rng(d):
        return {"value": d.get("median"), "min": d.get("min"), "max": d.get("max"), "n": None}
    tt = real["turn_table"]
    emb = real["embedding"]
    if key in ("meeting_minutes", "speakers_per_meeting", "speakers_under_2min_per_hour",
               "top_speaker_share", "switches_per_min", "overlap_share"):
        return rng(real[key])
    if key == "gap_median_s":
        return scalar(real["gap"]["median_s"])
    if key == "gap_share_under":
        return scalar(real["gap"]["share_under_0.3s"])
    if key.startswith("turn_share_"):
        return scalar(tt["share_of_turns"][int(key.rsplit("_", 1)[1])])
    if key.startswith("talk_share_"):
        return scalar(tt["share_of_talk"][int(key.rsplit("_", 1)[1])])
    m = {"emb_correct_pass1": "correct_pass1", "emb_over_split": "over_split",
         "emb_blend": "blend", "emb_unenrolled": "unenrolled",
         "emb_purity_split": "purity_split", "emb_purity_blend": "purity_blend"}
    if key in m:
        return rng(emb[m[key]])
    return None  # e.g. time-weighted purity: no real counterpart


def metric_list(real):
    bins = real["turn_table"]["bins"]
    rows = [("meeting minutes", "meeting_minutes", "range"),
            ("speakers per meeting", "speakers_per_meeting", "range"),
            ("speakers under 2 min/hour of talk", "speakers_under_2min_per_hour", "range"),
            ("top-speaker talk share", "top_speaker_share", "pct"),
            ("speaker switches / min", "switches_per_min", "range"),
            ("gap median (s)", "gap_median_s", "num"),
            ("gaps under 0.3 s", "gap_share_under", "pct"),
            ("overlap share of talk", "overlap_share", "pct")]
    for i, b in enumerate(bins):
        rows.append(("turns %s: share of turns" % b, "turn_share_%d" % i, "pct"))
    for i, b in enumerate(bins):
        rows.append(("turns %s: share of talk" % b, "talk_share_%d" % i, "pct"))
    rows += [("cosine, correct pass-1", "emb_correct_pass1", "num"),
             ("cosine, over-split second cluster", "emb_over_split", "num"),
             ("cosine, blend best candidate", "emb_blend", "num"),
             ("cosine, unenrolled best", "emb_unenrolled", "num"),
             ("purity (recorded), splits", "emb_purity_split", "num"),
             ("purity (recorded), blends", "emb_purity_blend", "num"),
             ("purity (time-weighted), splits", "emb_purity_tw_split", "num"),
             ("purity (time-weighted), blends", "emb_purity_tw_blend", "num")]
    return rows


def fmt(s, kind):
    if s is None or s.get("value") is None:
        return "-"
    f = (lambda x: "%.1f%%" % (100 * x)) if kind == "pct" else (lambda x: "%.2f" % x)
    out = f(s["value"])
    if s.get("min") is not None and s.get("max") is not None and s["min"] != s["max"]:
        out += " (%s-%s)" % (f(s["min"]).rstrip("%"), f(s["max"]))
    if s.get("n") is not None and kind == "num" and s["n"] and s["min"] is not None:
        out += " n=%d" % s["n"]
    return out


def fmt_real(s, kind):
    if s is None:
        return "-"
    f = (lambda x: "%.1f%%" % (100 * x)) if kind == "pct" else (lambda x: "%.2f" % x)
    lo, hi, v = s.get("min"), s.get("max"), s.get("value")
    if v is not None and lo is not None:
        return "%s (%s-%s)" % (f(v), f(lo).rstrip("%"), f(hi))
    if v is not None:
        return f(v)
    if lo is not None:
        return "%s-%s" % (f(lo).rstrip("%"), f(hi))
    return "-"


def build_report(sd, st, real):
    tol = real.get("tolerance", 0.2)
    rows = []
    for label, key, kind in metric_list(real):
        r = real_stat(real, key)
        rows.append({"metric": label, "key": key, "kind": kind,
                     "synthetic_diarizer": sd.get(key), "synthetic_truth": st.get(key),
                     "real": r, "within": within(sd.get(key), r, tol)})
    return rows


def render_markdown(rows, header=""):
    lines = [header] if header else []
    lines.append("| metric | synthetic (diarizer) | synthetic (truth) | real | within +-20%? |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        k = r["kind"]
        flag = r["within"]
        if flag == "NO":
            flag = "**NO**"
        lines.append("| %s | %s | %s | %s | %s |" % (
            r["metric"], fmt(r["synthetic_diarizer"], k), fmt(r["synthetic_truth"], k),
            fmt_real(r["real"], k), flag))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--meetings", required=True, help="dir with <id>/truth.json")
    ap.add_argument("--run", required=True, help="run dir with <id>/<mode>/<id>_<mode>.diarization.json")
    ap.add_argument("--mode", default="refs")
    ap.add_argument("--json", help="also write the rows here")
    ap.add_argument("--real", default=REAL_PATH, help="real_stats.json (default: next to this file)")
    a = ap.parse_args(argv)
    with open(a.real) as f:
        real = json.load(f)
    meetings = load_meetings(os.path.expanduser(a.meetings), os.path.expanduser(a.run), a.mode)
    if not meetings:
        print("no meetings found with both truth.json and a %s sidecar" % a.mode, file=sys.stderr)
        return 2
    sd, st, emb_rows = compute(meetings, real)
    rows = build_report(sd, st, real)
    flagged = sum(1 for r in rows if r["within"] == "NO")
    judged = sum(1 for r in rows if r["within"] != "n/a")
    header = "Calibration vs real corpus (%s): %d meetings, mode %s; %d of %d judged metrics outside tolerance\n" % (
        real.get("source"), len(meetings), a.mode, flagged, judged)
    print(render_markdown(rows, header))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"source": real.get("source"), "meetings": [m[0] for m in meetings],
                       "mode": a.mode, "rows": rows, "clusters": emb_rows}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
