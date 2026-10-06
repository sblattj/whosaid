#!/usr/bin/env python3
"""Assemble synthetic multi-speaker meetings from a rendered voice pool.

    python3 test/diarize_eval/build.py --pool POOL.json --out DIR --seed S --count N
        [--profile mixed|easy|hard|long] [--min-speakers 2 --max-speakers 6]

stdlib only. Every random choice comes from random.Random(seed * 100003 + i),
so a (seed, index) pair reproduces a meeting byte-for-byte, independent of
--count. Output per meeting: <out>/m<seed>-<i:03d>/{audio.wav,truth.json};
plus <out>/index.json. See /tmp/whosaid-synth/spec.md section 3 (format).
"""
import argparse
import json
import math
import os
import random
import sys
import wave
from array import array

SR = 16000
LEAD = 0.5
TRAIL = 0.5
SHORT_DUR = 1.5

PROFILES = {
    # p_* are probabilities that the scenario carries the feature.
    "mixed": dict(dur=(60, 240), p_n1=0.05, p_hard_voices=0.30, p_overlap=0.15, p_noise=0.15,
                  p_gain=0.20, p_backchannel=0.15, p_rapid=0.10,
                  patterns=(("balanced", 40), ("dominant", 20), ("rare-speaker", 15), ("cameo", 10))),
    "easy": dict(dur=(60, 150), p_n1=0.0, p_hard_voices=0.0, p_overlap=0.0, p_noise=0.0,
                 p_gain=0.0, p_backchannel=0.0, p_rapid=0.0,
                 patterns=(("balanced", 70), ("dominant", 30)), n_cap=3),
    "hard": dict(dur=(90, 240), p_n1=0.0, p_hard_voices=1.0, p_overlap=0.5, p_noise=0.4,
                 p_gain=0.4, p_backchannel=0.35, p_rapid=0.30, n_floor=3,
                 patterns=(("balanced", 30), ("dominant", 30), ("rare-speaker", 40), ("cameo", 20))),
    # 960-1300 s crosses whosaid's 900 s auto-chunk threshold
    # (lib/diarize_sherpa.py `wants_chunked`).
    "long": dict(dur=(960, 1300), p_n1=0.0, p_hard_voices=0.30, p_overlap=0.15, p_noise=0.15,
                 p_gain=0.20, p_backchannel=0.15, p_rapid=0.10, n_floor=3, n_cap=5,
                 patterns=(("balanced", 40), ("dominant", 20), ("rare-speaker", 15), ("cameo", 10))),
}
N_WEIGHTS = {2: 3.0, 3: 4.0, 4: 3.0, 5: 1.5, 6: 1.0}


def wchoice(rng, pairs):
    total = sum(w for _, w in pairs)
    x = rng.random() * total
    for v, w in pairs:
        x -= w
        if x < 0:
            return v
    return pairs[-1][0]


def read_wav(path, cache):
    if path in cache:
        return cache[path]
    with wave.open(path, "rb") as w:
        if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("clip is not 16 kHz mono s16: %s" % path)
        a = array("h")
        a.frombytes(w.readframes(w.getnframes()))
        if sys.byteorder == "big":
            a.byteswap()
    cache[path] = a
    return a


def pick_voices(rng, voices, n, hard):
    """Return (list of voice ids, achieved): achieved is 'full' (same gender+accent),
    'gender' (same gender only) or 'none'."""
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
    # Fall back: biggest same-gender pool, then fill from the biggest accent group first.
    by_gender = {}
    for vid in ids:
        by_gender.setdefault(voices[vid]["gender"], []).append(vid)
    big = sorted(by_gender, key=lambda g: (-len(by_gender[g]), g))[0]
    if len(by_gender[big]) >= n:
        pool = by_gender[big]
        # prefer sharing an accent as much as possible
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


def db_gain(db):
    return 10.0 ** (db / 20.0)


