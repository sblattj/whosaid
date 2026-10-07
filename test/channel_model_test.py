#!/usr/bin/env python3
"""
Offline test for the synthetic-meeting audio channel model
(test/diarize_eval/channel.py, issue #80). Synthetic signals only.

Run:
    uv run --with numpy --with mcp python test/channel_model_test.py
"""

import json
import random
import sys
import time
from array import array
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent / "diarize_eval"))
import channel as ch  # noqa: E402

SR = ch.SR
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def noise(seconds, amp=4000, seed=0):
    r = np.random.RandomState(seed)
    return (r.randn(int(seconds * SR)) * amp).clip(-32000, 32000).astype(np.int16)


def clicks(seconds=3.0, period=0.5):
    x = np.zeros(int(seconds * SR), dtype=np.int16)
    for t in np.arange(0.25, seconds - 0.25, period):
        x[int(t * SR):int(t * SR) + 3] = 20000
    return x


def chirp(seconds=2.0):
    t = np.arange(int(seconds * SR)) / SR
    return (8000 * np.sin(2 * np.pi * (300 * t + (3000 - 300) / (2 * seconds) * t * t))).astype(np.int16)


def offset_ms(ref, out):
    a = np.abs(ref.astype(np.float64))
    b = np.abs(out.astype(np.float64))
    n = 1 << int(np.ceil(np.log2(len(a) * 2)))
    c = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    i = int(np.argmax(np.concatenate([c[:2000], c[-2000:]])))
    lag = i if i < 2000 else i - 4000
    return lag * 1000.0 / SR


def hf_fraction(x, cutoff=7000):
    s = np.abs(np.fft.rfft(x.astype(np.float64))) ** 2
    f = np.fft.rfftfreq(len(x), 1.0 / SR)
    return s[f > cutoff].sum() / s.sum()


def profile_for(codec, seed):
    for k in range(2000):
        p = ch.remote_profile(random.Random(seed * 10000 + k))
        if p["codec"] == codec:
            return p
    raise AssertionError(f"no {codec} profile found")


def test_length():
    x = noise(2.3)
    for seed in range(10):
        for p in (ch.remote_profile(random.Random(seed)), ch.local_profile(random.Random(seed))):
            y = ch.apply(x, p)
            check(y.dtype == np.int16 and y.ndim == 1, f"seed {seed}: int16 1-D output")
            check(len(y) == len(x), f"seed {seed} {p['kind']}/{p['codec']}: length {len(y)} != {len(x)}")
    check(len(ch.apply(array("h", x.tolist()), ch.local_profile(random.Random(1)))) == len(x),
          "array('h') input accepted")
    check(len(ch.aac_roundtrip(x)) == len(x), "aac_roundtrip length")


def test_alignment():
    for name, sig in (("clicks", clicks()), ("chirp", chirp())):
        for codec in ch.CODECS:
            p = profile_for(codec, 7)
            off = offset_ms(sig, ch.apply(sig, p))
            check(abs(off) < 10, f"{name} via {codec}: offset {off:.2f} ms")
        off = offset_ms(sig, ch.apply(sig, ch.local_profile(random.Random(3))))
        check(abs(off) < 10, f"{name} via local: offset {off:.2f} ms")
        off = offset_ms(sig, ch.aac_roundtrip(sig))
        check(abs(off) < 10, f"{name} via aac_roundtrip: offset {off:.2f} ms")
    print("  alignment ok")


def test_hf():
    x = noise(6.0)
    base = hf_fraction(x)
    for seed in range(6):
        y = ch.apply(x, ch.remote_profile(random.Random(seed)))
        check(hf_fraction(y) < 0.25 * base, f"remote seed {seed}: >7 kHz fraction {hf_fraction(y):.4f} vs {base:.4f}")
    y = ch.apply(x, ch.local_profile(random.Random(0)))
    check(hf_fraction(y) > 0.7 * base, f"local keeps >7 kHz: {hf_fraction(y):.4f} vs {base:.4f}")


def test_determinism():
    x = noise(1.5)
    for p in (profile_for("opus", 1), profile_for("telephone", 1), ch.local_profile(random.Random(2))):
        check(np.array_equal(ch.apply(x, p), ch.apply(x, p)), f"deterministic {p['codec']}")


def test_json_and_drift():
    x = noise(1.0)
    for seed in range(8):
        for p in (ch.remote_profile(random.Random(seed)), ch.local_profile(random.Random(seed))):
            d = ch.drift(p, random.Random(seed + 100), 1.0)
            for q in (p, d):
                q2 = json.loads(json.dumps(q))
                check(q2 == q, "json round-trip equal")
                check(np.array_equal(ch.apply(x, q2), ch.apply(x, q)), "apply accepts round-tripped dict")
            check(d["codec"] == p["codec"], "drift keeps codec")
            check(d != p, "drift changes at least one parameter")
            check(ch.drift(p, random.Random(5), 0.0)["level_db"] == p["level_db"], "strength 0 keeps level")
    for codec in ch.CODECS:
        p = profile_for(codec, 2)
        check(ch.drift(p, random.Random(9))["codec"] == codec, f"drift keeps {codec}")


def test_tempo():
    x = noise(5.0)
    y = ch.tempo(x, 1.05)
    exp = len(x) / 1.05
    check(abs(len(y) - exp) / exp < 0.02, f"tempo length {len(y)} vs {exp:.0f}")
    y = ch.tempo(x, 0.8)
    check(abs(len(y) - len(x) / 0.8) / (len(x) / 0.8) < 0.02, "tempo 0.8 length")


def test_speed():
    x = noise(600.0)
    p = profile_for("opus", 4)
    t = time.time()
    y = ch.apply(x, p)
    dt = time.time() - t
    print(f"  apply on 10 min of noise: {dt:.1f} s")
    check(len(y) == len(x), "10 min length")
    check(dt < 15, f"apply speed {dt:.1f} s")


def main():
    test_length()
    test_alignment()
    test_hf()
    test_determinism()
    test_json_and_drift()
    test_tempo()
    test_speed()
    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
