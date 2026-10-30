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
import re
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
    # Lock: while a live loop holds it, run exits 0 at once without recording.
    counter.write_text("0")
    run2 = [sys.executable, str(ROOT / "lib" / "autorecord.py"), "run", "--app", "x",
            "--into", str(d / "rec"), "--whosaid", str(fake)]
    with open(d / "rec" / ar.LOCK_NAME, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ar.write_json_atomic(d / "rec" / ar.STATE_NAME, {"pid": os.getpid(), "state": "waiting"})
        r = subprocess.run(run2, capture_output=True, text=True, timeout=30, env=env)
        check(r.returncode == 0 and "already running" in r.stderr and counter.read_text().strip() == "0",
              f"second copy exits 0 without recording: {r.stderr}")
        # Held, but no live loop on record (a status probe at the wrong instant): exit 1 so launchd retries.
        ar.write_json_atomic(d / "rec" / ar.STATE_NAME, {"pid": os.getpid(), "state": "stopped"})
        r = subprocess.run(run2, capture_output=True, text=True, timeout=30, env=env)
        check(r.returncode == 1 and "retried" in r.stderr and counter.read_text().strip() == "0",
              f"a lost lock with no live owner exits 1: {r.returncode} {r.stderr}")
    # install --dry-run writes nothing and prints the plist.
    r = subprocess.run([sys.executable, str(ROOT / "lib" / "autorecord.py"), "install", "--browser",
                        "--into", str(d / "rec"), "--whosaid", str(fake), "--dry-run"],
                       capture_output=True, text=True, timeout=30,
                       env=dict(env, WHOSAID_AUTORECORD_APP_DIR=str(d / "apps")))
    check(r.returncode == 0 and "DRY RUN" in r.stderr, f"dry run: {r.stderr}")
    check(plistlib.loads(r.stdout.encode())["Label"] == ar.LABEL, "dry run prints the plist")
    check(not (d / "apps").exists(), "dry run builds nothing")

    # ---- setup (#127) ----
    calls = d / "calls"
    fake2 = d / "whosaid2"
    fake2.write_text(f"""#!/bin/bash
echo "$*" >> {calls}
[[ $1 == setup ]] && exit 7
exit 0
""")
    fake2.chmod(0o755)
    (d / "ws").mkdir()
    setup = [sys.executable, str(ROOT / "lib" / "autorecord.py"), "setup", "--app", "com.example.app",
             "--into", str(d / "rec2"), "--workspace", str(d / "ws"), "--whosaid", str(fake2)]
    senv = dict(env, WHOSAID_AUTORECORD_APP_DIR=str(d / "apps"))
    r = subprocess.run(setup + ["--owner", "Alice_Example", "--dry-run"], capture_output=True, text=True,
                       timeout=30, env=senv)
    check(r.returncode == 0, f"setup dry run exits 0: {r.stderr}")
    check(f"{fake2} setup --yes" in r.stderr and "2/3 record every call" in r.stderr
          and "watch install --into" in r.stderr and "--no-fda --dry-run" in r.stderr,
          f"setup dry run lists all three steps: {r.stderr}")
    check(calls.read_text().split("\n")[0].startswith("watch install") and "setup --yes" not in calls.read_text(),
          "dry run only calls watch install --dry-run")
    check(not (d / "ws" / "whosaid.toml").exists() and not (d / "apps").exists() and not (d / "rec2").exists(),
          "setup dry run writes nothing")
    check('Whosaid Call Recorder' in r.stdout and "whosaid enroll" in r.stdout, f"lists what is left: {r.stdout}")
    calls.write_text("")
    r = subprocess.run(setup, capture_output=True, text=True, timeout=30, env=senv)
    check(r.returncode == 7 and "`whosaid setup` failed (exit 7)" in r.stderr
          and calls.read_text().strip() == "setup --yes" and not (d / "rec2").exists(),
          f"a failed deps step stops setup before install: {r.returncode} {r.stderr}")
    r = subprocess.run(setup + ["--owner", 'x"; rm', "--dry-run"], capture_output=True, text=True,
                       timeout=30, env=senv)
    check(r.returncode == 2, "an owner that would break the toml is refused")
    r = subprocess.run(setup[:-6] + ["--into", str(d / "ws"), "--workspace", str(d / "ws"),
                                    "--whosaid", str(fake2), "--dry-run"],
                       capture_output=True, text=True, timeout=30, env=senv)
    check(r.returncode == 2 and "must be different" in r.stderr, "recordings folder != workspace")
    ar.write_owner(d / "ws", "Alice_Example", False)
    check((d / "ws" / "whosaid.toml").read_text().startswith('[workspace]\nowner = "Alice_Example"'),
          "owner written to a new whosaid.toml")
    (d / "ws" / "whosaid.toml").write_text("# mine\n")
    ar.write_owner(d / "ws", "Bob_Example", False)
    check((d / "ws" / "whosaid.toml").read_text() == "# mine\n", "an existing whosaid.toml is never edited")

# ---- state file, stop-now, pause/resume, status --json (macOS app, P1) ----
import json  # noqa: E402
import time  # noqa: E402

check(ar.state_from_log([
    "2026-10-29 10:00:00 autorecord: app x -> /r; Ctrl-C to quit",
    "2026-10-29 10:00:01 record-app: X opened the mic; starting the capture.",
])["state"] == "recording", "log fallback: an open capture is recording")
sl = ar.state_from_log([
    "2026-10-29 10:00:01 record-app: X opened the mic; starting the capture.",
    "2026-10-29 10:30:00 autorecord: saved /r/a.m4a",
    "2026-10-29 10:31:00 autorecord: record exited 1; retrying in 30s",
])
check(sl["state"] == "error" and sl["last_saved"]["path"] == "/r/a.m4a" and sl["last_error"]["rc"] == 1,
      f"log fallback: saved then failed: {sl}")
check(ar.state_from_log(["2026-10-29 10:31:00 autorecord: record exited 2 (app not running or no meeting tab); "
                         "retrying in 30s"])["state"] == "waiting", "log fallback: exit 2 is waiting")


def wait_for(pred, secs=15.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.1)
    return None


with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    rec, home = d / "rec", d / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    fake = d / "whosaid"
    fake.write_text(f"""#!/bin/bash
n=$(( $(cat {d}/n 2>/dev/null || echo 0) + 1 )); echo $n > {d}/n
got=0; trap 'got=$((got+1)); echo "child $n SIGINT $got" >> {d}/sigs' INT
echo $n > {d}/ready
echo "record-app: waiting for Example to open the mic (a call to start); Ctrl-C to give up." >&2
if [[ $(cat {d}/mode) == record ]]; then
  sleep 0.3
  echo "record-app: Example opened the mic; starting the capture." >&2
  echo "record-app:    10s  app peak -5 dB" >&2
  while [[ $got == 0 ]]; do sleep 0.1; done
  echo "{rec}/20261029T150000Z-call-$n.m4a"; exit 0
fi
while [[ $got == 0 ]]; do sleep 0.1; done
echo "record-app: stopped while waiting for a call; nothing recorded." >&2; exit 0
""")
    fake.chmod(0o755)
    (d / "mode").write_text("record")
    env = dict(os.environ, HOME=str(home), WHOSAID_AUTORECORD_MAX_LOOPS="3", WHOSAID_AUTORECORD_DELAY_SCALE="0.001")
    py = [sys.executable, str(ROOT / "lib" / "autorecord.py")]

    def cli(*args, timeout=30):
        return subprocess.run(py + list(args), capture_output=True, text=True, timeout=timeout, env=env)

    def st():
        r = cli("status", "--json", "--into", str(rec))
        return json.loads(r.stdout) if r.returncode == 0 else {}

    s0 = st()
    check(s0.get("installed") is False and s0.get("running") is False and s0.get("state") == "not-installed"
          and s0.get("loaded") is None, f"status --json, nothing installed: {s0}")
    r = cli("stop-now", "--into", str(rec))
    check(r.returncode == 4 and "not running" in r.stderr, f"stop-now with no loop refuses: {r.stderr}")

    loop = subprocess.Popen(py + ["run", "--app", "com.example.app", "--into", str(rec), "--whosaid", str(fake)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        s1 = wait_for(lambda: (x := st()).get("state") == "recording" and x)
        check(bool(s1) and s1["running"] and s1["controls"] and s1["source"] == "state-file"
              and isinstance(s1["elapsed"], int), f"state file: recording: {s1}")
        (d / "mode").write_text("wait")  # child 1 is past its mode check; child 2 will wait
        r = cli("stop-now", "--into", str(rec), "--wait", "10")
        check(r.returncode == 0 and r.stdout.strip().endswith("call-1.m4a"), f"stop-now saves: {r.stdout} {r.stderr}")
        check((d / "sigs").read_text() == "child 1 SIGINT 1\n", f"exactly one SIGINT: {(d / 'sigs').read_text()}")
        check(not (rec / ar.STOP_NOW_NAME).exists(), "the stop-now request is consumed")
        s2 = wait_for(lambda: (x := st()).get("state") == "waiting"
                      and (d / "ready").read_text().strip() == "2" and x)
        check(bool(s2) and s2["last_saved"]["path"].endswith("call-1.m4a"), f"loop goes on after stop-now: {s2}")
        r = cli("stop-now", "--into", str(rec))
        check(r.returncode == 1 and "nothing is being recorded" in r.stderr, "stop-now while waiting refuses")
        r = cli("pause", "--minutes", "5", "--into", str(rec))
        check(r.returncode == 0 and "paused until" in r.stderr, f"pause: {r.stderr}")
        s3 = wait_for(lambda: (x := st()).get("state") == "paused" and x)
        check(bool(s3) and s3["paused_until"], f"pause ends the wait; state paused with an end time: {s3}")
        check(bool(wait_for(lambda: "child 2 SIGINT 1" in (d / "sigs").read_text(), 5)),
              f"the waiting child got one SIGINT: {(d / 'sigs').read_text()!r} {(rec / ar.LOG_NAME).read_text()}")
        time.sleep(1.0)
        check((d / "n").read_text().strip() == "2", "no new record child while paused")
        r = cli("resume", "--into", str(rec))
        check(r.returncode == 0 and not (rec / ar.PAUSE_NAME).exists(), "resume removes the pause")
        check(bool(wait_for(lambda: (d / "n").read_text().strip() == "3")), "resume starts the next wait")
        loop.terminate()
        loop.wait(timeout=15)
    finally:
        if loop.poll() is None:
            loop.kill()
    alive = wait_for(lambda: not subprocess.run(["pgrep", "-f", str(fake)], capture_output=True).stdout, 5)
    check(bool(alive), "no record child outlives the loop")
    subprocess.run(["pkill", "-f", str(fake)], capture_output=True)
    s4 = st()
    check(s4["state"] == "stopped" and s4["running"] is False and s4["controls"] is False, f"stopped: {s4}")
    check(not any(p.name.startswith(".autorecord.") and p.suffix != ".log" and p.name != ar.LOCK_NAME
                  for p in rec.iterdir()), f"state and control files stay out of <into> itself: {list(rec.iterdir())}")
    check(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$", s4["since"] or ""),
          f"RFC 3339 times with a colon offset: {s4['since']}")

    # A long-quiet state file whose pid is alive but holds no lock (pid reuse after a crash).
    ar.write_json_atomic(rec / ar.STATE_NAME, {"pid": os.getpid(), "state": "waiting", "updated": "2026-01-01T00:00:00"})
    s4b = st()
    check(s4b["running"] is False and s4b["controls"] is False, f"a stale state file is not a running loop: {s4b}")

    # An older loop: the lock is held, but no state file. Read the log; refuse controls.
    (rec / ar.STATE_NAME).unlink()
    (rec / ar.LOG_NAME).write_text("2026-10-29 10:00:01 record-app: X opened the mic; starting the capture.\n")
    with open(rec / ar.LOCK_NAME, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s5 = st()
        r = cli("pause", "--into", str(rec))
    check(s5["source"] == "log" and s5["state"] == "recording" and s5["running"] and not s5["controls"],
          f"older loop read from the log: {s5}")
    check(r.returncode == 4 and "reinstall" in r.stderr, f"pause refuses on an older loop: {r.stderr}")

    # Installed + a fake launchctl that says loaded.
    bindir = d / "bin"
    bindir.mkdir()
    (bindir / "launchctl").write_text(f"#!/bin/bash\necho \"$*\" >> {d}/launchctl.calls\nexit 0\n")
    (bindir / "launchctl").chmod(0o755)
    i = ar.parse_args(["install", "--app", "com.example.app", "--into", str(rec), "--whosaid", str(fake)])
    with open(home / "Library" / "LaunchAgents" / f"{ar.LABEL}.plist", "wb") as fh:
        plistlib.dump(ar.build_plist(i, Path("/Apps/X.app")), fh)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    s6 = json.loads(cli("status", "--json").stdout)
    check(s6["installed"] and s6["mode"] == "app" and s6["app"] == "com.example.app" and s6["into"] == str(rec),
          f"installed config read from the plist: {s6}")
    check(s6["loaded"] is (sys.platform == "darwin"), f"loaded via launchctl print: {s6['loaded']}")
    if sys.platform == "darwin":
        check((d / "launchctl.calls").read_text().startswith("print gui/"), "only launchctl print is called")

# ---- the call ends by itself: while the file is being written, nothing signals it ----
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    rec, home = d / "rec", d / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    fake = d / "whosaid"
    fake.write_text(f"""#!/bin/bash
trap 'echo "SIGINT" >> {d}/sigs' INT
echo "record-app: Example opened the mic; starting the capture." >&2
sleep 0.5
echo "whosaid: capture ended; saving the recording." >&2
for i in $(seq 40); do [[ -e {d}/go ]] && break; sleep 0.1; done
echo "{rec}/20261029T160000Z-call.m4a"; exit 0
""")
    fake.chmod(0o755)
    env = dict(os.environ, HOME=str(home), WHOSAID_AUTORECORD_MAX_LOOPS="1", WHOSAID_AUTORECORD_DELAY_SCALE="0.001")
    py = [sys.executable, str(ROOT / "lib" / "autorecord.py")]
    loop = subprocess.Popen(py + ["run", "--app", "com.example.app", "--into", str(rec), "--whosaid", str(fake)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        def sj():
            r = subprocess.run(py + ["status", "--json", "--into", str(rec)], capture_output=True, text=True,
                               timeout=30, env=env)
            return json.loads(r.stdout)
        s7 = wait_for(lambda: (x := sj())["state"] == "saving" and x)
        check(bool(s7), "the capture-ended line moves the state to saving")
        r = subprocess.run(py + ["stop-now", "--into", str(rec)], capture_output=True, text=True, timeout=30, env=env)
        check(r.returncode == 1 and "saving" in r.stderr, f"stop-now while saving refuses: {r.stderr}")
        r = subprocess.run(py + ["pause", "--minutes", "5", "--into", str(rec)], capture_output=True, text=True,
                           timeout=30, env=env)
        check(r.returncode == 0, f"pause while saving is accepted for later: {r.stderr}")
        time.sleep(1.2)  # two control polls with the pause file in place
        check(sj()["state"] == "saving", "a pause does not interrupt the save")
        (d / "go").write_text("")
        loop.wait(timeout=20)
    finally:
        if loop.poll() is None:
            loop.kill()
    check(not (d / "sigs").exists(), f"no SIGINT while saving: {(d / 'sigs').read_text() if (d / 'sigs').exists() else ''}")
    check("autorecord: saved " in (rec / ar.LOG_NAME).read_text(), "the file is saved")

# ---- --live: `whosaid live` starts with the capture and stops with it (macOS app, P8) ----
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    rec = d / "rec"
    fake = d / "whosaid"
    # `live`: records its argv and SIGTERM, then exits like live does (final chunk, ended).
    # `record`: call 1 captures for 1 s and saves; call 2 finds no app (exit 2), so no live.
    fake.write_text(f"""#!/bin/bash
if [[ $1 == live ]]; then
  echo "$*" > {d}/live.args
  trap 'echo stopped >> {d}/live.events; exit 0' TERM
  echo started >> {d}/live.events
  while true; do sleep 0.1; done
fi
n=$(( $(cat {d}/n 2>/dev/null || echo 0) + 1 )); echo $n > {d}/n
if [[ $n == 1 ]]; then
  echo "record-app: Example opened the mic; starting the capture." >&2
  sleep 1
  [[ -f {d}/live.events ]] && cp {d}/live.events {d}/live.during
  echo "record-app: capture ended; saving the recording" >&2
  echo "{rec}/20261029T160000Z-call.m4a"; exit 0
fi
echo "no running app matches" >&2; exit 2
""")
    fake.chmod(0o755)
    env = dict(os.environ, HOME=str(d / "home"), WHOSAID_AUTORECORD_MAX_LOOPS="2",
               WHOSAID_AUTORECORD_DELAY_SCALE="0.001")
    run = [sys.executable, str(ROOT / "lib" / "autorecord.py"), "run", "--app", "com.example.app",
           "--into", str(rec), "--whosaid", str(fake)]
    r = subprocess.run(run, capture_output=True, text=True, timeout=60, env=env)
    check(r.returncode == 0 and not (d / "live.events").exists() and not (rec / ar.LIVE_LOG).exists(),
          f"without --live no live child runs: {r.stderr}")
    (d / "n").unlink()
    r = subprocess.run(run + ["--live"], capture_output=True, text=True, timeout=60, env=env)
    logtxt = (rec / ar.LOG_NAME).read_text().split("autorecord: stopped")[1]
    check(r.returncode == 0, f"run --live exits 0: {r.stderr}")
    check((d / "live.args").read_text().split() == ["live", "run", "--into", str(rec), "--once"],
          f"live follows this folder, one call: {(d / 'live.args').read_text()}")
    check((d / "live.during").read_text() == "started\n", "live runs while the capture runs")
    check((d / "live.events").read_text() == "started\nstopped\n", f"live got one SIGTERM: {(d / 'live.events').read_text()}")
    order = [logtxt.find(x) for x in ("starting the capture", "live transcript started",
                                      "live transcript stopped", "autorecord: saved", "record exited 2")]
    check(all(i >= 0 for i in order) and order == sorted(order),
          f"capture start -> live start -> live stop -> saved -> next wait: {order}\n{logtxt}")
    check(logtxt.count("live transcript started") == 1, "the no-app wait starts no live child")
    sj = json.loads((rec / ar.STATE_NAME).read_text())
    check(sj["live"] is True and sj["live_pid"] is None and sj["state"] == "stopped", f"state file: {sj}")
    check(not subprocess.run(["pgrep", "-f", f"{fake} live"], capture_output=True).stdout, "no live child outlives the loop")
    # install and status carry the opt-in.
    i = ar.parse_args(["install", "--app", "com.example.app", "--into", "/r", "--whosaid", "/w", "--live"])
    check(ar.build_plist(i, None)["ProgramArguments"][-1] == "--live", "install --live reaches the agent's run")
    i = ar.parse_args(["install", "--app", "com.example.app", "--into", "/r", "--whosaid", "/w"])
    check("--live" not in ar.build_plist(i, None)["ProgramArguments"], "live stays off by default")
    s = json.loads(subprocess.run([sys.executable, str(ROOT / "lib" / "autorecord.py"), "status", "--json",
                                   "--into", str(rec)], capture_output=True, text=True, timeout=30, env=env).stdout)
    check(s["live"] is True, f"status --json reports live from the last loop: {s}")

print(f"PASS: autorecord_test ({CHECKS} checks)")
