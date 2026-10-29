#!/usr/bin/env python3
"""
autorecord.py: record every call unattended (`whosaid autorecord`, GitHub issue #122).

  run        Loop: wait for a call, record it into --into, repeat. Built on
             `whosaid record --app A --wait-for-call --until-call-ends --into DIR`
             (#119) or `whosaid record --browser --wait-for-call --into DIR` (#121).
             One copy per folder (<into>/.autorecord.lock); a second exits 0.
             It never gives up: "app not running" and "browser not reachable"
             are waiting states, other failures back off (30 s doubling to 5 min).
  install    Start `run` at every login.
               --app A     needs Screen & System Audio Recording + Microphone. macOS
                           grants those to an app, so this builds a small generic
                           launcher, "Whosaid Call Recorder.app" in ~/Applications,
                           and a LaunchAgent that opens it with the run command as
                           launch arguments. The launcher is rebuilt only when its
                           source changes, so reconfiguring keeps the grant.
               --browser   needs no grant (the page records itself over CDP): a plain
                           LaunchAgent runs `whosaid autorecord run` directly. The
                           browser must be started with --remote-debugging-port.
  uninstall  Stop it (a call in progress is saved) and remove the LaunchAgent.
             --purge also removes the launcher app (and with it the grant).
  status     Installed mode, running or not, recent log lines. --json: one object
             (installed, loaded, running, state, since, paused_until, last_saved,
             last_error, controls) for the macOS app and scripts.
  pause      --minutes N (default 60): record no new call until then. A call
             being recorded keeps going; a wait for the next call ends at once.
  resume     Undo pause.
  stop-now   End the call being recorded now and save it; the loop keeps
             running and waits for the next call.
  setup      A new Mac in one command (#127): `whosaid setup --yes` (deps, models),
             `install`, then `whosaid watch install --into <workspace> --source <into>`
             so each saved call is transcribed. --owner starts a whosaid.toml naming
             the workspace owner; --skip-deps; --dry-run changes nothing.

The `whosaid watch` LaunchAgent pointed at the same folder ingests each file.
The run loop keeps <into>/.autorecord/state.json current (waiting | recording |
saving | paused | error | stopped) and polls two control files there: pause and
stop-now. An older loop without them is read from the log instead
(`controls: false`); pause and stop-now then refuse with a reinstall hint.

Stdlib only.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

LABEL = "com.whosaid.autorecord"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
APP_NAME = "Whosaid Call Recorder"
APP_BUNDLE_ID = "com.whosaid.call-recorder"
LAUNCHER_SRC = Path(__file__).resolve().parent / "autorecord_launcher.swift"
LOCK_NAME = ".autorecord.lock"
LOG_NAME = ".autorecord.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
# State and control files live one folder down: the watcher's WatchPaths fires on
# every entry change in <into>, and these are rewritten several times per call.
CONTROL_DIR = ".autorecord"
STATE_NAME = f"{CONTROL_DIR}/state.json"
PAUSE_NAME = f"{CONTROL_DIR}/pause"
STOP_NOW_NAME = f"{CONTROL_DIR}/stop-now"
STALE_STATE_SECONDS = 600  # an older state file is cross-checked against the lock (pid reuse)
CONTROL_POLL = 0.5
# A record child's stderr line that means the capture (not the wait) started.
CAPTURE_STARTED = re.compile(r"opened the mic; starting the capture|\brecording( \(|$)| peak ")
# ...and the line that means it is over and the file is being written (no more SIGINT).
CAPTURE_ENDED = re.compile(r"capture ended; saving the recording|keeping the audio recorded so far")
DEFAULT_INTO = Path.home() / "Recordings"
SCREEN_PANE_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
AGENT_PATH = ":".join([
    str(Path.home() / ".local/bin"), str(Path.home() / "bin"),
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
])

# `whosaid record` exit codes and what the loop does with them.
WAIT_RETRY = {2: 30.0, 6: 10.0}  # app not running / bad flag; CDP target race
EXIT_PERMISSION = 5               # Screen & System Audio Recording missing or revoked
BACKOFF_START, BACKOFF_MAX = 30.0, 300.0


def log(msg: str) -> None:
    print(f"whosaid: {msg}", file=sys.stderr, flush=True)


def app_dir() -> Path:
    return Path(os.environ.get("WHOSAID_AUTORECORD_APP_DIR") or Path.home() / "Applications") / f"{APP_NAME}.app"


def plist_path() -> Path:
    return LAUNCH_AGENTS / f"{LABEL}.plist"


def default_whosaid() -> str:
    return os.environ.get("WHOSAID_BIN") or str(Path(__file__).resolve().parent.parent / "whosaid")


# ---------------------------------------------------------------- run

def record_argv(a: argparse.Namespace) -> list[str]:
    """The `whosaid record` command one loop iteration runs."""
    argv = [a.whosaid, "record"]
    if a.app:
        argv += ["--app", a.app, "--wait-for-call", "--until-call-ends",
                 "--idle-seconds", str(a.idle_seconds)]
    else:
        argv += ["--browser", "--wait-for-call", "--cdp-port", str(a.cdp_port)]
        if a.target:
            argv += ["--target", a.target]
    if a.no_mic:
        argv.append("--no-mic")
    argv += ["--into", str(a.into)]
    if a.label:
        argv += ["--label", a.label]
    return argv


def classify(rc: int, saved: str | None) -> tuple[str, float | None]:
    """(kind, fixed delay) for one `whosaid record` exit.

    kind: saved | idle | waiting | permission | failed. A fixed delay of None
    means "use the backoff"; 0 means go again at once.
    """
    if rc == 0:
        return ("saved", 0.0) if saved else ("idle", 0.0)
    if rc == 3 and saved:
        return "saved", 0.0  # capture ended early; the partial file was kept
    if rc in WAIT_RETRY:
        return "waiting", WAIT_RETRY[rc]
    if rc == EXIT_PERMISSION:
        return "permission", None
    return "failed", None


def iso(ts: float | None = None) -> str:
    """RFC 3339 local time with a colon offset (2026-10-29T15:00:00-07:00)."""
    z = time.strftime("%z", time.localtime(time.time() if ts is None else ts))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() if ts is None else ts)) + z[:3] + ":" + z[3:]


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def paused_until(into: Path, now: float | None = None) -> float | None:
    """The pause end (epoch seconds) while a pause is in force, else None."""
    data = read_json(into / PAUSE_NAME)
    until = data.get("until") if data else None
    if isinstance(until, (int, float)) and until > (time.time() if now is None else now):
        return float(until)
    return None


class State:
    """<into>/.autorecord/state.json, rewritten atomically on every change."""

    def __init__(self, into: Path, a: argparse.Namespace):
        self.path = into / STATE_NAME
        self.lock = threading.Lock()
        self.data = {"version": 1, "pid": os.getpid(), "controls": True,
                     "mode": "app" if a.app else "browser", "app": a.app, "label": a.label,
                     "into": str(into), "state": "waiting", "since": iso(), "child_pid": None,
                     "paused_until": None, "last_saved": None, "last_error": None}

    def set(self, **kw) -> None:
        with self.lock:
            if "state" in kw and kw["state"] != self.data["state"]:
                kw.setdefault("since", iso())
            self.data.update(kw)
            self.data["updated"] = iso()
            write_json_atomic(self.path, self.data)


class Tee:
    """Timestamped lines to <into>/.autorecord.log and stderr."""

    def __init__(self, path: Path):
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        self.fh = open(path, "a", buffering=1)
        self.lock = threading.Lock()

    def line(self, text: str) -> None:
        stamped = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text.rstrip()}"
        with self.lock:
            self.fh.write(stamped + "\n")
            print(stamped, file=sys.stderr, flush=True)


def cmd_run(a: argparse.Namespace) -> int:
    into = Path(a.into).expanduser()
    into.mkdir(parents=True, exist_ok=True)
    a.into = into
    lock = open(into / LOCK_NAME, "w")
    for attempt in range(4):  # a `status` probe holds the lock for an instant; never lose to it
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if attempt == 3:
                held = read_json(into / STATE_NAME) or {}
                if held.get("pid") != os.getpid() and held.get("state") != "stopped" and pid_alive(held.get("pid")):
                    log(f"autorecord: already running for {into} (lock {into / LOCK_NAME}); exiting.")
                    return 0
                # No live owner on record: exit non-zero so launchd retries after ThrottleInterval.
                log(f"autorecord: could not take the lock {into / LOCK_NAME}; exiting to be retried.")
                return 1
            time.sleep(0.25)
    tee = Tee(into / LOG_NAME)
    argv = record_argv(a)
    tee.line(f"autorecord: {'app ' + a.app if a.app else 'browser on CDP port ' + str(a.cdp_port)}"
             f" -> {into}; Ctrl-C to quit")

    # phase: waiting (for a call) -> recording -> saving (muxing; never signalled)
    state = {"stop": False, "child": None, "phase": "waiting", "signalled": False}
    st = State(into, a)
    st.set(state="waiting")
    # Reentrant: the signal handler runs on the main thread, which may hold it.
    sig_lock = threading.RLock()

    def interrupt_child(phase: str | None = None) -> bool:
        """SIGINT the current child's group, at most once per child: a second
        SIGINT makes the capture helper exit without saving. phase= signals
        only in that phase, checked under the lock the pump moves it with."""
        with sig_lock:
            child = state["child"]
            if not child or child.poll() is not None or state["signalled"]:
                return False
            if phase is not None and state["phase"] != phase:
                return False
            state["signalled"] = True
            try:  # the whole group: bash traps INT, the capture helper finishes and saves
                os.killpg(child.pid, signal.SIGINT)
                return True
            except OSError:
                return False

    def on_signal(_signum, _frame):
        state["stop"] = True
        interrupt_child()

    def controls() -> None:
        """Poll the pause and stop-now files while the loop runs."""
        while not state["stop"]:
            stop_req = into / STOP_NOW_NAME
            if stop_req.exists():
                try:
                    stop_req.unlink()
                except OSError:
                    pass
                if interrupt_child(phase="recording"):
                    tee.line("autorecord: stop-now: ending the capture and saving it")
            until = paused_until(into)
            if until and interrupt_child(phase="waiting"):
                tee.line(f"autorecord: paused until {iso(until)}; stopped waiting for a call")
            time.sleep(CONTROL_POLL)

    threading.Thread(target=controls, daemon=True).start()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    backoff = BACKOFF_START
    permission_logged = False
    max_loops = int(os.environ.get("WHOSAID_AUTORECORD_MAX_LOOPS") or 0)  # test hook
    loops = 0
    while not state["stop"]:
        until = paused_until(into)
        if until:
            st.set(state="paused", paused_until=iso(until), child_pid=None)
            tee.line(f"autorecord: paused until {iso(until)}")
            while not state["stop"] and paused_until(into):
                time.sleep(0.2)
            if state["stop"]:
                break
            tee.line("autorecord: resumed")
        loops += 1
        # New session: a terminal Ctrl-C reaches only this loop, which forwards
        # one SIGINT; a second one would make the capture helper exit unsaved.
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        with sig_lock:
            state.update(child=child, phase="waiting", signalled=False)
        if state["stop"]:  # a stop that landed before the child was known: it would wait on alone
            interrupt_child()
        st.set(state="waiting", child_pid=child.pid, paused_until=None)

        def pump(stream=child.stderr, mine=child):
            for line in stream:
                text = line.rstrip()
                with sig_lock:  # a late line from a finished child changes nothing
                    if state["child"] is mine and state["phase"] != "saving":
                        if CAPTURE_ENDED.search(text):
                            state["phase"] = "saving"
                            st.set(state="saving")
                        elif state["phase"] == "waiting" and CAPTURE_STARTED.search(text):
                            state["phase"] = "recording"
                            st.set(state="recording")
                if " peak " not in line:  # the 10 s progress ticks stay out of the log
                    tee.line(line)

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        out = child.stdout.read()
        rc = child.wait()
        t.join(timeout=5)
        saved = next((ln for ln in reversed(out.splitlines()) if ln.strip().endswith(".m4a")), None)
        kind, delay = classify(rc, saved)
        if kind == "failed" and state["signalled"]:
            kind, delay = "idle", 0.0  # we stopped it (pause, stop-now) before it could clean up
        if kind == "saved":
            tee.line(f"autorecord: saved {saved}")
            backoff, permission_logged = BACKOFF_START, False
            st.set(state="waiting", child_pid=None, last_saved={"path": saved, "at": iso()})
        elif kind in ("permission", "failed"):
            st.set(state="error", child_pid=None, last_error={"rc": rc, "kind": kind, "at": iso()})
        else:
            st.set(state="waiting", child_pid=None)
        with sig_lock:
            state.update(child=None, phase="waiting")
        if kind == "saved":
            pass
        elif kind == "permission":
            if not permission_logged:
                tee.line("autorecord: no Screen & System Audio Recording permission. Turn on "
                         f"'{APP_NAME}' (or your terminal, when run by hand) in System Settings > "
                         "Privacy & Security > Screen & System Audio Recording. No admin rights? "
                         "Use `whosaid autorecord install --browser` with Teams on the web.")
                permission_logged = True
            delay = None
        elif kind == "waiting":
            tee.line(f"autorecord: record exited {rc} (app not running or no meeting tab); retrying in {delay:.0f}s")
        elif kind == "failed":
            tee.line(f"autorecord: record exited {rc}; retrying in {backoff:.0f}s")
        if max_loops and loops >= max_loops:
            break
        if state["stop"]:
            break
        if delay is None:
            delay, backoff = backoff, min(backoff * 2, BACKOFF_MAX)
        end = time.monotonic() + delay * float(os.environ.get("WHOSAID_AUTORECORD_DELAY_SCALE") or 1)
        while not state["stop"] and time.monotonic() < end:
            time.sleep(0.2)
    st.set(state="stopped", child_pid=None)
    tee.line("autorecord: stopped")
    return 0


# ---------------------------------------------------------------- install

def launcher_hash() -> str:
    return hashlib.sha256(LAUNCHER_SRC.read_bytes()).hexdigest()[:16]


def info_plist() -> dict:
    return {
        "CFBundleIdentifier": APP_BUNDLE_ID,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": "whosaid-call-recorder",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1",
        "CFBundleVersion": "1",
        "LSUIElement": True,  # no Dock icon, no menu bar
        "LSMinimumSystemVersion": "13.0",
        "NSMicrophoneUsageDescription": "Records your side of calls for whosaid transcripts.",
    }


def ensure_launcher_app(dry: bool) -> tuple[Path, bool]:
    """Build the launcher app unless the installed one matches the source. (path, rebuilt)."""
    app = app_dir()
    stamp = app / "Contents" / "Resources" / "launcher.sha"
    want = launcher_hash()
    if stamp.exists() and stamp.read_text().strip() == want and (app / "Contents/MacOS/whosaid-call-recorder").exists():
        return app, False
    if dry:
        return app, True
    if not shutil.which("swiftc"):
        raise SystemExit("autorecord install --app needs swiftc to build the launcher: xcode-select --install")
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    exe = macos / "whosaid-call-recorder"
    arch = os.uname().machine
    r = subprocess.run(["swiftc", "-O", "-swift-version", "5", "-target", f"{arch}-apple-macos13.0",
                        str(LAUNCHER_SRC), "-o", str(exe)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launcher build failed:\n{r.stderr}")
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(info_plist(), fh)
    subprocess.run(["codesign", "-s", "-", "-f", str(app)], capture_output=True)
    stamp.write_text(want + "\n")
    return app, True


def run_args(a: argparse.Namespace) -> list[str]:
    """`autorecord run` flags that reproduce this install's configuration."""
    args = ["autorecord", "run", "--into", str(Path(a.into).expanduser())]
    if a.app:
        args += ["--app", a.app, "--idle-seconds", str(a.idle_seconds)]
    else:
        args += ["--browser", "--cdp-port", str(a.cdp_port)]
        if a.target:
            args += ["--target", a.target]
    if a.no_mic:
        args.append("--no-mic")
    if a.label:
        args += ["--label", a.label]
    return args


