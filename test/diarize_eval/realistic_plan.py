#!/usr/bin/env python3
"""Planner for the `realistic` synthetic meetings of GitHub issue #80.

No audio is mixed here. `plan_meeting` returns a JSON-able plan (schema 1) that a
renderer turns into audio; `phrase_bank` splits every pool clip into phrases at
internal silences so a turn can be assembled from consecutive phrases.

Plan schema (the contract with the renderer):
  {"schema": 1, "id": "m<seed>-<idx:03d>", "seed": S, "duration": seconds,
   "tags": ["realistic", "n8", ...],
   "speakers": [{"voice", "name", "channel": "local"|"remote", "share_target"}],
   "epochs": {vid: [{"start", "end", "tempo", "gain_db", "drift_seed"}]},
   "turns": [{"speaker", "start", "end", "kind": "turn"|"backchannel",
              "pieces": [{"clip", "line_id", "t0", "t1", "at"}]}],
   "overlap_target": f}
All planned times are POST-tempo: a piece of source duration d in an epoch with
tempo f occupies d/f seconds; `at` is the piece offset from the turn start.

Needs numpy (`uv run --with numpy ...`).
"""
import bisect
import json
import math
import os
import random
import re
import statistics
import wave

import numpy as np

LEAD = 0.5
TRAIL = 0.5
FRAME = 0.010
SIL_DB = -35.0
SIL_RUN = 0.180
PAD = 0.030
MIN_PHRASE = 0.15
REUSE_WINDOW = 180.0

# (lo, hi) in seconds; share of turns from issue #80. The last bin is 8-30 s.
BINS = [(0.0, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 30.0)]
BIN_SHARE = [0.26, 0.18, 0.21, 0.21, 0.14]
BIN_TARGETS = [0.26, 0.18, 0.21, 0.21, 0.14]


# ---------------------------------------------------------------------------
# phrase bank
# ---------------------------------------------------------------------------

def _read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def split_phrases(x, sr):
    """[(t0, t1)] phrases of a mono signal, split at silences >= 180 ms that are
    below -35 dB re the clip's peak 10 ms frame. Leading/trailing silence is
    trimmed (30 ms pad kept)."""
    n = int(round(sr * FRAME))
    nf = len(x) // n
    total = len(x) / sr
    if nf == 0:
        return []
    fr = x[: nf * n].reshape(nf, n)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    peak = rms.max()
    if peak <= 0:
        return []
    db = 20.0 * np.log10(np.maximum(rms, 1e-9) / peak)
    voiced = db >= SIL_DB
    need = int(round(SIL_RUN / FRAME))
    runs = []  # (start_frame, end_frame_exclusive) of voiced stretches
    i = 0
    while i < nf:
        if not voiced[i]:
            i += 1
            continue
        j = i
        last = i  # last voiced frame
        sil = 0
        while j < nf:
            if voiced[j]:
                last = j
                sil = 0
            else:
                sil += 1
                if sil >= need:
                    break
            j += 1
        runs.append((i, last + 1))
        i = last + 1 + sil if sil >= need else nf
    out = []
    for a, b in runs:
        t0 = max(0.0, a * FRAME - PAD)
        t1 = min(total, b * FRAME + PAD)
        if t1 - t0 >= MIN_PHRASE:
            out.append((round(t0, 3), round(t1, 3)))
    if not out:  # unsplittable / too short: keep the whole clip
        out = [(0.0, round(total, 3))]
    return out


