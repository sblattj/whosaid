#!/usr/bin/env python3
"""
One `whosaid app-state` snapshot per menu bar state (gate G6), each built from
real inputs in a temp dir: the watcher's .watch.log, the autorecord state and
pause files, a .live session, an installed plist with a fake launchctl.
Made-up names and paths only; nothing outside the temp dir is read or touched.

  python3 test/app_state_fixtures.py            check every state (a test)
  python3 test/app_state_fixtures.py --out DIR  also write DIR/<state>.json
                                                (the Swift menu tests read them)
"""
import json
import os
import plistlib
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import app_state  # noqa: E402

ar = app_state.autorecord


def stamp(ago: float = 0) -> str:
    return ar.iso(time.time() - ago)


def watch_log(ws: Path, *msgs: str) -> None:
    (ws / ".watch.log").write_text("".join(f"{stamp(5)} whosaid: {m}\n" for m in msgs))


def loop_state(rec: Path, state: str, since_ago: float = 0, **extra) -> None:
    """A state file owned by a live pid (this process) that updated just now."""
    ar.write_json_atomic(rec / ar.STATE_NAME, {"version": 1, "pid": os.getpid(), "controls": True, "mode": "app",
                                               "app": "com.example.meet", "state": state, "since": stamp(since_ago),
                                               "updated": stamp(), "last_saved": None, "last_error": None, **extra})


def live_session(rec: Path, active: bool) -> None:
    (rec / ".live").mkdir(exist_ok=True)
    now = time.time()
    meta = {"started": now - 60, "ended": not active, "updated": now}
    (rec / ".live" / "20261029T150000.jsonl").write_text(json.dumps({"meta": meta}) + "\n")


def install_agent(home: Path, rec: Path, bindir: Path, loaded: bool) -> None:
    i = ar.parse_args(["install", "--app", "com.example.meet", "--into", str(rec), "--whosaid", "/bin/true"])
    with open(home / "Library" / "LaunchAgents" / f"{ar.LABEL}.plist", "wb") as fh:
        plistlib.dump(ar.build_plist(i, Path("/Apps/Example.app")), fh)
    (bindir / "launchctl").write_text(f"#!/bin/bash\nexit {0 if loaded else 113}\n")
    (bindir / "launchctl").chmod(0o755)


# state -> (setup, expected menubar.state); setup(ws, rec, home, bindir)
CASES = {
    "idle": lambda ws, rec, home, b: None,
    "waiting": lambda ws, rec, home, b: watch_log(ws, "1 new recording(s)", "waiting for the file to settle"),
    "ingesting": lambda ws, rec, home, b: watch_log(ws, "1 new recording(s)", "> 1/1 ingest 20261029T150000Z-call.m4a"),
    "done": lambda ws, rec, home, b: watch_log(ws, "> 1/1 ingest a.m4a", "ok ingested 2026-10-29-1500-call"),
    "warn": lambda ws, rec, home, b: watch_log(ws, "> 1/1 ingest a.m4a", "! ingest failed: a.m4a (exit 1)"),
    "agent-not-loaded": lambda ws, rec, home, b: install_agent(home, rec, b, loaded=False),
    "recording": lambda ws, rec, home, b: loop_state(rec, "recording", since_ago=761),
    "recording-live": lambda ws, rec, home, b: (loop_state(rec, "recording", since_ago=761), live_session(rec, True)),
    "paused": lambda ws, rec, home, b: (loop_state(rec, "waiting"),
                                        ar.write_json_atomic(rec / ar.PAUSE_NAME, {"until": time.time() + 3600})),
    "saving": lambda ws, rec, home, b: loop_state(rec, "saving", since_ago=3),
}
EXPECT = {"saving": "recording"}


def build(name: str, root: Path) -> dict:
    ws, rec, home, bindir = root / "ws", root / "rec", root / "home", root / "bin"
    for p in (ws, rec, home / "Library" / "LaunchAgents", bindir):
        p.mkdir(parents=True)
    (bindir / "launchctl").write_text("#!/bin/bash\nexit 113\n")  # nothing loaded unless a case says so
    (bindir / "launchctl").chmod(0o755)
    app_state.watch.LAUNCH_AGENTS = ar.LAUNCH_AGENTS = home / "Library" / "LaunchAgents"
    os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"
    try:
        CASES[name](ws, rec, home, bindir)
        return app_state.snapshot(ws, str(rec), app_state.Unnamed())
    finally:
        os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]


def main(argv: list[str]) -> int:
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    checks = 0
    for name in CASES:
        with tempfile.TemporaryDirectory() as d:
            snap = build(name, Path(d))
        got, want = snap["menubar"]["state"], EXPECT.get(name, name)
        checks += 1
        if got != want:
            print(f"FAIL: {name}: menubar.state {got!r}, want {want!r}: {snap['menubar']} {snap['autorecord']['state']}")
            return 1
        if "percent" in json.dumps(snap["menubar"]):
            print(f"FAIL: {name}: the menu bar never shows a percent")
            return 1
        if out:
            (out / f"{name}.json").write_text(json.dumps(snap, indent=1))
    print(f"PASS: app_state_fixtures ({checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
