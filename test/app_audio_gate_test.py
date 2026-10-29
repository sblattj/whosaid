#!/usr/bin/env python3
"""G3 for the macOS app: nothing plays audio without a click.

Every AVAudioPlayer / AVPlayer / AVAudioEngine use in app/Sources lives in
WhosaidKit/Speakers.swift (ClipPlayer), and every ClipPlayer play() call sits
inside a Button action closure. Static check over the Swift tree; no audio."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "Sources"
CHECKS = 0


def check(ok, msg):
    global CHECKS
    CHECKS += 1
    if not ok:
        print(f"FAIL: {msg}")
        sys.exit(1)


files = sorted(SRC.rglob("*.swift"))
check(files, f"no Swift sources under {SRC}")
owner = SRC / "WhosaidKit" / "Speakers.swift"
for f in files:
    text = f.read_text()
    for m in re.finditer(r"\bAV(Audio)?Player\b|\bAVAudioEngine\b|\bNSSound\b", text):
        check(f == owner, f"{f.relative_to(ROOT)}:{text[:m.start()].count(chr(10)) + 1}: audio API outside ClipPlayer")

calls = 0
for f in files:
    if f == owner:
        continue
    lines = f.read_text().splitlines()
    for i, line in enumerate(lines):
        if "ClipPlayer" in line and ".play(" in line:
            calls += 1
            # Walk back to the enclosing block opener: it must be a Button action.
            window = "\n".join(lines[max(0, i - 6):i + 1])
            check(re.search(r"Button\s*(\([^)]*\))?\s*\{", window) is not None,
                  f"{f.relative_to(ROOT)}:{i + 1}: ClipPlayer.play outside a Button action")
check(calls >= 1, "expected the clip button's ClipPlayer.play call")
owner_text = owner.read_text()
check(owner_text.count(".play()") == 1 and "func play(_ url: URL)" in owner_text,
      "ClipPlayer has exactly one play() and it is the click entry point")
print(f"PASS: app_audio_gate_test ({CHECKS} checks)")
