#!/usr/bin/env python3
"""Offline tests for lib/teams_launch.py (GitHub issue #196).

Run: uv run --quiet python test/teams_launch_test.py
A real local http.server fakes /json/version and /json/list; every external command (open,
osascript, pgrep, the watch-call child) is replaced by a recorder, so nothing touches the real
Teams app.
"""

import contextlib
import http.server
import io
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import teams_launch as tl  # noqa: E402


class Fake:
    """Fake DevTools endpoint. `browser` None = nothing listening (server not started)."""

    def __init__(self, browser):
        self.browser = browser
        self.srv = None
        self.port = None
        if browser is not None:
            self.start()

    def start(self):
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/json/version":
                    body = {"Browser": fake.browser}
                elif self.path == "/json/list":
                    body = [{"id": "P1", "title": "Chat | Someone", "type": "page", "url": "https://x/secret"},
                            {"id": "W1", "title": "worker", "type": "service_worker"}]
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()


def free_port():
    f = Fake("x")
    p = f.port
    f.close()
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.running = False
        self.mic = {"app": "com.microsoft.teams2", "running": True, "mic": False, "speaker": False}
        self.on_open = None  # called after open_teams records, e.g. to bring the fake up
        self.saved = {n: getattr(tl, n) for n in
                      ("is_running", "quit_teams", "open_teams", "watch_call_state", "sleep")}
        tl.is_running = self._is_running
        tl.quit_teams = self._quit
        tl.open_teams = self._open
        tl.watch_call_state = self._watch
        tl.sleep = lambda s: None
        self.fakes = []

    def tearDown(self):
        for n, f in self.saved.items():
            setattr(tl, n, f)
        for f in self.fakes:
            f.close()

    def fake(self, browser):
        f = Fake(browser)
        self.fakes.append(f)
        return f

    def _is_running(self):
        self.calls.append(("is_running",))
        return self.running

    def _quit(self):
        self.calls.append(("quit",))
        self.running = False

    def _open(self, port):
        self.calls.append(("open", port))
        self.running = True
        if self.on_open:
            self.on_open()

    def _watch(self, timeout=10.0):
        self.calls.append(("watch",))
        return self.mic

    def names(self):
        return [c[0] for c in self.calls if c[0] != "is_running"]

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tl.main(list(argv))
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None), err.getvalue()


