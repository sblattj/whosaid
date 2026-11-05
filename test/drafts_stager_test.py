#!/usr/bin/env python3
"""Offline tests for lib/drafts_stager.py, the built-in Teams web stager (GitHub issue #168).

Run: uv run --quiet --with websockets python test/drafts_stager_test.py
The end-to-end cases drive the real CLI against a fake CDP endpoint (http for /json/version, a
websockets server for the protocol) that keeps a small DOM state keyed on the /*op:NAME*/ markers
(see the docstring of lib/drafts_stager.py), records every CDP call, and in every case checks that
no Enter key and no click on the fake send button ever happened.
"""

import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import drafts  # noqa: E402
import drafts_stager as ds  # noqa: E402

SCRIPT = str(ROOT / "lib" / "drafts_stager.py")
SEND_XY = (900, 900)
OP_RE = re.compile(r"^/\*op:(\w+)\*//\*args:(.*?)\*/")


class MdToHtml(unittest.TestCase):
    def test_paragraphs_and_breaks(self):
        self.assertEqual(ds.md_to_html("one\ntwo\n\nthree"), "<p>one<br>two</p><p>three</p>")

    def test_inline(self):
        h = ds.md_to_html("a **b** and *c* and `d<e>` and [x](https://e.com/?a=1&b=2)")
        self.assertIn("<b>b</b>", h)
        self.assertIn("<i>c</i>", h)
        self.assertIn("<code>d&lt;e&gt;</code>", h)
        self.assertIn('<a href="https://e.com/?a=1&amp;b=2">x</a>', h)

    def test_escapes_html_and_blocks_js_links(self):
        h = ds.md_to_html("<script>x</script> [bad](javascript:alert(1))")
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertNotIn("<a ", h)

    def test_lists_headings_fences(self):
        h = ds.md_to_html("# Title\n\n- a\n- b *i*\n\n1. one\n2. two\n\n```\nx < y\n**no**\n```\nafter")
        self.assertIn("<h1>Title</h1>", h)
        self.assertIn("<ul><li>a</li><li>b <i>i</i></li></ul>", h)
        self.assertIn("<ol><li>one</li><li>two</li></ol>", h)
        self.assertIn("<pre><code>x &lt; y\n**no**</code></pre>", h)
        self.assertTrue(h.endswith("<p>after</p>"))

    def test_snake_case_and_stars_are_not_italics(self):
        h = ds.md_to_html("snake_case_name and 2 * 3 * 4")
        self.assertNotIn("<i>", h)


