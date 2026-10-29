#!/usr/bin/env python3
"""
record_browser.py: record a browser meeting tab over the Chrome DevTools
Protocol, for `whosaid record --browser` (GitHub issue #118).

`whosaid record --app` needs the Screen & System Audio Recording grant for the
terminal app, which agent and headless sessions rarely have. A meeting running
in a Chrome started with --remote-debugging-port needs no grant: this script
connects to the tab's CDP websocket and injects a small recorder into the page.

  - left  = the local mic, via getUserMedia (the meeting origin already holds
            mic permission, so there is no prompt)
  - right = every <audio>/<video> element whose srcObject is a MediaStream,
            mixed into one bus and re-scanned on each poll (meeting apps swap
            elements mid-call)

A MediaRecorder writes webm/opus in 1 s slices. Chunks are drained over
Runtime.evaluate as base64 every --drain-seconds and appended to a temp .webm,
so a tab crash loses only the undrained tail. The tab is polled for a
provider-specific "in call" element (the hang-up button); once seen, two
consecutive misses mean the call ended. Finalize converts the webm with ffmpeg
to a sibling temp name that has no audio suffix, then os.replace()s it onto
--out, so `whosaid watch` never picks up a half-written file. The stereo file
carries the same comment tag `record --app` writes (lib/stereo_channels.py
CHANNEL_TAG), so mic-vs-app attribution works on it unchanged.

Usage:
  record_browser.py --out PATH.m4a [--cdp-host 127.0.0.1] [--cdp-port 9222]
                    [--target TARGETID|URL-SUBSTRING] [--leave-selector CSS]
                    [--max-seconds N] [--no-mic] [--leave]
                    [--poll-seconds 2] [--drain-seconds 2] [--arm-seconds 60]
                    [--wait-for-call [--wait-poll-seconds 4]]
  record_browser.py --list-targets [--cdp-host H] [--cdp-port P]

--wait-for-call (#121) first waits, polling every --wait-poll-seconds, until a
page on a known meeting host (or the --target match) shows its in-call
element; an unreachable CDP endpoint counts as "not yet" (the browser may not
be open). A signal during the wait exits 0 with no file.

Exit codes: 0 finalized (call-ended / max-seconds / interrupted); 2 usage;
3 tab closed or CDP failed mid-run, drained audio finalized to --out;
6 cannot reach CDP or target selection failed; 7 nothing captured (no --out).

Runtime deps: stdlib + websockets (`uv run --with websockets`), ffmpeg/ffprobe.
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

# Must equal stereo_channels.CHANNEL_TAG. Not imported: that module pulls numpy,
# and this script runs with websockets only. The test asserts they match.
CHANNEL_TAG = "whosaid:channels=mic,app"

EXIT_OK, EXIT_USAGE, EXIT_TAB_CLOSED, EXIT_NO_CDP, EXIT_EMPTY = 0, 2, 3, 6, 7
STOP_REASONS = ("call-ended", "max-seconds", "interrupted", "tab-closed")

# Host suffix -> CSS for an element that only exists while in a call (the
# hang-up control). Matched on the URL's host, as a suffix, so subdomains such
# as us05web.zoom.us hit the Zoom row.
PROVIDERS = (
    ("teams.microsoft.com", '#hangup-button,[data-tid="hangup-main-btn"],[data-tid="call-hangup"]'),
    ("teams.live.com", '#hangup-button,[data-tid="hangup-main-btn"],[data-tid="call-hangup"]'),
    ("teams.cloud.microsoft", '#hangup-button,[data-tid="hangup-main-btn"],[data-tid="call-hangup"]'),
    ("meet.google.com", '[aria-label*="Leave call"]'),
    ("zoom.us", '.footer__leave-btn,[aria-label*="Leave"]'),
)

# Consecutive polls without the in-call element that count as "call ended".
MISSES_TO_END = 2
# Per-message CDP reply deadline; a wedged page then takes the tab-closed path.
CDP_TIMEOUT = 30.0


def log(msg: str) -> None:
    print(f"record-browser: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- targets

class SelectionError(Exception):
    """Target selection failed; .candidates holds the pages to list."""

    def __init__(self, msg: str, candidates: list[dict]):
        super().__init__(msg)
        self.candidates = candidates


def fetch_targets(host: str, port: int, timeout: float = 5.0) -> list[dict]:
    """GET /json/list. Raises OSError (URLError included) when CDP is unreachable."""
    with urllib.request.urlopen(f"http://{host}:{port}/json/list", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def pages_only(targets: list[dict]) -> list[dict]:
    return [t for t in targets if t.get("type") == "page"]


def format_target(t: dict) -> str:
    """One --list-targets line: id<TAB>title<TAB>url."""
    return f"{t.get('id', '')}\t{t.get('title', '')}\t{t.get('url', '')}"


def provider_selector(url: str) -> str | None:
    """In-call selector for a meeting URL, or None for an unknown host."""
    host = (urlparse(url).hostname or "").lower()
    for suffix, sel in PROVIDERS:
        if host == suffix or host.endswith("." + suffix):
            return sel
    return None


def select_target(targets: list[dict], target: str | None) -> dict:
    """Pick one page target.

    --target matches an exact targetId first, else a URL substring. Without
    --target, the single page on a known meeting host is picked. Anything that
    does not end in exactly one page raises SelectionError.
    """
    pages = pages_only(targets)
    if target:
        exact = [p for p in pages if p.get("id") == target]
        if exact:
            return exact[0]
        hits = [p for p in pages if target in p.get("url", "")]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise SelectionError(f"no page target matches {target!r}", pages)
        raise SelectionError(f"{len(hits)} page targets match {target!r}; be more specific", hits)
    meetings = [p for p in pages if provider_selector(p.get("url", ""))]
    if len(meetings) == 1:
        return meetings[0]
    if not meetings:
        raise SelectionError("no meeting tab found (Teams/Meet/Zoom web); pass --target", pages)
    raise SelectionError(f"{len(meetings)} meeting tabs open; pass --target", meetings)


# ---------------------------------------------------------- call tracking

class CallTracker:
    """Call-ended detection from per-poll "in call" observations.

    Arms on the first poll that sees the in-call element. Once armed,
    MISSES_TO_END consecutive misses mean the call ended; a hit resets the
    count. Before arming, misses mean nothing (the user may still be in the
    lobby), and arm_warning_due() fires once after arm_seconds.
    """

    def __init__(self, arm_seconds: float, start: float):
        self.arm_seconds = arm_seconds
        self.start = start
        self.armed = False
        self.misses = 0
        self.warned = False

    def update(self, in_call: bool) -> bool:
        """Feed one poll result; True when the call has ended."""
        if in_call:
            self.armed = True
            self.misses = 0
            return False
        if not self.armed:
            return False
        self.misses += 1
        return self.misses >= MISSES_TO_END

    def arm_warning_due(self, now: float) -> bool:
        """True exactly once, if still unarmed arm_seconds after start."""
        if self.armed or self.warned or now - self.start < self.arm_seconds:
            return False
        self.warned = True
        return True


# ------------------------------------------------------------- finalize

def part_path(out: str) -> str:
    """Sibling temp name with no audio suffix: dir/.name.m4a.part."""
    d, name = os.path.split(os.path.abspath(out))
    return os.path.join(d, f".{name}.part")


def finalize_cmd(webm: str, part: str, no_mic: bool, started: str | None = None) -> list[str]:
    """ffmpeg argv: webm -> m4a at `part`. -f mp4 since `part` ends in .part.

    `started` (UTC ISO 8601) becomes the creation_time tag, which dates the
    meeting folder on ingest; without it ingest falls back to the mtime.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", webm]
    if no_mic:
        cmd += ["-ac", "1", "-c:a", "aac", "-b:a", "96k"]
    else:
        cmd += ["-ac", "2", "-c:a", "aac", "-b:a", "128k", "-metadata", f"comment={CHANNEL_TAG}"]
    if started:
        cmd += ["-metadata", f"creation_time={started}"]
    return cmd + ["-f", "mp4", part]