def scale(clip, g):
    if abs(g - 1.0) < 1e-9:
        return clip
    return array("h", [max(-32768, min(32767, int(round(x * g)))) for x in clip])


def make_noise(rng, n_samples):
    """Pink-ish noise: white noise through a one-pole low-pass (1 s * 8 block, tiled)."""
    block = 8 * SR
    out = []
    prev = 0.0
    for _ in range(block):
        w = rng.gauss(0.0, 1.0)
        prev = 0.85 * prev + 0.15 * w
        out.append(prev)
    return out


def plan_meeting(rng, voices, profile, args, seed, idx):
    P = PROFILES[profile]
    lo, hi = args.min_speakers, args.max_speakers
    lo = max(lo, P.get("n_floor", 1)) if hi >= P.get("n_floor", 1) else lo
    hi = min(hi, P.get("n_cap", 99))
    hi = min(hi, len(voices))
    lo = min(lo, hi)
    tags = []
    if P["p_n1"] and rng.random() < P["p_n1"]:
        n = 1
    else:
        cand = [k for k in range(max(lo, 2) if hi >= 2 else 1, hi + 1)] or [hi]
        n = wchoice(rng, [(k, N_WEIGHTS.get(k, 0.5)) for k in cand])
    tags.append("n%d" % n)
    hard = rng.random() < P["p_hard_voices"]
    vids, achieved = pick_voices(rng, voices, n, hard and n > 1)
    if hard and n > 1:
        tags.append("hard-voices" if achieved == "full" else
                    ("hard-voices-gender" if achieved == "gender" else "hard-voices-unmet"))
    pattern = None
    if n >= 2:
        # rare-speaker and cameo need someone else to carry the meeting.
        pat = [p for p in P["patterns"] if p[0] not in ("rare-speaker", "cameo") or n >= 3]
        pattern = wchoice(rng, pat)
        if args.pattern and (args.pattern not in ("rare-speaker", "cameo") or n >= 3):
            pattern = args.pattern
        tags.append(pattern)
    backchannel = n >= 2 and rng.random() < P["p_backchannel"]
    rapid = rng.random() < P["p_rapid"]
    overlap = n >= 2 and rng.random() < P["p_overlap"]
    noise = rng.random() < P["p_noise"]
    gain = rng.random() < P["p_gain"]
    if backchannel:
        tags.append("backchannel")
    if rapid:
        tags.append("rapid")
    if overlap:
        tags.append("overlap")
    if noise:
        tags.append("noise")
    if gain:
        tags.append("gain")
    target = rng.uniform(*P["dur"])
    return dict(n=n, vids=vids, tags=tags, pattern=pattern, backchannel=backchannel, rapid=rapid,
                overlap=overlap, noise=noise, gain=gain, target=target)