def build_plist(a: argparse.Namespace, app: Path | None) -> dict:
    into = Path(a.into).expanduser()
    if app is not None:
        # open -W keeps this job alive as long as the app runs, so KeepAlive can
        # restart it after a crash; -g keeps it in the background.
        program = ["/usr/bin/open", "-W", "-g", "-a", str(app), "--args", a.whosaid, *run_args(a)]
    else:
        program = [a.whosaid, *run_args(a)]
    return {
        "Label": LABEL,
        "ProgramArguments": program,
        "EnvironmentVariables": {"PATH": AGENT_PATH},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "StandardOutPath": str(into / ".autorecord.launchd.log"),
        "StandardErrorPath": str(into / ".autorecord.launchd.log"),
    }


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def domain() -> str:
    return f"gui/{os.getuid()}"


def stop_running() -> None:
    """Stop an installed autorecord (job, launcher app). A call in progress is saved."""
    launchctl("bootout", f"{domain()}/{LABEL}")
    subprocess.run(["pkill", "-TERM", "-f", f"{APP_NAME}.app/Contents/MacOS/"], capture_output=True)


def cmd_install(a: argparse.Namespace) -> int:
    if sys.platform != "darwin" and not a.dry_run:
        log("autorecord install: macOS only (launchd).")
        return 1
    into = Path(a.into).expanduser()
    a.whosaid = str(Path(a.whosaid).expanduser().resolve())
    app = None
    rebuilt = False
    if a.app:
        app, rebuilt = ensure_launcher_app(a.dry_run)
    data = build_plist(a, app)
    if a.dry_run:
        log("== DRY RUN: nothing written, nothing loaded ==")
        log(f"mode      -> {'app ' + a.app if a.app else 'browser, CDP port ' + str(a.cdp_port)}")
        log(f"records   -> {into}")
        if app:
            log(f"launcher  -> {app} ({'would build' if rebuilt else 'up to date'})")
        log(f"runs      -> {' '.join(data['ProgramArguments'])}")
        sys.stdout.write(plistlib.dumps(data).decode())
        return 0
    into.mkdir(parents=True, exist_ok=True)
    stop_running()
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    with open(plist_path(), "wb") as fh:
        plistlib.dump(data, fh)
    boot = launchctl("bootstrap", domain(), str(plist_path()))
    if boot.returncode != 0:
        log(f"autorecord install: launchctl bootstrap failed ({boot.stderr.strip() or boot.returncode})")
        return 1
    log(f"installed {LABEL}: records every call into {into}; starts at login.")
    if a.app:
        log(f"launcher app: {app}" + (" (built; macOS will ask for permission again)" if rebuilt else ""))
        print(f"""
Next: allow the launcher to record.
  1. System Settings > Privacy & Security > Screen & System Audio Recording:
     turn on "{APP_NAME}" (+ to add {app} if it is missing).
  2. Allow the microphone prompt the first time a call starts.
Until then each attempt logs a permission line and retries (backing off to 5 min).
No admin rights for that toggle? Use: whosaid autorecord install --browser
Log: tail -f {into / LOG_NAME}""")
        if not a.no_open:
            subprocess.run(["open", SCREEN_PANE_URL], capture_output=True)
    else:
        print(f"""
Next: start your browser with --remote-debugging-port={a.cdp_port} and use the meeting
in it (Teams web, Meet, Zoom web). Autorecord does not launch or change the browser.
A browser managed by an organization policy can disable remote debugging.
Log: tail -f {into / LOG_NAME}""")
    if not getattr(a, "from_setup", False):
        print(f"Transcribe each call automatically: whosaid watch install --into <workspace> --source {into} --no-fda")
    return 0