def probe_duration(path: str) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True,
                             timeout=30).stdout.strip()
        return float(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ---------------------------------------------------------------- CDP

class CDPError(Exception):
    """The websocket dropped, timed out, or Runtime.evaluate failed."""


class CDPSession:
    """Minimal request/response CDP client; events are read and dropped."""

    def __init__(self, ws_url: str):
        from websockets.sync.client import connect  # deferred: --list-targets needs no websockets
        try:
            # max_size=None: a final stop() can return several MB of base64.
            # __enter__ is the context-manager form websockets >= 17.1 expects
            # (no legacy warning); close() below is its exit.
            self.ws = connect(ws_url, max_size=None, open_timeout=10).__enter__()
        except Exception as e:  # noqa: BLE001 - any connect failure is "no CDP"
            raise CDPError(f"cannot open {ws_url}: {e}") from e
        self.ids = itertools.count(1)

    def send(self, method: str, params: dict | None = None, timeout: float = CDP_TIMEOUT) -> dict:
        msg_id = next(self.ids)
        deadline = time.monotonic() + timeout
        try:
            self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise CDPError(f"{method}: no reply in {timeout:.0f}s")
                msg = json.loads(self.ws.recv(timeout=left))
                if msg.get("id") == msg_id:  # frames without our id are events
                    break
        except CDPError:
            raise
        except Exception as e:  # noqa: BLE001 - ConnectionClosed, TimeoutError, bad JSON
            raise CDPError(f"{method}: {type(e).__name__}: {e}") from e
        if "error" in msg:
            raise CDPError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    def evaluate(self, expression: str):
        """Runtime.evaluate returning the JS value; page exceptions raise CDPError."""
        res = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                             "awaitPromise": True, "userGesture": True})
        if "exceptionDetails" in res:
            ex = res["exceptionDetails"]
            text = (ex.get("exception") or {}).get("description") or ex.get("text", "exception")
            raise CDPError(f"page exception: {text}")
        return (res.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------- injected JS

# Installs window.__whosaidRec once (a second run returns the existing state, so
# a reconnect never starts a second MediaRecorder). __NO_MIC__ is substituted.
INSTALL_JS = r"""
(async () => {
  const noMic = __NO_MIC__;
  if (window.__whosaidRec) return {installed: false, micOk: window.__whosaidRec.micOk,
                                   micError: window.__whosaidRec.micError};
  const R = {chunks: [], streams: new Set(), micOk: false, micError: null, micStream: null};
  window.__whosaidRec = R;
  const ctx = new AudioContext();
  if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (e) {} }
  R.ctx = ctx;
  R.remote = ctx.createGain();
  const dest = ctx.createMediaStreamDestination();
  if (noMic) {
    R.remote.connect(dest);
  } else {
    // Merger inputs downmix to mono: input 0 = left = mic, input 1 = right = remote.
    const merger = ctx.createChannelMerger(2);
    R.remote.connect(merger, 0, 1);
    merger.connect(dest);
    // An unanswered permission prompt never settles, so race it: after 8s record
    // with a silent left channel, and wire the mic in if it is granted later.
    const gum = navigator.mediaDevices.getUserMedia({audio: true}).then((s) => {
      R.micStream = s;
      ctx.createMediaStreamSource(s).connect(merger, 0, 0);
      R.micOk = true; R.micError = null;
    }, (e) => {
      R.micError = String(e && (e.name || e.message) || e);  // left stays silent
    });
    await Promise.race([gum, new Promise((r) => setTimeout(() => {
      if (!R.micOk && !R.micError) R.micError = 'no answer to the mic permission prompt after 8s';
      r();
    }, 8000))]);
  }
  // Connect every element's MediaStream with audio once (keyed by stream id).
  R.scan = () => {
    for (const el of document.querySelectorAll('audio, video')) {
      const s = el.srcObject;
      if (!(s instanceof MediaStream) || !s.getAudioTracks().length || R.streams.has(s.id)) continue;
      try { ctx.createMediaStreamSource(s).connect(R.remote); R.streams.add(s.id); } catch (e) {}
    }
    return R.streams.size;
  };
  R.scan();
  const mr = new MediaRecorder(dest.stream, {mimeType: 'audio/webm;codecs=opus'});
  mr.ondataavailable = (ev) => { if (ev.data && ev.data.size) R.chunks.push(ev.data); };
  R.mr = mr;
  mr.start(1000);  // 1s slices: a closed tab loses at most ~1 slice + 1 drain
  // Queued chunks as one base64 string ('' when none); the queue is cleared.
  R.drain = async () => {
    const parts = R.chunks.splice(0);
    if (!parts.length) return '';
    const url = await new Promise((ok, bad) => {
      const fr = new FileReader();
      fr.onload = () => ok(fr.result);
      fr.onerror = () => bad(fr.error);
      fr.readAsDataURL(new Blob(parts));
    });
    return url.slice(url.indexOf(',') + 1);
  };
  R.poll = (sel) => ({
    inCall: sel ? !!document.querySelector(sel) : null,
    remoteStreams: R.scan(),
    micOk: R.micOk,
    micError: R.micError,
    recorder: R.mr.state,
  });
  R.click = (sel) => { const el = sel && document.querySelector(sel); if (el) el.click(); return !!el; };
  // Stop after the final dataavailable (onstop follows it), release the mic,
  // close the context, and hand back whatever is still queued.
  R.stop = async () => {
    if (R.mr.state !== 'inactive') {
      await new Promise((ok) => { R.mr.onstop = ok; R.mr.stop(); });
    }
    if (R.micStream) R.micStream.getTracks().forEach((t) => t.stop());
    try { await R.ctx.close(); } catch (e) {}
    const rest = await R.drain();
    delete window.__whosaidRec;
    return rest;
  };
  return {installed: true, micOk: R.micOk, micError: R.micError};
})()
"""


def install_js(no_mic: bool) -> str:
    return INSTALL_JS.replace("__NO_MIC__", "true" if no_mic else "false")


def in_call_js(selector: str) -> str:
    """Standalone in-call probe for --wait-for-call (no recorder injected yet)."""
    return f"!!document.querySelector({json.dumps(selector)})"


def poll_js(selector: str | None) -> str:
    return f"window.__whosaidRec.poll({json.dumps(selector)})"


DRAIN_JS = "window.__whosaidRec.drain()"
STOP_JS = "window.__whosaidRec.stop()"


def click_js(selector: str) -> str:
    return f"window.__whosaidRec.click({json.dumps(selector)})"


# ---------------------------------------------------------------- main

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="record_browser.py",
                                description="Record a browser meeting tab over CDP.")
    p.add_argument("--out")
    p.add_argument("--list-targets", action="store_true")
    p.add_argument("--cdp-host", default="127.0.0.1")
    p.add_argument("--cdp-port", type=int, default=9222)
    p.add_argument("--target")
    p.add_argument("--leave-selector")
    p.add_argument("--max-seconds", type=float, default=10800)
    p.add_argument("--no-mic", action="store_true")
    p.add_argument("--leave", action="store_true")
    # Floats so tests can run the loop fast.
    p.add_argument("--poll-seconds", type=float, default=2.0)
    p.add_argument("--drain-seconds", type=float, default=2.0)
    p.add_argument("--arm-seconds", type=float, default=60.0)
    p.add_argument("--wait-for-call", action="store_true")
    p.add_argument("--wait-poll-seconds", type=float, default=4.0)
    a = p.parse_args(argv)
    if not a.list_targets and not a.out:
        p.error("--out is required (or --list-targets)")
    if min(a.poll_seconds, a.drain_seconds, a.max_seconds, a.wait_poll_seconds) <= 0:
        p.error("--poll-seconds, --drain-seconds, --max-seconds and --wait-poll-seconds must be > 0")
    if a.wait_for_call and a.list_targets:
        p.error("--wait-for-call does not apply to --list-targets")
    return a


