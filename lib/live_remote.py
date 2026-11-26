#!/usr/bin/env python3
"""
live_remote.py: share a live session with another Mac (`whosaid live serve` / `live pull`, #244).

  serve  On the Mac with the call audio: serve <into>/.live/ read-only over HTTP.
           GET /sessions                       the sessions: stem, started, updated, ended, lines
           GET /sessions/<stem>?cursor=C&wait=S the lines after cursor C (the same cursors
                                               `live follow` uses) plus the next cursor; with
                                               wait=S (at most 30) it holds the request until a
                                               new line arrives, the call ends or S seconds pass
         It binds one address (default 127.0.0.1), never a wildcard. Every request needs
         `Authorization: Bearer <token>` (the token file, default ~/.config/whosaid/live-token);
         without a token file it serves loopback only and refuses to start on any other address.
         Nothing changes state: GET only, and only transcript text and metadata. Audio and
         voiceprints stay on this Mac.
  pull   On the reading Mac: mirror the remote's newest session into the local
         <into>/.live/<stem>.md and .jsonl, so `live show`, `live follow`, the MCP tool and the
         explainer read it like any live session. The mirror's meta says
         "source": "remote:<url>". Lines keep the remote's times; a dropped connection
         resumes from the last cursor (no duplicate line); each new connection, every
         --resync seconds and the end of the call fetch the whole session again, so a name a
         voice got later reaches the earlier lines too. When the call ends the mirror gets
         the `ended` marker, then pull waits for the next call (--once: exits).
  token  Create the token file (0600) if it is missing and print the token, to copy into the
         same file on the other Mac.

stdlib only.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live  # noqa: E402

DEFAULT_PORT = 8766
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "whosaid" / "live-token"
MAX_WAIT = 30.0
STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} live: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- token

def token_path(arg: str | None) -> Path:
    return Path(arg or os.environ.get("WHOSAID_LIVE_TOKEN_FILE") or DEFAULT_TOKEN_FILE).expanduser()


def read_token(path: Path) -> str | None:
    try:
        tok = path.read_text().strip()
    except FileNotFoundError:
        return None
    return tok or None


def ensure_token(path: Path) -> str:
    tok = read_token(path)
    if tok:
        return tok
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


# ---------------------------------------------------------------- server

def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, token: str | None) -> str | None:
    """Why serve must not start on `host`, or None. Never a wildcard; off loopback, a token."""
    if host in ("", "0.0.0.0", "::", "*"):
        return f"refusing to bind {host or 'every interface'!r}: pass --bind with one address (a VPN or LAN IP)"
    if not token and not is_loopback(host):
        return (f"refusing to serve {host} without a token: run `whosaid live token` and copy the "
                "token file to the other Mac")
    return None


def session_list(into: Path) -> list[dict]:
    out = []
    for jl in live.sessions(into):
        try:
            meta, entries = live.read_entries(jl)
        except (OSError, ValueError):
            continue
        out.append({"stem": jl.stem, "started": meta.get("started"), "updated": meta.get("updated"),
                    "ended": bool(meta.get("ended")), "lines": len(entries)})
    return out


def session_after(into: Path, stem: str, cursor: str | None) -> dict:
    """The lines after `cursor` in one session, the next cursor and the session's meta.
    Raises FileNotFoundError (no such session) or ValueError (bad cursor)."""
    jl = into / live.LIVE_DIR / f"{stem}.jsonl"
    meta, entries = live.read_entries(jl)
    cur = live.parse_cursor(cursor) if cursor else None
    new, nxt, reset = live.turns_after(entries, stem, cur)
    picked = [e for e, _ in new] if cur is not None else entries
    return {"stem": stem, "meta": {k: meta.get(k) for k in ("started", "ended", "updated")},
            "entries": picked, "cursor": nxt, "reset": reset, "full": cur is None}


def make_handler(into: Path, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "whosaid-live"

        def log_message(self, fmt, *args):  # quiet: one line per refusal only
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = (json.dumps(obj) + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not token:
                return is_loopback(self.client_address[0])
            got = self.headers.get("Authorization", "")
            if not got.startswith("Bearer "):
                return False
            return hmac.compare_digest(got[len("Bearer "):].strip().encode(), token.encode())

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                log(f"serve: refused {self.client_address[0]} (no or wrong token)")
                return self._send(401, {"ok": False, "error": "unauthorized: send Authorization: Bearer <token>"})
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            parts = [p for p in u.path.split("/") if p]
            if parts == ["sessions"]:
                return self._send(200, {"ok": True, "sessions": session_list(into)})
            if len(parts) == 2 and parts[0] == "sessions":
                stem = parts[1]
                if not STEM_RE.match(stem):
                    return self._send(400, {"ok": False, "error": "bad session name"})
                cursor = (q.get("cursor") or [None])[0]
                try:
                    wait = min(max(float((q.get("wait") or ["0"])[0]), 0.0), MAX_WAIT)
                except ValueError:
                    return self._send(400, {"ok": False, "error": "wait must be a number"})
                deadline = time.monotonic() + wait
                while True:
                    try:
                        r = session_after(into, stem, cursor)
                    except FileNotFoundError:
                        return self._send(404, {"ok": False, "error": f"no session {stem}"})
                    except ValueError as err:
                        return self._send(400, {"ok": False, "error": str(err)})
                    if r["entries"] or r["meta"]["ended"] or r["reset"] or time.monotonic() >= deadline:
                        return self._send(200, {"ok": True, **r})
                    time.sleep(0.5)
            return self._send(404, {"ok": False, "error": "not found: GET /sessions or /sessions/<stem>"})

        def _read_only(self):
            self._send(405, {"ok": False, "error": "read-only: GET only"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _read_only  # noqa: N815

    return Handler


def start_server(into: Path, host: str, port: int, token: str | None) -> ThreadingHTTPServer:
    """Bind and serve in a daemon thread; the caller has run check_bind."""
    srv = ThreadingHTTPServer((host, port), make_handler(into, token))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="live-serve").start()
    h, p = srv.server_address[:2]
    log(f"serving {into / live.LIVE_DIR} read-only on http://{h}:{p} "
        f"({'token required' if token else 'loopback only, no token'})")
    return srv


# ---------------------------------------------------------------- client (pull)

class Unauthorized(Exception):
    pass


def base_url(src: str) -> str:
    return src.rstrip("/") if "://" in src else f"http://{src}"


def get_json(url: str, token: str | None, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as err:
        if err.code == 401:
            raise Unauthorized(f"{url}: 401 unauthorized (no or wrong token)") from None
        raise


def merge(entries: list[dict], new: list[dict]) -> int:
    """Add the lines of `new` that `entries` lacks (by n, else by t/who/text); returns how many."""
    have = {e["n"] for e in entries if isinstance(e.get("n"), int)}
    seen = {(e.get("t"), e.get("who"), e.get("text")) for e in entries}
    added = 0
    for e in new:
        k = e.get("n")
        if isinstance(k, int) and k in have or not isinstance(k, int) and (e.get("t"), e.get("who"), e.get("text")) in seen:
            continue
        entries.append(e)
        added += 1
    return added


def write_mirror(into: Path, stem: str, meta: dict, entries: list[dict], source: str) -> None:
    d = into / live.LIVE_DIR
    started = float(meta.get("started") or 0.0)
    live.write_session(d / f"{stem}.md", d / f"{stem}.jsonl", started, entries, bool(meta.get("ended")),
                       extra={"source": source, "remote_updated": meta.get("updated")})


def newest_stem(url: str, token: str | None, timeout: float) -> tuple[str | None, bool]:
    ss = get_json(f"{url}/sessions", token, timeout).get("sessions") or []
    if not ss:
        return None, False
    s = max(ss, key=lambda s: s["stem"])
    return s["stem"], bool(s.get("ended"))


def pull(src: str, into: Path, token: str | None, stem: str | None = None, once: bool = False,
         wait: float = 25.0, resync: float = 60.0, max_hours: float = 12.0, stop=lambda: False) -> int:
    """Mirror the remote's sessions into <into>/.live until stopped; 0 ok, 2 unauthorized."""
    url = base_url(src)
    source = f"remote:{url}"
    deadline = time.monotonic() + max_hours * 3600
    done: set[str] = set()
    backoff = 1.0
    cur_stem, entries, cursor, meta, last_full = None, [], None, {}, 0.0
    logged_wait = False
    while not stop() and time.monotonic() < deadline:
        try:
            if cur_stem is None:
                s, ended = (stem, False) if stem else newest_stem(url, token, 15)
                if not s or s in done or (ended and not stem and done):
                    if not logged_wait:
                        log(f"pull: no new session on {url}; waiting")
                        logged_wait = True
                    time.sleep(min(wait, 5.0))
                    continue
                cur_stem, entries, cursor, logged_wait = s, [], None, False
                log(f"pull: mirroring {s} from {url}")
            full = cursor is None or time.monotonic() - last_full >= resync
            q = {"wait": "0" if full else str(wait)}
            if not full:
                q["cursor"] = cursor
            r = get_json(f"{url}/sessions/{cur_stem}?{urllib.parse.urlencode(q)}", token, wait + 15)
            backoff = 1.0
            if r.get("full") or r.get("reset"):
                entries = list(r["entries"])  # the whole session: later names reach earlier lines
                last_full = time.monotonic()
                changed = True
            else:
                changed = merge(entries, r["entries"]) > 0
            cursor = r["cursor"]
            if changed or r["meta"] != meta or not (into / live.LIVE_DIR / f"{cur_stem}.jsonl").exists():
                if r["meta"].get("ended") and not (r.get("full") or r.get("reset")):
                    cursor = None  # one last whole fetch before marking it ended
                    meta = {**r["meta"], "ended": False}
                    write_mirror(into, cur_stem, meta, entries, source)
                    continue
                meta = r["meta"]
                write_mirror(into, cur_stem, meta, entries, source)
            if meta.get("ended"):
                log(f"pull: {cur_stem} ended; {len(entries)} line(s)")
                done.add(cur_stem)
                if once or stem:
                    return 0
                cur_stem = None
        except Unauthorized as err:
            log(f"pull: {err}")
            return 2
        except (urllib.error.URLError, OSError, ValueError) as err:
            log(f"pull: {url}: {err}; retrying in {backoff:.0f}s from the last cursor")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    return 0


