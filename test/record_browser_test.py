#!/usr/bin/env python3
"""
Offline test for lib/record_browser.py (`whosaid record --browser`, issue #118).
Pure functions (target selection, provider table, call-ended tracking, the
ffmpeg finalize command), plus one end-to-end run against a fake CDP endpoint:
a tiny http.server for /json/list and a websockets server answering
Runtime.evaluate with canned results (skipped without ffmpeg/ffprobe). No
browser, no network beyond 127.0.0.1, no audio playback.

Run:
    uv run --quiet --with websockets --with numpy python test/record_browser_test.py
"""

import base64
import http.server
import itertools
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import record_browser as rb  # noqa: E402
import stereo_channels as sc  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def page(tid, url, title="t", type_="page"):
    return {"id": tid, "type": type_, "url": url, "title": title,
            "webSocketDebuggerUrl": f"ws://127.0.0.1:1/devtools/page/{tid}"}


def selection_fails(targets, target):
    try:
        rb.select_target(targets, target)
    except rb.SelectionError as e:
        return e
    return None


# ---- CHANNEL_TAG matches the attribution module ----
check(rb.CHANNEL_TAG == sc.CHANNEL_TAG, "CHANNEL_TAG equals stereo_channels.CHANNEL_TAG")

# ---- provider table ----
teams_sel = '#hangup-button,[data-tid="hangup-main-btn"],[data-tid="call-hangup"]'
check(rb.provider_selector("https://teams.microsoft.com/v2/?meet") == teams_sel, "Teams web")
check(rb.provider_selector("https://meet.google.com/abc-defg-hij") == '[aria-label*="Leave call"]', "Meet")
check(rb.provider_selector("https://us05web.zoom.us/wc/123/start")
      == '.footer__leave-btn,[aria-label*="Leave"]', "Zoom subdomain")
check(rb.provider_selector("https://example.com/teams.microsoft.com") is None, "host only, not path")
check(rb.provider_selector("https://notzoom.us/") is None, "suffix match needs a dot boundary")
check(rb.provider_selector("about:blank") is None, "no host")

# ---- target selection ----
meet = page("AAA", "https://meet.google.com/abc-defg-hij")
news = page("BBB", "https://news.example.com/")
worker = page("CCC", "https://meet.google.com/sw.js", type_="service_worker")
teams = page("DDD", "https://teams.microsoft.com/v2/")
check(rb.select_target([meet, news, worker], None)["id"] == "AAA", "auto-pick the single meeting page")
check(rb.select_target([meet, news], "BBB")["id"] == "BBB", "exact targetId")
check(rb.select_target([meet, news], "news.example")["id"] == "BBB", "URL substring")
e = selection_fails([meet, news], "nomatch")
check(e is not None and {t["id"] for t in e.candidates} == {"AAA", "BBB"}, "0 matches lists all pages")
e = selection_fails([meet, news, teams], "https://")
check(e is not None and len(e.candidates) == 3, ">1 substring matches is an error listing matches")
e = selection_fails([meet, teams], None)
check(e is not None and {t["id"] for t in e.candidates} == {"AAA", "DDD"}, "two meeting tabs need --target")
e = selection_fails([news], None)
check(e is not None and [t["id"] for t in e.candidates] == ["BBB"], "no meeting tab needs --target")
check(selection_fails([worker], "CCC") is not None, "non-page targets are never picked")
check(rb.format_target(meet) == "AAA\tt\thttps://meet.google.com/abc-defg-hij", "list-targets line")

# ---- call tracker ----
t = rb.CallTracker(arm_seconds=60, start=0.0)
check(not any(t.update(False) for _ in range(10)), "never armed: misses never end the call")
check(not t.arm_warning_due(59.0) and t.arm_warning_due(60.0), "arm warning at arm_seconds")
check(not t.arm_warning_due(120.0), "arm warning fires once")
t = rb.CallTracker(arm_seconds=60, start=0.0)
check(not t.update(True) and t.armed, "first hit arms")
check(not t.update(False), "one miss is not the end")
check(not t.update(True), "hit resets the miss count")
check(not t.update(False) and t.update(False), "armed + two consecutive misses = call ended")
check(not t.arm_warning_due(100.0), "no arm warning once armed")

