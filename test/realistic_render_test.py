#!/usr/bin/env python3
"""
Offline test for the `realistic` meeting renderer (issue #80, test/diarize_eval/realistic.py).

The planner (realistic_plan) and the channel model (channel) are replaced by fakes
injected through sys.modules: an identity channel and a deterministic toy planner. The
pool is a handful of noise-burst WAVs in a temp dir (no TTS).

Run:
    uv run --with numpy --with mcp python test/realistic_render_test.py
"""

import hashlib
import json
import os
import random
import sys
import tempfile
import time
import types
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent / "diarize_eval"
SR = 16000
CHECKS = 0


def check(cond, msg):
    global CHECKS
    assert cond, msg
    CHECKS += 1


# ---- fakes ----------------------------------------------------------------------
fake_channel = types.ModuleType("channel")
fake_channel.remote_profile = lambda rng: {"kind": "remote", "x": rng.random()}
fake_channel.local_profile = lambda rng: {"kind": "local", "x": rng.random()}
fake_channel.drift = lambda profile, rng, strength=1.0: dict(profile, drift=rng.random())
fake_channel.apply = lambda samples, profile: samples
fake_channel.tempo = lambda samples, factor: samples  # identity: renderer must pad/trim
fake_channel.aac_roundtrip = lambda samples, bitrate_k=64: samples


def fake_phrase_bank(pool_root, voices, cache_path=None):
    bank = {}
    for vid, v in voices.items():
        bank[vid] = [{"clip": c["path"], "line_id": c["line_id"], "t0": t0, "t1": t1,
                      "dur": round(t1 - t0, 3)}
                     for c in v["clips"] for t0, t1 in ((0.0, 1.8), (2.0, 3.8))]
    return bank


def fake_plan_meeting(rng, voices, bank, *, minutes=(30, 120), speakers=(6, 10),
                      roster=None, mid="m0-000"):
    """Toy planner: back-to-back 3 s turns, voices round-robin, one tempo change."""
    dur = minutes[0] * 60.0
    vids = sorted(voices)[:speakers[0]]
    plan_sp = [{"voice": v, "name": "Name%d" % i, "channel": "local" if i == 0 else "remote",
                "share_target": 1.0 / len(vids)} for i, v in enumerate(vids)]
    epochs = {v: [{"start": 0.0, "end": dur / 2, "tempo": 1.0, "gain_db": 0.0, "drift_seed": 1},
                  {"start": dur / 2, "end": dur, "tempo": 1.1, "gain_db": -3.0, "drift_seed": 2}]
              for v in vids}
    turns, t, i = [], 5.0, 0
    while t + 4.0 < dur:
        v = vids[i % len(vids)]
        ph = bank[v][rng.randrange(len(bank[v]))]
        ep = epochs[v][0] if t < dur / 2 else epochs[v][1]
        d = ph["dur"] / ep["tempo"]
        turns.append({"speaker": v, "start": round(t, 3), "end": round(t + d, 3), "kind": "turn",
                      "pieces": [{"clip": ph["clip"], "line_id": ph["line_id"], "t0": ph["t0"],
                                  "t1": ph["t1"], "at": 0.0}]})
        t += 3.0
        i += 1
    return {"schema": 1, "id": mid, "seed": rng.randrange(10 ** 6), "duration": dur,
            "tags": ["realistic", "n%d" % len(vids)], "speakers": plan_sp, "epochs": epochs,
            "turns": turns, "overlap_target": 0.0}


fake_plan = types.ModuleType("realistic_plan")
fake_plan.phrase_bank = fake_phrase_bank
fake_plan.plan_meeting = fake_plan_meeting
sys.modules["channel"] = fake_channel
sys.modules["realistic_plan"] = fake_plan
sys.path.insert(0, str(HERE))
import realistic  # noqa: E402

check(realistic.channel is fake_channel and realistic.realistic_plan is fake_plan, "fakes in use")


# ---- tiny pool ------------------------------------------------------------------
def make_pool(root, n_voices):
    voices = {}
    for vi in range(n_voices):
        vid = "v%d" % vi
        gen = np.random.default_rng(vi)
        clips = []
        for ci in range(12):
            rel = "clips/%s/c%02d.wav" % (vid, ci)
            os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
            x = np.clip(gen.standard_normal(4 * SR) * 3000, -32768, 32767).astype("<i2")
            with wave.open(os.path.join(root, rel), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(x.tobytes())
            clips.append({"line_id": "L%02d" % ci, "kind": "line", "text": "text %s %d" % (vid, ci),
                          "path": rel, "dur": 4.0})
        voices[vid] = {"name": "Voice%d" % vi, "clips": clips,
                       "enroll": {"id": "E", "path": clips[0]["path"], "dur": 4.0, "text": "enroll"}}
    with open(os.path.join(root, "pool.json"), "w") as f:
        json.dump({"voices": voices}, f)
    return os.path.join(root, "pool.json"), voices


def rms(a):
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) if len(a) else 0.0


