#!/usr/bin/env python3
"""Offline test for test/live_diarize_eval.py (#218): the --realtime output guard (only a
loopback driver, never speakers, headphones, a meeting app's device or a combination),
the release gate, and a cassette miss that live.py's error handling can't swallow.
No audio is played and no device is opened: the guard is fed device lists.
Run: python3 test/live_diarize_eval_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_diarize_eval as ev  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def dev(name, transport="virt", outputs=1, index=0):
    return {"index": index, "name": name, "transport": transport, "outputs": outputs}


DEVICES = [dev("MacBook Pro Microphone", "bltn", 0, 0), dev("MacBook Pro Speakers", "bltn", 1, 1),
           dev("Microsoft Teams Audio", "virt", 1, 2), dev("BlackHole 2ch", "virt", 1, 3),
           dev("BlackHole + Speakers", "grup", 1, 4), dev("Multi-Output Device", "grup", 1, 5),
           dev("BlackHole 16ch", "virt", 0, 6), dev("External Headphones", "bltn", 1, 7),
           dev("BlackHole Speakers", "virt", 1, 8)]


def ok(name, devices=DEVICES):
    return ev.check_output_device(name, devices)[0]


# ---------------------------------------------------------------- --realtime output guard
check(ok("BlackHole 2ch"), "a BlackHole loopback with outputs: allowed")
check(ok("blackhole 2ch"), "the name is matched without case")
check(not ok("MacBook Pro Speakers"), "built-in speakers: refused")
check(not ok("External Headphones"), "headphones: refused")
check(not ok("Microsoft Teams Audio"), "a meeting app's virtual device: refused (it feeds a meeting)")
check(not ok("BlackHole + Speakers"), "an aggregate of BlackHole and speakers: refused")
check(not ok("Multi-Output Device"), "a multi-output device: refused")
check(not ok("BlackHole Speakers"), "a name that says speakers: refused even with a loopback prefix")
check(not ok("BlackHole 16ch"), "a loopback with no output streams: refused")
check(not ok("BlackHole"), "a prefix of a device name is not that device: refused")
check(not ok("BlackHole 2ch", [dev("BlackHole 2ch", "bltn")]), "a loopback name on a non-virtual transport: refused")
check(not ok("BlackHole 2ch", DEVICES + [dev("BlackHole 2ch", "virt", 1, 9)]),
      "two devices with the name: refused (ffmpeg's index would be a guess)")
check(not ok("", DEVICES) and not ok("BlackHole 2ch", []), "no name or no devices: refused")
why = ev.check_output_device("MacBook Pro Speakers", DEVICES)[1]
check("never to speakers" in why, f"the refusal says why: {why}")
for d in ev.audio_devices():  # this Mac, read-only: whatever it has, speakers are never allowed
    if "speaker" in d["name"].lower() or d["transport"] == "bltn":
        check(not ok(d["name"], ev.audio_devices()), f"this Mac's {d['name']!r}: refused")
rc = ev.main(["--realtime", "--device", "MacBook Pro Speakers", "--app", "com.apple.Terminal", "--only", "handoff"])
check(rc == 3, f"--realtime to speakers exits 3 before anything loads or plays: {rc}")

# ---------------------------------------------------------------- the release gate
def suite(name_acc=0.9, retro=0.8, **live):
    s = {"live": dict({"name_acc": name_acc, "handoff_acc": 0.9, "backchannel_acc": 0.8, "unnamed": 0.01,
                       "split_voice": 0.0, "guest_named": 0.01}, **live),
         "retro": {"name_acc": retro}, "disagree_live_off": 0.01, "_calls": ["a", "b"]}
    return s


check(ev.gate(suite(), 5, None) == [], "live above retroactive, no baseline: pass")
check(ev.gate(suite(0.86, 0.9), 5, None) == [], "live 4 points under retroactive, max gap 5: pass")
bad = ev.gate(suite(0.80, 0.9), 5, None)
check(len(bad) == 1 and "10.0 points under retroactive" in bad[0], f"live 10 points under retroactive: fail: {bad}")
base = {"suite": suite(), "calls": {"a": {}, "b": {}}}
check(ev.gate(suite(), 5, base) == [], "same as the baseline: pass")
check(ev.gate(suite(0.897), 5, base) == [], "0.3 points under the baseline: inside the tolerance")
bad = ev.gate(suite(handoff_acc=0.8), 5, base)
check(len(bad) == 1 and "handoff_acc" in bad[0], f"handoffs 10 points under the baseline: fail: {bad}")
bad = ev.gate(suite(split_voice=0.1), 5, base)
check(len(bad) == 1 and "split_voice" in bad[0], f"a lower-is-better metric that rose: fail: {bad}")
worse = suite()
worse["disagree_live_off"] = 0.05
bad = ev.gate(worse, 5, base)
check(len(bad) == 1 and "disagree_live_off" in bad[0], f"more turns live got wrong: fail: {bad}")
bad = ev.gate(suite(), 5, {"suite": suite(), "calls": {"a": {}}})
check(any("other calls" in b for b in bad), f"a baseline over other calls: fail: {bad}")

# ---------------------------------------------------------------- a cassette miss stops the run
class Silence(list):  # numpy-free stand-in for a chunk: what chunk_key reads
    def astype(self, _):
        return self

    def tobytes(self):
        return bytes(4 * len(self))


caught = None
try:
    try:
        ev.cassette_transcriber({"live": {}}, False, False)(Silence([0.0] * 16000))
    except Exception:  # noqa: BLE001  what lib/live.py does around a transcription
        caught = "swallowed"
except ev.CassetteMiss:
    caught = "raised"
check(caught == "raised", f"a cassette miss is not an Exception live.py would skip past: {caught}")

# ---------------------------------------------------------------- --realtime follows only its own capture
# A fake `whosaid`: record makes a capture folder the way the real one does (mktemp -d -t),
# live run writes down its arguments. A real capture folder made just before must not be the
# one followed. The device re-check refuses, so ffmpeg is never reached (and would raise).
try:
    import numpy  # noqa: F401  run_realtime assembles the call
except ImportError:
    numpy = None
if numpy is None:
    print("skip: --realtime capture pinning needs numpy (uv run --with numpy python test/live_diarize_eval_test.py)")
else:
    import os
    import subprocess
    import tempfile
    import time
    import types
    work = Path(tempfile.mkdtemp(prefix="live-eval-test-"))
    fake = work / "whosaid"
    fake.write_text('#!/bin/bash\nif [[ "$1" == record ]]; then d=$(mktemp -d -t whosaid-record-app); '
                    'echo "$d" > "%s/rec-dir"; exec sleep 60; fi\n'
                    'echo "$@" > "%s/live-args"; exec sleep 60\n' % (work, work))
    fake.chmod(0o755)
    other = Path(subprocess.check_output(["mktemp", "-d", "-t", "whosaid-record-app"], text=True).strip())
    os.environ["WHOSAID_LIVE_EVAL_BIN"] = str(fake)
    real_run, real_devices = subprocess.run, ev.audio_devices

    def no_play(cmd, *a, **k):
        if cmd and cmd[0] == "ffmpeg":
            raise AssertionError("ffmpeg must not run in this test")
        return real_run(cmd, *a, **k)
    subprocess.run = no_play
    ev.audio_devices = lambda: []  # the device vanished before playback: refuse
    try:
        args = types.SimpleNamespace(app="com.example.player", device="BlackHole 2ch")
        call = next(c for c in __import__("json").loads((ev.FIX / "calls.json").read_text())["calls"] if c["name"] == "backchannel")
        t0 = time.time()
        rc = ev.run_realtime(args, call, ev.load_bank())
        live_args = (work / "live-args").read_text().split() if (work / "live-args").exists() else []
        rec_dir = (work / "rec-dir").read_text().strip() if (work / "rec-dir").exists() else ""
    finally:
        subprocess.run, ev.audio_devices = real_run, real_devices
        for d in (other, Path(rec_dir) if rec_dir else None):
            if d and d.is_dir():
                d.rmdir()
    check(rc == 3, f"a device gone at the re-check: refused with 3: {rc}")
    check(rec_dir and "--capture" in live_args and live_args[live_args.index("--capture") + 1] == rec_dir,
          f"live run follows the recorder's own folder {rec_dir}: {live_args}")
    check(str(other) not in live_args, "an older capture folder (maybe a real call) is not followed")

print(f"PASS: live_diarize_eval_test ({CHECKS} checks)")