def finalize(webm: str, out: str, no_mic: bool, elapsed: float, started: str | None = None) -> int:
    """webm -> --out via a .part sibling. Returns the exit code for "no file"."""
    if not os.path.exists(webm) or os.path.getsize(webm) == 0:
        log("nothing captured; no file written")
        return EXIT_EMPTY
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    part = part_path(out)
    rc = subprocess.run(finalize_cmd(webm, part, no_mic, started), stdin=subprocess.DEVNULL).returncode
    if rc != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
        if os.path.exists(part):
            os.remove(part)
        # Keep the raw capture so nothing is lost; the caller's tmpdir cleanup skips it.
        keep = os.path.join(os.path.dirname(os.path.abspath(out)),
                            f".{os.path.basename(out)}.webm.part")
        shutil.move(webm, keep)
        log(f"ffmpeg failed (rc {rc}); raw capture kept at {keep}")
        return EXIT_EMPTY
    os.replace(part, out)
    dur = probe_duration(out) or elapsed
    log(f"captured {dur:.0f}s -> {os.path.abspath(out)}")
    return EXIT_OK


def wait_candidates(targets: list[dict], target: str | None, leave_selector: str | None) -> list[tuple[dict, str]]:
    """(page, in-call selector) pairs --wait-for-call probes on one poll."""
    out = []
    for p in pages_only(targets):
        url = p.get("url", "")
        if target and not (p.get("id") == target or target in url):
            continue
        sel = leave_selector or provider_selector(url)
        if sel and p.get("webSocketDebuggerUrl"):
            out.append((p, sel))
    return out


