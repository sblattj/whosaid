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
  status     Installed mode, running or not, recent log lines.

The `whosaid watch` LaunchAgent pointed at the same folder ingests each file.
Stdlib only.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import plistlib
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
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(f"autorecord: already running for {into} (lock {into / LOCK_NAME}); exiting.")
        return 0
    tee = Tee(into / LOG_NAME)
    argv = record_argv(a)
    tee.line(f"autorecord: {'app ' + a.app if a.app else 'browser on CDP port ' + str(a.cdp_port)}"
             f" -> {into}; Ctrl-C to quit")

    state = {"stop": False, "child": None}

    def on_signal(_signum, _frame):
        state["stop"] = True
        child = state["child"]
        if child and child.poll() is None:
            try:  # the whole group: bash traps INT, the capture helper finishes and saves
                os.killpg(child.pid, signal.SIGINT)
            except OSError:
                pass

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    backoff = BACKOFF_START
    permission_logged = False
    max_loops = int(os.environ.get("WHOSAID_AUTORECORD_MAX_LOOPS") or 0)  # test hook
    loops = 0
    while not state["stop"]:
        loops += 1
        # New session: a terminal Ctrl-C reaches only this loop, which forwards
        # one SIGINT; a second one would make the capture helper exit unsaved.
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        state["child"] = child

        def pump(stream=child.stderr):
            for line in stream:
                if " peak " not in line:  # the 10 s progress ticks stay out of the log
                    tee.line(line)

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        out = child.stdout.read()
        rc = child.wait()
        t.join(timeout=5)
        state["child"] = None
        saved = next((ln for ln in reversed(out.splitlines()) if ln.strip().endswith(".m4a")), None)
        kind, delay = classify(rc, saved)
        if kind == "saved":
            tee.line(f"autorecord: saved {saved}")
            backoff, permission_logged = BACKOFF_START, False
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
    print(f"Transcribe each call automatically: whosaid watch install --into <workspace> --source {into} --no-fda")
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


def cmd_status(a: argparse.Namespace) -> int:
    data = None
    if plist_path().exists():
        with open(plist_path(), "rb") as fh:
            data = plistlib.load(fh)
    if not data:
        print("autorecord: not installed")
        return 1
    args = list(data.get("ProgramArguments") or [])
    into = Path(args[args.index("--into") + 1]) if "--into" in args else DEFAULT_INTO
    mode = f"app {args[args.index('--app') + 1]}" if "--app" in args else "browser"
    running = False
    try:
        with open(into / LOCK_NAME, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # got it: nobody holds it
    except OSError:
        running = True
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
    s = sub.add_parser("status")
    s.add_argument("--lines", type=int, default=8)
    a = p.parse_args(argv)
    if getattr(a, "idle_seconds", 1) <= 0:
        p.error("--idle-seconds must be > 0")
    if a.cmd in ("run", "install") and a.browser is False and a.target:
        p.error("--target only applies with --browser")
    return a


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    return {"run": cmd_run, "install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
