#!/usr/bin/env python3
"""
Offline test for lib/app_state.py (`whosaid app-state`, the macOS app's menu bar
feed): every composed menu bar state, the watch log rules it reuses from the
SwiftBar plugin, unnamed-voice counting, the newest meeting, and the --watch
stream. Temp dirs and a fake HOME only: no launchctl job, no real workspace.

Run: python3 test/app_state_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import app_state  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


# ---- compose: one state per G6 case, in precedence order ----
idle_w = {"installed": True, "loaded": True, "state": "idle", "detail": {}}
ar_wait = {"installed": True, "loaded": True, "state": "waiting"}
no_live = {"ok": False}
cases = [
    ("warn", ar_wait, {**idle_w, "state": "warn", "detail": {"msg": "! ingest failed"}}, no_live),
    ("warn", {**ar_wait, "state": "error", "last_error": {"kind": "permission", "rc": 5}}, idle_w, no_live),
    ("warn", {**ar_wait, "state": "error", "last_error": {"kind": "failed", "rc": 1}}, idle_w, no_live),
    ("warn", {**ar_wait, "running": False}, idle_w, no_live),
    ("warn", ar_wait, {**idle_w, "running": False, "last_exit_status": 1}, no_live),
    ("agent-not-loaded", ar_wait, {**idle_w, "loaded": False}, no_live),
    ("agent-not-loaded", {**ar_wait, "loaded": False}, idle_w, no_live),
    ("recording-live", {**ar_wait, "state": "recording"}, idle_w, {"ok": True, "active": True}),
    ("recording", {**ar_wait, "state": "recording"}, idle_w, {"ok": True, "active": False, "ended": True}),
    ("recording", {**ar_wait, "state": "saving"}, idle_w, no_live),
    ("paused", {**ar_wait, "state": "paused", "paused_until": "2026-10-29T16:00:00-07:00"}, idle_w, no_live),
    ("ingesting", ar_wait, {**idle_w, "state": "ingesting"}, no_live),
    ("waiting", ar_wait, {**idle_w, "state": "waiting"}, no_live),
    ("done", ar_wait, {**idle_w, "state": "done"}, no_live),
    ("idle", ar_wait, idle_w, no_live),
    # between on-demand runs the watcher is loaded, not running, last exit 0: not a warning
    ("idle", ar_wait, {**idle_w, "running": False, "last_exit_status": 0}, no_live),
    ("idle", ar_wait, {**idle_w, "running": False, "last_exit_status": None}, no_live),
    ("idle", {"installed": False, "loaded": None, "state": "not-installed"},
     {"installed": False, "loaded": None, "state": "idle"}, no_live),
]
for want, ar, w, lv in cases:
    got = app_state.compose(ar, w, lv)
    check(got["state"] == want and got["headline"], f"compose -> {want}: {got}")
check("2026-10-29T16:00" in app_state.compose(next(c[1] for c in cases if c[0] == "paused"), idle_w, no_live)["headline"],
      "paused shows its end")
check("percent" not in json.dumps([app_state.compose(*c[1:]) for c in cases]), "no ingest percent anywhere")
check(app_state.compose({**ar_wait, "state": "recording"}, {**idle_w, "state": "ingesting"}, no_live)["state"]
      == "recording", "recording outranks ingesting")

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    home, ws, rec = d / "home", d / "ws", d / "rec"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    ws.mkdir()
    rec.mkdir()
    app_state.watch.LAUNCH_AGENTS = home / "Library" / "LaunchAgents"
    app_state.autorecord.LAUNCH_AGENTS = home / "Library" / "LaunchAgents"

    # ---- empty: a clear idle snapshot, nothing installed ----
    s = app_state.snapshot(ws, str(rec), app_state.Unnamed())
    check(s["menubar"]["state"] == "idle" and s["autorecord"]["state"] == "not-installed"
          and s["watch"]["installed"] is False and s["live"]["ok"] is False
          and s["last_ingest"] is None and s["unnamed"]["voices"] == 0, f"empty snapshot: {s}")
    check({"active", "ended", "lines", "updated", "jsonl", "started"} <= set(s["live"]), "live keys present when ok=false")
    s = app_state.snapshot(None, str(rec), app_state.Unnamed())
    check(s["workspace"] is None and s["watch"]["state"] == "idle"
          and {"label", "last_exit_status"} <= set(s["watch"]), "no workspace at all still answers, same keys")

    # ---- watch log: only the watcher's own stage lines count (plugin rules) ----
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    stamp = stamp[:-2] + ":" + stamp[-2:]
    (ws / ".watch.log").write_text(
        f"{stamp} whosaid: 1 new recording(s)\n"
        f"{stamp} whosaid: > 1/1 ingest 20261029T150000Z-call.m4a\n"
        "chunk 8/8 done: diarizer output that looks like a done line\n")
    w = app_state.watch_state(ws)
    check(w["state"] == "ingesting" and w["installed"] is False, f"ingest stage line: {w}")

    # ---- unnamed voices and the newest meeting ----
    m1, m2, m3 = ws / "2026-10-28-0900", ws / "2026-10-29-1000", ws / "2026-09-01-0900"
    for m in (m1, m2, m3):
        m.mkdir()
    (m1 / "a.speakers.txt").write_text("[00:00:01] Alice_Example: hi\n[00:00:05] SPEAKER_01: hello\n"
                                      "[00:00:09] SPEAKER_01: again\n[00:00:12] SPEAKER_02: yes\n")
    (m2 / "b.speakers.txt").write_text("[00:00:01] Alice_Example: hi\n[00:00:04] Bob_Example: ok\n")
    (m3 / "c.speakers.txt").write_text("[00:00:01] SPEAKER_00: old\n")
    old = time.time() - 30 * 86400
    os.utime(m3 / "c.speakers.txt", (old, old))
    os.utime(m1 / "a.speakers.txt", (time.time() - 3600,) * 2)
    u = app_state.Unnamed()
    check(u.count(ws) == {"voices": 2, "meetings": 1, "days": 14}, f"unnamed: 2 voices in 1 recent meeting: {u.count(ws)}")
    (m2 / "b.speakers.txt").write_text("[00:00:01] SPEAKER_00: renamed back\n")
    check(u.count(ws)["voices"] == 3, "a rewritten file is re-read (mtime cache)")
    li = app_state.last_ingest(ws)
    check(li and li["folder"] == "2026-10-29-1000", f"newest meeting: {li}")

    # ---- --watch: one line at start, one more when something changes ----
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.Popen([sys.executable, str(ROOT / "lib" / "app_state.py"), "--watch", "0.5",
                             "--workspace", str(ws), "--into", str(rec), "--max-seconds", "4"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    time.sleep(1.5)
    (rec / ".live").mkdir()
    now = time.time()
    (rec / ".live" / "20261029T150000.jsonl").write_text(
        json.dumps({"meta": {"started": now, "ended": False, "updated": now + 3}}) + "\n")
    out, err = proc.communicate(timeout=30)
    lines = [json.loads(x) for x in out.splitlines() if x.strip()]
    check(proc.returncode == 0, f"--watch exits 0 at its bound: {err}")
    check(len(lines) == 2, f"--watch prints on change only: {len(lines)} line(s)")
    check(lines[0]["live"]["ok"] is False and lines[1]["live"]["ok"] is True, "the change is the new live session")
    check(not (rec / app_state.autorecord.CONTROL_DIR).exists() and not (rec / ".autorecord.lock").exists(),
          "app-state writes nothing into the recordings folder")

    # ---- the CLI front ----
    r = subprocess.run([str(ROOT / "whosaid"), "app-state", "--json", "--workspace", str(ws), "--into", str(rec)],
                       capture_output=True, text=True, timeout=60, env=env)
    check(r.returncode == 0 and json.loads(r.stdout)["version"] == 1, f"whosaid app-state --json: {r.stderr}")

print(f"PASS: app_state_test ({CHECKS} checks)")