class LaunchTest(Base):
    def test_check_up(self):
        f = self.fake("Edg/154.0.1")
        code, res, _ = self.cli("--check", "--port", str(f.port))
        self.assertEqual(code, 0)
        self.assertTrue(res["up"])
        self.assertEqual(res["action"], "none")
        self.assertEqual(res["browser"], "Edg/154.0.1")
        self.assertEqual(res["port"], f.port)
        self.assertEqual(res["pages"], [{"id": "P1", "title": "Chat | Someone"}])
        self.assertEqual(self.calls, [])

    def test_check_down_runs_nothing(self):
        port = free_port()
        self.running = True
        code, res, _ = self.cli("--check", "--port", str(port))
        self.assertEqual(code, 1)
        self.assertFalse(res["up"])
        self.assertEqual(res["action"], "down")
        self.assertIsNone(res["browser"])
        self.assertEqual(res["pages"], [])
        self.assertEqual(self.calls, [])

    def test_port_held_by_other_browser(self):
        f = self.fake("Chrome/156.0.1")
        code, res, err = self.cli("--port", str(f.port))
        self.assertEqual(code, 1)
        self.assertFalse(res["up"])
        self.assertEqual(res["browser"], "Chrome/156.0.1")
        self.assertIn("held by Chrome/156.0.1", err)
        self.assertIn("not the Teams app", err)
        self.assertEqual(self.calls, [])

    def test_not_running_opens(self):
        port = free_port()
        self.running = False
        self.on_open = self._bring_up_on(port)
        code, res, _ = self.cli("--port", str(port))
        self.assertEqual(code, 0)
        self.assertEqual(res["action"], "launched")
        self.assertTrue(res["up"])
        self.assertEqual(self.names(), ["open"])
        self.assertEqual(self.calls[-1], ("open", port))

    @staticmethod
    def _handler(browser):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = {"Browser": browser} if self.path == "/json/version" else []
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        return H

    def _bring_up_on(self, port):
        def start():
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), self._handler("Edg/154.0.1"))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            f = Fake(None)
            f.srv = srv
            self.fakes.append(f)
        return start

    def test_open_env_arg_contains_port(self):
        # the real open_teams builds the env arg; record the argv it would run
        seen = []

        class R:
            returncode = 0
            stdout = ""

        real_run = tl.subprocess.run
        tl.subprocess.run = lambda argv, **kw: (seen.append(argv), R())[1]
        try:
            tl.open_teams = self.saved["open_teams"]
            tl.open_teams(9444)
        finally:
            tl.subprocess.run = real_run
        self.assertEqual(seen[0][:3], ["open", "-a", "Microsoft Teams"])
        self.assertIn("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9444", seen[0])

    def test_running_mic_true_refused(self):
        port = free_port()
        self.running = True
        self.mic = {"running": True, "mic": True, "speaker": True}
        code, res, err = self.cli("--port", str(port))
        self.assertEqual(code, 2)
        self.assertEqual(res["action"], "refused")
        self.assertFalse(res["up"])
        self.assertEqual(self.names(), ["watch"])
        self.assertIn("mic", err)

    def test_running_watch_fails_refused_then_force_proceeds(self):
        port = free_port()
        self.running = True
        self.mic = None
        code, res, _ = self.cli("--port", str(port))
        self.assertEqual(code, 2)
        self.assertEqual(res["action"], "refused")
        self.assertEqual(self.names(), ["watch"])
        self.calls = []
        self.running = True
        self.on_open = self._bring_up_on(port)
        code, res, _ = self.cli("--port", str(port), "--force")
        self.assertEqual(code, 0)
        self.assertEqual(res["action"], "relaunched")
        self.assertEqual(self.names(), ["quit", "open"])
        self.assertNotIn("watch", self.names())

    def test_running_mic_false_quit_then_open(self):
        port = free_port()
        self.running = True
        self.on_open = self._bring_up_on(port)
        code, res, _ = self.cli("--port", str(port))
        self.assertEqual(code, 0)
        self.assertEqual(res["action"], "relaunched")
        self.assertTrue(res["up"])
        self.assertEqual(self.names(), ["watch", "quit", "open"])
        self.assertEqual(self.calls[-1], ("open", port))

    def test_wait_timeout(self):
        port = free_port()
        self.running = False
        code, res, err = self.cli("--port", str(port), "--wait", "0")
        self.assertEqual(code, 1)
        self.assertFalse(res["up"])
        self.assertEqual(res["action"], "launched")
        self.assertEqual(self.names(), ["open"])
        self.assertIn("no DevTools answer", err)

    def test_bad_port(self):
        for bad in ("abc", "0", "70000"):
            self.calls = []
            code, res, err = self.cli("--port", bad)
            self.assertEqual(code, 2, bad)
            self.assertIsNone(res)
            self.assertIn("bad port", err)
            self.assertEqual(self.calls, [])

    def test_env_port_default_and_override(self):
        import os
        old = os.environ.pop("WHOSAID_TEAMS_CDP_PORT", None)
        try:
            self.assertEqual(tl.resolve_port(None), 9333)
            os.environ["WHOSAID_TEAMS_CDP_PORT"] = "9444"
            self.assertEqual(tl.resolve_port(None), 9444)
            self.assertEqual(tl.resolve_port("9555"), 9555)
            os.environ["WHOSAID_TEAMS_CDP_PORT"] = "nope"
            with self.assertRaises(ValueError):
                tl.resolve_port(None)
        finally:
            os.environ.pop("WHOSAID_TEAMS_CDP_PORT", None)
            if old is not None:
                os.environ["WHOSAID_TEAMS_CDP_PORT"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