def page_in_call(page: dict, selector: str) -> bool:
    """One short CDP session: does the page show its in-call element right now?"""
    try:
        cdp = CDPSession(page["webSocketDebuggerUrl"])
    except CDPError:
        return False
    try:
        return bool(cdp.evaluate(in_call_js(selector)))
    except CDPError:
        return False
    finally:
        cdp.close()


def wait_for_call(a: argparse.Namespace, stop: dict) -> dict | None:
    """Block until a meeting page is in a call; None if a signal arrived first."""
    log(f"waiting for a meeting tab to join a call (CDP port {a.cdp_port}); Ctrl-C to give up.")
    cdp_down = False
    while stop["sig"] is None:
        try:
            targets = fetch_targets(a.cdp_host, a.cdp_port)
            if cdp_down:
                log(f"CDP at {a.cdp_host}:{a.cdp_port} is reachable again.")
            cdp_down = False
        except (OSError, ValueError) as e:
            if not cdp_down:
                log(f"CDP at {a.cdp_host}:{a.cdp_port} is not reachable yet ({e}); still waiting. "
                    f"The browser needs --remote-debugging-port={a.cdp_port}.")
            cdp_down = True
            targets = []
        for page, sel in wait_candidates(targets, a.target, a.leave_selector):
            if stop["sig"] is not None:
                break
            if page_in_call(page, sel):
                log(f"{page.get('title', '') or page.get('url', '')} is in a call; starting.")
                return page
        deadline = time.monotonic() + a.wait_poll_seconds
        while stop["sig"] is None and time.monotonic() < deadline:
            time.sleep(0.1)
    return None


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)

    stop = {"sig": None}

    def on_signal(signum, _frame):
        stop["sig"] = signum  # checked by the loops; never raise mid-I/O

    if a.wait_for_call:
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        found = wait_for_call(a, stop)
        if found is None:
            log("stopped while waiting for a call; nothing recorded.")
            return EXIT_OK
        # Re-read the target list so the page's websocket URL is current.
        a.target = found.get("id")

    try:
        targets = fetch_targets(a.cdp_host, a.cdp_port)
    except (OSError, ValueError) as e:
        log(f"cannot reach CDP at {a.cdp_host}:{a.cdp_port} ({e}); start the browser "
            f"with --remote-debugging-port={a.cdp_port}")
        return EXIT_NO_CDP

    if a.list_targets:
        for t in pages_only(targets):
            print(format_target(t))
        return EXIT_OK

    try:
        page = select_target(targets, a.target)
    except SelectionError as e:
        log(str(e))
        for t in e.candidates:
            print(format_target(t), file=sys.stderr)
        return EXIT_NO_CDP
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        log(f"target {page.get('id')} has no webSocketDebuggerUrl (another debugger attached?)")
        return EXIT_NO_CDP

    selector = a.leave_selector or provider_selector(page.get("url", ""))
    log(f"target {page.get('id')}: {page.get('title', '')} <{page.get('url', '')}>")
    if not selector:
        log("no in-call selector for this host (pass --leave-selector); "
            "stop with Ctrl-C or --max-seconds")

    try:
        cdp = CDPSession(ws_url)
    except CDPError as e:
        log(str(e))
        return EXIT_NO_CDP

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    tmpdir = tempfile.mkdtemp(prefix="whosaid-browser-")
    webm = os.path.join(tmpdir, "capture.webm")
    start = time.monotonic()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    reason = None
    try:
        try:
            info = cdp.evaluate(install_js(a.no_mic)) or {}
        except CDPError as e:
            log(f"recorder injection failed: {e}")
            return EXIT_NO_CDP
        if not a.no_mic and not info.get("micOk"):
            log(f"warning: mic unavailable ({info.get('micError')}); left channel will be silent")
        log("recording" + (" (no mic)" if a.no_mic else " (L=mic, R=tab audio)"))

        tracker = CallTracker(a.arm_seconds, start)
        if a.wait_for_call:
            tracker.armed = True  # the wait already saw the in-call element
        streams_seen = 0
        stream_warned = False
        next_poll = next_drain = start
        with open(webm, "ab") as fh:
            def drain(js: str) -> None:
                data = cdp.evaluate(js)
                if data:
                    fh.write(base64.b64decode(data))
                    fh.flush()

            try:
                while True:
                    now = time.monotonic()
                    if stop["sig"] is not None:
                        reason = "interrupted"
                        break
                    if now - start >= a.max_seconds:
                        reason = "max-seconds"
                        if a.leave and selector:
                            clicked = cdp.evaluate(click_js(selector))
                            log("left the call" if clicked else "warning: --leave: in-call element not found")
                        break
                    if now >= next_poll:
                        next_poll = now + a.poll_seconds
                        st = cdp.evaluate(poll_js(selector)) or {}
                        streams_seen = max(streams_seen, int(st.get("remoteStreams") or 0))
                        if selector and tracker.update(bool(st.get("inCall"))):
                            reason = "call-ended"
                            break
                        if selector and tracker.arm_warning_due(now):
                            log(f"warning: in-call element not seen in {a.arm_seconds:.0f}s; "
                                "auto-stop is off, stop with Ctrl-C or --max-seconds")
                        if not stream_warned and not streams_seen and now - start >= a.arm_seconds:
                            stream_warned = True
                            log("warning: no <audio>/<video> MediaStream found in the tab yet; "
                                "the right channel may be silent")
                    if now >= next_drain:
                        next_drain = now + a.drain_seconds
                        drain(DRAIN_JS)
                    time.sleep(max(0.05, min(next_poll, next_drain) - time.monotonic()))
                # Normal stop: flush the recorder. A failure here keeps the reason's
                # exit code; everything drained so far is still finalized.
                try:
                    drain(STOP_JS)
                except CDPError as e:
                    log(f"warning: final flush failed ({e}); keeping drained audio")
            except CDPError as e:
                log(f"tab closed or CDP failed: {e}")
                reason = "tab-closed"
        cdp.close()
        log(f"stopped: {reason}")
        rc = finalize(webm, a.out, a.no_mic, time.monotonic() - start, started)
        if rc != EXIT_OK:
            return rc
        return EXIT_TAB_CLOSED if reason == "tab-closed" else EXIT_OK
    finally:
        cdp.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
