#!/usr/bin/env python3
"""
Offline test for lib/live.py (`whosaid live`, issue #131): the in-progress WAV
header walk, chunking at quiet points, the silence gate, following two growing
legs (me = mic, them = app) to the end of a capture, and the reader the MCP tool
uses. A stand-in transcriber replaces Whisper; no model, no audio playback.

Run: uv run --with numpy python test/live_test.py
"""
import json
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import live  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def header(fllr: bool = True) -> bytes:
    """A WAV header as a writer leaves it mid-recording: placeholder sizes, an
    FLLR padding chunk before 'data' (so the data offset is not 44)."""
    fmt = struct.pack("<HHIIHH", 1, 1, live.RATE, live.RATE * 2, 2, 16)
    out = b"RIFF" + struct.pack("<I", 0) + b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if fllr:
        out += b"FLLR" + struct.pack("<I", 30) + b"\0" * 30
    return out + b"data" + struct.pack("<I", 0)


def pcm(seconds: float, loud: bool) -> bytes:
    n = int(seconds * live.RATE)
    if not loud:
        return np.zeros(n, dtype="<i2").tobytes()
    t = np.arange(n) / live.RATE
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2").tobytes()


# ---- header walk ----
h = header()
check(live.data_offset(h) == len(h), f"data offset found past FLLR: {live.data_offset(h)} vs {len(h)}")
check(live.data_offset(header(fllr=False)) == 44, "plain header: 44")
check(live.data_offset(b"junk" * 10) is None, "not a WAV: None")

# ---- chunking: both legs cut at one shared quiet point ----
def leg_with(speaker, data):
    leg = live.Leg(Path("/nonexistent"), speaker)
    leg.offset, leg.pending = 44, data
    return leg


mic = leg_with("me", pcm(18, True) + pcm(0.6, False) + pcm(3, True))
app = leg_with("them", pcm(21.6, False))
check(live.take_chunks([mic, app], 30, final=False) is None, "under chunk length: wait")
got = live.take_chunks([mic, app], 20, final=False)
cut = len(got["me"][1]) / live.RATE
check(got["me"][0] == 0 and 18.0 <= cut <= 18.6, f"cut lands in the quiet gap: {cut:.2f}s")
check(len(got["them"][1]) == len(got["me"][1]), "the app leg is cut at the same point")
check(mic.pending_start == app.pending_start == len(got["me"][1]), "remainders keep their sample position")
rest = live.take_chunks([mic, app], 20, final=True)
check(len(rest["me"][1]) + len(got["me"][1]) == int(21.6 * live.RATE), "final flush takes the rest")
check(live.take_chunks([mic, app], 20, final=True) is None, "nothing left after the flush")
check(live.rms(np.zeros(100, dtype=np.float32)) < live.RMS_FLOOR, "silence is under the floor")

# ---- bleed: the other side heard through the speakers is not 'me' ----
loud = np.frombuffer(pcm(2, True), dtype="<i2").astype(np.float32) / 32768.0
bleed = live.mask_bleed(loud * 0.05, loud)            # mic = app 26 dB down: speaker bleed
check(live.rms(bleed) < live.RMS_FLOOR, "speaker bleed on the mic is silenced")
own = live.mask_bleed(loud, np.zeros_like(loud))      # app silent: the local voice
check(np.array_equal(own, loud), "the local voice is kept")

# ---- speech islands: the masked mic is cut down to your voice ----
tone = np.frombuffer(pcm(1, True), dtype="<i2").astype(np.float32) / 32768.0
gap = np.zeros(5 * live.RATE, dtype=np.float32)
sparse = np.concatenate([gap, tone, gap, tone, gap])           # 2 s of voice in 17 s
small, runs = live.compact(sparse)
check(2 * live.RATE <= len(small) <= 3 * live.RATE, f"long silences cut down: {len(small) / live.RATE:.2f}s")
check(abs(live.original_time(runs, 0.2) - 5.05) < 0.11, f"time maps back: {live.original_time(runs, 0.2):.2f}")
check(abs(live.original_time(runs, len(small) / live.RATE - 0.2) - 11.8) < 0.2, "second island maps back")
check(live.compact(np.zeros(100, dtype=np.float32))[1] == [], "all silence: nothing left")

# ---- Whisper's guesses are dropped ----
ok = {"text": " We ship the fix on Friday.", "no_speech_prob": 0.05, "avg_logprob": -0.3, "compression_ratio": 1.2}
check(live.keep_segment(ok), "a confident line is kept")
for bad, why in ((dict(ok, text=" you"), "'you' alone"), (dict(ok, text=" Thank you."), "'Thank you.' alone"),
                 (dict(ok, text=" ."), "punctuation only"), (dict(ok, avg_logprob=-1.4), "a low-confidence guess"),
                 (dict(ok, compression_ratio=3.1), "a decoding loop"), (dict(ok, no_speech_prob=0.9), "no speech")):
    check(not live.keep_segment(bad), f"dropped: {why}")
