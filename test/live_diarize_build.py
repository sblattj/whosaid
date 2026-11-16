#!/usr/bin/env python3
"""Build the live diarization eval's clip bank (#218) from the diarize_eval voice pool.

    uv run --with numpy --with sherpa-onnx==1.13.8 python test/live_diarize_build.py \
        [--pool ~/.cache/whosaid/diarize-eval/pool.json] [--out test/fixtures/live_diarize]

Dev-only: reads clips ElevenLabs already rendered for test/diarize_eval (no new renders,
no network) and writes, under --out:

    clips/<voice>/<line>.wav   16 kHz mono s16, trimmed to the phrase's speech span
    bank.json                  every clip: voice, person, text, kind, seconds
    registry.json              a throwaway speaker registry (fake names) built from the
                               pool's 60 s enrollment clips, in the WHOSAID_SPEAKER_DB shape

One person (Bea) is also written as a dial-in voice ("bea-far"): the same clips through
diarize_eval's conferencing channel (phone codec, speaker EQ, room) so the live pass sees one
known person as two different-sounding voices. The guest (Gus) is in the bank but never
in the registry. Runs once; test/live_diarize_eval.py only reads what this writes.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(HERE / "diarize_eval"))

SR = 16000
PAD = 0.05  # seconds kept around the phrase's speech span
# person -> (pool voice, enrolled). Fake names; the voices are synthetic.
PEOPLE = {
    "Abel": ("el-george", True),
    "Bea": ("el-lily", True),
    "Cyd": ("el-charlie", True),
    "Dora": ("el-matilda", True),
    "Gus": ("oa-verse", False),  # a guest nobody enrolled
}
MEDIUM_PER_VOICE = 4
FAR_PERSON = "Bea"
FAR_SEED = 21804  # a phone dial-in (telephone codec): 0.54 to her near voice, 0.43-0.55 to her print, <= 0.28 to anyone else


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()


def write_wav(path: Path, x: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.asarray(x, dtype="<i2").tobytes())


def trim(x: np.ndarray, spans: list) -> np.ndarray:
    if not spans:
        return x
    a = max(0, int((spans[0][0] - PAD) * SR))
    b = min(len(x), int((spans[-1][1] + PAD) * SR))
    return x[a:b]


def pick(clips: list) -> list:
    shorts = [c for c in clips if c["kind"] == "short"]
    mediums = sorted((c for c in clips if c["kind"] == "medium"), key=lambda c: c["dur"])[:MEDIUM_PER_VOICE]
    return shorts + mediums


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="live_diarize_build.py")
    ap.add_argument("--pool", default=os.path.expanduser("~/.cache/whosaid/diarize-eval/pool.json"))
    ap.add_argument("--out", default=str(ROOT / "test" / "fixtures" / "live_diarize"))
    a = ap.parse_args(argv)
    pool_path = Path(a.pool)
    root = pool_path.parent
    pool = json.loads(pool_path.read_text())["voices"]
    phrases = json.loads((root / "phrases.json").read_text()) if (root / "phrases.json").exists() else {}
    out = Path(a.out)

    import channel  # diarize_eval's conferencing model (needs ffmpeg)
    import diarize_sherpa as ds
    embed = ds.make_ref_embedder()

    bank, registry = [], {"speakers": []}
    rng = random.Random(FAR_SEED)
    far_profile = channel.remote_profile(rng)
    for person, (vid, enrolled) in PEOPLE.items():
        voice = pool[vid]
        for c in pick(voice["clips"]):
            x = trim(read_wav(root / c["path"]), (phrases.get(c["path"]) or {}).get("spans") or [])
            rel = f"clips/{person.lower()}/{c['line_id']}.wav"
            write_wav(out / rel, x)
            bank.append({"voice": person.lower(), "person": person, "line": c["line_id"], "kind": c["kind"],
                         "text": c["text"], "seconds": round(len(x) / SR, 3), "path": rel})
            if person == FAR_PERSON:
                far = channel.apply(x, far_profile)
                rel = f"clips/{person.lower()}-far/{c['line_id']}.wav"
                write_wav(out / rel, far)
                bank.append({"voice": f"{person.lower()}-far", "person": person, "line": c["line_id"],
                             "kind": c["kind"], "text": c["text"], "seconds": round(len(far) / SR, 3),
                             "path": rel})
        if enrolled:
            v = embed(read_wav(root / "enroll" / f"{vid}.wav").astype(np.float32) / 32768.0)
            registry["speakers"].append({"name": person, "model": ds.EMB_NAME,
                                         "embedding": [round(float(t), 6) for t in v],
                                         "added": "2026-11-16T00:00:00", "synthetic": True})
    (out / "bank.json").write_text(json.dumps({"schema": 1, "far_profile": far_profile, "clips": bank},
                                              indent=1) + "\n")
    (out / "registry.json").write_text(json.dumps(registry, indent=1) + "\n")
    total = sum((out / c["path"]).stat().st_size for c in bank)
    print(f"wrote {len(bank)} clips ({total / 1e6:.2f} MB) and {len(registry['speakers'])} voiceprints to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
