#!/usr/bin/env python3
"""Build `realistic` synthetic meetings (issue #80): CLI + renderer.

    uv run --with numpy python test/diarize_eval/realistic.py --pool POOL.json --out DIR \
        --seed S --count N [--minutes 30-120] [--speakers 6-10] [--roster TAG]

The planner (realistic_plan.py) decides who says what and when; the channel model
(channel.py) makes the local speaker sound near-mic and every other speaker sound like
a conferencing path, with per-epoch drift. This file only places pooled phrases on a
timeline, runs each speaker's epochs through the channel, mixes, adds room noise, and
round-trips the mix through AAC. It writes, per meeting, audio.wav (16 kHz mono s16),
truth.json (schema 1, see docs/diarize-eval.md), plan.json and enroll/<voice>.wav,
plus <out>/index.json in the shape build.py writes.

Memory: one float32 mix buffer plus one int16 speaker-epoch span at a time. No
per-sample Python loops.
"""
import argparse
import hashlib
import json
import os
import random
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import channel  # noqa: E402
import realistic_plan  # noqa: E402

SR = 16000
ENROLL_SECONDS = 60.0
CACHE_BYTES = 256 * 1024 * 1024


def derive(*parts):
    """Stable int seed from arbitrary parts (never Python hash(): it is salted)."""
    h = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big")


def write_atomic(path, data, binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb" if binary else "w") as f:
        f.write(data)
    os.replace(tmp, path)


def write_wav(path, samples):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.ascontiguousarray(samples, dtype="<i2").tobytes())
    os.replace(tmp, path)


def to_i16(x):
    return np.clip(np.rint(x), -32768, 32767).astype(np.int16)


class Clips:
    """Decoded-clip cache plus a (clip,t0,t1,tempo) cache, with a byte cap."""

    def __init__(self, pool_root):
        self.root = pool_root
        self.raw = {}
        self.slices = {}
        self.bytes = 0

    def _note(self, n):
        self.bytes += n
        if self.bytes > CACHE_BYTES:
            self.raw.clear()
            self.slices.clear()
            self.bytes = 0

    def read(self, rel):
        a = self.raw.get(rel)
        if a is None:
            with wave.open(os.path.join(self.root, rel), "rb") as w:
                if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
                    raise ValueError("%s: want 16 kHz mono s16" % rel)
                a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.int16)
            self._note(a.nbytes)
            self.raw[rel] = a
        return a

    def piece(self, rel, t0, t1, tempo):
        """Slice [t0,t1] s, tempo-stretched, padded/trimmed to the planned length."""
        key = (rel, round(t0, 4), round(t1, 4), round(tempo, 5))
        a = self.slices.get(key)
        if a is None:
            src = self.read(rel)
            seg = src[int(round(t0 * SR)):int(round(t1 * SR))]
            if tempo != 1.0:
                seg = channel.tempo(seg, tempo)
            want = int(round((t1 - t0) / tempo * SR))
            if len(seg) < want:
                seg = np.concatenate([seg, np.zeros(want - len(seg), dtype=np.int16)])
            a = np.ascontiguousarray(seg[:want], dtype=np.int16)
            self._note(a.nbytes)
            self.slices[key] = a
        return a


def gain(arr, db):
    if not db:
        return arr
    return to_i16(arr.astype(np.float32) * np.float32(10 ** (db / 20.0)))


def add_clipped(dst, pos, arr):
    """dst[pos:pos+len(arr)] += arr (int16 saturating); trims to dst. pos may be < 0."""
    if pos < 0:
        arr = arr[-pos:]
        pos = 0
    n = min(len(arr), len(dst) - pos)
    if n <= 0:
        return
    dst[pos:pos + n] = to_i16(dst[pos:pos + n].astype(np.int32) + arr[:n].astype(np.int32))


def room_noise_block(gen, n=8 * SR):
    """~Pink room noise, unit RMS, one cyclic block."""
    spec = np.fft.rfft(gen.standard_normal(n).astype(np.float64))
    f = np.maximum(np.arange(len(spec)), 1.0)
    x = np.fft.irfft(spec / np.sqrt(f), n)
    return (x / np.sqrt(np.mean(x * x))).astype(np.float32)