def build_meeting(pool_root, voices, profile, args, seed, idx, cache):
    rng = random.Random(seed * 100003 + idx)
    sc = plan_meeting(rng, voices, profile, args, seed, idx)
    vids = sc["vids"]
    n = sc["n"]
    gains = {v: (db_gain(rng.uniform(-8.0, 3.0)) if sc["gain"] else 1.0) for v in vids}
    gain_db = {v: round(20 * math.log10(gains[v]), 2) for v in vids}
    # speaker choice weights
    dom = vids[rng.randrange(n)] if sc["pattern"] == "dominant" else None
    # rare-speaker: one voice speaks 1-2 ordinary turns. cameo: one voice speaks only
    # 1-3 SHORT turns (< SHORT_DUR) -- the brief distinct guest the count estimator
    # must not fold into a regular.
    rare = vids[rng.randrange(n)] if sc["pattern"] in ("rare-speaker", "cameo") else None
    rare_marks = []
    if rare:
        k = rng.randint(1, 3) if sc["pattern"] == "cameo" else rng.randint(1, 2)
        rare_marks = sorted(rng.uniform(0.1, 0.9) * sc["target"] for _ in range(k))
    gap_lo, gap_hi = (0.05, 0.25) if sc["rapid"] else (0.2, 1.2)

    def pick_clip(vid, prev_key, want_short=False):
        clips = voices[vid]["clips"]
        if want_short:
            c = [x for x in clips if x["dur"] < SHORT_DUR] or [min(clips, key=lambda x: x["dur"])]
        else:
            c = [(x, {"short": 2.0, "medium": 4.0, "long": 3.0}.get(x["kind"], 3.0)) for x in clips]
            c = [(x, w) for x, w in c if (vid, x["line_id"]) != prev_key] or c
            return wchoice(rng, c)
        c = [x for x in c if (vid, x["line_id"]) != prev_key] or c
        return c[rng.randrange(len(c))]

    placed = []  # dicts: vid, clip, start_sample
    cursor = int(LEAD * SR)  # end of the latest-ending clip so far
    prev_spk = None
    prev_key = None
    prev_start = prev_end = None
    t_limit = sc["target"]

    def place(vid, clip, overlap_ok=True, force_gap=None):
        nonlocal cursor, prev_spk, prev_key, prev_start, prev_end
        arr = read_wav(os.path.join(pool_root, clip["path"]), cache)
        ln = len(arr)
        if not placed:
            start = int(LEAD * SR)
        else:
            if (overlap_ok and sc["overlap"] and prev_spk != vid and rng.random() < 0.30):
                ov = rng.uniform(0.2, 0.8)
                ov = min(ov, 0.5 * (prev_end - prev_start) / SR, 0.5 * ln / SR)
                start = prev_end - int(ov * SR)
                start = max(start, prev_start + 1)
            else:
                g = force_gap if force_gap is not None else rng.uniform(gap_lo, gap_hi)
                start = cursor + int(g * SR)
        placed.append((vid, clip, start, ln))
        prev_spk, prev_key = vid, (vid, clip["line_id"])
        prev_start, prev_end = start, start + ln
        cursor = max(cursor, start + ln)

    while (cursor / SR) < t_limit:
        # choose speaker
        if rare_marks and cursor / SR >= rare_marks[0]:
            rare_marks.pop(0)
            vid = rare
        elif n == 1:
            vid = vids[0]
        else:
            others = [v for v in vids if v != rare] or vids
            if sc["pattern"] == "dominant" and rng.random() < 0.6:
                vid = dom
            else:
                pool_ = [v for v in others if v != prev_spk] or others
                if sc["pattern"] == "dominant":
                    pool_ = [v for v in pool_ if v != dom] or pool_
                vid = pool_[rng.randrange(len(pool_))]
        clip = pick_clip(vid, prev_key, want_short=(vid == rare and sc["pattern"] == "cameo"))
        place(vid, clip)
        if sc["backchannel"] and clip["dur"] >= SHORT_DUR and rng.random() < 0.35:
            cands = [v for v in vids if v != vid and v != rare] or [v for v in vids if v != vid]
            bv = cands[rng.randrange(len(cands))]
            place(bv, pick_clip(bv, prev_key, want_short=True), overlap_ok=False,
                  force_gap=rng.uniform(0.05, 0.3))

    total = cursor + int(TRAIL * SR)
    acc = array("i", bytes(4 * total))
    for vid, clip, start, ln in placed:
        arr = read_wav(os.path.join(pool_root, clip["path"]), cache)
        arr = scale(arr, gains[vid])
        seg = acc[start:start + ln]
        acc[start:start + ln] = array("i", [a + b for a, b in zip(seg, arr)])
    if sc["noise"]:
        # 25 dB SNR relative to the RMS of the speech-bearing samples.
        mask_samples = 0
        energy = 0.0
        for vid, clip, start, ln in placed:
            seg = acc[start:start + ln]
            energy += sum(x * x for x in seg)
            mask_samples += ln
        sig_rms = math.sqrt(energy / max(1, mask_samples))
        nz = make_noise(rng, total)
        nrms = math.sqrt(sum(x * x for x in nz) / len(nz))
        k = (sig_rms / (10 ** (25 / 20.0))) / nrms if nrms else 0.0
        nzi = [int(round(x * k)) for x in nz]
        nl = len(nzi)
        off = rng.randrange(nl)
        pos = 0
        while pos < total:
            take = min(total - pos, nl - off)
            seg = acc[pos:pos + take]
            acc[pos:pos + take] = array("i", [a + b for a, b in zip(seg, nzi[off:off + take])])
            pos += take
            off = 0
    out = array("h", [32767 if x > 32767 else -32768 if x < -32768 else x for x in acc])
    if sys.byteorder == "big":
        out.byteswap()
    turns = []
    for vid, clip, start, ln in sorted(placed, key=lambda p: (p[2], p[3])):
        turns.append({"speaker": voices[vid]["name"], "start": round(start / SR, 5),
                      "end": round((start + ln) / SR, 5), "text": clip["text"],
                      "line_id": clip["line_id"]})
    speakers = [{"name": voices[v]["name"], "voice": v, "enrolled_clip": voices[v]["enroll"]["path"],
                 "gain_db": gain_db[v]} for v in vids]
    mid = "m%d-%03d" % (seed, idx)
    truth = {"schema": 1, "id": mid, "seed": seed, "tags": sc["tags"],
             "duration": round(total / SR, 3), "speakers": speakers, "turns": turns}
    return mid, truth, out


