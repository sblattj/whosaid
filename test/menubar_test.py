#!/usr/bin/env python3
"""
Offline test for the SwiftBar menu bar plugin (GitHub issue #24).

Covers contrib/swiftbar/whosaid.10s.py's pure functions (the ~2 MB byte tail,
the stage-line classifier with its subprocess-noise traps, the glyph picker,
REC detection, the dated-folderNAME latest-meeting rule, the action token
round-trip, and the notify-once-per-transition state file) and lib/watch.py's
`menubar` subcommand group (install / uninstall / status) against temp plugin
directories via the WHOSAID_SWIFTBAR_DIR / WHOSAID_SWIFTBAR_APP env overrides
and a fake `defaults` on PATH. The plugin itself is never executed beyond
importing it as a module; no SwiftBar, launchd agent, notification, or real
Voice Memos store is ever touched.

Run:
    python3 test/menubar_test.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "contrib" / "swiftbar" / "whosaid.10s.py"
WATCH_PY = REPO / "lib" / "watch.py"

CHECKS = 0


def check(cond: bool, what: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError(what)


def load_plugin():
    spec = importlib.util.spec_from_file_location("whosaid_menubar_plugin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def stage(msg: str, minute: int, second: int = 0, hour: int = 9) -> str:
    return f"2026-09-18T{hour:02d}:{minute:02d}:{second:02d}+03:00 whosaid: {msg}"


def when(minute: int, second: int = 0, hour: int = 9) -> dt.datetime:
    return dt.datetime.fromisoformat(f"2026-09-18T{hour:02d}:{minute:02d}:{second:02d}+03:00")


# ---- plugin pure functions -----------------------------------------------------------


def test_classifier(P) -> None:
    idle = [stage("no new recordings.", 0, 0)]
    state, detail = P.classify(P.parse_stage_lines(idle), now=when(0, 30))
    check(state == "idle" and "no new recordings" in detail["msg"],
          "idle comes from the 'no new recordings.' stage line")

    syncing = [stage("still syncing (90s until stable): new.m4a", 5, 0),
               stage("  waiting 92s ...", 5, 2)]
    state, detail = P.classify(P.parse_stage_lines(syncing), now=when(6, 32))
    check(state == "waiting" and detail["age"] == 90,
          "waiting comes from still-syncing/waiting lines, with the age since the line")

    fresh = [stage("1 new recording(s): new.m4a", 7, 0)]
    state, _ = P.classify(P.parse_stage_lines(fresh), now=when(7, 30))
    check(state == "waiting", "'N new recording(s)' counts as waiting")

    ingesting = [stage("1 new recording(s): new.m4a", 7, 0),
                 stage("  > whosaid ingest new.m4a", 7, 1)]
    state, detail = P.classify(P.parse_stage_lines(ingesting), now=when(10, 1))
    check(state == "ingesting" and detail["msg"] == "new.m4a" and detail["age"] == 180,
          "ingesting comes from '> ingest', naming the file, age 3m")

    state, detail = P.classify(P.parse_stage_lines(ingesting), watcher_running=False, now=when(10, 1))
    check(state == "warn" and "no watcher process" in detail["msg"],
          "an ingest line with no watcher process behind it is a warn")

    state, _ = P.classify(P.parse_stage_lines(ingesting), watcher_running=None, now=when(10, 1))
    check(state == "ingesting", "watcher_running unknown leaves ingesting alone")

    done = ingesting + [stage("  ok ingested new.m4a", 12, 0),
                        stage("  > whosaid roll-up /ws", 12, 1),
                        stage("done: 1/1 ingested.", 13, 0)]
    state, detail = P.classify(P.parse_stage_lines(done), now=when(18, 0))
    check(state == "done" and "1/1" in detail["msg"],
          "done comes from 'ok ingested'/'done:' lines (roll-up lines ignored)")

    state, detail = P.classify(P.parse_stage_lines(done), now=when(44, 0))
    check(state == "idle" and "done" in detail["msg"],
          "done is held 30 minutes, then expires to idle")

    state, _ = P.classify(P.parse_stage_lines(done), now=when(42, 30))
    check(state == "done", "done still held at 29.5 minutes")

    for warn_line in ("  ! whosaid exited 1 on bad.m4a; will retry next trigger.",
                      "! cannot read the source folder: [Errno 1] Operation not permitted"):
        state, _ = P.classify(P.parse_stage_lines([stage(warn_line, 9, 0)]), now=when(9, 30))
        check(state == "warn", f"a '!' stage line is a warn ({warn_line[:24]!r}...)")

    state, _ = P.classify([], now=when(9, 0))
    check(state == "idle", "no stage lines at all degrades to idle")

    # subprocess noise lands in the same log but must NEVER match a state rule
    noise = idle + [
        "chunk 8/8 done: mel-spectrograms",                          # the classic diarizer trap
        "2026-09-18T09:05:00+03:00 chunk 8/8 done: mel",             # timestamped, no whosaid marker
        "[00:16:32] Alice_Example: ok, done: shipping it",           # transcript dump
        "2026-09-18T09:05:01+03:00 whosaid done:",                   # missing colon after whosaid
        "progress 47% eta 00:12:34",
    ]
    check(len(P.parse_stage_lines(noise)) == 1, "parse keeps only true 'whosaid:' stage lines")
    state, _ = P.classify(P.parse_stage_lines(noise), now=when(0, 30))
    check(state == "idle", "diarizer/transcript noise never flips the state")

    noisy_ingest = [stage("  > whosaid ingest new.m4a", 7, 0),
                    "chunk 8/8 done: mel",
                    "2026-09-18T09:07:30+03:00 chunk 8/8 done: mel"]
    state, _ = P.classify(P.parse_stage_lines(noisy_ingest), now=when(8, 0))
    check(state == "ingesting", "noise mid-ingest does not end the ingest")


def test_watcher_liveness(P) -> None:
    live = {"loaded": True, "state": "active", "pid": os.getpid(), "pid_alive": True}
    check(P.watcher_running(live) is True, "active + live PID is a live launchd watcher")
    live["state"] = "running"
    check(P.watcher_running(live) is True, "running + live PID is a live launchd watcher")
    live["state"] = "not running"
    check(P.watcher_running(live) is False, "a multiword stopped state is not live")
    live["state"] = "active"
    live["pid_alive"] = False
    check(P.watcher_running(live) is False, "active without a live PID is not live")
    del live["pid_alive"]
    live["pid"] = None
    check(P.watcher_running(live) is False, "active without a PID is not live")
    check(P.watcher_running(None) is None, "unavailable status remains unknown")


def test_render_controls(P, tmp: Path) -> None:
    """Exercise the plugin entry point with one fresh log and paired statuses."""
    source = tmp / "plain-recordings"
    source.mkdir()
    ws = tmp / "render-workspace"
    ws.mkdir()
    log = ws / ".watch.log"
    log.write_text(dt.datetime.now().astimezone().isoformat(timespec="seconds")
                   + " whosaid:   > whosaid ingest fixture.m4a\n")
    fake_whosaid = tmp / "fake-whosaid"
    fake_whosaid.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if sys.argv[1:3] == ['watch', 'status']:\n"
        "    print(os.environ['FAKE_STATUS'])\n"
        "elif sys.argv[1:3] == ['memos', 'list']:\n"
        "    print('{\\\"recordings\\\": []}')\n"
        "else:\n"
        "    raise SystemExit(64)\n")
    fake_whosaid.chmod(0o755)
    common = {"loaded": True, "pid": os.getpid(), "workspace": str(ws), "source": str(source),
              "source_readable": True, "processed": 0, "seeded": 0, "log": str(log),
              "last_exit_status": 0}

    def render(status: dict) -> str:
        env = dict(os.environ, HOME=str(tmp / "render-home"), WHOSAID_WORKSPACE=str(ws),
                   WHOSAID_BIN=str(fake_whosaid), FAKE_STATUS=json.dumps(status))
        result = subprocess.run([sys.executable, str(PLUGIN)], capture_output=True, text=True, env=env)
        check(result.returncode == 0, "plugin entry point renders its controlled status")
        return result.stdout

    active = dict(common, state="active", pid_alive=True)
    active_output = render(active)
    check("no watcher process is running" not in active_output,
          "active live watcher does not turn a fresh ingest log into a warning")
    check("cannot read the Voice Memos store" not in active_output,
          "a readable plain source does not warn about unrelated Voice Memos access")
    check("configured recording source" in active_output,
          "plugin names successful plain-source access")
    stopped = dict(common, state="not running", pid_alive=False)
    stopped_output = render(stopped)
    check("no watcher process is running" in stopped_output,
          "a stopped multiword state warns when the same log says ingesting")
    missing_pid = dict(common, state="running", pid=None, pid_alive=False)
    check("no watcher process is running" in render(missing_pid),
          "a running state without a live PID warns when the same log says ingesting")
    voice_source = tmp / "render-home/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
    voice_output = render(dict(common, source=str(voice_source), source_readable=False,
                               state="active", pid_alive=True))
    check("cannot read the Voice Memos store" in voice_output,
          "the Voice Memos source retains its FDA-specific diagnostic")


def test_tail(P, tmp: Path) -> None:
    big = tmp / ".watch.log"
    noise = "SPEAKER_00 0.82 | card " + "x" * 40 + "\n"
    big.write_text(stage("no new recordings.", 0, 0) + "\n"
                   + noise * 55_000
                   + stage("done: 1/1 ingested.", 30, 0) + "\n")
    check(big.stat().st_size > 2_000_000, "fixture log exceeds 2 MB")
    lines = P.tail_lines(big)
    check(len(lines) < 55_002, "the tail is a byte window, so the head is dropped")
    state, detail = P.classify(P.parse_stage_lines(lines), now=when(31, 0))
    check(state == "done" and "1/1" in detail["msg"],
          "a >2MB log tail still finds the newest stage lines")
    small = tmp / "small.log"
    small.write_text("a\nb\nc\n")
    check(P.tail_lines(small) == ["a", "b", "c"], "a small log is tailed whole")
    check(P.tail_lines(tmp / "absent.log") == [], "an absent log degrades to no lines")
    check(P.tail_lines(None) == [], "no log path degrades to no lines")


def test_glyph(P) -> None:
    check(P.pick_glyph("warn", {"msg": "x", "age": 1}, True, None)[0] == "⚠️", "warn glyph")
    check(P.pick_glyph("idle", {"msg": "", "age": None}, False, None)[0] == "🎙✗",
          "agent-not-loaded glyph wins over plain idle")
    check(P.pick_glyph("idle", {"msg": "", "age": None}, False, "New Memo")[0] == "🎙✗",
          "agent-not-loaded glyph wins over REC")
    check(P.pick_glyph("idle", {"msg": "", "age": None}, True, "New Memo") == ("🔴 REC", "recording: New Memo"),
          "REC glyph from a store row")
    check(P.pick_glyph("ingesting", {"msg": "new.m4a", "age": 180}, True, None)[0] == "⚙️ 3m",
          "ingesting glyph carries elapsed minutes")
    check(P.pick_glyph("waiting", {"msg": "sync", "age": 90}, True, None)[0] == "⏳ 90s",
          "waiting glyph carries elapsed seconds")
    check(P.pick_glyph("done", {"msg": "d", "age": 60}, True, None)[0] == "✅", "done glyph")
    check(P.pick_glyph("idle", {"msg": "", "age": None}, True, None)[0] == "🎙", "idle glyph")
    check(P.pick_glyph("idle", {"msg": "", "age": None}, None, None)[0] == "🎙",
          "unknown loaded state keeps the idle glyph")
    check(P.pick_glyph("ingesting", {"msg": "x", "age": None}, True, None)[0] == "⚙️",
          "ingesting without an age still renders")


def test_rec_rows(P) -> None:
    rows = [
        {"title": "Old", "file": "a.m4a", "seconds": 61, "recently_deleted": False},
        {"title": "New Memo", "file": "", "seconds": 0, "recently_deleted": False},
    ]
    check(P.active_recording(rows) == "New Memo", "a row with no file and 0s is a live REC")
    check(P.active_recording(rows[:1]) is None, "synced rows are not REC")
    check(P.active_recording([{"title": "x", "file": "", "seconds": 0, "recently_deleted": True}]) is None,
          "recently-deleted rows are not REC")
    check(P.active_recording([{"title": "plain", "file": "p.m4a", "bytes": 1}]) is None,
          "plain-folder rows are never REC")


def test_latest_meeting(P, tmp: Path) -> None:
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "2026-09-17-0900").mkdir()
    new = ws / "2026-09-18-0930"
    new.mkdir()
    custom = ws / "z-custom-name"
    custom.mkdir()
    (ws / "_WIKI.md").write_text("wiki\n")
    os.utime(new, (1_000_000, 1_000_000))
    os.utime(custom, (time.time(), time.time()))
    got = P.latest_meeting_folder(ws)
    check(got is not None and got.name == "2026-09-18-0930",
          "latest meeting = greatest dated folder NAME, not newest mtime")
    check(P.latest_meeting_folder(tmp / "nope") is None, "an absent workspace degrades to None")
    empty = tmp / "empty"
    empty.mkdir()
    check(P.latest_meeting_folder(empty) is None, "no dated folders degrades to None")


def test_action_tokens(P) -> None:
    token = P.encode_action("open", "/tmp/a path/with |pipe.txt")
    check(" " not in token and "|" not in token, "action tokens are space- and pipe-free")
    check(P.decode_action(token) == ("open", "/tmp/a path/with |pipe.txt"),
          "action tokens round-trip paths with spaces and pipes")


def test_notify_once(P, tmp: Path) -> None:
    state_file = tmp / "menubar-state.json"
    fired: list[tuple[str, str]] = []
    runner = lambda title, msg: fired.append((title, msg))  # noqa: E731
    check(P.notify_transition(state_file, "ingesting", "t1", runner=runner) is True,
          "a transition into ingesting notifies once")
    check(P.notify_transition(state_file, "ingesting", "t2", runner=runner) is False,
          "the same state again does not re-notify")
    check(P.notify_transition(state_file, "done", "t3", runner=runner) is True,
          "a transition into done notifies")
    check(P.notify_transition(state_file, "idle", "-", runner=runner) is False,
          "idle never notifies (but is recorded)")
    check(json.loads(state_file.read_text())["last"] == "idle", "the state file tracks the last state")
    check(P.notify_transition(state_file, "warn", "t4", runner=runner) is True,
          "re-entry after idle notifies again")
    check(len(fired) == 3 and fired[0][0] == "whosaid", "exactly one notification per transition, titled whosaid")
    broken = tmp / "broken.json"
    broken.write_text("{nope")
    check(P.notify_transition(broken, "done", "t5", runner=runner) is True,
          "a corrupt state file resets and notifies")
    check(P.notify_transition(broken, "waiting", "-", runner=runner) is False,
          "waiting is a silent state")


# ---- lib/watch.py menubar subcommands (subprocess, hermetic) -------------------------


def run_watch(args: list[str], env_extra: dict | None = None, cwd: Path | None = None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("WHOSAID_WORKSPACE", "WHOSAID_SWIFTBAR_DIR", "WHOSAID_SWIFTBAR_APP",
                        "FAKE_DEFAULTS_DIR", "FAKE_DEFAULTS_FILE", "FAKE_DEFAULTS_WRITE_FAIL",
                        "FAKE_DEFAULTS_WRITE_NO_EFFECT",
                        "WHOSAID_BIN")}
    env["PATH"] = f"{FAKE_BIN}:{env.get('PATH', '')}"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(WATCH_PY), *args],
                          capture_output=True, text=True, env=env, cwd=str(cwd or TMP))


def test_cli() -> None:
    plugins = TMP / "plugins"
    r = run_watch(["menubar", "install", "--workspace", str(TMP)],
                  {"WHOSAID_SWIFTBAR_DIR": str(plugins)})
    check(r.returncode == 0, "menubar install exits 0 with the dir override")
    link = plugins / "whosaid.10s.py"
    check(link.is_symlink() and link.resolve() == PLUGIN.resolve(),
          "install symlinks whosaid.<interval>.py at the repo plugin source")
    check("launchctl setenv WHOSAID_WORKSPACE" in r.stdout, "install prints the launchctl setenv hint")
    r2 = run_watch(["menubar", "install"], {"WHOSAID_SWIFTBAR_DIR": str(plugins)})
    check(r2.returncode == 0 and "already installed" in r2.stderr, "a second install is an idempotent no-op")

    r = run_watch(["menubar", "install", "--interval", "5s"], {"WHOSAID_SWIFTBAR_DIR": str(plugins)})
    check((plugins / "whosaid.5s.py").is_symlink(), "a non-default interval names the symlink")
    r = run_watch(["menubar", "install", "--interval", "fast"], {"WHOSAID_SWIFTBAR_DIR": str(plugins)})
    check(r.returncode == 2, "a malformed interval is a usage error (exit 2)")

    stale = plugins / "whosaid.1m.py"
    stale.symlink_to("/nowhere/at-all.py")
    r = run_watch(["menubar", "install", "--interval", "1m"], {"WHOSAID_SWIFTBAR_DIR": str(plugins)})
    check(r.returncode == 0 and stale.resolve() == PLUGIN.resolve(), "a stale link is re-pointed")
    occupied = plugins / "whosaid.2m.py"
    occupied.write_text("mine")
    r = run_watch(["menubar", "install", "--interval", "2m"], {"WHOSAID_SWIFTBAR_DIR": str(plugins)})
    check(r.returncode == 1 and occupied.read_text() == "mine",
          "a real file at the target is never clobbered")

    r = run_watch(["menubar", "install", "--workspace", str(TMP / "nope")],
                  {"WHOSAID_SWIFTBAR_DIR": str(plugins)})
    check(r.returncode == 1 and "not found" in r.stderr, "a missing --workspace is refused")

    r = run_watch(["menubar", "install"], {"WHOSAID_SWIFTBAR_APP": str(TMP / "absent.app")})
    check(r.returncode != 0 and "swiftbar.com" in (r.stdout + r.stderr),
          "SwiftBar absent -> a clear install-then-retry message, non-zero exit")

    home2 = TMP / "home2"
    app2 = TMP / "SwiftBar.app"
    app2.mkdir()
    preference = TMP / "swiftbar-plugin-directory"
    r = run_watch(["menubar", "install"], {"HOME": str(home2), "WHOSAID_SWIFTBAR_APP": str(app2),
                                          "FAKE_DEFAULTS_FILE": str(preference)})
    fallback = home2 / ".config/swiftbar/whosaid.10s.py"
    check(r.returncode == 0 and fallback.is_symlink(),
          "app present + PluginDirectory unset configures ~/.config/swiftbar (HOME redirected)")
    check(preference.read_text().strip() == str(home2 / ".config/swiftbar"),
          "first install writes SwiftBar's PluginDirectory preference")
    check("refresh or relaunch SwiftBar" in r.stdout,
          "configuration gives an actionable refresh step without claiming live discovery")
    r = run_watch(["menubar", "install"], {"HOME": str(home2), "WHOSAID_SWIFTBAR_APP": str(app2),
                                          "FAKE_DEFAULTS_FILE": str(preference)})
    check(r.returncode == 0 and preference.read_text().strip() == str(home2 / ".config/swiftbar"),
          "second install preserves the configured preference")

    failed_home = TMP / "failed-home"
    r = run_watch(["menubar", "install"], {"HOME": str(failed_home), "WHOSAID_SWIFTBAR_APP": str(app2),
                                          "FAKE_DEFAULTS_FILE": str(TMP / "failed-preference"),
                                          "FAKE_DEFAULTS_WRITE_FAIL": "1"})
    check(r.returncode == 1 and "could not be configured" in r.stderr,
          "an unset directory that cannot be configured is an actionable incomplete install")
    check(not (failed_home / ".config/swiftbar/whosaid.10s.py").exists(),
          "failed configuration does not claim discovery by creating a plugin link")

    no_effect_home = TMP / "no-effect-home"
    r = run_watch(["menubar", "install"], {"HOME": str(no_effect_home), "WHOSAID_SWIFTBAR_APP": str(app2),
                                          "FAKE_DEFAULTS_FILE": str(TMP / "no-effect-preference"),
                                          "FAKE_DEFAULTS_WRITE_NO_EFFECT": "1"})
    check(r.returncode == 1 and "could not be configured" in r.stderr,
          "a successful defaults write without a readable preference is incomplete")
    check(not (no_effect_home / ".config/swiftbar/whosaid.10s.py").exists(),
          "unverified preference writes do not leave a misleading plugin link")

    defaults_dir = TMP / "plug-defaults"
    r = run_watch(["menubar", "install"], {"FAKE_DEFAULTS_DIR": str(defaults_dir)})
    check((defaults_dir / "whosaid.10s.py").is_symlink() and "PluginDirectory" in r.stdout,
          "`defaults read com.ameba.SwiftBar PluginDirectory` is honoured when set")

    r = run_watch(["menubar", "status"],
                  {"WHOSAID_SWIFTBAR_DIR": str(plugins), "WHOSAID_SWIFTBAR_APP": str(app2)})
    check(r.returncode == 0, "menubar status exits 0")
    check("installed" in r.stdout and "SwiftBar running:" in r.stdout,
          "status reports the app and whether SwiftBar is running")
    check("whosaid.10s.py" in r.stdout and "whosaid.5s.py" in r.stdout and "whosaid.1m.py" in r.stdout,
          "status lists every linked plugin (not the unrelated file)")
    check("WHOSAID_WORKSPACE" in r.stdout, "status mentions the workspace env the plugin reads")
    check("source readable:" in r.stdout, "status reports the configured-source probe")

    plain_workspace = TMP / "plain-workspace"
    plain_source = TMP / "status-plain-source"
    plain_workspace.mkdir()
    plain_source.mkdir()
    (plain_workspace / "whosaid.toml").write_text(f"[watch]\nsource = {json.dumps(str(plain_source))}\n")
    r = run_watch(["menubar", "status"], {"WHOSAID_WORKSPACE": str(plain_workspace),
                                           "WHOSAID_SWIFTBAR_DIR": str(plugins),
                                           "WHOSAID_SWIFTBAR_APP": str(app2)})
    check(str(plain_source) in r.stdout and "Full Disk Access" not in r.stdout,
          "menubar status bases permission guidance on the configured plain source")

    pinned_home = TMP / "pinned-home"
    pinned_agents = pinned_home / "Library/LaunchAgents"
    pinned_agents.mkdir(parents=True)
    pinned_workspace = TMP / "pinned-workspace"
    pinned_source = TMP / "pinned-no-fda-source"
    pinned_workspace.mkdir()
    pinned_source.mkdir()
    pinned_label = "com.whosaid.watch." + hashlib.sha256(str(pinned_workspace).encode()).hexdigest()[:8]
    with open(pinned_agents / f"{pinned_label}.plist", "wb") as fh:
        plistlib.dump({"Label": pinned_label, "WatchPaths": [str(pinned_source)],
                       "ProgramArguments": [sys.executable, str(WATCH_PY), "run", "--into", str(pinned_workspace),
                                            "--source", str(pinned_source)]}, fh)
    r = run_watch(["menubar", "status"], {"HOME": str(pinned_home), "WHOSAID_WORKSPACE": str(pinned_workspace),
                                           "WHOSAID_SWIFTBAR_DIR": str(plugins),
                                           "WHOSAID_SWIFTBAR_APP": str(app2)})
    check(str(pinned_source) in r.stdout and "Full Disk Access" not in r.stdout,
          "menubar status honors a plain source pinned in the installed watcher plist")

    fake_launchctl = FAKE_BIN / "launchctl"
    fake_launchctl.write_text(
        "#!/bin/bash\n"
        "echo \"state = $FAKE_LAUNCHD_STATE\"\n"
        "echo \"pid = $FAKE_LAUNCHD_PID\"\n")
    fake_launchctl.chmod(0o755)
    status_env = {"FAKE_LAUNCHD_STATE": "active", "FAKE_LAUNCHD_PID": str(os.getpid())}
    r = run_watch(["status", "--json", "--label", "com.whosaid.test-status"], status_env)
    status = json.loads(r.stdout)
    check(status["state"] == "active" and status["pid_alive"] is True,
          "actual status CLI reports an active state with a live PID")
    status_env["FAKE_LAUNCHD_STATE"] = "not running"
    r = run_watch(["status", "--json", "--label", "com.whosaid.test-status"], status_env)
    status = json.loads(r.stdout)
    check(status["state"] == "not running", "actual status CLI preserves multiword launchd state")

    occupied.unlink()
    r = run_watch(["menubar", "uninstall"], {"WHOSAID_SWIFTBAR_DIR": str(plugins)})
    check(r.returncode == 0, "menubar uninstall exits 0")
    check(not any(plugins.glob("whosaid.*.py")), "uninstall removed every whosaid plugin link")
    r = run_watch(["menubar", "uninstall"], {"WHOSAID_SWIFTBAR_DIR": str(plugins)})
    check(r.returncode == 0 and "no whosaid" in r.stderr, "uninstall with nothing left is a clean no-op")

    r = run_watch(["menubar"])
    check(r.returncode != 0, "menubar without a subcommand is a usage error")

    src = WATCH_PY.read_text()
    check("whosaid watch menubar install" in src, "watch install suggests the menu bar plugin")


def main() -> None:
    global TMP, FAKE_BIN
    if not PLUGIN.is_file():
        raise SystemExit(f"FAIL: plugin missing: {PLUGIN}")
    with tempfile.TemporaryDirectory(prefix="whosaid-menubar-test.") as d:
        TMP = Path(d)
        FAKE_BIN = TMP / "bin"
        FAKE_BIN.mkdir()
        fake_defaults = FAKE_BIN / "defaults"
        fake_defaults.write_text(
            "#!/bin/bash\n"
            "# fake defaults: read an explicit test preference or a directory fixture.\n"
            "if [ \"$1\" = read ]; then\n"
            "  if [ -n \"${FAKE_DEFAULTS_FILE:-}\" ] && [ -f \"$FAKE_DEFAULTS_FILE\" ]; then cat \"$FAKE_DEFAULTS_FILE\"; exit 0; fi\n"
            "  if [ -n \"${FAKE_DEFAULTS_DIR:-}\" ]; then echo \"$FAKE_DEFAULTS_DIR\"; exit 0; fi\n"
            "  exit 1\n"
            "fi\n"
            "if [ \"$1\" = write ]; then\n"
            "  [ \"${FAKE_DEFAULTS_WRITE_FAIL:-}\" = 1 ] && exit 1\n"
            "  [ -n \"${FAKE_DEFAULTS_FILE:-}\" ] || exit 1\n"
            "  [ \"${FAKE_DEFAULTS_WRITE_NO_EFFECT:-}\" = 1 ] && exit 0\n"
            "  printf '%s\\n' \"${@: -1}\" > \"$FAKE_DEFAULTS_FILE\"; exit 0\n"
            "fi\n"
            "exit 64\n")
        fake_defaults.chmod(0o755)
        plugin = load_plugin()
        test_classifier(plugin)
        test_watcher_liveness(plugin)
        test_render_controls(plugin, TMP)
        test_tail(plugin, TMP)
        test_glyph(plugin)
        test_rec_rows(plugin)
        test_latest_meeting(plugin, TMP)
        test_action_tokens(plugin)
        test_notify_once(plugin, TMP)
        test_cli()
    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