def active_rms(mix):
    """RMS of speech-bearing 10 ms frames (frames above 5% of the mean frame energy)."""
    fr = SR // 100
    n = (len(mix) // fr) * fr
    if n == 0:
        return 0.0
    e = np.mean(mix[:n].reshape(-1, fr).astype(np.float64) ** 2, axis=1)
    thr = 0.05 * e.mean()
    act = e[e > thr]
    return float(np.sqrt(act.mean())) if len(act) else float(np.sqrt(e.mean()))


def overlap_fraction(turns):
    """Seconds with >= 2 speakers talking / seconds with >= 1 speaker talking."""
    ev = []
    for t in turns:
        if t["end"] > t["start"]:
            ev.append((t["start"], 1))
            ev.append((t["end"], -1))
    ev.sort(key=lambda x: (x[0], x[1]))
    depth, last, union, over = 0, 0.0, 0.0, 0.0
    for tm, d in ev:
        if depth >= 1:
            union += tm - last
        if depth >= 2:
            over += tm - last
        depth += d
        last = tm
    return over / union if union > 0 else 0.0


def build_phrase_text(voices):
    """clip rel path -> (text, line_id) from the pool."""
    out = {}
    for v in voices.values():
        for c in v.get("clips", []):
            out[c["path"]] = (c.get("text", ""), c.get("line_id", ""))
        if v.get("enroll"):
            e = v["enroll"]
            out[e["path"]] = (e.get("text", ""), e.get("id", ""))
    return out


def render_audio(plan, pool_root, base_profiles, clips):
    """Mix every speaker's epochs. Returns the int16-range float32 mix."""
    n = int(round(plan["duration"] * SR))
    mix = np.zeros(n, dtype=np.float32)
    by_speaker = {}
    for t in plan["turns"]:
        by_speaker.setdefault(t["speaker"], []).append(t)
    for sp in plan["speakers"]:
        vid = sp["voice"]
        turns = by_speaker.get(vid, [])
        epochs = plan["epochs"][vid]
        for k, ep in enumerate(epochs):
            last = k == len(epochs) - 1
            mine = [t for t in turns
                    if t["start"] >= ep["start"] and (last or t["start"] < ep["end"])]
            if not mine:
                continue
            placed = []
            for t in mine:
                for p in t["pieces"]:
                    if p["t1"] <= p["t0"]:
                        continue
                    arr = gain(clips.piece(p["clip"], p["t0"], p["t1"], ep["tempo"]), ep["gain_db"])
                    placed.append((int(round((t["start"] + p["at"]) * SR)), arr))
            if not placed:
                continue
            s0 = int(round(ep["start"] * SR))
            s1 = max(int(round(ep["end"] * SR)), max(a + len(x) for a, x in placed))
            span = np.zeros(s1 - s0, dtype=np.int16)
            for a, x in placed:
                add_clipped(span, a - s0, x)
            prof = base_profiles[vid] if k == 0 else channel.drift(
                base_profiles[vid], random.Random(ep["drift_seed"]))
            out = channel.apply(span, prof)
            m0 = max(s0, 0)
            o = out[m0 - s0:]
            e = min(m0 + len(o), n)
            if e > m0:
                mix[m0:e] += o[:e - m0].astype(np.float32)
            del span, out
    return mix


def enroll_order(vid, bank, used, rng):
    """One voice's phrases, shuffled, those unused in the meeting first."""
    phr = [p for p in bank[vid] if p["dur"] > 0]
    if not phr:
        raise ValueError("no phrases for %s" % vid)
    rng.shuffle(phr)
    key = lambda p: (p["clip"], round(p["t0"], 4), round(p["t1"], 4))  # noqa: E731
    return [p for p in phr if key(p) not in used] + [p for p in phr if key(p) in used]


def render_enroll_audio(order, clips, rng):
    """Phrases joined by 0.3-0.6 s pauses until ENROLL_SECONDS, trimmed to exactly that."""
    want = int(ENROLL_SECONDS * SR)
    pieces, have, i = [], 0, 0
    while have < want:
        x = clips.piece(order[i % len(order)]["clip"], order[i % len(order)]["t0"],
                        order[i % len(order)]["t1"], 1.0)
        i += 1
        gap = np.zeros(int(rng.uniform(0.3, 0.6) * SR), dtype=np.int16)
        pieces += [x, gap]
        have += len(x) + len(gap)
    return np.concatenate(pieces)[:want]


def render_meeting(plan, voices, bank, pool_root, out_dir, clips=None):
    """Render one planned meeting into out_dir/<id>/. Returns the index entry."""
    mid = plan["id"]
    mdir = os.path.join(out_dir, mid)
    clips = clips or Clips(pool_root)
    texts = build_phrase_text(voices)
    base = {}
    for sp in plan["speakers"]:
        r = random.Random(derive(plan["seed"], mid, sp["voice"], "profile"))
        base[sp["voice"]] = channel.local_profile(r) if sp["channel"] == "local" else channel.remote_profile(r)

    mix = render_audio(plan, pool_root, base, clips)

    nrng = random.Random(derive(plan["seed"], mid, "noise"))
    snr = round(nrng.uniform(35.0, 45.0), 2)
    gen = np.random.default_rng(derive(plan["seed"], mid, "noise-np"))
    sig = active_rms(mix)
    blk = np.roll(room_noise_block(gen), int(gen.integers(0, 8 * SR)))
    k = np.float32(sig / (10 ** (snr / 20.0)))
    for pos in range(0, len(mix), len(blk)):
        m = min(len(blk), len(mix) - pos)
        mix[pos:pos + m] += blk[:m] * k
    audio = to_i16(mix)
    del mix
    audio = to_i16(channel.aac_roundtrip(audio, 64))
    write_wav(os.path.join(mdir, "audio.wav"), audio)

    used = {(p["clip"], round(p["t0"], 4), round(p["t1"], 4))
            for t in plan["turns"] for p in t["pieces"]}
    enroll_paths = {}
    for sp in plan["speakers"]:
        vid = sp["voice"]
        er = random.Random(derive(plan["seed"], mid, vid, "enroll"))
        raw = render_enroll_audio(enroll_order(vid, bank, used, er), clips, er)
        prof = channel.drift(base[vid], random.Random(er.getrandbits(48)), 1.0)
        e = to_i16(channel.aac_roundtrip(to_i16(channel.apply(raw, prof)), 64))
        p = os.path.abspath(os.path.join(mdir, "enroll", vid + ".wav"))
        write_wav(p, e)
        enroll_paths[vid] = p

    names = {sp["voice"]: sp["name"] for sp in plan["speakers"]}
    turns = []
    for t in sorted(plan["turns"], key=lambda t: (t["start"], t["end"])):
        seen, txt = set(), []
        for p in t["pieces"]:
            s = texts.get(p["clip"], (p.get("text", ""), ""))[0]
            if s and s not in seen:
                seen.add(s)
                txt.append(s)
        first = t["pieces"][0] if t["pieces"] else {}
        turns.append({"speaker": names[t["speaker"]], "start": round(t["start"], 5),
                      "end": round(t["end"], 5), "text": " ".join(txt),
                      "line_id": first.get("line_id", texts.get(first.get("clip"), ("", ""))[1]),
                      "kind": t.get("kind", "turn")})
    truth = {"schema": 1, "id": mid, "seed": plan["seed"], "tags": plan["tags"],
             "duration": round(len(audio) / SR, 3),
             "speakers": [{"name": sp["name"], "voice": sp["voice"],
                           "enrolled_clip": enroll_paths[sp["voice"]], "gain_db": 0.0,
                           "channel": sp["channel"], "profile": base[sp["voice"]],
                           "epochs": plan["epochs"][sp["voice"]]} for sp in plan["speakers"]],
             "turns": turns, "overlap_frac": round(overlap_fraction(turns), 4),
             "overlap_target": plan.get("overlap_target"), "noise_snr_db": snr}
    write_atomic(os.path.join(mdir, "truth.json"), json.dumps(truth, indent=1, default=repr) + "\n")
    write_atomic(os.path.join(mdir, "plan.json"), json.dumps(plan, indent=1, default=repr) + "\n")
    return {"id": mid, "tags": plan["tags"], "duration": truth["duration"],
            "n_speakers": len(plan["speakers"]), "n_turns": len(turns)}


def parse_range(s, name):
    try:
        parts = [float(x) for x in s.split("-")]
    except ValueError:
        parts = []
    if len(parts) == 1:
        parts = parts * 2
    if len(parts) != 2 or parts[0] > parts[1] or parts[0] <= 0:
        raise SystemExit("bad %s range %r (want LO-HI)" % (name, s))
    return tuple(parts)


def load_pool(path):
    with open(path) as f:
        pool = json.load(f)
    return os.path.dirname(os.path.abspath(path)), pool["voices"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pool", required=True, help="pool.json from render.py")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--minutes", default="30-120")
    ap.add_argument("--speakers", default="6-10")
    ap.add_argument("--roster", default=None)
    args = ap.parse_args(argv)
    minutes = parse_range(args.minutes, "--minutes")
    nsp = tuple(int(x) for x in parse_range(args.speakers, "--speakers"))
    pool_root, voices = load_pool(args.pool)
    os.makedirs(args.out, exist_ok=True)
    bank = realistic_plan.phrase_bank(pool_root, voices,
                                      cache_path=os.path.join(pool_root, "phrases.json"))
    clips = Clips(pool_root)
    entries = []
    for i in range(args.count):
        rng = random.Random(args.seed * 100003 + i)
        plan = realistic_plan.plan_meeting(rng, voices, bank, minutes=minutes, speakers=nsp,
                                           roster=args.roster, mid="m%d-%03d" % (args.seed, i))
        e = render_meeting(plan, voices, bank, pool_root, args.out, clips)
        entries.append(e)
        print("%s  %7.1fs  %2d speakers  %4d turns  %s" % (
            e["id"], e["duration"], e["n_speakers"], e["n_turns"], ",".join(e["tags"])), flush=True)
    write_atomic(os.path.join(args.out, "index.json"), json.dumps(
        {"schema": 1, "seed": args.seed, "profile": "realistic", "pool_root": pool_root,
         "meetings": entries}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
