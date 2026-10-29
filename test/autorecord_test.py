#!/usr/bin/env python3
"""
Offline test for lib/autorecord.py (`whosaid autorecord`, issue #122): exit-code
classification, the record command per mode, the LaunchAgent plist, and the run
loop against a fake `whosaid` (saved / app not running / permission), plus the
one-copy-per-folder lock. No launchctl, no capture, no audio.

Run: python3 test/autorecord_test.py
"""
import fcntl
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import autorecord as ar  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


# ---- classification ----
check(ar.classify(0, "/r/a.m4a") == ("saved", 0.0), "0 + path = saved")
check(ar.classify(0, None) == ("idle", 0.0), "0, no path = interrupted wait")
check(ar.classify(3, "/r/a.m4a") == ("saved", 0.0), "3 + partial file = saved")
check(ar.classify(2, None) == ("waiting", 30.0), "2 = app not running, wait 30s")
check(ar.classify(6, None) == ("waiting", 10.0), "6 = CDP target race, wait 10s")
check(ar.classify(5, None) == ("permission", None), "5 = permission, back off")
check(ar.classify(1, None) == ("failed", None), "other = failed, back off")

# ---- record argv / plist ----
a = ar.parse_args(["run", "--app", "com.example.app", "--into", "/r", "--label", "teams", "--whosaid", "/w"])
check(ar.record_argv(a) == ["/w", "record", "--app", "com.example.app", "--wait-for-call", "--until-call-ends",
                            "--idle-seconds", "30", "--into", "/r", "--label", "teams"], f"app argv {ar.record_argv(a)}")
b = ar.parse_args(["run", "--browser", "--cdp-port", "9333", "--target", "teams", "--into", "/r", "--whosaid", "/w"])
check(ar.record_argv(b) == ["/w", "record", "--browser", "--wait-for-call", "--cdp-port", "9333",
                            "--target", "teams", "--into", "/r", "--label", "call"], f"browser argv {ar.record_argv(b)}")
i = ar.parse_args(["install", "--app", "com.example.app", "--into", "/r", "--whosaid", "/w"])
pl = ar.build_plist(i, Path("/Apps/X.app"))
check(pl["ProgramArguments"][:7] == ["/usr/bin/open", "-W", "-g", "-a", "/Apps/X.app", "--args", "/w"]
      and pl["ProgramArguments"][7:9] == ["autorecord", "run"], f"app plist opens the launcher: {pl['ProgramArguments']}")
check(pl["KeepAlive"] == {"SuccessfulExit": False} and pl["RunAtLoad"] is True, "restarts after a crash, starts at login")
j = ar.parse_args(["install", "--browser", "--into", "/r", "--whosaid", "/w"])
pl = ar.build_plist(j, None)
check(pl["ProgramArguments"][:3] == ["/w", "autorecord", "run"] and "--browser" in pl["ProgramArguments"],
      "browser plist runs whosaid directly (no app, no grant)")
try:
    ar.parse_args(["run", "--app", "x", "--target", "t"])
    check(False, "--target without --browser must fail")
except SystemExit as e:
    check(e.code == 2, "--target without --browser exits 2")

# ---- run loop against a fake whosaid ----
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    counter = d / "n"
    fake = d / "whosaid"
    # Call 1: saves a file. Call 2: app not running (2). Call 3: no permission (5).
    fake.write_text(f"""#!/bin/bash
n=$(( $(cat {counter} 2>/dev/null || echo 0) + 1 )); echo $n > {counter}
echo "record-app:    10s  app peak -5 dB" >&2
case $n in
  1) echo "whosaid: captured 3s" >&2; echo "{d}/rec/20261029T150000Z-call.m4a"; exit 0 ;;
  2) echo "no running app matches" >&2; exit 2 ;;
  *) echo "permission denied" >&2; exit 5 ;;
esac
""")
    fake.chmod(0o755)
    env = dict(os.environ, WHOSAID_AUTORECORD_MAX_LOOPS="3", WHOSAID_AUTORECORD_DELAY_SCALE="0.001")
    r = subprocess.run([sys.executable, str(ROOT / "lib" / "autorecord.py"), "run", "--app", "com.example.app",
                        "--into", str(d / "rec"), "--whosaid", str(fake)],
                       capture_output=True, text=True, timeout=60, env=env)
    logtxt = (d / "rec" / ".autorecord.log").read_text()
    check(r.returncode == 0, f"run exits 0: {r.returncode} {r.stderr}")
    check("autorecord: saved " + str(d / "rec" / "20261029T150000Z-call.m4a") in logtxt, f"saved line: {logtxt}")
    check("record exited 2 (app not running" in logtxt, "exit 2 is a waiting state")
    check("no Screen & System Audio Recording permission" in logtxt, "exit 5 logs the permission fix")
    check(" peak " not in logtxt, "progress ticks stay out of the log")
    check(counter.read_text().strip() == "3", "three iterations ran")
    # Lock: while another process holds it, run exits 0 at once without recording.
    counter.write_text("0")
    with open(d / "rec" / ar.LOCK_NAME, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = subprocess.run([sys.executable, str(ROOT / "lib" / "autorecord.py"), "run", "--app", "x",
                            "--into", str(d / "rec"), "--whosaid", str(fake)],
                           capture_output=True, text=True, timeout=30, env=env)
    check(r.returncode == 0 and "already running" in r.stderr and counter.read_text().strip() == "0",
          f"second copy exits 0 without recording: {r.stderr}")
    # install --dry-run writes nothing and prints the plist.
    r = subprocess.run([sys.executable, str(ROOT / "lib" / "autorecord.py"), "install", "--browser",
                        "--into", str(d / "rec"), "--whosaid", str(fake), "--dry-run"],
                       capture_output=True, text=True, timeout=30,
                       env=dict(env, WHOSAID_AUTORECORD_APP_DIR=str(d / "apps")))
    check(r.returncode == 0 and "DRY RUN" in r.stderr, f"dry run: {r.stderr}")
    check(plistlib.loads(r.stdout.encode())["Label"] == ar.LABEL, "dry run prints the plist")
    check(not (d / "apps").exists(), "dry run builds nothing")

print(f"PASS: autorecord_test ({CHECKS} checks)")