def phrase_bank(pool_root, voices, cache_path=None):
    """{vid: [{"clip", "line_id", "t0", "t1", "dur"}]}, clips in pool order and
    phrases in time order. Cached by rel path + size + mtime."""
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    fresh = {}
    bank = {}
    dirty = False
    for vid in sorted(voices):
        items = []
        for c in voices[vid].get("clips", []):
            rel = c["path"]
            full = os.path.join(pool_root, rel)
            try:
                st = os.stat(full)
            except OSError:
                continue
            key = [st.st_size, st.st_mtime_ns]
            hit = cache.get(rel)
            if hit and hit.get("key") == key:
                spans = [tuple(s) for s in hit["spans"]]
            else:
                x, sr = _read_wav(full)
                spans = split_phrases(x, sr)
                dirty = True
            fresh[rel] = {"key": key, "spans": [list(s) for s in spans]}
            for t0, t1 in spans:
                items.append({"clip": rel, "line_id": c.get("line_id", ""), "t0": t0,
                              "t1": t1, "dur": round(t1 - t0, 3)})
        bank[vid] = items
    if cache_path and (dirty or set(fresh) != set(cache)):
        tmp = cache_path + ".tmp%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(fresh, f)
        os.replace(tmp, cache_path)
    return bank


# ---------------------------------------------------------------------------
# voice choice
# ---------------------------------------------------------------------------

def pick_voices(rng, voices, n, hard):
    """Copied from build.py: (ids, achieved) with achieved 'full'|'gender'|'none'."""
    ids = sorted(voices)
    if not hard:
        return rng.sample(ids, n), "none"
    groups = {}
    for vid in ids:
        groups.setdefault((voices[vid]["gender"], voices[vid]["accent"]), []).append(vid)
    ok = sorted(k for k, g in groups.items() if len(g) >= n)
    if ok:
        k = ok[rng.randrange(len(ok))]
        return rng.sample(groups[k], n), "full"
    by_gender = {}
    for vid in ids:
        by_gender.setdefault(voices[vid]["gender"], []).append(vid)
    big = sorted(by_gender, key=lambda g: (-len(by_gender[g]), g))[0]
    if len(by_gender[big]) >= n:
        pool = by_gender[big]
        accs = {}
        for v in pool:
            accs.setdefault(voices[v]["accent"], []).append(v)
        a = sorted(accs, key=lambda x: (-len(accs[x]), x))[0]
        chosen = list(accs[a])
        rng.shuffle(chosen)
        rest = [v for v in pool if v not in chosen]
        rng.shuffle(rest)
        return (chosen + rest)[:n], "gender"
    return rng.sample(ids, n), "none"