# ---------------------------------------------------------------- CLI

def cmd_token(a: argparse.Namespace) -> int:
    p = token_path(a.token_file)
    existed = read_token(p) is not None
    tok = ensure_token(p)
    print(tok)
    print(f"# {'existing' if existed else 'new'} token in {p} (mode 600); put the same line in that file on the other Mac",
          file=sys.stderr)
    return 0


def cmd_serve_only(a: argparse.Namespace) -> int:
    srv = prepare_server(a)
    if srv is None:
        return 2
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def prepare_server(a: argparse.Namespace) -> ThreadingHTTPServer | None:
    token = read_token(token_path(a.token_file))
    why = check_bind(a.bind, token)
    if why:
        log(f"serve: {why}")
        return None
    try:
        return start_server(Path(a.into).expanduser(), a.bind, a.port, token)
    except OSError as err:
        log(f"serve: cannot bind {a.bind}:{a.port}: {err}")
        return None


def cmd_pull(a: argparse.Namespace) -> int:
    p = token_path(a.token_file)
    token = read_token(p)
    host = urllib.parse.urlsplit(base_url(a.src)).hostname or ""
    if not token and not is_loopback(host):
        log(f"pull: no token in {p}; copy it from the serving Mac (`whosaid live token` there)")
        return 2
    try:
        return pull(a.src, Path(a.into).expanduser(), token, stem=a.session, once=a.once, wait=a.wait,
                    resync=a.resync, max_hours=a.max_hours)
    except KeyboardInterrupt:
        return 0