OWNER_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .'-]*$")


def write_owner(ws: Path, owner: str, dry: bool) -> None:
    """Start a workspace config naming its owner; never edit an existing one."""
    toml = ws / "whosaid.toml"
    if toml.exists():
        log(f"setup: {toml} exists; left as is (set [workspace] owner there to change it)")
        return
    if dry:
        log(f"setup: would write {toml}: [workspace] owner = \"{owner}\"")
        return
    toml.write_text(f'[workspace]\nowner = "{owner}"   # whose action items this workspace tracks\n')
    log(f"setup: wrote {toml} (owner {owner})")


def step(title: str, argv: list[str], dry: bool) -> int:
    log(f"== {title}: {' '.join(argv)}")
    if dry and "--dry-run" not in argv:
        return 0
    return subprocess.run(argv).returncode


def cmd_setup(a: argparse.Namespace) -> int:
    """One command for a new Mac: deps + models, autorecord at login, transcribe each call."""
    if sys.platform != "darwin" and not a.dry_run:
        log("autorecord setup: macOS only (launchd).")
        return 1
    if a.owner and not OWNER_OK.match(a.owner):
        log(f"autorecord setup: --owner {a.owner!r}: use letters, digits, space, _ . ' -")
        return 2
    whosaid = str(Path(a.whosaid).expanduser().resolve())
    ws = Path(a.workspace).expanduser().resolve()
    into = Path(a.into).expanduser().resolve()
    if ws == into:
        log("autorecord setup: --workspace and --into must be different folders")
        return 2
    if a.dry_run:
        log("== DRY RUN: nothing installed, nothing written ==")

    if not a.skip_deps:
        rc = step("1/3 dependencies and models", [whosaid, "setup", "--yes"], a.dry_run)
        if rc:
            log(f"autorecord setup: `whosaid setup` failed (exit {rc}); fix that and re-run")
            return rc

    log(f"== 2/3 record every call ({'app ' + a.app if a.app else 'browser'}) into {into}")
    a.into, a.whosaid, a.from_setup = str(into), whosaid, True
    rc = cmd_install(a)
    if rc:
        return rc

    if not a.dry_run:
        ws.mkdir(parents=True, exist_ok=True)
    if a.owner:
        write_owner(ws, a.owner, a.dry_run)
    watch = [whosaid, "watch", "install", "--into", str(ws), "--source", str(into), "--no-fda"]
    if a.dry_run and ws.is_dir():
        watch.append("--dry-run")
    rc = step("3/3 transcribe each saved call", watch, a.dry_run)
    if rc:
        log(f"autorecord setup: `whosaid watch install` failed (exit {rc})")
        return rc

    todo = []
    if a.app:
        todo.append(f'turn on "{APP_NAME}" in System Settings > Privacy & Security > '
                    "Screen & System Audio Recording")
    else:
        todo.append(f"start your browser with --remote-debugging-port={a.cdp_port} and join meetings there")
    if not a.owner:
        todo.append(f'name the workspace owner: [workspace] owner = "Your_Name" in {ws / "whosaid.toml"}')
    todo.append("teach whosaid your voice: whosaid enroll")
    todo.append("check: whosaid autorecord status   (then join a call)")
    print("\nSetup " + ("would be " if a.dry_run else "") + "done. Left for you:")
    print("\n".join(f"  {n}. {t}" for n, t in enumerate(todo, 1)))
    return 0