# ---- finalize command ----
check(rb.part_path("/r/2026-10-28-1200.m4a") == "/r/.2026-10-28-1200.m4a.part", "part path")
part = rb.part_path("/r/x.m4a")
check(Path(part).suffix == ".part", "part name has no audio suffix")
cmd = rb.finalize_cmd("/t/c.webm", part, no_mic=False)
check(cmd == ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", "/t/c.webm",
              "-ac", "2", "-c:a", "aac", "-b:a", "128k", "-metadata", f"comment={sc.CHANNEL_TAG}",
              "-f", "mp4", part], "stereo finalize command")
cmd = rb.finalize_cmd("/t/c.webm", part, no_mic=True)
check(cmd == ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", "/t/c.webm",
              "-ac", "1", "-c:a", "aac", "-b:a", "96k", "-f", "mp4", part], "no-mic finalize command")
check(rb.poll_js('a"b') == 'window.__whosaidRec.poll("a\\"b")', "poll selector is JSON-quoted")


# ---- fake CDP end to end ----
def fake_cdp(webm_b64, ws_port_box, close_after_drain=False, in_call_forever=False):
    """Serve canned Runtime.evaluate replies; returns the websockets server."""
    from websockets.sync.server import serve
    polls = itertools.repeat(True) if in_call_forever else iter([True, True, False, False])
    sent = {"drain": False}

    def handler(ws):
        for raw in ws:
            msg = json.loads(raw)
            expr = msg["params"]["expression"]
            if "MediaRecorder" in expr:  # install (its body also mentions drain/poll)
                value = {"installed": True, "micOk": True, "micError": None}
            elif ".poll(" in expr:
                value = {"inCall": next(polls, False), "remoteStreams": 1, "micOk": True}
            elif ".drain()" in expr:
                value, sent["drain"] = ("" if sent["drain"] else webm_b64), True
            else:  # .stop() hands back whatever was never drained, like the real one
                value, sent["drain"] = ("" if sent["drain"] else webm_b64), True
            ws.send(json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}))  # event noise
            ws.send(json.dumps({"id": msg["id"], "result": {"result": {"type": "object", "value": value}}}))
            if close_after_drain and sent["drain"]:
                ws.close()  # the tab went away mid-call
                return

    srv = serve(handler, "127.0.0.1", 0)
    ws_port_box.append(srv.socket.getsockname()[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if shutil.which("ffmpeg") and shutil.which("ffprobe"):
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        src = Path(d, "src.webm")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-ac", "2", "-c:a", "libopus", "-t", "3", str(src)], check=True)
        box = []
        b64 = base64.b64encode(src.read_bytes()).decode()
        wsrv = fake_cdp(b64, box)
        wsrv2 = fake_cdp(b64, box, close_after_drain=True)
        wsrv3 = fake_cdp(b64, box, in_call_forever=True)
        listing = [page("PAGE1", "https://meet.google.com/abc-defg-hij") |
                   {"webSocketDebuggerUrl": f"ws://127.0.0.1:{box[0]}/devtools/page/PAGE1"},
                   page("PAGE2", "https://example.com/closing") |
                   {"webSocketDebuggerUrl": f"ws://127.0.0.1:{box[1]}/devtools/page/PAGE2"},
                   page("PAGE3", "https://example.com/forever") |
                   {"webSocketDebuggerUrl": f"ws://127.0.0.1:{box[2]}/devtools/page/PAGE3"}]

        class Lister(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(listing).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        hsrv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Lister)
        threading.Thread(target=hsrv.serve_forever, daemon=True).start()
        port = str(hsrv.server_address[1])
        script = str(ROOT / "lib" / "record_browser.py")
        try:
            r = subprocess.run([sys.executable, script, "--list-targets", "--cdp-port", port],
                               capture_output=True, text=True, timeout=30)
            check(r.returncode == 0 and r.stdout.startswith("PAGE1\t")
                  and len(r.stdout.splitlines()) == 3, f"--list-targets: {r!r}")
            out = Path(d, "rec", "call.m4a")
            r = subprocess.run([sys.executable, script, "--out", str(out), "--cdp-port", port,
                                "--poll-seconds", "0.1", "--drain-seconds", "0.1", "--max-seconds", "30"],
                               capture_output=True, text=True, timeout=60)
            # No --target: auto-picks PAGE1, the only meeting tab (PAGE2 is example.com).
            check(r.returncode == 0, f"fake call exits 0: rc={r.returncode} {r.stderr}")
            check(r.stderr.count("record-browser: stopped: ") == 1
                  and "record-browser: stopped: call-ended" in r.stderr, f"stopped: call-ended once: {r.stderr}")
            check("captured " in r.stderr and str(out) in r.stderr, "captured line names the out path")
            check(out.exists() and sc.is_tagged(str(out)), "out file is stereo and tagged")
            check(not Path(rb.part_path(str(out))).exists(), "no .part left behind")
            dur = rb.probe_duration(str(out))
            check(dur is not None and 2.5 < dur < 3.5, f"duration ~3s, got {dur}")
            # SIGINT mid-call (what `whosaid_record_browser stop` sends): exit 0, finalized.
            intr = Path(d, "rec", "intr.m4a")
            p = subprocess.Popen([sys.executable, script, "--out", str(intr), "--cdp-port", port,
                                  "--target", "PAGE3", "--poll-seconds", "0.1", "--drain-seconds", "0.1"],
                                 stderr=subprocess.PIPE, text=True)
            lines = []
            for line in p.stderr:
                lines.append(line)
                if line.startswith("record-browser: recording"):
                    break
            p.send_signal(signal.SIGINT)
            lines += p.stderr.readlines()
            rc = p.wait(timeout=30)
            err = "".join(lines)
            check(rc == 0 and err.count("record-browser: stopped: ") == 1
                  and "record-browser: stopped: interrupted" in err, f"SIGINT exits 0, interrupted: rc={rc} {err}")
            check(intr.exists() and sc.is_tagged(str(intr)), "interrupted capture finalized, stereo and tagged")
            closed = Path(d, "rec", "closed.m4a")
            r = subprocess.run([sys.executable, script, "--out", str(closed), "--cdp-port", port,
                                "--target", "PAGE2", "--no-mic", "--poll-seconds", "0.1",
                                "--drain-seconds", "0.1", "--max-seconds", "30"],
                               capture_output=True, text=True, timeout=60)
            check(r.returncode == 3 and "record-browser: stopped: tab-closed" in r.stderr,
                  f"tab closed mid-call exits 3: rc={r.returncode} {r.stderr}")
            check(closed.exists() and not sc.is_tagged(str(closed)), "tab-closed audio finalized, no-mic untagged")
            ch = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=channels", "-of", "csv=p=0",
                                 str(closed)], capture_output=True, text=True).stdout.strip()
            check(ch == "1", f"no-mic file is mono, got {ch!r}")
            r = subprocess.run([sys.executable, script, "--out", str(out), "--cdp-port", "1"],
                               capture_output=True, text=True, timeout=30)
            check(r.returncode == 6, f"unreachable CDP exits 6, got {r.returncode}")
            r = subprocess.run([sys.executable, script, "--cdp-port", port], capture_output=True, text=True)
            check(r.returncode == 2, "missing --out exits 2")
        finally:
            hsrv.shutdown()
            wsrv.shutdown()
            wsrv2.shutdown()
            wsrv3.shutdown()
else:
    print("SKIP: ffmpeg/ffprobe not found (fake CDP end to end)")

print(f"PASS: record_browser_test ({CHECKS} checks)")
