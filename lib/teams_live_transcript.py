#!/usr/bin/env python3
"""
teams_live_transcript.py: the Teams desktop app's live transcript panel as a read-only live source,
plus one allow-listed action that only opens that panel (#242).

Both talk to the DevTools port `whosaid teams launch` sets up (--port, else $WHOSAID_TEAMS_CDP_PORT,
else 9333).

  source         Polls the panel rows ([data-tid^="transcript-chat-message-"]) every --interval
                 seconds with one Runtime.evaluate of a pure query and prints each row once, deduped
                 by row id (the last path segment of its data-tid). With --jsonl a row is
                 {"id","at","speaker","text","final"} and a status change is {"status","at"}
                 (status: ok | panel closed | window not found), printed once per change, plus
                 {"gap","at","after","before"} when a poll shares no row with the rows already
                 seen (the list is virtualized: rows that scrolled past between polls are lost,
                 and the gap says so instead of hiding it). Never sends an input event: the CDP
                 wrapper it uses refuses every method but Runtime.evaluate.
  open-transcript
                 Clicks only the control that shows the transcript panel, by exact accessible name
                 from an allow-list (OPEN_NAMES), going through the call's More menu and its
                 "Record and transcribe" submenu when the control is not on the toolbar (MENU_PATH,
                 also exact names). Every click goes through click_allowed(): it hit-tests the
                 point, takes the control there, refuses it when any of its labels matches DENY
                 (stop, leave, hang up, mute, unmute, end, turn off, hide, close, start recording,
                 ...) or when its name is not on this hop's allow-list, and only then sends one
                 mouse press. Refused when the panel is already open (it never toggles it shut).

Caveats (see the issue): speaker names are mostly absent (the author element is on some rows only,
often empty; speaker is "" then, and the recorder's diarization stays the source of truth); the list
is virtualized (dedupe by id, gaps reported); the panel closes on rejoin (status "panel closed", run
open-transcript); page ids change across rejoins, so the call window is found again by title (and a
read-only probe for the call's hang-up control) on every reconnect, never cached by id.

Selectors: CHECKED = seen in a live Teams desktop call (the row, author and text data-tids, from the
#242 prototype); UNVERIFIED = a guess until a live call confirms it (panel container, hang-up probe,
control names). Override the window match with --title SUBSTRING.

Usage:
  teams_live_transcript.py source [--port P] [--title T] [--interval S] [--jsonl] [--once]
                                  [--max-seconds N] [--no-settle]
  teams_live_transcript.py open-transcript [--port P] [--title T] [--dry-run]

Exit codes: 0 ok / opened; 2 usage; 3 refused: panel already open; 4 refused by the allow-list or
deny-list; 5 no call window or no open control found; 6 cannot reach the DevTools port.
Runtime deps: stdlib + websockets (`uv run --with websockets`).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import signal
import sys
import time
import urllib.request
from datetime import datetime, timezone

DEFAULT_PORT = 9333

# ---- selectors ----------------------------------------------------------------------------------
ROW_SEL = '[data-tid^="transcript-chat-message-"]'          # CHECKED
AUTHOR_SEL = '[data-tid="transcript-item-author"]'          # CHECKED (present on some rows only)
WRAPPER_SEL = '[data-tid="transcript-message-wrapper"]'     # CHECKED
TEXT_SEL = '[data-tid="call-transcript-panel-message"]'     # CHECKED
# The panel itself, open but still empty. UNVERIFIED.
PANEL_SEL = ('[data-tid="call-transcript-panel"],[data-tid="transcript-panel"],'
             '[role="region"][aria-label="Transcript" i],[role="region"][aria-label="Live transcript" i]')
# Proof the page is a call window (read-only probe). UNVERIFIED.
IN_CALL_SEL = '[data-tid="call-hangup"],#hangup-button,[data-tid="hangup-main-btn"]'
CONTROL_SEL = 'button,[role="button"],[role="menuitem"],[role="menuitemcheckbox"],[role="menuitemradio"],[role="tab"]'

# ---- the allow-list: exact accessible names, lowercased, whitespace collapsed --------------------
OPEN_NAMES = ("show transcript", "view transcript", "open transcript", "show live transcript",
              "view live transcript")                       # UNVERIFIED wording
MENU_PATH = (("more", "more actions", "more options"),       # the call toolbar's More button
             ("record and transcribe", "recording and transcription"))  # its submenu opener
# Checked before every click, over every label of the control at the point, even an allow-listed one.
DENY = re.compile(r"\b(stop|leave|hang\s*-?\s*up|mute|unmute|end|ends|ending|turn\s+off|hide|close|"
                  r"start\s+recording|start\s+transcription|start\s+transcribing|disable|remove|"
                  r"camera|video|share|raise|react)\b", re.I)


class LiveError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


# ---- the read (a pure query) ----------------------------------------------------------------------
JS_READ = ("(()=>{const q=s=>document.querySelector(s);"
           f"const rows=[...document.querySelectorAll({json.dumps(ROW_SEL)})].map(m=>{{"
           "const id=(m.getAttribute('data-tid')||'').split('/').pop();"
           f"const a=m.querySelector({json.dumps(AUTHOR_SEL)})||"
           f"m.closest({json.dumps(WRAPPER_SEL)})?.querySelector({json.dumps(AUTHOR_SEL)});"
           f"const t=m.querySelector({json.dumps(TEXT_SEL)})||m;"
           "return {id,author:a?(a.innerText||'').trim():'',text:(t.innerText||'').replace(/\\s+/g,' ').trim()};});"
           f"return {{panel:rows.length>0||!!q({json.dumps(PANEL_SEL)}),inCall:!!q({json.dumps(IN_CALL_SEL)}),"
           "title:document.title,rows};})()")


class Tracker:
    """Turns one poll's read into events: rows once each by id, status on change only, gaps.

    read is None when no call window was found, else {"panel", "rows": [{id, author, text}]}.
    settle=True holds a row until its text is unchanged across two polls (the newest line can still
    grow under the same id) or until it leaves the DOM, then emits it once."""

    def __init__(self, settle: bool = True):
        self.settle = settle
        self.seen: set[str] = set()
        self.pending: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.status: str | None = None
        self.last_id: str | None = None

    def _status(self, s: str, at: str) -> list[dict]:
        if s == self.status:
            return []
        self.status = s
        return [{"status": s, "at": at}]

    def _row(self, r: dict) -> dict:
        self.seen.add(r["id"])
        self.last_id = r["id"]
        return {"id": r["id"], "at": r["at"], "speaker": r.get("author") or "", "text": r["text"], "final": True}

    def flush(self) -> list[dict]:
        out = [self._row(r) for r in self.pending.values()]
        self.pending.clear()
        return out

    def step(self, read: dict | None, at: str | None = None) -> list[dict]:
        at = at or now_iso()
        if read is None:
            return self.flush() + self._status("window not found", at)
        if not read.get("panel"):
            return self.flush() + self._status("panel closed", at)
        out = self._status("ok", at)
        rows, ids = [], set()
        for r in read.get("rows") or []:
            rid, text = str(r.get("id") or ""), str(r.get("text") or "")
            if rid and text and rid not in ids:
                ids.add(rid)
                rows.append({"id": rid, "author": str(r.get("author") or ""), "text": text})
        fresh = [r for r in rows if r["id"] not in self.seen]
        known = self.seen | set(self.pending)
        if known and fresh and not (ids & known):
            out.append({"gap": True, "at": at, "after": self.last_id, "before": fresh[0]["id"]})
        # rows that left the DOM before they settled: emit what was last read
        for rid in [k for k in self.pending if k not in ids]:
            out.append(self._row(self.pending.pop(rid)))
        blocked = False
        for r in fresh:
            prev = self.pending.get(r["id"])
            if not self.settle or (prev is not None and prev["text"] == r["text"] and not blocked):
                self.pending.pop(r["id"], None)
                out.append(self._row({**r, "at": prev["at"] if prev else at}))
                continue
            blocked = True  # keep DOM order: nothing after an unsettled row goes out before it
            self.pending[r["id"]] = {**r, "at": prev["at"] if prev else at}
        return out


# ---- CDP --------------------------------------------------------------------------------------------
def http_json(port: int, path: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001 - any failure is "port down"
        raise LiveError(f"cannot reach the Teams DevTools port {port}: {e}; run `whosaid teams launch`", 6) from e


class ReadOnlyCDP:
    """One page's websocket. Only Runtime.evaluate leaves this process (the source never clicks)."""

    ALLOWED = frozenset({"Runtime.evaluate"})

    def __init__(self, ws_url: str, timeout: float = 10.0):
        from websockets.sync.client import connect
        try:
            self.ws = connect(ws_url, max_size=None, open_timeout=timeout).__enter__()
        except Exception as e:  # noqa: BLE001
            raise LiveError(f"cannot open the page: {e}", 6) from e
        self.seq = 0
        self.timeout = timeout
        self.sent: list[str] = []

    def guard(self, method: str) -> None:
        if method not in self.ALLOWED:
            raise LiveError(f"refusing {method}: the transcript source is read-only", 4)

    def send(self, method: str, params: dict | None = None) -> dict:
        self.guard(method)
        self.seq += 1
        seq = self.seq
        self.sent.append(method)
        deadline = time.monotonic() + self.timeout
        try:
            self.ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LiveError(f"{method}: no reply in {self.timeout:.0f}s", 6)
                msg = json.loads(self.ws.recv(timeout=left))
                if msg.get("id") == seq:
                    break
        except LiveError:
            raise
        except Exception as e:  # noqa: BLE001 - closed socket, timeout
            raise LiveError(f"{method}: {type(e).__name__}: {e}", 6) from e
        if msg.get("error"):
            raise LiveError(f"{method}: {msg['error'].get('message')}", 6)
        return msg.get("result") or {}

    def ev(self, expression: str):
        r = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            ex = r["exceptionDetails"]
            raise LiveError((ex.get("exception") or {}).get("description") or ex.get("text", "script error"), 6)
        return (r.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def find_window(port: int, title: str | None, cdp_class=ReadOnlyCDP):
    """Find the call window again (page ids change across rejoins): page targets whose title holds
    `title` (any page when None), each probed read-only; the first with the panel open wins, else the
    first that is in a call, else (only with an explicit title) the first title match.
    Returns (cdp, read) or (None, None)."""
    pages = [p for p in http_json(port, "/json/list") if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
    if title:
        pages = [p for p in pages if norm(title) in norm(p.get("title"))]
    best = None
    for p in pages:
        try:
            c = cdp_class(p["webSocketDebuggerUrl"])
        except LiveError:
            continue
        try:
            read = c.ev(JS_READ)
        except LiveError:
            c.close()
            continue
        if not isinstance(read, dict):
            c.close()
            continue
        rank = 0 if read.get("panel") else 1 if read.get("inCall") else 2 if title else None
        if rank is None or (best and best[0] <= rank):
            c.close()
            continue
        if best:
            best[1].close()
        best = (rank, c, read)
        if rank == 0:
            break
    return (best[1], best[2]) if best else (None, None)


# ---- the source ---------------------------------------------------------------------------------------
def format_event(e: dict, jsonl: bool) -> str:
    if jsonl:
        return json.dumps(e, ensure_ascii=False)
    clock = e["at"][11:19]
    if "status" in e:
        return f"[{clock}] source: teams-transcript {e['status']}"
    if e.get("gap"):
        return f"[{clock}] gap: rows may have scrolled past unread"
    return f"[{clock}] {e['speaker'] or '?'}: {e['text']}"


def run_source(port: int, title: str | None, interval: float, jsonl: bool, once: bool = False,
               max_seconds: float = 0, settle: bool = True, out=None, sleep=time.sleep, clock=time.monotonic,
               cdp_class=ReadOnlyCDP) -> int:
    out = out or sys.stdout
    tr = Tracker(settle=settle and not once)
    cdp = None
    stop = [False]
    start = clock()

    def emit(evs):
        for e in evs:
            out.write(format_event(e, jsonl) + "\n")
        out.flush()

    def on_signal(*_):
        stop[0] = True

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, on_signal)
        except ValueError:  # not the main thread (tests)
            pass
    try:
        while not stop[0]:
            read = None
            if cdp is not None:
                try:
                    read = cdp.ev(JS_READ)
                except LiveError:
                    cdp.close()
                    cdp = None
            if cdp is None:  # (re)connect: find the window again by title, never by a cached page id
                try:
                    cdp, read = find_window(port, title, cdp_class)
                except LiveError:  # the app restarting on a rejoin: keep polling, report it
                    cdp, read = None, None
            emit(tr.step(read if isinstance(read, dict) else None))
            if once or (max_seconds and clock() - start >= max_seconds):
                break
            sleep(interval)
    finally:
        emit(tr.flush())
        if cdp is not None:
            cdp.close()
    return 0


# ---- the action -----------------------------------------------------------------------------------------
class ActionCDP(ReadOnlyCDP):
    """ReadOnlyCDP plus mouse presses, and only from click_allowed(). Never a key or text input."""

    def __init__(self, ws_url: str, timeout: float = 10.0):
        super().__init__(ws_url, timeout)
        self._armed = False

    def guard(self, method: str) -> None:
        if method == "Input.dispatchMouseEvent" and self._armed:
            return
        super().guard(method)


# The control at a point and every label it carries (its own and its control ancestor's).
JS_AT = ("((x,y)=>{const e=document.elementFromPoint(x,y);if(!e)return null;"
         f"const c=e.closest({json.dumps(CONTROL_SEL)})||e;"
         "const L=n=>[n.getAttribute('aria-label'),n.getAttribute('title'),n.getAttribute('data-tid'),n.innerText];"
         "return {name:(c.getAttribute('aria-label')||c.getAttribute('title')||c.innerText||'').replace(/\\s+/g,' ').trim(),"
         "pressed:c.getAttribute('aria-pressed')==='true'||c.getAttribute('aria-checked')==='true',"
         "labels:[...L(c),...L(e)].filter(Boolean)};})(%s,%s)")

# Visible controls whose exact accessible name is in a list, with their centers.
JS_FIND = ("((names)=>{const out=[];"
           f"for(const c of document.querySelectorAll({json.dumps(CONTROL_SEL)})){{"
           "const n=(c.getAttribute('aria-label')||c.getAttribute('title')||c.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase();"
           "if(!names.includes(n))continue;const r=c.getBoundingClientRect();if(!r.width||!r.height)continue;"
           "out.push({name:n,x:r.x+r.width/2,y:r.y+r.height/2});}return out;})(%s)")


def denied(labels) -> str | None:
    for lab in labels or []:
        m = DENY.search(str(lab))
        if m:
            return f"{str(lab).strip()[:60]!r} matches the deny-list ({m.group(0)!r})"
    return None


def click_allowed(cdp: ActionCDP, x: float, y: float, allowed: tuple[str, ...]) -> str:
    """The one place a mouse event is sent. Refuses (LiveError code 4, zero events) unless the
    control at (x, y) has an exact allow-listed name for this hop and no label on the deny-list."""
    at = cdp.ev(JS_AT % (json.dumps(x), json.dumps(y)))
    if not isinstance(at, dict):
        raise LiveError(f"refusing to click: nothing at ({x:.0f}, {y:.0f})", 4)
    why = denied(at.get("labels"))
    if why:
        raise LiveError(f"refusing to click: {why}", 4)
    name = norm(at.get("name"))
    if name not in allowed:
        raise LiveError(f"refusing to click {at.get('name')!r}: not on the allow-list {list(allowed)}", 4)
    if at.get("pressed"):
        raise LiveError(f"refusing to click {at.get('name')!r}: it reads as on, a click would toggle it off", 3)
    cdp._armed = True
    try:
        for typ in ("mousePressed", "mouseReleased"):
            cdp.send("Input.dispatchMouseEvent", {"type": typ, "x": x, "y": y, "button": "left", "clickCount": 1})
    finally:
        cdp._armed = False
    return name


def open_transcript(port: int, title: str | None, dry_run: bool = False, sleep=time.sleep,
                    wait: float = 5.0, cdp_class=ActionCDP) -> dict:
    cdp, read = find_window(port, title, cdp_class)
    if cdp is None:
        raise LiveError("no Teams call window found" + (f" with {title!r} in its title" if title else "")
                        + "; pass --title to pick one", 5)
    try:
        if read.get("panel"):
            raise LiveError("refusing: the transcript panel is already open (a click would close it)", 3)
        clicked: list[str] = []
        for _ in range(len(MENU_PATH) + 1):
            direct = cdp.ev(JS_FIND % json.dumps(list(OPEN_NAMES))) or []
            if direct:
                if dry_run:
                    return {"ok": True, "action": "dry-run", "would_click": clicked + [direct[0]["name"]]}
                clicked.append(click_allowed(cdp, direct[0]["x"], direct[0]["y"], OPEN_NAMES))
                break
            nxt = None
            for names in MENU_PATH:
                if any(n in clicked for n in names):
                    continue
                found = cdp.ev(JS_FIND % json.dumps(list(names))) or []
                if found:
                    nxt = (found[0], names)
                    break
            if nxt is None:
                raise LiveError("no allow-listed control to show the transcript was found"
                                + (f" (after {clicked})" if clicked else ""), 5)
            if dry_run:
                clicked.append(nxt[0]["name"])
                continue
            clicked.append(click_allowed(cdp, nxt[0]["x"], nxt[0]["y"], nxt[1]))
            sleep(0.6)
        else:
            raise LiveError(f"the transcript control did not appear after {clicked}", 5)
        if dry_run:
            return {"ok": True, "action": "dry-run", "would_click": clicked}
        end = time.monotonic() + wait
        while True:
            after = cdp.ev(JS_READ) or {}
            if after.get("panel"):
                return {"ok": True, "action": "opened", "clicked": clicked}
            if time.monotonic() >= end:
                return {"ok": False, "action": "clicked", "clicked": clicked,
                        "error": "clicked, but the panel did not show within the wait"}
            sleep(0.3)
    finally:
        cdp.close()


# ---- CLI ---------------------------------------------------------------------------------------------------
def resolve_port(arg) -> int:
    raw = arg if arg is not None else os.environ.get("WHOSAID_TEAMS_CDP_PORT")
    if raw in (None, ""):
        return DEFAULT_PORT
    try:
        p = int(raw)
    except ValueError:
        raise LiveError(f"bad port {raw!r}", 2)
    if not 0 < p < 65536:
        raise LiveError(f"bad port {raw!r}", 2)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="whosaid teams", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("source", prog="whosaid teams transcript-live", help="read-only live source: new transcript rows, once each")
    o = sub.add_parser("open-transcript", prog="whosaid teams open-transcript", help="click only the control that shows the transcript panel")
    for p in (s, o):
        p.add_argument("--port", help="DevTools port (default $WHOSAID_TEAMS_CDP_PORT, else 9333)")
        p.add_argument("--title", help="substring of the call window's title (re-matched on every reconnect)")
    s.add_argument("--interval", type=float, default=2.0, help="seconds between polls (default 2)")
    s.add_argument("--jsonl", action="store_true", help='one JSON object per line: rows {"id","at","speaker","text","final"}, '
                   'status {"status","at"}, gaps {"gap","at","after","before"}')
    s.add_argument("--once", action="store_true", help="one poll, every row in view, then exit")
    s.add_argument("--max-seconds", type=float, default=0, help="stop after N seconds (default: until interrupted)")
    s.add_argument("--no-settle", action="store_true", help="emit a row on first sight, not once its text stops changing")
    o.add_argument("--dry-run", action="store_true", help="find the controls, click nothing")
    a = ap.parse_args(argv)
    try:
        port = resolve_port(a.port)
        if a.cmd == "source":
            if a.interval <= 0:
                raise LiveError("--interval must be > 0", 2)
            return run_source(port, a.title, a.interval, a.jsonl, once=a.once, max_seconds=a.max_seconds,
                              settle=not a.no_settle)
        res = open_transcript(port, a.title, dry_run=a.dry_run)
        print(json.dumps(res))
        return 0 if res.get("ok") else 5
    except LiveError as e:
        if a.cmd == "open-transcript":
            print(json.dumps({"ok": False, "action": "refused" if e.code in (3, 4) else "failed", "error": str(e)}))
        print(f"whosaid teams {a.cmd}: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
