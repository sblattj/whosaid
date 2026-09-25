#!/usr/bin/env python3
# <swiftbar.hideSwiftBar>
# <swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>
# <swiftbar.hideLastUpdated>
# <swiftbar.hideAbout>
"""
whosaid.10s.py: SwiftBar menu bar glyph for the whosaid watcher (issue #24).

A single stdlib-only script, refreshed by SwiftBar every 10s. It reads
WHOSAID_WORKSPACE (env, exactly like the MCP server's default) and shells the
INSTALLED `whosaid` for `watch status --json` and `memos list --json`; the log
is read directly (<workspace>/.watch.log). Install it with:

    whosaid watch menubar install

Menu bar glyphs:

    🎙      idle, watching the store        last stage line "no new recordings"
    🔴 REC  a memo is being recorded        memos list row with no file and 0s
    ⏳ Ns   new memo settling               "still syncing"/"waiting"/"new recording"
    ⚙️ Nm   transcribing/diarizing          "> ingest ..." while the watcher runs
    ✅      ingested (held 30 min)          "done:" / "ok ingested"
    ⚠️      needs a look                    a "!" line, or an ingest with no watcher
    🎙✗    launch agent not loaded          watch status --json loaded=false

Lessons baked in (from the private plugin this was ported from):

- ONLY the watcher's own timestamped stage lines
  (``YYYY-MM-DDTHH:MM:SS+ZZ:ZZ whosaid: ...``) drive the state rules.
  Subprocess output lands in the same log and may even carry timestamps, but
  it never carries the ``whosaid:`` marker, so it can never match: the
  diarizer prints ``chunk 8/8 done:`` lines that look exactly like the done
  rule otherwise.
- The last ~2 MB of the log is tailed, not the last N lines: one ingest dumps
  a thousand speaker-card lines, so a fixed line window would miss every
  stage line.
- REC detection needs SwiftBar itself to hold Full Disk Access (the store is
  a protected group container; the watcher's own grant is separate). When the
  plugin's store probe fails the menu shows a clear line plus a link to the
  Full Disk Access pane and a "Relaunch SwiftBar" action: a fresh grant only
  applies after a relaunch, and this plugin hides SwiftBar's own menu.
- Notifications (osascript display notification) fire ONCE per transition
  into ingesting/done/warn, tracked in ~/.cache/whosaid/menubar-state.json.
- Notch: on a notched display a new status item can land behind the notch.
  Fix it by hand (do not automate):

      defaults write com.ameba.SwiftBar "NSStatusItem Preferred Position <plugin path>" -float <n>

  then relaunch SwiftBar.

Every failure degrades to a readable menu line; the script never tracebacks
into the menu bar. Import-safe: everything side-effecting lives in functions
and main().
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

VOICE_MEMOS_STORE = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
# keep in sync with lib/watch.py FDA_PANE_URL (the plugin is stdlib-only by design
# and never imports lib/; test/menubar_test.py guards the two against drifting)
FDA_PANE_URL = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles"
STATE_FILE = Path.home() / ".cache/whosaid/menubar-state.json"
LOG_NAME = ".watch.log"
LABEL_PREFIX = "com.whosaid.watch."
LOG_TAIL_BYTES = 2_000_000
DONE_HOLD_SECONDS = 30 * 60
NOTIFY_STATES = ("ingesting", "done", "warn")

STAGE_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}) whosaid: (.*)$")
MEETING_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}")


# ---- pure functions (unit-tested by test/menubar_test.py) ---------------------------


def tail_lines(path: Path | None, max_bytes: int = LOG_TAIL_BYTES) -> list[str]:
    """The last ~max_bytes of a log as lines (a byte window, NOT a line count:
    one ingest dumps a thousand speaker-card lines and a fixed line window
    would miss every stage line). Degrades to [] when unreadable."""
    if path is None:
        return []
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
                fh.readline()  # the first remainder of a cut line
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def parse_stage_lines(lines: list[str]) -> list[tuple[dt.datetime, str]]:
    """Only the watcher's own timestamped stage lines
    (``...+ZZ:ZZ whosaid: msg``). Subprocess output that merely lands in the
    log (even timestamped, like ``2026-... chunk 8/8 done:``) never carries
    the ``whosaid:`` marker and is dropped here."""
    out: list[tuple[dt.datetime, str]] = []
    for line in lines:
        m = STAGE_LINE_RE.match(line.rstrip())
        if not m:
            continue
        try:
            out.append((dt.datetime.fromisoformat(m.group(1)), m.group(2)))
        except ValueError:
            continue
    return out


def fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours, minutes = divmod(seconds, 3600)
    return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"


def fmt_secs(seconds: int | None) -> str:
    """Seconds-first (the ⏳ waiting glyph reads '90s', not '1m')."""
    if seconds is None:
        return ""
    if seconds < 600:
        return f"{seconds}s"
    return fmt_age(seconds)


def classify(stage_lines: list[tuple[dt.datetime, str]],
             watcher_running: bool | None = True,
             now: dt.datetime | None = None) -> tuple[str, dict]:
    """(state, detail) from the watcher's stage lines ONLY. States: warn |
    ingesting | waiting | done | idle. The last recognized line wins; anything
    else (roll-up/index lines, seeded counts, lock notices) leaves the state
    where it was.

    watcher_running gates the ingest rule: an "> ingest" line with no live
    watcher process behind it means the agent died mid-ingest -> warn. None
    means unknown, which leaves ingesting alone."""
    now = now if now is not None else dt.datetime.now().astimezone()
    state, ts, msg = "idle", None, ""
    for line_ts, line_msg in stage_lines:
        m = line_msg.lstrip()
        if m.startswith("!"):
            state, ts, msg = "warn", line_ts, line_msg.strip()
        elif re.match(r"> \S+ ingest ", m):
            state, ts, msg = "ingesting", line_ts, m.split(" ingest ", 1)[1].strip()
        elif "ok ingested" in m:
            state, ts, msg = "done", line_ts, m.strip()
        elif m.startswith("done:"):
            state, ts, msg = "done", line_ts, m.strip()
        elif m.startswith("no new recordings"):
            # must win over the waiting rule: "no new recordings." contains
            # the words "new recording"
            state, ts, msg = "idle", line_ts, m.strip()
        elif "still syncing" in m or "waiting" in m or re.match(r"\d+ new recording", m):
            state, ts, msg = "waiting", line_ts, m.strip()
    if state == "ingesting" and watcher_running is False:
        state, msg = "warn", f"ingest started but no watcher process is running: {msg}"
    age = None if ts is None else max(0, int((now - ts).total_seconds()))
    if state == "done" and age is not None and age > DONE_HOLD_SECONDS:
        state, msg = "idle", f"done {fmt_age(age)} ago"
    detail = {"msg": msg, "age": age,
              "since": ts.isoformat(timespec="seconds") if ts is not None else None}
    return state, detail


def pick_glyph(state: str, detail: dict, loaded: bool | None,
               rec_title: str | None = None) -> tuple[str, str]:
    """(menu bar line, headline) from the composed signals. Precedence:
    warn > agent-not-loaded > REC > ingesting > waiting > done > idle."""
    age = detail.get("age")
    if state == "warn":
        return "⚠️", "needs a look"
    if loaded is False:
        return "🎙✗", "launch agent not loaded"
    if rec_title is not None:
        return "🔴 REC", f"recording: {rec_title}"
    if state == "ingesting":
        return f"⚙️ {fmt_age(age)}".rstrip(), "ingesting"
    if state == "waiting":
        return f"⏳ {fmt_secs(age)}".rstrip(), "waiting for the recording to settle"
    if state == "done":
        return "✅", "ingested"
    return "🎙", "watching"


def active_recording(rows: list[dict]) -> str | None:
    """The title of a store row with no synced file and 0s duration: a memo
    being recorded right now (REC). Plain-folder rows always have a file."""
    for r in rows:
        if not r.get("file") and not r.get("seconds") and not r.get("recently_deleted"):
            title = str(r.get("title") or "").strip()
            return title or "new memo"
    return None


def latest_meeting_folder(ws: Path) -> Path | None:
    """The greatest dated folder NAME (YYYY-MM-DD-HHMM) in the workspace, not
    the newest mtime: relabels and renames touch mtimes."""
    try:
        dated = [p for p in ws.iterdir() if p.is_dir() and MEETING_FOLDER_RE.match(p.name)]
    except OSError:
        return None
    return max(dated, key=lambda p: p.name) if dated else None


def encode_action(kind: str, arg: str = "") -> str:
    """A space- and pipe-free token for SwiftBar's paramN (base64url), so
    action arguments (paths with spaces) never break the menu line format."""
    return base64.urlsafe_b64encode(f"{kind}\x00{arg}".encode()).decode()


def decode_action(token: str) -> tuple[str, str]:
    kind, _, arg = base64.urlsafe_b64decode(token.encode()).decode().partition("\x00")
    return kind, arg


def applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify_transition(state_file: Path | None, state: str, message: str,
                      runner=None) -> bool:
    """Fire a notification once per TRANSITION into ingesting/done/warn,
    tracked as {"last": state} in a small JSON state file. Same state twice ->
    no notification; every state change (even to a silent one) updates the
    file so a later re-entry notifies again."""
    path = Path(state_file) if state_file else STATE_FILE
    last = None
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            last = data.get("last")
    except (OSError, ValueError):
        last = None
    if state == last:
        return False
    fired = False
    if state in NOTIFY_STATES:
        (runner or osascript_notify)("whosaid", message or state)
        fired = True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last": state}) + "\n")
    except OSError:
        pass
    return fired


# ---- side-effecting helpers ---------------------------------------------------------


def probe_dir(path: Path) -> bool:
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


def process_alive(pid: object) -> bool:
    """Whether a watcher PID is still live from SwiftBar's user session."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watcher_running(status: dict | None) -> bool | None:
    """True only for a loaded active/running launchd job with a live PID.

    ``None`` means status was unavailable. ``active`` and ``running`` are both
    valid launchd states; stopped and multiword states deliberately do not
    pass, even when an old log still contains an ingest line.
    """
    if status is None:
        return None
    if not status.get("loaded"):
        return False
    state = str(status.get("state") or "").strip().lower()
    if state not in {"active", "running"}:
        return False
    reported = status.get("pid_alive")
    return reported if isinstance(reported, bool) else process_alive(status.get("pid"))