def write_atomic(path, data, binary=False):
    tmp = path + ".tmp"
    with open(tmp, "wb" if binary else "w") as f:
        f.write(data)
    os.replace(tmp, path)


def write_meeting(outdir, mid, truth, samples):
    d = os.path.join(outdir, mid)
    os.makedirs(d, exist_ok=True)
    wtmp = os.path.join(d, "audio.wav.tmp")
    with wave.open(wtmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(samples.tobytes())
    os.replace(wtmp, os.path.join(d, "audio.wav"))
    write_atomic(os.path.join(d, "truth.json"), json.dumps(truth, indent=1) + "\n")


def load_pool(path):
    with open(path) as f:
        pool = json.load(f)
    voices = pool["voices"]
    for vid, v in voices.items():
        if not v.get("clips") or not v.get("enroll"):
            raise SystemExit("voice %s has no clips/enroll" % vid)
    return os.path.dirname(os.path.abspath(path)), voices


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pool", required=True, help="pool.json from render.py")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--profile", default="mixed", choices=sorted(PROFILES))
    ap.add_argument("--pattern", choices=("balanced", "dominant", "rare-speaker", "cameo"),
                    help="force the talk pattern (rare-speaker/cameo apply only when n >= 3)")
    ap.add_argument("--min-speakers", type=int, default=2)
    ap.add_argument("--max-speakers", type=int, default=6)
    args = ap.parse_args(argv)
    if args.min_speakers > args.max_speakers or args.min_speakers < 1:
        raise SystemExit("bad speaker range")
    pool_root, voices = load_pool(args.pool)
    os.makedirs(args.out, exist_ok=True)
    cache = {}
    entries = []
    for i in range(args.count):
        mid, truth, samples = build_meeting(pool_root, voices, args.profile, args, args.seed, i, cache)
        write_meeting(args.out, mid, truth, samples)
        entries.append({"id": mid, "tags": truth["tags"], "duration": truth["duration"],
                        "n_speakers": len(truth["speakers"]), "n_turns": len(truth["turns"])})
        print("%s  %7.1fs  %2d turns  %s" % (mid, truth["duration"], len(truth["turns"]),
                                            ",".join(truth["tags"])))
    write_atomic(os.path.join(args.out, "index.json"), json.dumps(
        {"schema": 1, "seed": args.seed, "profile": args.profile, "pool_root": pool_root,
         "meetings": entries}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
