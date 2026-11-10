#!/usr/bin/env python3
"""Start the Microsoft Teams desktop app with a DevTools (CDP) port (GitHub issue #196).

    whosaid teams launch [--port N] [--check] [--force] [--wait S]

The new Teams app is a WebView2 (Edge) shell. WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS passed in
the environment of `open -a` makes it listen for DevTools on 127.0.0.1:N, so the drafts stager
can drive the real app instead of the Teams web app.

Port: --port, else $WHOSAID_TEAMS_CDP_PORT, else 9333.
Prints one JSON object on stdout:
  {"port": N, "up": bool, "action": "none|launched|relaunched|refused|down",
   "browser": "Edg/..."|null, "pages": [{"id", "title"}]}
Errors go to stderr. Exit 0 = port up (as the Teams app), 1 = down / timed out / port held by
something else, 2 = refused (a call may be live) or bad usage.

Relaunching a running Teams quits it, which would drop a live call, so it is refused unless the
mic is verifiably idle (`whosaid record --app com.microsoft.teams2 --watch-call`), or --force.
Fail closed: an unreadable mic state counts as "in a call". --check never touches the app.

Every external command goes through a small module-level function so tests can replace it.
Stdlib only.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

DEFAULT_PORT = 9333
BUNDLE_ID = "com.microsoft.teams2"
APP_NAME = "Microsoft Teams"
PROCESS_NAME = "MSTeams"  # /Applications/Microsoft Teams.app/Contents/MacOS/MSTeams
QUIT_WAIT_S = 15.0
POLL_S = 0.5


def _err(msg):
    print(f"teams launch: {msg}", file=sys.stderr)


# ---- external commands (monkeypatched in tests) ---------------------------------------

def http_get(url, timeout=2.0):
    """GET url, return the decoded JSON body, or None on any failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def is_running():
    """True when the Teams app has a process. pgrep first; osascript's `is running` (which does
    not launch the app) as the fallback."""
    try:
        if subprocess.run(["pgrep", "-x", PROCESS_NAME], capture_output=True, timeout=10).returncode == 0:
            return True
    except Exception:
        pass
    try:
        out = subprocess.run(["osascript", "-e", f'application id "{BUNDLE_ID}" is running'],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == "true"
    except Exception:
        return False


def quit_teams():
    subprocess.run(["osascript", "-e", f'quit app "{APP_NAME}"'], capture_output=True, timeout=30)


def open_teams(port):
    env = f"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port={port}"
    subprocess.run(["open", "-a", APP_NAME, "--env", env], capture_output=True, timeout=30)


def watch_call_state(timeout=10.0):
    """First line of `whosaid record --app <bundle> --watch-call` as a dict, or None on any
    failure or timeout. The child is terminated either way."""
    binary = os.environ.get("WHOSAID_BIN") or "whosaid"
    try:
        proc = subprocess.Popen([binary, "record", "--app", BUNDLE_ID, "--watch-call"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return None
    box = []
    t = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
    t.start()
    t.join(timeout)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        return json.loads(box[0]) if box and box[0].strip() else None
    except ValueError:
        return None


def sleep(seconds):
    time.sleep(seconds)


# ---- logic ----------------------------------------------------------------------------

def probe(port):
    """Browser string from /json/version, or None when nothing answers."""
    data = http_get(f"http://127.0.0.1:{port}/json/version")
    if not isinstance(data, dict):
        return None
    return str(data.get("Browser") or "")


def pages(port):
    data = http_get(f"http://127.0.0.1:{port}/json/list")
    if not isinstance(data, list):
        return []
    return [{"id": p.get("id"), "title": p.get("title")}
            for p in data if isinstance(p, dict) and p.get("type") == "page"]


def _result(port, action, browser):
    is_teams = bool(browser) and browser.startswith("Edg/")
    return {"port": port, "up": is_teams, "action": action,
            "browser": browser or None, "pages": pages(port) if is_teams else []}


def resolve_port(arg):
    raw = arg if arg is not None else os.environ.get("WHOSAID_TEAMS_CDP_PORT")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_PORT
    try:
        n = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"bad port {raw!r}: want an integer 1-65535")
    if not 1 <= n <= 65535:
        raise ValueError(f"bad port {raw!r}: want an integer 1-65535")
    return n


def run(port, check=False, force=False, wait=30.0):
    """Returns (result dict, exit code)."""
    browser = probe(port)
    if browser is not None:
        if browser.startswith("Edg/"):
            return _result(port, "none", browser), 0
        _err(f"port {port} is held by {browser or 'an unknown program'}, not the Teams app")
        res = _result(port, "refused", browser)
        return res, 1
    if check:
        return _result(port, "down", None), 1

    if not is_running():
        action = "launched"
    else:
        if not force:
            state = watch_call_state(10.0)
            if state is None:
                _err("cannot read the Teams mic state; refusing to quit Teams (use --force)")
                return _result(port, "refused", None), 2
            if state.get("mic") is not False:
                _err("Teams is using the mic (a call may be live); refusing to quit it (use --force)")
                return _result(port, "refused", None), 2
        quit_teams()
        deadline = time.monotonic() + QUIT_WAIT_S
        while is_running():
            if time.monotonic() >= deadline:
                _err(f"Teams did not quit within {int(QUIT_WAIT_S)} s")
                return _result(port, "down", None), 1
            sleep(POLL_S)
        action = "relaunched"
    open_teams(port)

    deadline = time.monotonic() + wait
    while True:
        browser = probe(port)
        if browser is not None:
            break
        if time.monotonic() >= deadline:
            _err(f"no DevTools answer on port {port} after {wait:g} s")
            return _result(port, action, None), 1
        sleep(POLL_S)
    res = _result(port, action, browser)
    if not res["up"]:
        _err(f"port {port} is held by {browser or 'an unknown program'}, not the Teams app")
        return res, 1
    return res, 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="whosaid teams launch",
                                 description="Start the Teams desktop app with a DevTools port.")
    ap.add_argument("--port", help="DevTools port (default $WHOSAID_TEAMS_CDP_PORT, else 9333)")
    ap.add_argument("--check", action="store_true", help="only probe the port; never touch the app")
    ap.add_argument("--force", action="store_true", help="quit a running Teams even if the mic is busy or unreadable")
    ap.add_argument("--wait", type=float, default=30.0, help="seconds to wait for the port (default 30)")
    args = ap.parse_args(argv)
    try:
        port = resolve_port(args.port)
    except ValueError as exc:
        _err(str(exc))
        return 2
    res, code = run(port, check=args.check, force=args.force, wait=max(0.0, args.wait))
    print(json.dumps(res))
    return code


if __name__ == "__main__":
    sys.exit(main())
