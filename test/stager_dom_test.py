#!/usr/bin/env python3
"""The real stager, --edit-sent, against a fixture DOM (#209). No real Teams, no browser.

test/fixtures/stager-edit-dom/chat.html is a hand-written stand-in for a Teams chat; bridge.mjs runs it
in jsdom (under bun) and this file serves CDP in front of it, so the stager's own JavaScript (message
lookup, the hover toolbar's Edit, the edit box, the paste) runs against a DOM, not against op stubs.

Run: uv run --quiet --with websockets python test/stager_dom_test.py
Needs bun; jsdom is installed once into ~/.cache/whosaid-test-jsdom (WHOSAID_TEST_JSDOM_DIR). Without
them every case is skipped, loudly (WHOSAID_REQUIRE_DOM_TEST=1 turns the skip into a failure).
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "test" / "fixtures" / "stager-edit-dom"
SCRIPT = str(ROOT / "lib" / "drafts_stager.py")
JSDOM_DIR = Path(os.environ.get("WHOSAID_TEST_JSDOM_DIR") or Path.home() / ".cache" / "whosaid-test-jsdom")
os.environ["WHOSAID_TEAMS_CDP_PORT"] = "9"  # never the real Teams app port


def jsdom_ready() -> str | None:
    """None when bun and jsdom are usable, else why not."""
    if not shutil.which("bun"):
        return "bun is not installed"
    if not (JSDOM_DIR / "node_modules" / "jsdom").is_dir():
        JSDOM_DIR.mkdir(parents=True, exist_ok=True)
        (JSDOM_DIR / "package.json").write_text('{"private":true,"dependencies":{"jsdom":"^26.0.0"}}\n')
        r = subprocess.run(["bun", "install"], cwd=JSDOM_DIR, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return f"bun install jsdom failed: {(r.stderr or r.stdout)[-300:]}"
    return None


WHY_NOT = jsdom_ready()
if WHY_NOT:
    print(f"SKIP stager_dom_test: {WHY_NOT}", file=sys.stderr)


class DomTeams:
    """CDP in front of the jsdom bridge: Runtime.evaluate, mouse and keys go to the DOM."""

    def __init__(self, later=False):
        from websockets.sync.server import serve
        env = {**os.environ, "NODE_PATH": str(JSDOM_DIR / "node_modules")}
        self.p = subprocess.Popen(["bun", str(FIX / "bridge.mjs"), str(FIX / "chat.html")] + (["later"] if later else []),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict]] = []
        self.srv = serve(self.handler, "127.0.0.1", 0)
        ws_port = self.srv.socket.getsockname()[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{ws_port}/devtools/browser/x"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def ask(self, q: dict) -> dict:
        with self.lock:
            self.p.stdin.write(json.dumps(q) + "\n")
            self.p.stdin.flush()
            return json.loads(self.p.stdout.readline())

    def state(self) -> dict:
        return self.ask({"state": True})["value"]

    def handler(self, ws):
        try:
            self.serve_conn(ws)
        except Exception as e:  # noqa: BLE001 - the stager hanging up is the normal end
            if "ConnectionClosed" not in type(e).__name__:
                raise

    def serve_conn(self, ws):
        for raw in ws:
            msg = json.loads(raw)
            method, params = msg["method"], msg.get("params", {})
            self.calls.append((method, params))
            result: dict = {}
            if method == "Target.getTargets":
                result = {"targetInfos": [{"targetId": "T1", "type": "page", "title": "Microsoft Teams",
                                           "url": "https://teams.cloud.microsoft/v2/"}]}
            elif method == "Target.attachToTarget":
                result = {"sessionId": "S1"}
            elif method == "Runtime.evaluate":
                r = self.ask({"eval": params["expression"]})
                if "error" in r:
                    result = {"exceptionDetails": {"text": r["error"]}}
                else:
                    result = {"result": {"type": "object", "value": r["value"]}}
            elif method == "Input.dispatchMouseEvent":
                self.ask({"mouse": params})
            elif method == "Input.dispatchKeyEvent":
                self.ask({"key": params})
            elif method == "Input.insertText":
                self.ask({"insert": params["text"]})
            ws.send(json.dumps({"id": msg["id"], "result": result}))

    def close(self):
        self.srv.shutdown()
        self.http.shutdown()
        self.p.stdin.close()
        self.p.wait(timeout=10)


@unittest.skipIf(WHY_NOT and not os.environ.get("WHOSAID_REQUIRE_DOM_TEST"), f"no DOM engine: {WHY_NOT}")
class EditSentAgainstFixtureDom(unittest.TestCase):
    SENT = "can you add the two DNS records for the staging hosts?"
    NEW = "can you add the two DNS records for the staging hosts? the list is in [the runbook](https://example.com/rb)"

    def setUp(self):
        if WHY_NOT:
            self.fail(f"WHOSAID_REQUIRE_DOM_TEST is set but {WHY_NOT}")
        self.tmp = tempfile.TemporaryDirectory(prefix="stager-dom-")
        d = Path(self.tmp.name)
        self.md, self.match, self.lockd = d / "draft.md", d / "match.txt", d / "lock"
        self.md.write_text(self.NEW + "\n")
        self.match.write_text(self.SENT)
        self.lockd.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_stager(self, f, *extra):
        env = {**os.environ, "WHOSAID_STAGER_TIME_SCALE": "0.02", "TEAMS_OUTBOX_LOCK": str(self.lockd)}
        return subprocess.run([sys.executable, SCRIPT, "--chat", "Ann Lee", "--md", str(self.md), "--cdp-port",
                               str(f.port), "--edit-sent", "--match-file", str(self.match), *extra],
                              capture_output=True, text=True, env=env, timeout=60)

    def never_saved(self, f, st):
        self.assertFalse(st["saved"], "the edit was saved")
        self.assertFalse(st["sent"], "something was sent")
        keys = [p for m, p in f.calls if m == "Input.dispatchKeyEvent"]
        self.assertFalse([k for k in keys if str(k.get("key", "")).lower() in ("enter", "return")], keys)

    def test_edit_opens_on_that_message_and_holds_the_new_text_with_its_link(self):
        f = DomTeams()
        try:
            r = self.run_stager(f)
            st = f.state()
        finally:
            f.close()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual((out["edit"], out["saved"], out["sent"], out["links"]), (True, False, False, 1))
        self.assertEqual(st["title"], "Chat | Ann Lee | Microsoft Teams")
        self.assertEqual(st["editing"], 2, "Edit opened on the matching message, not its neighbour")
        self.assertEqual(len(st["edit_html"]), 1)
        self.assertIn('href="https://example.com/rb"', st["edit_html"][0])
        self.assertNotIn("staging hosts?can you", st["edit_html"][0], "the old text was cleared first")
        self.assertEqual(st["compose"], "", "the compose box is never touched")
        self.assertFalse([c for c in st["clicks"] if c[0] == 972], "clicked the hidden 4px More button")
        self.never_saved(f, st)

    def test_a_reply_after_the_message_stops_it_unless_allow_newer(self):
        f = DomTeams(later=True)
        try:
            r = self.run_stager(f)
            st = f.state()
        finally:
            f.close()
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 newer message(s) from others", r.stderr)
        self.assertIsNone(st["editing"])
        f = DomTeams(later=True)
        try:
            r = self.run_stager(f, "--allow-newer")
            st = f.state()
        finally:
            f.close()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(st["editing"], 2)
        self.never_saved(f, st)

    def test_no_matching_own_message(self):
        self.match.write_text("sure, on it")  # someone else's words: never ours to edit
        f = DomTeams(later=True)
        try:
            r = self.run_stager(f, "--allow-newer")
            st = f.state()
        finally:
            f.close()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no message of yours", r.stderr)
        self.assertIsNone(st["editing"])
        self.never_saved(f, st)


@unittest.skipIf(WHY_NOT and not os.environ.get("WHOSAID_REQUIRE_DOM_TEST"), f"no DOM engine: {WHY_NOT}")
class MentionAgainstFixtureDom(unittest.TestCase):
    """A mention whose last name is one letter: the picker finds nobody for "@Ana B", so the stager
    asks for "@Ana" and picks the suggestion named "Ana B" exactly; a failed mention leaves no text."""

    def setUp(self):
        if WHY_NOT:
            self.fail(f"WHOSAID_REQUIRE_DOM_TEST is set but {WHY_NOT}")
        self.tmp = tempfile.TemporaryDirectory(prefix="stager-dom-")
        d = Path(self.tmp.name)
        self.md, self.lockd = d / "draft.md", d / "lock"
        self.md.write_text("the staging hosts are in [the runbook](https://example.com/rb)\n")
        self.lockd.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_stager(self, f, *extra):
        env = {**os.environ, "WHOSAID_STAGER_TIME_SCALE": "0.02", "TEAMS_OUTBOX_LOCK": str(self.lockd)}
        return subprocess.run([sys.executable, SCRIPT, "--chat", "Ann Lee", "--md", str(self.md), "--cdp-port",
                               str(f.port), *extra], capture_output=True, text=True, env=env, timeout=90)

    def test_one_letter_last_name_is_found_by_first_name_and_picked_exactly(self):
        f = DomTeams()
        try:
            r = self.run_stager(f, "--mention", "Ana B")
            st = f.state()
        finally:
            f.close()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(st.get("mentions"), ["Ana B"], "picked Ana B, not Ana Bell")
        self.assertIn('itemtype="http://schema.skype.com/Mention">Ana B</span>', st["compose"])
        self.assertNotIn("@", st["compose"].split("</span>")[-1], "no leftover @ text")
        self.assertIn('href="https://example.com/rb"', st["compose"])
        self.assertFalse(st["sent"])

    def test_a_failed_mention_leaves_no_typed_text(self):
        f = DomTeams()
        try:
            r = self.run_stager(f, "--mention", "Cy D")
            st = f.state()
        finally:
            f.close()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no mention suggestion for Cy D", r.stderr)
        self.assertEqual(st["compose"], "", "the typed @Cy D / @Cy was taken back")
        self.assertFalse(st["list"])
        self.assertFalse(st["sent"])


if __name__ == "__main__":
    unittest.main()