prior = [{"t": i, "who": "me", "text": "I have a call."} for i in range(2)]
check(live.repeated(prior, "me", "I have a call"), "the same short line again is a loop")
check(not live.repeated(prior, "them", "I have a call."), "the other leg saying it is not")

# a mic chunk with only a blip of your voice is not sent at all
blip = np.concatenate([np.zeros(10 * live.RATE, dtype=np.float32), tone[: live.RATE // 4]])
sent = []
live.transcribe_chunks({"me": (0, blip), "them": (0, np.zeros_like(blip))},
                       lambda x: sent.append(len(x)) or [], [])
check(sent == [], "a quarter second of mic is not sent to Whisper")

# ---- follow a capture: mic speaks, app is silent, then the capture ends ----
calls = []


def fake_transcribe(samples):
    calls.append(len(samples))
    return [{"start": 0.5, "text": f" said {len(calls)}", "no_speech_prob": 0.01},
            {"start": 1.0, "text": " Thank you.", "no_speech_prob": 0.9}]


with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    cap, into = d / "whosaid-record-app.X", d / "rec"
    cap.mkdir()
    for name in ("mic.wav", "app.wav"):
        (cap / name).write_bytes(header())

    def writer():
        for _ in range(3):
            with open(cap / "mic.wav", "ab") as fh:
                fh.write(pcm(4, True) + pcm(0.5, False))
            with open(cap / "app.wav", "ab") as fh:
                fh.write(pcm(4.5, False))
            time.sleep(0.15)

    th = threading.Thread(target=writer)
    th.start()
    md = live.follow(cap, into, fake_transcribe, chunk_seconds=5, poll_seconds=0.05, ended_after=0.8)
    th.join()
    check(md is not None and md.exists(), "follow returns the transcript path")
    text = md.read_text()
    check("me: said" in text, f"mic speech is 'me': {text}")
    check("them:" not in text, "a silent app leg adds nothing (no Whisper call, no phantom lines)")
    check("Thank you." not in text, "no-speech segments are dropped")
    check("(call ended)" in text, "the end is marked")
    check(12 * live.RATE <= sum(calls) < 13.5 * live.RATE,
          f"all 12 s of mic speech sent once, the silences between cut short: {sum(calls)}")
    meta, entries = live.read_entries(md.with_suffix(".jsonl"))
    check(meta["ended"] is True and all(e["who"] == "me" for e in entries), "jsonl meta + entries")
    check(entries == sorted(entries, key=lambda e: e["t"]), "lines in time order")
    check(list(cap.iterdir()) and (cap / "mic.wav").exists(), "the capture folder is left untouched")

    # ---- reader (MCP tool + `live show`) ----
    r = live.latest_session(into)
    check(r["ok"] and r["ended"] and not r["active"] and "me: said" in r["transcript"], f"latest_session: {r}")
    jl = md.with_suffix(".jsonl")
    lines = [json.dumps({"meta": {"started": time.time(), "ended": False, "updated": time.time()}})]
    lines += [json.dumps({"t": t, "clock": "00:00:00", "who": "them", "text": f"at {t}"}) for t in (0, 300, 600)]
    jl.write_text("\n".join(lines) + "\n")
    r = live.latest_session(into, minutes=6)
    check(r["active"] and r["lines"] == 2 and "at 0" not in r["transcript"], f"last N minutes, live: {r}")
    check(not live.latest_session(d / "none")["ok"], "no session: ok=false with a hint")

# ---- live status --json (macOS app): newest session, no text, exit 0 ----
import contextlib, io  # noqa: E402
with tempfile.TemporaryDirectory() as d:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = live.main(["status", "--json", "--into", d])
    r = json.loads(buf.getvalue())
    check(rc == 0 and r["ok"] is False and "hint" in r, f"status --json with no session: {r}")
    jl = Path(d) / live.LIVE_DIR / "20261029T150000.jsonl"
    jl.parent.mkdir()
    now = time.time()
    jl.write_text(json.dumps({"meta": {"started": now - 60, "ended": False, "updated": now}}) + "\n"
                  + json.dumps({"t": 1, "clock": "00:00:01", "who": "me", "text": "hello"}) + "\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = live.main(["status", "--json", "--into", d])
    r = json.loads(buf.getvalue())
    check(rc == 0 and r["ok"] and r["active"] and r["lines"] == 1 and "transcript" not in r
          and r["jsonl"].endswith(".jsonl") and r["updated"], f"status --json, live session: {r}")

# ---- CLI: bare `whosaid live --flags` means run; a missing --capture fails ----
live.cmd_run = lambda a: ("run", a.once, a.capture)
check(live.main(["--once", "--capture", "/x"]) == ("run", True, "/x"), "bare flags parse as run")
check(live.main([]) == ("run", False, None), "no args = run")

print(f"PASS: live_test ({CHECKS} checks)")