def read_wav(path):
    with wave.open(path, "rb") as w:
        check(w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2,
              "16 kHz mono s16: " + path)
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hand_plan(voices, mid):
    """3 min, 3 speakers; A and B overlap 50-53 s; B solo 60-63; gap 100-101 s empty."""
    vids = ["v0", "v1", "v2"]
    dur = 180.0
    spk = [{"voice": v, "name": "N%d" % i, "channel": "local" if i == 0 else "remote",
            "share_target": 0.33} for i, v in enumerate(vids)]
    ep = {v: [{"start": 0.0, "end": dur, "tempo": 1.0, "gain_db": 0.0, "drift_seed": 7}] for v in vids}

    def turn(v, s, e, ci, t0=0.0, t1=None):
        clip = voices[v]["clips"][ci]
        t1 = t1 if t1 is not None else t0 + (e - s)
        return {"speaker": v, "start": s, "end": e, "kind": "turn",
                "pieces": [{"clip": clip["path"], "line_id": clip["line_id"], "t0": t0, "t1": t1,
                            "at": 0.0}]}
    turns = [turn("v0", 10.0, 13.0, 1), turn("v1", 20.0, 23.0, 2), turn("v2", 30.0, 33.0, 3),
             turn("v0", 50.0, 53.0, 4), turn("v1", 50.0, 53.0, 5), turn("v1", 60.0, 63.0, 6),
             turn("v2", 120.0, 123.0, 7)]
    # one two-piece turn: second piece placed 1.5 s in
    t = turn("v0", 140.0, 144.0, 8, 0.0, 1.5)
    t["pieces"].append(dict(turn("v0", 0, 0, 9, 0.0, 2.0)["pieces"][0], at=2.0))
    turns.append(t)
    return {"schema": 1, "id": mid, "seed": 5, "duration": dur, "tags": ["realistic", "n3"],
            "speakers": spk, "epochs": ep, "turns": turns, "overlap_target": 0.05}


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    pool_path, voices = make_pool(str(tmp / "pool"), 8)
    pool_root = str(tmp / "pool")
    bank = fake_phrase_bank(pool_root, voices)

    # ---- hand plan: audio, truth, enrollment --------------------------------------
    out = str(tmp / "out1")
    plan = hand_plan(voices, "m5-000")
    entry = realistic.render_meeting(plan, voices, bank, pool_root, out)
    mdir = Path(out) / "m5-000"
    audio = read_wav(str(mdir / "audio.wav"))
    check(abs(len(audio) - round(180.0 * SR)) <= 1, "audio length == round(duration*SR)")

    def seg(a, b):
        return audio[int(a * SR):int(b * SR)]
    gap = rms(seg(100.0, 101.0))
    check(gap > 0, "room noise present in the gap")
    for t in plan["turns"]:
        e = rms(seg(t["start"], t["end"]))
        check(e > 20 * gap, "turn %s %.0f-%.0f energy %.1f well above gap %.2f" % (
            t["speaker"], t["start"], t["end"], e, gap))
    solo = rms(seg(60.0, 63.0))
    both = rms(seg(50.0, 53.0))
    check(both > 1.25 * solo, "overlapping turns sum (%.0f vs solo %.0f)" % (both, solo))
    check(rms(seg(142.0, 143.5)) > 20 * gap, "second piece of a multi-piece turn placed at `at`")
    check(rms(seg(141.6, 141.95)) < 5 * gap, "silence between pieces of a multi-piece turn")

    for sp in plan["speakers"]:
        ew = mdir / "enroll" / (sp["voice"] + ".wav")
        d = len(read_wav(str(ew))) / SR
        check(55 <= d <= 65, "enrollment %s is %.1f s" % (sp["voice"], d))
        check(rms(read_wav(str(ew))) > 100, "enrollment has speech")

    truth = json.load(open(mdir / "truth.json"))
    check(truth["schema"] == 1 and truth["id"] == "m5-000" and truth["seed"] == 5, "truth header")
    for k in ("tags", "duration", "speakers", "turns", "overlap_frac", "noise_snr_db"):
        check(k in truth, "truth has " + k)
    check(35 <= truth["noise_snr_db"] <= 45, "noise SNR recorded in 35-45 dB")
    check(0.0 < truth["overlap_frac"] < 0.2, "overlap_frac measured: %s" % truth["overlap_frac"])
    for s in truth["speakers"]:
        for k in ("name", "voice", "enrolled_clip", "gain_db", "channel", "profile", "epochs"):
            check(k in s, "speaker has " + k)
        check(os.path.isabs(s["enrolled_clip"]) and os.path.exists(s["enrolled_clip"]),
              "enrolled_clip is an existing absolute path")
        # run.py: os.path.join(pool_root, abs) must be unchanged
        check(os.path.join("/some/other/root", s["enrolled_clip"]) == s["enrolled_clip"],
              "join(pool_root, abs) keeps abs")
    names = {s["name"] for s in truth["speakers"]}
    starts = [t["start"] for t in truth["turns"]]
    check(starts == sorted(starts), "turns sorted by start")
    for t in truth["turns"]:
        for k in ("speaker", "start", "end", "text", "line_id", "kind"):
            check(k in t, "turn has " + k)
        check(t["speaker"] in names and t["end"] > t["start"] and t["text"], "turn sane")
    multi = [t for t in truth["turns"] if t["start"] == 140.0][0]
    check(multi["text"] == "text v0 8 text v0 9" and multi["line_id"] == "L08",
          "multi-piece text joined, first line_id")
    check((mdir / "plan.json").exists(), "plan.json written")

    # score.py consumes this truth without error
    sys.path.insert(0, str(HERE))
    from score import score_meeting  # noqa: E402
    segs = [{"start": t["start"], "end": t["end"], "speaker": t["speaker"]} for t in truth["turns"]]
    res = score_meeting(truth, segs, "named", enrolled=sorted(names), hyp_text="x")
    check(res["der"] is not None and res["der"] < 0.05, "score_meeting accepts truth (der=%s)" % res["der"])

    # ---- determinism --------------------------------------------------------------
    out2 = str(tmp / "out2")
    realistic.render_meeting(hand_plan(voices, "m5-000"), voices, bank, pool_root, out2)
    check(sha(str(mdir / "audio.wav")) == sha(os.path.join(out2, "m5-000", "audio.wav")),
          "same plan -> identical audio bytes")
    check(sha(str(mdir / "enroll" / "v1.wav")) == sha(os.path.join(out2, "m5-000", "enroll", "v1.wav")),
          "same plan -> identical enrollment bytes")

    # ---- CLI: --count 2, index.json, seed determinism -----------------------------
    cli_a, cli_b = str(tmp / "cliA"), str(tmp / "cliB")
    argv = ["--pool", pool_path, "--minutes", "3-3", "--speakers", "3-3", "--seed", "9", "--count", "2"]
    check(realistic.main(argv + ["--out", cli_a]) == 0, "CLI returns 0")
    realistic.main(argv + ["--out", cli_b])
    idx = json.load(open(os.path.join(cli_a, "index.json")))
    check(idx["schema"] == 1 and idx["seed"] == 9 and idx["profile"] == "realistic"
          and idx["pool_root"] == pool_root, "index.json header")
    check([m["id"] for m in idx["meetings"]] == ["m9-000", "m9-001"], "two meetings in index")
    for m in idx["meetings"]:
        check(set(m) == {"id", "tags", "duration", "n_speakers", "n_turns"}, "index entry shape")
        for f in ("audio.wav", "truth.json", "plan.json"):
            check(os.path.exists(os.path.join(cli_a, m["id"], f)), m["id"] + "/" + f)
        check(len(os.listdir(os.path.join(cli_a, m["id"], "enroll"))) == m["n_speakers"], "enroll per speaker")
        check(sha(os.path.join(cli_a, m["id"], "audio.wav")) == sha(os.path.join(cli_b, m["id"], "audio.wav")),
              "CLI same seed -> identical audio " + m["id"])
    check(sha(os.path.join(cli_a, "m9-000", "audio.wav")) != sha(os.path.join(cli_a, "m9-001", "audio.wav")),
          "different meetings differ")

    # ---- speed: 30 min, 8 speakers, identity channel ------------------------------
    pool8 = str(tmp / "pool8")
    p8, v8 = make_pool(pool8, 8)
    b8 = fake_phrase_bank(pool8, v8)
    big = fake_plan_meeting(random.Random(1), v8, b8, minutes=(30, 30), speakers=(8, 8), mid="m1-000")
    t0 = time.time()
    realistic.render_meeting(big, v8, b8, pool8, str(tmp / "big"))
    dt = time.time() - t0
    print("speed: 30-min, 8-speaker, %d-turn meeting rendered in %.1f s" % (len(big["turns"]), dt))
    check(dt < 60, "30-min render under 60 s (%.1f s)" % dt)
    check(abs(len(read_wav(str(tmp / "big" / "m1-000" / "audio.wav"))) - 1800 * SR) <= 1, "big length")

print("realistic_render_test: %d checks passed" % CHECKS)