def _choose_voices(rng, voices, eligible, n, roster):
    """(ids, tags). Roster voices first; fill with same-gender voices (same
    accent first, then at most 2 of another accent). Caps n if the pool is short."""
    tags = []
    sub = {v: voices[v] for v in eligible}
    if n > len(sub):
        n = len(sub)
        tags.append("capped-n")
    if roster is not None:
        chosen = sorted(v for v in sub if sub[v].get("roster") == roster)
        rng.shuffle(chosen)
        chosen = chosen[:n]
        if chosen:
            tags.append("roster")
        rest = sorted(v for v in sub if v not in chosen)
        if len(chosen) < n:
            genders = sorted({sub[v]["gender"] for v in chosen}) or sorted({sub[v]["gender"] for v in sub})
            gender = chosen and max(genders, key=lambda g: sum(sub[v]["gender"] == g for v in chosen)) \
                or genders[rng.randrange(len(genders))]
            accents = [sub[v]["accent"] for v in chosen if sub[v]["gender"] == gender]
            if accents:
                accent = max(sorted(set(accents)), key=accents.count)
            else:
                cnt = {}
                for v in rest:
                    if sub[v]["gender"] == gender:
                        cnt[sub[v]["accent"]] = cnt.get(sub[v]["accent"], 0) + 1
                accent = sorted(cnt, key=lambda a: (-cnt[a], a))[0] if cnt else None
            same = [v for v in rest if sub[v]["gender"] == gender and sub[v]["accent"] == accent]
            other = [v for v in rest if sub[v]["gender"] == gender and sub[v]["accent"] != accent]
            rng.shuffle(same)
            rng.shuffle(other)
            take = same[: n - len(chosen)]
            chosen += take
            if len(chosen) < n:
                chosen += other[: min(2, n - len(chosen))]
            if len(chosen) < n:  # still short: any gender, rather than shrink n
                left = [v for v in rest if v not in chosen]
                rng.shuffle(left)
                chosen += left[: n - len(chosen)]
                tags.append("mixed-gender")
        if len(chosen) < n:
            n = len(chosen)
            if "capped-n" not in tags:
                tags.append("capped-n")
        return chosen, tags
    ids, ach = pick_voices(rng, sub, n, hard=True)
    if ach != "full":
        tags.append("accent-mixed" if ach == "gender" else "gender-mixed")
    return ids, tags


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _shares(rng, n):
    """Talk-share targets (sum 1): top 25-75% (median ~41%), a few speakers tiny."""
    top = min(0.75, max(0.25, math.exp(rng.gauss(math.log(0.41), 0.28))))
    k = min(max(1, n - 2), rng.randint(max(1, n // 4), max(1, n // 2)))
    tiny = [rng.uniform(0.004, 0.025) for _ in range(k)]
    mid_n = n - 1 - k
    rest = 1.0 - top - sum(tiny)
    w = [math.exp(rng.gauss(0, 0.6)) for _ in range(mid_n)]
    mid = [rest * x / sum(w) for x in w] if mid_n else []
    if not mid_n:
        tiny = [t + rest / k for t in tiny]
    sh = [top] + mid + tiny
    s = sum(sh)
    return [x / s for x in sh]


def _make_epochs(rng, duration):
    eps = []
    t = 0.0
    while t < duration - 1e-9:
        length = rng.uniform(300.0, 1200.0)
        if duration - (t + length) < 300.0:
            length = duration - t
        eps.append({"start": t, "end": t + length, "tempo": round(rng.uniform(0.94, 1.06), 4),
                    "gain_db": round(rng.uniform(-3.0, 3.0), 2),
                    "drift_seed": rng.randrange(2 ** 31)})
        t += length
    return eps


def _sample_target(rng, adj=None):
    ws = [s * (adj[i] if adj else 1.0) for i, s in enumerate(BIN_SHARE)]
    r = rng.random() * sum(ws)
    acc = 0.0
    b = len(BINS) - 1
    for i, s in enumerate(ws):
        acc += s
        if r < acc:
            b = i
            break
    lo, hi = BINS[b]
    if b == 0:
        return b, rng.uniform(0.3, 0.92)
    if b == 4:
        return b, min(28.0, 8.8 + rng.expovariate(1.0 / 4.5))
    w = hi - lo
    return b, rng.uniform(lo + 0.08 * w, hi - 0.12 * w)


class _Ctx:
    pass


def _tempo_at(ctx, vid, t):
    starts, eps = ctx.ep_idx[vid]
    i = max(0, bisect.bisect_right(starts, t) - 1)
    return eps[i]["tempo"]


def _build_turn(rng, ctx, vid, start, target, bin_idx, max_dur=None):
    """Assemble pieces for one turn. Returns (pieces, end) and marks phrases used."""
    flat = ctx.flat[vid]
    nxt = ctx.nxt[vid]
    recent = ctx.recent[vid]
    cap = 0.99 if bin_idx == 0 else 1.15 * target
    if max_dur is not None:
        cap = min(cap, max_dur)
    lo_ok = 0.85 * target

    def fresh(p):
        return start - recent.get((p["clip"], p["t0"]), -1e9) >= REUSE_WINDOW

    best = None
    for _ in range(8):
        pieces = []
        used = set()
        cur = start
        f0 = _tempo_at(ctx, vid, cur)
        cands = [p for p in flat if p["dur"] / f0 <= cap]
        pool = [p for p in cands if fresh(p)] or cands
        if not pool:
            pool = [min(flat, key=lambda p: p["dur"])]
        # prefer a first phrase that is not much shorter than needed
        p = pool[rng.randrange(len(pool))]
        while True:
            f = _tempo_at(ctx, vid, cur)
            pieces.append({"clip": p["clip"], "line_id": p["line_id"], "t0": p["t0"],
                           "t1": p["t1"], "at": cur - start})
            used.add((p["clip"], p["t0"]))
            cur += p["dur"] / f
            if cur - start >= lo_ok:
                break
            q = nxt.get((p["clip"], p["t0"]))
            if q is not None:
                pause = (q["t0"] - p["t1"]) / f
                if cur + pause + q["dur"] / _tempo_at(ctx, vid, cur + pause) - start <= cap:
                    cur += pause
                    p = q
                    continue
            rem = cap - (cur - start) - 0.3
            c2 = [x for x in flat if (x["clip"], x["t0"]) not in used
                  and x["dur"] / f <= rem and x["dur"] / f >= 0.4 * rem]
            if not c2:
                c2 = [x for x in flat if (x["clip"], x["t0"]) not in used and x["dur"] / f <= rem]
            c2f = [x for x in c2 if fresh(x)] or c2
            if not c2f:
                break
            cur += rng.uniform(0.2, 0.5)
            p = c2f[rng.randrange(len(c2f))]
        total = cur - start
        score = abs(total - target) / target + (10.0 if total > cap + 1e-9 else 0.0)
        if best is None or score < best[0]:
            best = (score, pieces, cur)
        if score <= 0.15:
            break
    _, pieces, end = best
    for pc in pieces:
        recent[(pc["clip"], pc["t0"])] = end
    return pieces, end


def _parse_mid(mid):
    m = re.match(r"^m(\d+)-(\d+)$", mid)
    if not m:
        raise ValueError("mid must look like m<seed>-<idx>: %r" % mid)
    return int(m.group(1)), int(m.group(2))


def plan_meeting(rng, voices, bank, *, minutes=(30, 120), speakers=(6, 10), roster=None,
                 mid="m0-000"):
    seed, idx = _parse_mid(mid)
    eligible = sorted(v for v in voices if bank.get(v))
    if not eligible:
        raise ValueError("no voice has any phrase in the bank")
    n_req = rng.randint(speakers[0], speakers[1])
    vids, tags = _choose_voices(rng, voices, eligible, n_req, roster)
    n = len(vids)
    D = rng.uniform(minutes[0], minutes[1]) * 60.0
    if minutes[0] <= 44 <= minutes[1] and rng.random() < 0.5:  # median ~44 min
        D = min(D, rng.uniform(minutes[0], max(minutes[0], 44)) * 60.0)
    overlap_target = min(0.08, max(0.012, math.exp(rng.gauss(math.log(0.04), 0.4))))
    rate = min(0.6, max(0.45, rng.gauss(0.52, 0.04)))  # switches per turn

    shares = _shares(rng, n)
    order = list(vids)
    rng.shuffle(order)
    share = dict(zip(order, shares))
    local = vids[rng.randrange(n)]
    spk_order = sorted(vids)

    ctx = _Ctx()
    epochs = {v: _make_epochs(rng, D) for v in spk_order}
    ctx.ep_idx = {v: ([e["start"] for e in epochs[v]], epochs[v]) for v in spk_order}
    ctx.flat = {v: bank[v] for v in spk_order}
    ctx.nxt = {}
    for v in spk_order:
        d = {}
        byclip = {}
        for p in bank[v]:
            byclip.setdefault(p["clip"], []).append(p)
        for lst in byclip.values():
            lst.sort(key=lambda p: p["t0"])
            for a, b in zip(lst, lst[1:]):
                d[(a["clip"], a["t0"])] = b
        ctx.nxt[v] = d
    ctx.recent = {v: {} for v in spk_order}

    # P(independent draw switches) ~ 1 - sum(share^2); force extra switches up to `rate`
    s_ind = 1.0 - sum(x * x for x in shares)
    p_stay = 0.0 if s_ind <= rate else 1.0 - rate / s_ind
    n_main = 0
    n_sw = 0
    bin_cnt = [0] * len(BINS)
    edges = [b[0] for b in BINS[1:]]

    talk = {v: 0.0 for v in spk_order}
    last_end = {v: -1e9 for v in spk_order}
    turns = []
    ov_secs = 0.0
    talk_total = 0.0
    prev = None  # (speaker, start, end)

    def weights(excl=None):
        out = []
        for v in spk_order:
            if v == excl:
                out.append(0.0)
                continue
            exp = share[v] * (talk_total + 30.0)
            out.append(max(exp - talk[v], 0.05 * exp, 1e-6))
        return out

    def draw(ws):
        r = rng.random() * sum(ws)
        for v, w in zip(spk_order, ws):
            r -= w
            if r < 0:
                return v
        return spk_order[-1]

    t = LEAD
    guard = 0
    while guard < 200000:
        guard += 1
        if prev is None:
            spk = draw(weights())
            start = t
        else:
            ps, qf = p_stay, 0.0
            if n_main >= 20:  # feedback on the realised switch rate per turn
                err = n_sw / n_main - rate
                ps = min(0.92, max(0.0, p_stay + 2.5 * err))
                qf = min(0.9, max(0.0, -6.0 * err))
            if rng.random() < qf:
                spk = draw(weights(excl=prev[0]))
            elif rng.random() < ps:
                spk = prev[0]
            else:
                spk = draw(weights())
            if spk != prev[0]:
                want_ov = ov_secs < overlap_target * talk_total
                if rng.random() < (0.2 if want_ov else 0.02):
                    ov = min(rng.uniform(0.2, 1.2), 0.5 * (prev[2] - prev[1]))
                    start = prev[2] - ov
                else:
                    gap = min(6.0, max(0.05, math.exp(rng.gauss(math.log(0.95), 1.15))))
                    start = prev[2] + gap
            else:
                start = prev[2] + rng.uniform(0.3, 1.5)
        start = max(start, last_end[spk] + 0.2)
        if start > D - TRAIL - 0.3:
            break
        n_all = sum(bin_cnt)
        adj = None
        if n_all >= 30:  # steer the realised table (backchannels included) to the targets
            adj = [max(0.25, min(2.5, 1.0 + 2.0 * (BIN_SHARE[i] - bin_cnt[i] / n_all) / BIN_SHARE[i]))
                   for i in range(len(BINS))]
        b, target = _sample_target(rng, adj)
        room = D - TRAIL - start
        if target > room:
            if room < 0.5:
                break
            target = room
            b = 0 if target < 1.0 else b
        pieces, end = _build_turn(rng, ctx, spk, start, target, b)
        for pc in pieces:
            pc["at"] = round(pc["at"], 4)
        dur = end - start
        bin_cnt[bisect.bisect_right(edges, dur)] += 1
        n_main += 1
        if prev is not None and prev[0] != spk:
            n_sw += 1
        turns.append({"speaker": spk, "start": start, "end": end, "kind": "turn", "pieces": pieces})
        talk[spk] += dur
        talk_total += dur
        last_end[spk] = end
        if prev is not None and prev[0] != spk and start < prev[2]:
            ov_secs += prev[2] - start
        # backchannel over a long turn
        if dur >= 5.0:
            want_ov = ov_secs < overlap_target * talk_total
            if rng.random() < (0.5 if want_ov else 0.04):
                others = [v for v in spk_order if v != spk]
                ws = [0.0 if v == spk else w for v, w in zip(spk_order, weights(excl=spk))]
                bv = draw(ws)
                bs = start + rng.uniform(0.25, 0.8) * dur
                f = _tempo_at(ctx, bv, bs)
                if bs > last_end[bv] + 0.3 and others:
                    room_bc = end - 0.8 - bs
                    if room_bc > 0.25:
                        bp, bend = _build_turn(rng, ctx, bv, bs, rng.uniform(0.3, 0.8), 0,
                                               max_dur=room_bc)
                        if bend - bs <= room_bc + 1e-9:
                            for pc in bp:
                                pc["at"] = round(pc["at"], 4)
                            turns.append({"speaker": bv, "start": bs, "end": bend,
                                          "kind": "backchannel", "pieces": bp})
                            bin_cnt[bisect.bisect_right(edges, bend - bs)] += 1
                            talk[bv] += bend - bs
                            talk_total += bend - bs
                            ov_secs += bend - bs
                            last_end[bv] = bend
        prev = (spk, start, end)

    turns.sort(key=lambda x: (x["start"], x["end"], x["speaker"]))
    duration = max([x["end"] for x in turns] + [LEAD]) + TRAIL
    for v in spk_order:
        eps = [e for e in epochs[v] if e["start"] < duration]
        eps[-1]["end"] = duration
        epochs[v] = eps
    for x in turns:
        x["start"] = round(x["start"], 3)
        x["end"] = round(x["end"], 3)
    for v in spk_order:
        for e in epochs[v]:
            e["start"] = round(e["start"], 3)
            e["end"] = round(e["end"], 3)
    for i in range(len(spk_order)):
        # keep epoch seams exact after rounding
        eps = epochs[spk_order[i]]
        for a, b in zip(eps, eps[1:]):
            b["start"] = a["end"]
    names = []
    for v in sorted(vids):
        names.append({"voice": v, "name": voices[v].get("name", v),
                      "channel": "local" if v == local else "remote",
                      "share_target": round(share[v], 4)})
    return {"schema": 1, "id": "m%d-%03d" % (seed, idx), "seed": seed,
            "duration": round(duration, 3),
            "tags": ["realistic", "n%d" % n] + tags,
            "speakers": names, "epochs": epochs, "turns": turns,
            "overlap_target": round(overlap_target, 4)}


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def plan_stats(plan):
    """Measure a plan. Switches/min and gaps use kind=="turn" turns only
    (backchannels are overlay, not floor changes); the turn table and the
    overlap fraction include everything."""
    turns = plan["turns"]
    dur = plan["duration"]
    lens = [t["end"] - t["start"] for t in turns]
    total = sum(lens) or 1.0
    edges = [b[0] for b in BINS[1:]]
    cnt = [0] * len(BINS)
    tk = [0.0] * len(BINS)
    for L in lens:
        i = bisect.bisect_right(edges, L)
        cnt[i] += 1
        tk[i] += L
    names = ["<1", "1-2", "2-4", "4-8", ">8"]
    table = {nm: {"turns": cnt[i] / max(1, len(lens)), "talk": tk[i] / total}
             for i, nm in enumerate(names)}
    main = [t for t in turns if t["kind"] == "turn"]
    sw = 0
    gaps = []
    for a, b in zip(main, main[1:]):
        if a["speaker"] != b["speaker"]:
            sw += 1
            gaps.append(b["start"] - a["end"])
    ev = []
    for t in turns:
        ev.append((t["start"], 1))
        ev.append((t["end"], -1))
    ev.sort(key=lambda e: (e[0], e[1]))
    ov = 0.0
    act = 0
    last = 0.0
    for tm, d in ev:
        if act >= 2:
            ov += (tm - last) * (act - 1)
        act += d
        last = tm
    per = {}
    for t in turns:
        per[t["speaker"]] = per.get(t["speaker"], 0.0) + (t["end"] - t["start"])
    for s in plan["speakers"]:
        per.setdefault(s["voice"], 0.0)
    under = sum(1 for v in per.values() if v / dur * 3600.0 < 120.0)
    return {"minutes": dur / 60.0, "n_speakers": len(plan["speakers"]), "n_turns": len(turns),
            "turn_table": table, "switches_per_min": sw / (dur / 60.0),
            "gap_median": statistics.median(gaps) if gaps else None,
            "gap_lt_0_3": (sum(1 for g in gaps if g < 0.3) / len(gaps)) if gaps else None,
            "overlap_frac": ov / total, "top_share": max(per.values()) / total,
            "under_2min_per_hour": under}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="plan one realistic meeting and print its stats")
    ap.add_argument("--pool", required=True, help="pool.json")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--out", default=None, help="write plan JSON here")
    a = ap.parse_args()
    root = os.path.dirname(os.path.abspath(a.pool))
    voices = json.load(open(a.pool))["voices"]
    bank = phrase_bank(root, voices, a.cache)
    plan = plan_meeting(random.Random(a.seed), voices, bank, mid="m%d-000" % a.seed)
    if a.out:
        json.dump(plan, open(a.out, "w"))
    print(json.dumps(plan_stats(plan), indent=1))


if __name__ == "__main__":
    main()