def cmd_uninstall(a: argparse.Namespace) -> int:
    existed = plist_path().exists()
    stop_running()
    if existed:
        plist_path().unlink()
        log(f"removed {plist_path()}")
    else:
        log(f"autorecord is not installed ({plist_path()} missing)")
    if a.purge and app_dir().exists():
        shutil.rmtree(app_dir())
        log(f"removed {app_dir()} (its recording permission entry can be removed in System Settings)")
    return 0


LOG_STATE_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")


def state_from_log(lines: list[str]) -> dict:
    """Best-effort state for a loop that writes no state file (an older install):
    the last saved file, the last failed exit, and whether a capture is open."""
    out = {"state": "waiting", "since": None, "last_saved": None, "last_error": None}
    for line in lines:
        m = LOG_STATE_LINE.match(line)
        if not m:
            continue
        at = iso(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
        msg = m.group(2)
        if msg.startswith("autorecord: saved "):
            out.update(state="waiting", since=at, last_saved={"path": msg[len("autorecord: saved "):], "at": at})
        elif msg.startswith("autorecord: stopped"):
            out.update(state="stopped", since=at)
        elif msg.startswith("autorecord: no Screen & System Audio Recording permission"):
            out.update(state="error", since=at, last_error={"rc": EXIT_PERMISSION, "kind": "permission", "at": at})
        elif (em := re.match(r"autorecord: record exited (\d+);", msg)):
            out.update(state="error", since=at, last_error={"rc": int(em.group(1)), "kind": "failed", "at": at})
        elif msg.startswith("autorecord: record exited"):
            out.update(state="waiting", since=at)
        elif CAPTURE_STARTED.search(msg) and not msg.startswith("autorecord:"):
            out.update(state="recording", since=at)
    return out


def lock_held(into: Path) -> bool:
    try:  # never create the lock file: a missing one is not held
        fd = os.open(into / LOCK_NAME, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # got it: nobody holds it
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def age_seconds(stamp: object) -> float:
    try:
        return time.time() - time.mktime(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return float("inf")


def installed_config() -> dict | None:
    if not plist_path().exists():
        return None
    try:
        with open(plist_path(), "rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    args = list(data.get("ProgramArguments") or [])
    return {"into": args[args.index("--into") + 1] if "--into" in args else str(DEFAULT_INTO),
            "mode": "app" if "--app" in args else "browser",
            "app": args[args.index("--app") + 1] if "--app" in args else None}


def status(into_arg: str | None = None) -> dict:
    """Everything the app and `status --json` show. Read-only."""
    cfg = installed_config()
    into = Path(into_arg or (cfg or {}).get("into") or DEFAULT_INTO).expanduser()
    loaded = None
    if cfg is not None and sys.platform == "darwin":
        loaded = launchctl("print", f"{domain()}/{LABEL}").returncode == 0
    data = read_json(into / STATE_NAME)
    owner_alive = bool(data) and data.get("state") != "stopped" and pid_alive(data.get("pid"))
    if owner_alive and age_seconds(data.get("updated")) > STALE_STATE_SECONDS:
        owner_alive = lock_held(into)  # a long-quiet file: make sure that pid is still the loop
    # The lock probe is skipped when the state file's live pid already answers it.
    running = owner_alive or (into.is_dir() and (into / LOCK_NAME).exists() and lock_held(into))
    live_file = owner_alive
    if live_file:
        src = {k: data.get(k) for k in ("state", "since", "last_saved", "last_error", "paused_until")}
    else:
        logf = into / LOG_NAME
        lines = logf.read_text(errors="replace").splitlines()[-400:] if logf.exists() else []
        src = state_from_log(lines)
        if not running:
            src["state"] = "stopped" if (cfg or lines) else "not-installed"
        src["paused_until"] = None
    until = paused_until(into)
    if until and live_file and src["state"] not in ("recording", "saving"):
        src["state"] = "paused"
    since = src.get("since")
    elapsed = None
    if since:
        try:
            elapsed = max(0, int(time.time() - time.mktime(time.strptime(since[:19], "%Y-%m-%dT%H:%M:%S"))))
        except ValueError:
            pass
    return {"installed": cfg is not None, "loaded": loaded, "running": running,
            "controls": live_file, "mode": (cfg or data or {}).get("mode"), "app": (cfg or data or {}).get("app"),
            "into": str(into), **src, "paused_until": iso(until) if until else src.get("paused_until"),
            "elapsed": elapsed, "source": "state-file" if live_file else "log"}


NO_CONTROLS = ("the running autorecord predates pause/stop-now (no live {name}); "
               "reinstall it to enable them: whosaid autorecord install ...")


def cmd_pause(a: argparse.Namespace) -> int:
    s = status(a.into)
    if not s["controls"]:
        log("autorecord pause: " + ("autorecord is not running" if not s["running"] else NO_CONTROLS.format(name=STATE_NAME)))
        return 4
    until = time.time() + a.minutes * 60
    write_json_atomic(Path(s["into"]) / PAUSE_NAME, {"until": until, "set_at": iso()})
    log(f"autorecord: paused until {iso(until)}" + (" (the call being recorded keeps going)" if s["state"] == "recording" else ""))
    return 0


def cmd_resume(a: argparse.Namespace) -> int:
    s = status(a.into)
    f = Path(s["into"]) / PAUSE_NAME
    existed = f.exists()
    f.unlink(missing_ok=True)
    log("autorecord: resumed" if existed else "autorecord: was not paused")
    return 0


def cmd_stop_now(a: argparse.Namespace) -> int:
    s = status(a.into)
    if not s["controls"]:
        log("autorecord stop-now: " + ("autorecord is not running" if not s["running"] else NO_CONTROLS.format(name=STATE_NAME)))
        return 4
    if s["state"] != "recording":
        log(f"autorecord stop-now: nothing is being recorded (state: {s['state']})")
        return 1
    before = (s.get("last_saved") or {}).get("path")
    write_json_atomic(Path(s["into"]) / STOP_NOW_NAME, {"at": iso()})
    log("autorecord: stop-now requested; the call is saved and autorecord keeps waiting for the next one")
    end = time.monotonic() + a.wait
    while time.monotonic() < end:
        time.sleep(0.5)
        saved = (status(a.into).get("last_saved") or {}).get("path")
        if saved and saved != before:
            print(saved)
            return 0
    if a.wait:
        log(f"autorecord stop-now: nothing saved within {a.wait:g}s; check `whosaid autorecord status`")
        return 2
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    if a.json:
        print(json.dumps(status(a.into), indent=1))
        return 0
    cfg = installed_config()
    if not cfg:
        print("autorecord: not installed")
        return 1
    into = Path(a.into or cfg["into"]).expanduser()
    mode = f"app {cfg['app']}" if cfg["mode"] == "app" else "browser"
    running = bool(status(str(into))["running"])
    print(f"autorecord: installed ({mode}) -> {into}; {'running' if running else 'NOT running'}")
    logf = into / LOG_NAME
    if logf.exists():
        lines = logf.read_text(errors="replace").splitlines()[-a.lines:]
        print("\n".join(lines))
    return 0 if running else 3


# ---------------------------------------------------------------- CLI

def add_mode_args(p: argparse.ArgumentParser) -> None:
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--app", help="bundle ID of the meeting app, e.g. com.microsoft.teams2")
    m.add_argument("--browser", action="store_true", help="record meeting tabs over CDP")
    p.add_argument("--into", default=str(DEFAULT_INTO))
    p.add_argument("--idle-seconds", type=int, default=30)
    p.add_argument("--cdp-port", type=int, default=int(os.environ.get("WHOSAID_CDP_PORT") or 9222))
    p.add_argument("--target")
    p.add_argument("--label", default="call")
    p.add_argument("--no-mic", action="store_true")
    p.add_argument("--whosaid", default=default_whosaid())


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="whosaid autorecord")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    add_mode_args(r)
    i = sub.add_parser("install")
    add_mode_args(i)
    i.add_argument("--no-open", action="store_true")
    i.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("uninstall")
    u.add_argument("--purge", action="store_true")
    st = sub.add_parser("setup")
    add_mode_args(st)
    st.add_argument("--workspace", required=True, help="meeting workspace the transcripts go into")
    st.add_argument("--owner", help="workspace owner label for a new whosaid.toml, e.g. Alice_Example")
    st.add_argument("--skip-deps", action="store_true", help="skip `whosaid setup` (deps, models)")
    st.add_argument("--no-open", action="store_true")
    st.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--lines", type=int, default=8)
    s.add_argument("--json", action="store_true", help="one JSON object; exit 0 even when not installed")
    s.add_argument("--into", help="recordings folder (default: the installed one, else ~/Recordings)")
    pz = sub.add_parser("pause")
    pz.add_argument("--minutes", type=float, default=60.0)
    rs = sub.add_parser("resume")
    sn = sub.add_parser("stop-now")
    sn.add_argument("--wait", type=float, default=0.0, help="wait up to N s and print the saved file")
    for q in (pz, rs, sn):
        q.add_argument("--into", help="recordings folder (default: the installed one, else ~/Recordings)")
    a = p.parse_args(argv)
    if a.cmd == "pause" and not 0 < a.minutes <= 24 * 60:
        p.error("--minutes must be > 0 and at most 1440")
    if getattr(a, "idle_seconds", 1) <= 0:
        p.error("--idle-seconds must be > 0")
    if a.cmd in ("run", "install", "setup") and a.browser is False and a.target:
        p.error("--target only applies with --browser")
    return a


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    return {"run": cmd_run, "install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status,
            "setup": cmd_setup, "pause": cmd_pause, "resume": cmd_resume, "stop-now": cmd_stop_now}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