def find_whosaid() -> str | None:
    """$WHOSAID_BIN > `whosaid` on PATH > the checkout beside contrib/swiftbar/."""
    env = os.environ.get("WHOSAID_BIN", "").strip()
    if env:
        return env
    found = shutil.which("whosaid")
    if found:
        return found
    repo = Path(__file__).resolve().parents[2] / "whosaid"
    return str(repo) if repo.exists() else None


def installed_labels(launch_agents: Path | None = None) -> list[str]:
    base = launch_agents if launch_agents is not None else Path.home() / "Library/LaunchAgents"
    try:
        return sorted(p.name[: -len(".plist")] for p in base.glob(f"{LABEL_PREFIX}*.plist"))
    except OSError:
        return []


def run_json(whosaid: str, args: list[str], timeout: float = 15.0) -> tuple[dict | None, str]:
    try:
        p = subprocess.run([whosaid, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"`whosaid {args[0]} {args[1]}` could not run: {e}"
    if p.returncode != 0:
        return None, f"`whosaid {args[0]} {args[1]}` exited {p.returncode}"
    try:
        return json.loads(p.stdout), None
    except ValueError:
        return None, f"`whosaid {args[0]} {args[1]}` printed no JSON"


def fetch_status(whosaid: str, ws: Path | None) -> tuple[dict | None, str]:
    if ws is not None:
        return run_json(whosaid, ["watch", "status", "--json", "--into", str(ws)])
    labels = installed_labels()
    if len(labels) == 1:
        return run_json(whosaid, ["watch", "status", "--json", "--label", labels[0]])
    if not labels:
        return None, "no WHOSAID_WORKSPACE set and no installed watcher agent"
    return None, "multiple watcher agents installed; set WHOSAID_WORKSPACE to pick one"


def osascript_notify(title: str, message: str) -> bool:
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    script = f"display notification {applescript_quote(message)} with title {applescript_quote(title)}"
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def notification_message(state: str, detail: dict) -> str:
    msg = str(detail.get("msg") or "")
    if state == "ingesting":
        return f"transcribing: {msg}"
    if state == "done":
        return msg or "ingested"
    if state == "warn":
        return msg or "needs a look"
    return ""


def perform_action(kind: str, arg: str) -> None:
    if kind == "open":
        subprocess.run(["open", arg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif kind == "taillog":
        inner = f"tail -n 200 -f {shlex.quote(arg)}"
        subprocess.run(["osascript", "-e",
                        f'tell application "Terminal" to do script {applescript_quote(inner)}'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif kind == "kickstart":
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{arg}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif kind == "relaunch":
        # a fresh Full Disk Access grant only applies after SwiftBar restarts,
        # and this plugin hides SwiftBar's own menu, so it must be relaunched
        subprocess.run(["killall", "SwiftBar"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["open", "-a", "SwiftBar"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def action_line(label: str, kind: str, arg: str = "", refresh: bool = False,
                self_path: str = "") -> str:
    parts = [f"shell={self_path}", f"param1={encode_action(kind, arg)}", "terminal=false"]
    if refresh:
        parts.append("refresh=true")
    return f"{label} | " + " ".join(parts)


# ---- rendering ----------------------------------------------------------------------


def render() -> str:
    now = dt.datetime.now().astimezone()
    env_ws = os.environ.get("WHOSAID_WORKSPACE", "").strip()
    ws = Path(env_ws).expanduser() if env_ws else None
    self_path = os.path.abspath(sys.argv[0])
    whosaid = find_whosaid()
    status: dict | None = None
    status_err = "whosaid not found (PATH or WHOSAID_BIN); the menu cannot see the watcher"
    if whosaid is not None:
        status, status_err = fetch_status(whosaid, ws)
    loaded: bool | None = None
    running: bool | None = None
    if status:
        loaded = bool(status.get("loaded"))
        running = watcher_running(status)
        if ws is None and status.get("workspace"):
            ws = Path(str(status["workspace"]))
    log_path = None
    if status and status.get("log"):
        log_path = Path(str(status["log"]))
    elif ws is not None:
        log_path = ws / LOG_NAME
    tail = tail_lines(log_path)
    state, detail = classify(parse_stage_lines(tail), watcher_running=running, now=now)

    rows: list[dict] = []
    if whosaid is not None:
        src = str(status.get("source")) if status and status.get("source") else None
        args = ["memos", "list", "--json"] + (["--source", src] if src else [])
        data, _err = run_json(whosaid, args, timeout=20)
        if isinstance(data, dict) and isinstance(data.get("recordings"), list):
            rows = data["recordings"]
    rec_title = active_recording(rows)

    bar, headline = pick_glyph(state, detail, loaded, rec_title)
    lines: list[str] = [bar, "---"]
    age = detail.get("age")
    age_bit = f" · {fmt_age(age)} ago" if age is not None else ""
    lines.append((f"{headline} · {detail['msg']}{age_bit}" if detail.get("msg") else headline))
    if status:
        run_bit = f"state={status.get('state') or 'n/a'}"
        exit_bit = (f" · last exit {status['last_exit_status']}"
                    if status.get("last_exit_status") is not None else " · last exit n/a")
        lines.append(f"watcher: {'loaded' if loaded else 'NOT loaded'} · {run_bit}{exit_bit}")
        lines.append(f"processed: {status.get('processed', 0)} recording(s), "
                     f"{status.get('seeded', 0)} seeded")
    else:
        lines.append(f"watcher: status unavailable · {status_err}")
    source = Path(str(status.get("source"))) if status and status.get("source") else VOICE_MEMOS_STORE
    reads = "?" if not status else ("reads it" if status.get("source_readable") else "CANNOT read it")
    lines.append(f"watcher source: {source} · {reads}")
    source_ok = probe_dir(source)
    voice_memos = source.expanduser() == VOICE_MEMOS_STORE or (source / "CloudRecordings.db").is_file()
    if source_ok:
        lines.append("SwiftBar reads the Voice Memos store (REC detection on)" if voice_memos
                     else "SwiftBar reads the configured recording source")
    elif voice_memos:
        lines.append(f"SwiftBar cannot read the Voice Memos store | color=red href={FDA_PANE_URL}")
        lines.append("grant SwiftBar Full Disk Access, then relaunch it below | color=red")
    else:
        lines.append("SwiftBar cannot read the configured recording source | color=red")
    if ws is not None:
        lines.append(f"workspace: {ws}")
    if log_path is not None:
        lines.append(f"log: {log_path}")

    lines.append("---")
    stage = parse_stage_lines(tail)
    if stage:
        lines.append("last stage lines:")
        for line_ts, msg in stage[-8:]:
            lines.append(f"{line_ts.strftime('%H:%M:%S')} {msg}".rstrip() + " | font=Menlo size=11")
    if state in ("ingesting", "waiting") and tail:
        lines.append("raw tail:")
        for raw in tail[-6:]:
            lines.append(raw[:160] + " | font=Menlo size=11")

    lines.append("---")
    if ws is not None and ws.is_dir():
        lines.append(action_line("Open workspace", "open", str(ws), self_path=self_path))
        latest = latest_meeting_folder(ws)
        if latest is not None:
            lines.append(action_line(f"Latest meeting: {latest.name}", "open", str(latest), self_path=self_path))
        else:
            lines.append("no dated meeting folder yet | color=#888888")
        for worklist in sorted(ws.glob("_WORKLIST-*.md")):
            owner = worklist.name[len("_WORKLIST-"): -len(".md")]
            lines.append(action_line(f"Worklist: {owner}", "open", str(worklist), self_path=self_path))
        wiki = ws / "_WIKI.md"
        if wiki.is_file():
            lines.append(action_line("Open _WIKI.md", "open", str(wiki), self_path=self_path))
    elif ws is not None:
        lines.append(f"workspace missing: {ws} | color=red")
    if log_path is not None:
        lines.append(action_line("Follow log in Terminal", "taillog", str(log_path), self_path=self_path))
    if status and status.get("label") and loaded:
        lines.append(action_line("Run watcher now", "kickstart", str(status["label"]),
                                 refresh=True, self_path=self_path))
    lines.append(action_line("Relaunch SwiftBar", "relaunch", "", refresh=True, self_path=self_path))
    lines.append("Refresh | href=swiftbar://refreshall")

    try:
        notify_transition(None, state, notification_message(state, detail))
    except Exception:  # noqa: BLE001  (a notification must never break the menu)
        pass
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        # SwiftBar action invocation: shell=<this script> param1=<token>
        try:
            perform_action(*decode_action(sys.argv[1]))
        except Exception as e:  # noqa: BLE001
            print(f"whosaid menubar action failed: {e}", file=sys.stderr)
        return 0
    try:
        out = render()
    except Exception as e:  # noqa: BLE001  (degrade to a readable line, never a traceback)
        out = "⚠️ | color=red\n---\nwhosaid menu bar plugin error: " + str(e).replace("\n", " ")
    sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