class Args(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stager168_args_")
        self.md = str(Path(self.tmp.name, "d.md"))
        Path(self.md).write_text("hi")
        self.lock = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, *extra):
        return ds.parse_args(["--chat", "Ann Lee", "--md", self.md, *extra])

    def test_defaults_and_port_env(self):
        with patch.dict(os.environ, {"WHOSAID_CDP_PORT": ""}):
            self.assertEqual(ds.parse_args(["--chat", "a", "--md", "b"]).cdp_port, 9222)
        with patch.dict(os.environ, {"WHOSAID_CDP_PORT": "9333"}):
            self.assertEqual(ds.parse_args(["--chat", "a", "--md", "b"]).cdp_port, 9333)

    def test_outbox_gate(self):
        with self.assertRaises(ds.StagerError) as e:
            ds.check_args(self.args(), env={})
        self.assertEqual(e.exception.code, 2)
        self.assertIn("TEAMS_OUTBOX_LOCK", str(e.exception))
        with self.assertRaises(ds.StagerError):
            ds.check_args(self.args(), env={"TEAMS_OUTBOX_LOCK": "/tmp/stager168_nope_xyz"})
        ds.check_args(self.args(), env={"TEAMS_OUTBOX_LOCK": self.lock})
        ds.check_args(self.args("--bypass-outbox"), env={})

    def test_wave2_flags_not_implemented(self):
        for flag in (["--mention", "Bob"], ["--new-chat"], ["--only-these-attachments"]):
            with self.assertRaises(ds.StagerError) as e:
                ds.check_args(self.args(*flag), env={"TEAMS_OUTBOX_LOCK": self.lock})
            self.assertEqual(e.exception.code, 2)
            self.assertIn("not implemented yet", str(e.exception))

    def test_bad_inputs_are_usage_errors(self):
        env = {"TEAMS_OUTBOX_LOCK": self.lock}
        with self.assertRaises(ds.StagerError) as e:
            ds.check_args(self.args("--attach", "relative.html"), env=env)
        self.assertEqual(e.exception.code, 2)
        a = ds.parse_args(["--chat", "a", "--md", "/tmp/stager168_missing.md"])
        with self.assertRaises(ds.StagerError):
            ds.check_args(a, env=env)

    def test_enter_is_refused_at_the_choke_point(self):
        for p in ({"key": "Enter"}, {"key": "x", "code": "Enter"}, {"key": "Return"},
                  {"key": "x", "windowsVirtualKeyCode": 13}, {"key": "x", "text": "a\n"}):
            self.assertTrue(ds._is_enter(p), p)
        self.assertFalse(ds._is_enter({"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8}))
        self.assertFalse(ds._is_enter({"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27}))


class FakeTeams:
    """One fake browser: http discovery + websocket CDP over a tiny Teams DOM."""

    def __init__(self, rail=(), search=(), compose="", teams_tab=True):
        from websockets.sync.server import serve
        self.rail, self.search_dir, self.compose = list(rail), list(search), compose
        self.chat = None
        self.title = "Chat | Microsoft Teams"
        self.typed = ""
        self.search_open = False
        self.selected = False
        self.chips: list[str] = []
        self.menu_open = False
        self.calls: list[tuple[str, dict]] = []
        self.ops: list[str] = []
        self.clicks: list[tuple[float, float]] = []
        self.teams_tab = teams_tab
        self.srv = serve(self.handler, "127.0.0.1", 0)
        self.ws_port = self.srv.socket.getsockname()[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{fake.ws_port}/devtools/browser/x"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.port = self.http.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.http.shutdown()
        self.http.server_close()

    # geometry
    def rail_xy(self, i):
        return (50, 100 + 40 * i)

    def opt_xy(self, i):
        return (300, 200 + 40 * i)

    SEARCH_XY, ATTACH_XY, MENU_XY = (200, 20), (500, 880), (520, 840)

    def click(self, x, y):
        self.clicks.append((x, y))
        near = lambda p: abs(p[0] - x) < 5 and abs(p[1] - y) < 5  # noqa: E731
        for i, n in enumerate(self.rail):
            if near(self.rail_xy(i)):
                self.chat, self.title = n, f"Chat | {n} | Microsoft Teams"
        if near(self.SEARCH_XY):
            self.search_open = True
        for i, n in enumerate(self.options()):
            if self.search_open and near(self.opt_xy(i)):
                self.chat, self.title, self.search_open = n, f"Chat | {n} | Microsoft Teams", False
        if near(self.ATTACH_XY):
            self.menu_open = True
        if near(self.MENU_XY) and self.menu_open:
            self.pending_chooser = True
        if near(SEND_XY):
            self.compose = ""  # a send would empty the box; the tests also assert it never happens

    def options(self):
        return [n for n in self.search_dir if self.search_open and self.typed and self.typed in n]

    def evaluate(self, expr):
        m = OP_RE.match(expr)
        assert m, f"evaluate without an op marker: {expr[:80]}"
        name, args = m.group(1), json.loads(m.group(2))
        self.ops.append(name)
        if name == "rail_find":
            hits = [{"x": self.rail_xy(i)[0], "y": self.rail_xy(i)[1]} for i, n in enumerate(self.rail) if n == args["name"]]
            return {"total": len(self.rail), "matches": hits}
        if name == "title":
            return self.title
        if name == "editor_ready":
            return self.chat is not None
        if name == "compose_len":
            return len(self.compose.strip())
        if name == "editor_select":
            self.selected = True
            return None
        if name == "paste":
            self.compose = args["plain"]
            self.pasted_html = args["html"]
            return {"len": len(self.compose.strip()), "links": args["html"].count("<a ")}
        if name == "search_box":
            return {"x": self.SEARCH_XY[0], "y": self.SEARCH_XY[1]}
        if name == "search_find":
            opts = self.options()
            return {"total": len(opts), "matches": [{"x": self.opt_xy(i)[0], "y": self.opt_xy(i)[1]}
                                                    for i, n in enumerate(opts) if n == args["name"]]}
        if name == "hit_test":
            return abs(args["x"] - SEND_XY[0]) < 20 and abs(args["y"] - SEND_XY[1]) < 20
        if name == "attach_btn":
            return {"x": self.ATTACH_XY[0], "y": self.ATTACH_XY[1]}
        if name == "menu_upload":
            return {"x": self.MENU_XY[0], "y": self.MENU_XY[1]} if self.menu_open else None
        if name == "chip_seen":
            return args["name"] in self.chips
        if name == "final":
            return {"len": len(self.compose.strip()), "links": getattr(self, "pasted_html", "").count("<a ")}
        return None  # editor_focus, caret_end

    def handler(self, ws):
        try:
            self.serve_conn(ws)
        except Exception as e:  # the stager hangs up on errors; only that is expected
            if "ConnectionClosed" not in type(e).__name__:
                raise

    def serve_conn(self, ws):
        self.pending_chooser = False
        for raw in ws:
            msg = json.loads(raw)
            method, params = msg["method"], msg.get("params", {})
            self.calls.append((method, params))
            result: dict = {}
            if method == "Target.getTargets":
                result = {"targetInfos": [{"targetId": "T1", "type": "page",
                                           "url": "https://teams.cloud.microsoft/v2/" if self.teams_tab else "https://example.com/"}]}
            elif method == "Target.createTarget":
                result = {"targetId": "T1"}
                self.teams_tab = True
            elif method == "Target.attachToTarget":
                result = {"sessionId": "S1"}
            elif method == "Runtime.evaluate":
                result = {"result": {"type": "object", "value": self.evaluate(params["expression"])}}
            elif method == "Input.dispatchMouseEvent":
                if params["type"] == "mousePressed":
                    self.click(params["x"], params["y"])
                    if self.pending_chooser:
                        self.pending_chooser = False
                        ws.send(json.dumps({"method": "Page.fileChooserOpened", "sessionId": "S1",
                                            "params": {"backendNodeId": 77, "mode": "selectMultiple"}}))
            elif method == "Input.dispatchKeyEvent":
                if params.get("key") == "Backspace" and params["type"] == "keyDown" and self.selected:
                    self.compose, self.selected = "", False
            elif method == "Input.insertText":
                self.typed += params["text"]
            elif method == "DOM.getDocument":
                result = {"root": {"nodeId": 1}}
            elif method == "DOM.querySelector":
                result = {"nodeId": 0}
            elif method == "DOM.setFileInputFiles":
                self.chips += [os.path.basename(f) for f in params["files"]]
            ws.send(json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}))  # event noise
            ws.send(json.dumps({"id": msg["id"], "result": result}))

    # assertions every case shares
    def never_sent(self, tc: unittest.TestCase):
        keys = [p for m, p in self.calls if m == "Input.dispatchKeyEvent"]
        tc.assertFalse([k for k in keys if str(k.get("key", "")).lower() in ("enter", "return")
                        or str(k.get("code", "")).lower() == "enter" or k.get("windowsVirtualKeyCode") == 13],
                       f"an Enter key was dispatched: {keys}")
        tc.assertFalse([c for c in self.clicks if abs(c[0] - SEND_XY[0]) < 20 and abs(c[1] - SEND_XY[1]) < 20],
                       f"the send button was clicked: {self.clicks}")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stager168_e2e_")
        self.d = Path(self.tmp.name)
        self.md = self.d / "draft.md"
        self.md.write_text("Hi **Ann**, see [the doc](https://example.com/d).\n\n- one\n- two\n")
        self.page = self.d / "page.html"
        self.page.write_text("<html>x</html>")
        self.lock = self.d / "lock"
        self.lock.mkdir()
        self.fakes: list[FakeTeams] = []

    def tearDown(self):
        for f in self.fakes:
            f.close()
        self.tmp.cleanup()

    def fake(self, **kw):
        f = FakeTeams(**kw)
        self.fakes.append(f)
        return f

    def run_stager(self, fake, *extra, lock=True, chat="Ann Lee"):
        env = {k: v for k, v in os.environ.items() if k != "TEAMS_OUTBOX_LOCK"}
        env["WHOSAID_STAGER_TIME_SCALE"] = "0.01"
        if lock:
            env["TEAMS_OUTBOX_LOCK"] = str(self.lock)
        return subprocess.run([sys.executable, SCRIPT, "--chat", chat, "--md", str(self.md),
                               "--cdp-port", str(fake.port), *extra],
                              capture_output=True, text=True, env=env, timeout=40)

    def test_stage_into_rail_chat(self):
        f = self.fake(rail=["Zed", "Ann Lee", "Bob"])
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIs(out["sent"], False)
        self.assertIn("Ann Lee", out["chat"])
        self.assertGreater(out["chars"], 0)
        self.assertEqual((out["links"], out["attachments"]), (1, 0))
        self.assertIn("<b>Ann</b>", f.pasted_html)
        self.assertIn(("Target.activateTarget", {"targetId": "T1"}), f.calls)
        f.never_sent(self)

    def test_chat_found_only_via_search(self):
        f = self.fake(rail=["Zed"], search=["Ann Lee", "Ann Leeson"])
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("search_box", f.ops)
        self.assertIn("Ann Lee", f.title)
        f.never_sent(self)

    def test_ambiguous_in_rail_refused(self):
        f = self.fake(rail=["Ann Lee", "Ann Lee"])
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ambiguous: 2 chats named Ann Lee", r.stderr)
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_ambiguous_in_search_refused(self):
        f = self.fake(rail=["Zed"], search=["Ann Lee", "Ann Lee"])
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ambiguous: 2 chats named Ann Lee", r.stderr)
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_no_such_chat(self):
        f = self.fake(rail=["Zed"], search=["Someone Else"])
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_nonempty_compose_refused_then_replaced(self):
        f = self.fake(rail=["Ann Lee"], compose="half-written reply")
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already holds", r.stderr)
        self.assertNotIn("paste", f.ops)
        self.assertEqual(f.compose, "half-written reply")
        f.never_sent(self)
        r = self.run_stager(f, "--replace")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Backspace", [p.get("key") for m, p in f.calls if m == "Input.dispatchKeyEvent"])
        self.assertTrue(f.compose.startswith("Hi **Ann**"))
        self.assertLess(f.ops.index("editor_select"), f.ops.index("paste"))
        f.never_sent(self)

    def test_attach_one_page(self):
        f = self.fake(rail=["Ann Lee"])
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["attachments"], 1)
        self.assertEqual(f.chips, ["page.html"])
        self.assertIn(("Page.setInterceptFileChooserDialog", {"enabled": True}), f.calls)
        self.assertIn("chip_seen", f.ops)
        f.never_sent(self)

    def test_attach_chip_never_shows(self):
        f = self.fake(rail=["Ann Lee"])
        f.chips_hidden = True
        orig = f.evaluate
        f.evaluate = lambda e: False if "op:chip_seen" in e else orig(e)
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 1)
        self.assertIn("never showed up", r.stderr)
        f.never_sent(self)

    def test_no_outbox_lock_refused_before_touching_the_browser(self):
        f = self.fake(rail=["Ann Lee"])
        r = self.run_stager(f, lock=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("TEAMS_OUTBOX_LOCK", r.stderr)
        self.assertEqual(f.calls, [])
        r = self.run_stager(f, "--bypass-outbox", lock=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        f.never_sent(self)

    def test_no_browser(self):
        f = self.fake(rail=["Ann Lee"])
        f.close()
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no browser on DevTools port", r.stderr)

    def test_opens_teams_tab_when_none(self):
        f = self.fake(rail=["Ann Lee"], teams_tab=False)
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Target.createTarget", [m for m, _ in f.calls])
        f.never_sent(self)

    def test_send_button_under_target_is_refused(self):
        f = self.fake(rail=["Ann Lee"])
        f.rail_xy = lambda i: SEND_XY  # the "chat" sits where the send button is
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 1)
        self.assertIn("never sends", r.stderr)
        f.never_sent(self)


class BuiltInStager(unittest.TestCase):
    def test_builtin_when_unset_override_when_set(self):
        with patch.dict(os.environ, {"WHOSAID_DRAFTS_STAGER": ""}):
            argv = drafts.stager()
        self.assertEqual(argv[:6], ["uv", "run", "--quiet", "--with", "websockets", "python"])
        self.assertEqual(argv[6], SCRIPT)
        self.assertTrue(Path(argv[6]).exists())
        with patch.dict(os.environ, {"WHOSAID_DRAFTS_STAGER": "my-stager --flag"}):
            self.assertEqual(drafts.stager(), ["my-stager", "--flag"])


if __name__ == "__main__":
    unittest.main()
