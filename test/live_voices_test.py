#!/usr/bin/env python3
"""
Offline test for lib/live_voices.py: running voices on the app leg, naming them from the
registry (best first, a split voice shares its sibling's name, a margin, never the self voice), short lines,
backfill of a name learned later, and the lines, summaries and next steps that show it.
Embeddings are faked from the samples' value; no model, no audio playback.

Run: uv run --with numpy python test/live_voices_test.py
"""
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import live  # noqa: E402
import live_insights as li  # noqa: E402
import live_voices as lv  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def unit(*xs):
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


# a "voice" is the constant value its samples carry; the fake embedding is its direction
DIRS = {1: unit(1, 0, 0, 0), 2: unit(0, 1, 0, 0), 3: unit(0, 0, 1, 0), 4: unit(0.9, 0.1, 0, 0)}


def fake_embed(samples):
    return DIRS[int(round(float(samples[0])))]


def say(voice, seconds):
    return np.full(int(seconds * lv.RATE), float(voice), dtype=np.float32)


KNOWN = [("Ada_Lovelace", "boss", unit(1, 0.05, 0, 0)), ("Grace_Hopper", "", unit(0.05, 1, 0, 0)),
         ("Me_Myself", "self", unit(0, 0, 0, 1))]

v = lv.Voices(KNOWN, fake_embed, me=("Me_Myself", "self"))
check(v.place(say(1, 0.3)) is None, "under half a second: no voice")
check(v.place(say(1, 1.0)) is None, "short line with no voice yet: never starts one")
check(v.place(say(1, 3)) == "v1", "a long line starts a voice")
check(v.place(say(4, 2)) == "v1", "a close line joins it")
check(v.place(say(2, 1.0)) is None, "short and unlike every voice: none")
check(v.place(say(2, 4)) == "v2", "an unlike long line starts a second voice")
check(v.place(say(2, 1.0)) == "v2", "a short line joins the voice it sounds like")
check(v.place(say(3, 5)) == "v3", "a third voice")
names = v.names()
check(names == {"v1": ("Ada_Lovelace", "boss"), "v2": ("Grace_Hopper", "")},
      f"named by the registry, the unknown voice stays unnamed: {names}")

# a name goes to the best voice; a second voice shares it only if it sounds like that voice
w = lv.Voices(KNOWN, fake_embed)
w.sums = [unit(1, 0.02, 0, 0) * 5, unit(1, 0.3, 0, 0) * 5]
check(w.names() == {"v1": ("Ada_Lovelace", "boss"), "v2": ("Ada_Lovelace", "boss")},
      f"one speaker split into two voices: both named: {w.names()}")
w.sums = [unit(1, 0.02, 0, 0) * 5, unit(1, 0, 1.2, 0) * 5]
check(w.names() == {"v1": ("Ada_Lovelace", "boss")}, f"a different voice that also looks like Ada: unnamed: {w.names()}")
# too close to call between two voiceprints: no name
amb = lv.Voices(KNOWN, fake_embed)
amb.sums = [unit(1, 1, 0, 0)]
check(amb.names() == {}, "no margin between two registry voices: unnamed")
# the self voice never names an app-leg voice
selfish = lv.Voices(KNOWN, fake_embed, me=("Me_Myself", "self"))
selfish.sums = [unit(0, 0, 0, 1)]
check(selfish.names() == {}, "the self voiceprint never names a 'them' voice")

# annotate: names and roles onto lines, me from the self voice, cleared when lost
entries = [{"t": 1, "who": "them", "text": "a", "voice": "v1"}, {"t": 2, "who": "me", "text": "b"},
           {"t": 3, "who": "them", "text": "c", "voice": "v3"}, {"t": 4, "who": "them", "text": "d"}]
v.annotate(entries)
check(entries[0]["name"] == "Ada Lovelace" and entries[0]["role"] == "boss", f"underscores read as spaces: {entries[0]}")
check(entries[1]["name"] == "Me Myself" and entries[1]["role"] == "self", "me lines carry the self name")
check("name" not in entries[2] and "name" not in entries[3], "unknown voice and no voice: unnamed")
check([live.speaker(e) for e in entries] == ["Ada Lovelace", "me", "them", "them"], "speaker(): me stays me")
check(live.fmt_line({**entries[0], "clock": "09:00:01"}) == "[09:00:01] Ada Lovelace: a", "the .md line shows the name")
v.sums[0] = unit(0, 0, 1, 0)  # v1 now sounds like nobody known
v.annotate(entries)
check("name" not in entries[0] and "role" not in entries[0], "a name the voice lost is cleared")

# transcribe_chunks: only app-leg lines are placed, from the segment's own samples
seen = []


class FakeVoices:
    def place(self, samples):
        seen.append(len(samples))
        return "v9"


def fake_transcribe(samples):
    return [{"start": 0.0, "end": 2.0, "text": "Release notes go out on Friday."}]


loud = (np.sin(np.arange(4 * lv.RATE) / 10) * 0.3).astype(np.float32)
got = live.transcribe_chunks({"them": (0, loud)}, fake_transcribe, [], FakeVoices())
check(got[0]["voice"] == "v9" and seen == [2 * lv.RATE], f"them line placed on its 2 s of samples: {got}, {seen}")


class Broken:
    def place(self, samples):
        raise RuntimeError("model fell over")


got = live.transcribe_chunks({"them": (0, loud)}, fake_transcribe, [], Broken())
check(got and "voice" not in got[0], "a voice failure keeps the line, unnamed")

# insights: names in summary bullets and next steps; different voices never join
ents = [{"t": 0, "who": "them", "voice": "v1", "name": "Ada Lovelace", "role": "boss", "clock": "09:00:00",
         "text": "We will ship the release notes on Friday after the storage review."},
        {"t": 2, "who": "them", "voice": "v2", "clock": "09:00:02",
         "text": "The storage bucket costs need a careful check before the release."},
        {"t": 5, "who": "me", "name": "Me Myself", "role": "self", "clock": "09:00:05",
         "text": "I'll send the release notes to the storage team today."}]
utts = li.utterances(ents)
check(len(utts) == 3, f"lines from different voices never join: {utts}")
ins = li.build({"started": 0}, ents, "s")
bullets = [b for s in ins["summaries"] for b in s["bullets"]]
check(any(b.startswith("Ada Lovelace: ") for b in bullets) and not any(b.startswith("v1") for b in bullets),
      f"summary bullets carry the name: {bullets}")
steps = {n["who"]: n for n in ins["nextSteps"]}
check(any(n.get("name") == "Ada Lovelace" and n["role"] == "boss" for n in ins["nextSteps"]),
      f"next steps carry name and role: {ins['nextSteps']}")
check(all("name" not in n for n in ins["nextSteps"] if n["who"] == "me"), "my next steps stay unnamed")

# off switch, and no sherpa-onnx in this env: load() says why and returns None
logs = []
os.environ["WHOSAID_LIVE_NAMES"] = "0"
check(lv.load(logs.append) is None and not logs, "WHOSAID_LIVE_NAMES=0: off, quietly")
os.environ.pop("WHOSAID_LIVE_NAMES")
try:
    import sherpa_onnx  # noqa: F401
except ImportError:
    check(lv.load(logs.append) is None and "live names off" in logs[0], f"no sherpa-onnx: off, logged: {logs}")

# the launcher brings sherpa-onnx (pinned) into the live run env
check('--with "$SHERPA_SPEC" --with mlx-whisper' in (ROOT / "whosaid").read_text(),
      "whosaid live run: sherpa-onnx in its uv env")

print(f"PASS: live_voices_test ({CHECKS} checks)")