def add_serve_args(q: argparse.ArgumentParser) -> None:
    q.add_argument("--bind", default="127.0.0.1",
                   help="the one address to listen on, e.g. this Mac's VPN or LAN IP (default 127.0.0.1; "
                   "never a wildcard)")
    q.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    q.add_argument("--token-file", help=f"bearer token file (default $WHOSAID_LIVE_TOKEN_FILE or {DEFAULT_TOKEN_FILE})")
    q.add_argument("--serve-only", action="store_true",
                   help="only serve the sessions already being written (another `live run`, autorecord); "
                   "do not start `live run` here")


def add_pull_args(q: argparse.ArgumentParser) -> None:
    q.add_argument("--from", dest="src", required=True, metavar="HOST:PORT", help="the Mac running `live serve`")
    q.add_argument("--token-file", help=f"bearer token file (default $WHOSAID_LIVE_TOKEN_FILE or {DEFAULT_TOKEN_FILE})")
    q.add_argument("--session", metavar="STEM", help="mirror this session, then exit (default: the newest, then the next)")
    q.add_argument("--once", action="store_true", help="exit after one call ends")
    q.add_argument("--wait", type=float, default=25.0, help="long-poll seconds per request (default 25, max 30)")
    q.add_argument("--resync", type=float, default=60.0,
                   help="fetch the whole session every S seconds, for names a voice got later (default 60)")
    q.add_argument("--max-hours", type=float, default=12.0)


if __name__ == "__main__":
    sys.exit(live.main())
