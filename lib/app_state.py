#!/usr/bin/env python3
"""
app_state.py: one JSON snapshot of everything the macOS app's menu bar shows
(`whosaid app-state`).

  whosaid app-state --json [--workspace WS] [--into DIR]
  whosaid app-state --watch SECONDS [--workspace WS] [--into DIR]

The snapshot joins:
  autorecord   `autorecord status --json` (lib/autorecord.py status())
  watch        the watcher LaunchAgent for WS (loaded, running) and its log
               state, with the SwiftBar plugin's own rules (idle | waiting |
               ingesting | done | warn), imported from contrib/swiftbar
  live         `live status --json` (newest .live session, no text)
  last_ingest  the newest meeting folder in WS
  unnamed      SPEAKER_NN clusters left in meetings from the last 14 days
  menubar      the composed state: warn > agent-not-loaded > recording-live >
               recording > paused > ingesting > waiting > done > idle

--watch prints one JSON line at start, then one whenever anything but the clock
changes (and at least every 60 s), so the app keeps ONE helper process instead
of spawning a command every 2 s. It exits when its stdout closes. Read-only:
nothing is written, nothing is started or stopped. Stdlib only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))
import autorecord  # noqa: E402
import live  # noqa: E402
import watch  # noqa: E402

PLUGIN = LIB.parent / "contrib" / "swiftbar" / "whosaid.10s.py"
UNNAMED_DAYS = 14
HEARTBEAT = 60.0
SPEAKER_RE = re.compile(r"^\[[\d:.]+\] (SPEAKER_\d+):", re.M)


def _plugin():
    spec = importlib.util.spec_from_file_location("whosaid_swiftbar_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # import-safe: side effects live in main()
    return mod


plugin = _plugin()


def find_watcher(ws: Path) -> str:
    """The installed watcher label whose plist points at ws, else the default."""
    want = ws.resolve()
    for label in watch.installed_watcher_labels():
        found = watch.plist_workspace(watch.read_plist(label))
        if found is not None and Path(found).expanduser().resolve() == want:
            return label
    return watch.default_label(ws)


def watch_state(ws: Path | None) -> dict:
    if ws is None:
        return {"installed": False, "label": None, "loaded": None, "running": None, "last_exit_status": None,
                "state": "idle", "detail": {}}
    label = find_watcher(ws)
    installed = watch.read_plist(label) is not None
    loaded, pid, last, lstate = watch.launchctl_status(label) if installed else (False, None, None, "")
    status = {"loaded": loaded, "state": lstate, "pid": pid, "pid_alive": watch.process_alive(pid)}
    running = plugin.watcher_running(status) if installed else None
    state, detail = plugin.classify(plugin.parse_stage_lines(plugin.tail_lines(ws / plugin.LOG_NAME)),
                                    watcher_running=running)
    return {"installed": installed, "label": label, "loaded": loaded if installed else None,
            "running": running, "last_exit_status": last, "state": state, "detail": detail}


class Unnamed:
    """SPEAKER_NN clusters per recent meeting, cached by speakers-file mtime."""

    def __init__(self):
        self.cache: dict[Path, tuple[float, int]] = {}

    def count(self, ws: Path | None, now: float | None = None) -> dict:
        if ws is None:
            return {"voices": 0, "meetings": 0, "days": UNNAMED_DAYS}
        cutoff = (now or time.time()) - UNNAMED_DAYS * 86400
        voices = meetings = 0
        for _name, files in watch_iter_meetings(ws):
            for f in files:
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                hit = self.cache.get(f)
                if not hit or hit[0] != mtime:
                    try:
                        n = len(set(SPEAKER_RE.findall(f.read_text(errors="replace"))))
                    except OSError:
                        n = 0
                    hit = self.cache[f] = (mtime, n)
                if hit[1]:
                    voices += hit[1]
                    meetings += 1
        return {"voices": voices, "meetings": meetings, "days": UNNAMED_DAYS}


def watch_iter_meetings(ws: Path):
    import wsconfig
    return wsconfig.iter_meetings(ws)


def last_ingest(ws: Path | None) -> dict | None:
    if ws is None:
        return None
    best = None
    for name, files in watch_iter_meetings(ws):
        try:
            mtime = max(f.stat().st_mtime for f in files)
        except (OSError, ValueError):
            continue
        if best is None or mtime > best[1]:
            best = (name, mtime)
    return None if best is None else {"folder": best[0], "at": autorecord.iso(best[1])}


def compose(ar: dict, w: dict, lv: dict) -> dict:
    """The one state the menu bar glyph shows, and a short headline."""
    if w.get("state") == "warn":
        return {"state": "warn", "headline": "needs a look: " + str((w.get("detail") or {}).get("msg") or "")}
    if ar.get("state") == "error":
        if (ar.get("last_error") or {}).get("kind") == "permission":
            return {"state": "warn", "headline": "autorecord has no Screen & System Audio Recording permission"}
        return {"state": "warn", "headline": f"autorecord: the recorder keeps failing (exit {(ar.get('last_error') or {}).get('rc')})"}
    if w.get("installed") and w.get("loaded") and w.get("running") is False:
        return {"state": "warn", "headline": "the watcher launch agent is loaded but not running"}
    if ar.get("installed") and ar.get("loaded") and ar.get("running") is False:
        return {"state": "warn", "headline": "the autorecord launch agent is loaded but not running"}
    if w.get("installed") and w.get("loaded") is False:
        return {"state": "agent-not-loaded", "headline": "the watcher launch agent is not loaded"}
    if ar.get("installed") and ar.get("loaded") is False:
        return {"state": "agent-not-loaded", "headline": "the autorecord launch agent is not loaded"}
    if ar.get("state") == "saving":
        return {"state": "recording", "headline": "saving the recording"}
    if ar.get("state") == "recording":
        if lv.get("ok") and lv.get("active"):
            return {"state": "recording-live", "headline": "recording, live transcript on"}
        return {"state": "recording", "headline": "recording"}
    if ar.get("state") == "paused":
        return {"state": "paused", "headline": f"autorecord paused until {ar.get('paused_until')}"}
    state = w.get("state") or "idle"
    return {"state": state, "headline": {"ingesting": "transcribing", "waiting": "waiting for the recording to settle",
                                         "done": "transcribed", "idle": "watching"}.get(state, state)}


def ttl_cache(fn, seconds: float):
    """fn memoized per argument tuple for `seconds` (launchctl print is a spawn)."""
    memo: dict = {}

    def wrapped(*args):
        hit = memo.get(args)
        if hit and time.monotonic() - hit[0] < seconds:
            return hit[1]
        val = fn(*args)
        memo[args] = (time.monotonic(), val)
        return val
    return wrapped


def snapshot(ws: Path | None, into: str | None, unnamed: Unnamed) -> dict:
    ar = autorecord.status(into)
    lv = live.latest_session(Path(ar["into"]))
    lv.pop("transcript", None)
    w = watch_state(ws)
    return {"version": 1, "at": autorecord.iso(), "workspace": str(ws) if ws else None, "into": ar["into"],
            "menubar": compose(ar, w, lv), "autorecord": ar, "watch": w, "live": lv,
            "last_ingest": last_ingest(ws), "unnamed": unnamed.count(ws)}


def _stable(s: dict) -> str:
    """The snapshot minus fields that change every second."""
    s = json.loads(json.dumps(s))
    s.pop("at", None)
    s["autorecord"].pop("elapsed", None)
    (s["watch"].get("detail") or {}).pop("age", None)
    return json.dumps(s, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whosaid app-state")
    p.add_argument("--json", action="store_true", help="print one snapshot (the default)")
    p.add_argument("--watch", type=float, metavar="SECONDS", help="poll every SECONDS; print a line on change")
    p.add_argument("--workspace", default=os.environ.get("WHOSAID_WORKSPACE") or None)
    p.add_argument("--into", help="recordings folder (default: autorecord's, else ~/Recordings)")
    p.add_argument("--max-seconds", type=float, default=0, help=argparse.SUPPRESS)  # test bound
    a = p.parse_args(argv)
    ws = Path(a.workspace).expanduser() if a.workspace else None
    unnamed = Unnamed()
    if not a.watch:
        print(json.dumps(snapshot(ws, a.into, unnamed), indent=1))
        return 0
    if a.watch < 0.5:
        p.error("--watch must be >= 0.5 seconds")
    # A long-lived poller: `launchctl print` once per 10 s per job, not every poll.
    autorecord.launchctl = ttl_cache(autorecord.launchctl, 10.0)
    watch.launchctl_status = ttl_cache(watch.launchctl_status, 10.0)
    last, last_at = None, 0.0
    start = time.monotonic()
    try:
        while not a.max_seconds or time.monotonic() - start < a.max_seconds:
            snap = snapshot(ws, a.into, unnamed)
            key = _stable(snap)
            if key != last or time.monotonic() - last_at >= HEARTBEAT:
                sys.stdout.write(json.dumps(snap) + "\n")
                sys.stdout.flush()
                last, last_at = key, time.monotonic()
            time.sleep(a.watch)
    except (BrokenPipeError, KeyboardInterrupt):
        try:
            sys.stdout.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
