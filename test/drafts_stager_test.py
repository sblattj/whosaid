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

# No test may reach a real Teams: the desktop app (9333) or a browser (9222) may be running here.
# The default app port points at a closed one; tests that want a port set it themselves.
os.environ["WHOSAID_TEAMS_CDP_PORT"] = "9"
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

    def test_link_schemes_allowlist(self):
        for bad in ("\x01javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "vbscript:x",
                    "file:///etc/passwd", "foo:bar"):
            h = ds.md_to_html(f"[label]({bad})")
            self.assertNotIn("<a ", h, bad)
            self.assertNotIn("href", h, bad)
            self.assertIn("label", h, bad)
        for good in ("https://ok", "http://ok/a?b=1", "mailto:a@b.co", "/relative/path", "#anchor", "page.html"):
            self.assertIn('<a href="', ds.md_to_html(f"[label]({good})"), good)
        self.assertIn('<a href="https://ok">label</a>', ds.md_to_html("[label](https://ok)"))

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

    def test_teams_tab_regex_is_anchored_to_the_host(self):
        for u in ("https://teams.cloud.microsoft/v2/", "https://teams.microsoft.com/_#/conversations",
                  "https://TEAMS.microsoft.com/"):
            self.assertTrue(ds.IS_TEAMS.search(u), u)
        for u in ("https://evil.example/?next=https://teams.microsoft.com/", "https://example.com/teams.microsoft.com/",
                  "https://teams.microsoft.com.evil.example/", "http://teams.microsoft.com/"):
            self.assertFalse(ds.IS_TEAMS.search(u), u)

    def test_mention_literal_kept_inside_code(self):
        md = "Hi @Ann Lee look\n`@Ann Lee` stays\n```\n@Ann Lee in a block\n```\nbye @Ann Lee"
        out = ds.strip_mention_literals(md, ["Ann Lee"])
        self.assertEqual(out, "Hi look\n`@Ann Lee` stays\n```\n@Ann Lee in a block\n```\nbye ")


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
        with patch.dict(os.environ, {"WHOSAID_CDP_PORT": "", "WHOSAID_TEAMS_CDP_PORT": ""}):
            a = ds.parse_args(["--chat", "a", "--md", "b"])
            self.assertEqual((a.cdp_port, a.app_port), (None, 9333))  # settled later by resolve_port
        with patch.dict(os.environ, {"WHOSAID_CDP_PORT": "9444"}):
            self.assertEqual(ds.parse_args(["--chat", "a", "--md", "b"]).cdp_port, 9444)
        with patch.dict(os.environ, {"WHOSAID_CDP_PORT": "", "WHOSAID_TEAMS_CDP_PORT": "9555"}):
            self.assertEqual(ds.parse_args(["--chat", "a", "--md", "b"]).app_port, 9555)

    def test_outbox_gate(self):
        with self.assertRaises(ds.StagerError) as e:
            ds.check_args(self.args(), env={})
        self.assertEqual(e.exception.code, 2)
        self.assertIn("TEAMS_OUTBOX_LOCK", str(e.exception))
        with self.assertRaises(ds.StagerError):
            ds.check_args(self.args(), env={"TEAMS_OUTBOX_LOCK": "/tmp/stager168_nope_xyz"})
        ds.check_args(self.args(), env={"TEAMS_OUTBOX_LOCK": self.lock})
        ds.check_args(self.args("--bypass-outbox"), env={})

    def test_only_these_attachments_needs_replace(self):
        env = {"TEAMS_OUTBOX_LOCK": self.lock}
        with self.assertRaises(ds.StagerError) as e:
            ds.check_args(self.args("--only-these-attachments"), env=env)
        self.assertEqual(e.exception.code, 2)
        self.assertIn("requires --replace", str(e.exception))
        ds.check_args(self.args("--only-these-attachments", "--replace"), env=env)
        ds.check_args(self.args("--mention", "Bob", "--new-chat"), env=env)

    def test_send_guard_is_narrow(self):
        attach = {"data-tid": "sendMessageCommands-FilePicker", "aria-label": "Attach files"}
        self.assertFalse(css_matches(ds.SEND_GUARD, attach))
        for attrs in ({"data-tid": "sendMessageCommands-send"}, {"data-tid": "send-button"},
                      {"aria-label": "Send"}, {"aria-label": "Send (Ctrl+Enter)"}, {"aria-label": "Send message"}):
            self.assertTrue(css_matches(ds.SEND_GUARD, attrs), attrs)
        self.assertFalse(css_matches(ds.SEND_GUARD, {"aria-label": "Sender info"}))

    def test_strip_mention_literals(self):
        self.assertEqual(ds.strip_mention_literals("Hi @Bob Ray, see @Bob Rays and a@Bob Ray", ["Bob Ray"]),
                         "Hi , see @Bob Rays and a@Bob Ray")

    def test_bad_inputs_are_usage_errors(self):
        env = {"TEAMS_OUTBOX_LOCK": self.lock}
        with self.assertRaises(ds.StagerError) as e:
            ds.check_args(self.args("--attach", "relative.html"), env=env)
        self.assertEqual(e.exception.code, 2)
        a = ds.parse_args(["--chat", "a", "--md", "/tmp/stager168_missing.md"])
        with self.assertRaises(ds.StagerError):
            ds.check_args(a, env=env)

    def test_mentions_validated_before_the_browser(self):
        env = {"TEAMS_OUTBOX_LOCK": self.lock}
        for bad in ("", "   ", "Bob\nRay", "Bob\rRay"):
            with self.assertRaises(ds.StagerError, msg=repr(bad)) as e:
                ds.check_args(self.args("--mention", bad), env=env)
            self.assertEqual(e.exception.code, 2)
        ds.check_args(self.args("--mention", "Bob Ray"), env=env)

    def test_non_utf8_md_is_a_usage_error(self):
        Path(self.md).write_bytes(b"caf\xe9 \xff\xfe")
        with self.assertRaises(ds.StagerError) as e:
            ds.check_args(self.args(), env={"TEAMS_OUTBOX_LOCK": self.lock})
        self.assertEqual(e.exception.code, 2)
        self.assertIn("UTF-8", str(e.exception))

    def test_enter_guard_covers_every_way_to_commit(self):
        for p in ({"type": "keyDown", "commands": ["insertNewline"]}, {"type": "rawKeyDown", "commands": []},
                  {"key": "x", "nativeVirtualKeyCode": 36}, {"key": "x", "nativeVirtualKeyCode": 76},
                  {"key": "x", "unmodifiedText": "\r"}, {"key": "x", "unmodifiedText": "a\n"},
                  {"key": "x", "text": "\r"}):
            self.assertTrue(ds._is_enter(p), p)

    def test_choke_point_writes_nothing_to_the_socket(self):
        """Drive CDP.send itself with a stub websocket: refusal must happen BEFORE any byte is written."""
        import asyncio

        class StubWS:
            def __init__(self):
                self.sent = []
                self.replies = asyncio.Queue()

            async def send(self, data):
                self.sent.append(data)
                self.replies.put_nowait(json.dumps({"id": json.loads(data)["id"], "result": {}}))

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.replies.get()

        async def go():
            ws = StubWS()
            c = ds.CDP(ws)
            refused = [("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter"}),
                       ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "commands": ["insertNewline"]}),
                       ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "nativeVirtualKeyCode": 36}),
                       ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "nativeVirtualKeyCode": 76}),
                       ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "unmodifiedText": "\n"}),
                       ("Input.dispatchKeyEvent", {"type": "char", "key": "a", "text": "\r"}),
                       ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "windowsVirtualKeyCode": 13}),
                       ("Input.insertText", {"text": "a\nb"})]
            try:
                for method, params in refused:
                    with self.assertRaises(ds.StagerError, msg=str((method, params))):
                        await c.send(method, params)
                self.assertEqual(ws.sent, [], "something reached the socket")
            finally:
                c.reader.cancel()

        asyncio.run(go())

    def test_bad_port_env_is_exit_2_not_a_traceback(self):
        env = {k: v for k, v in os.environ.items() if k != "TEAMS_OUTBOX_LOCK"}
        env["WHOSAID_CDP_PORT"] = "not-a-port"
        r = subprocess.run([sys.executable, SCRIPT, "--chat", "Ann Lee", "--md", self.md, "--bypass-outbox"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("WHOSAID_CDP_PORT", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_bad_app_port_env_is_exit_2_not_a_traceback(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TEAMS_OUTBOX_LOCK", "WHOSAID_CDP_PORT")}
        env["WHOSAID_TEAMS_CDP_PORT"] = "not-a-port"
        r = subprocess.run([sys.executable, SCRIPT, "--chat", "Ann Lee", "--md", self.md, "--bypass-outbox"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("WHOSAID_TEAMS_CDP_PORT", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_non_utf8_md_via_cli_is_exit_2_not_a_traceback(self):
        Path(self.md).write_bytes(b"\xff\xfe bad")
        env = {k: v for k, v in os.environ.items() if k != "TEAMS_OUTBOX_LOCK"}
        r = subprocess.run([sys.executable, SCRIPT, "--chat", "Ann Lee", "--md", self.md, "--bypass-outbox"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_enter_is_refused_at_the_choke_point(self):
        for p in ({"key": "Enter"}, {"key": "x", "code": "Enter"}, {"key": "Return"},
                  {"key": "x", "windowsVirtualKeyCode": 13}, {"key": "x", "text": "a\n"}):
            self.assertTrue(ds._is_enter(p), p)
        self.assertFalse(ds._is_enter({"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8}))
        self.assertFalse(ds._is_enter({"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27}))


def css_matches(selector: str, attrs: dict) -> bool:
    """A tiny CSS matcher for comma lists of [attr], [attr=v], [attr^=v], [attr*=v] with an optional ` i`."""
    for part in selector.split(","):
        m = re.fullmatch(r'\[([\w-]+)(?:([\^*]?=)"([^"]*)"( i)?)?\]', part.strip())
        assert m, f"selector the fake can't read: {part}"
        attr, op_, val, ci = m.groups()
        have = attrs.get(attr)
        if have is None:
            continue
        if op_ is None:
            return True
        h, v = (have.lower(), val.lower()) if ci else (have, val)
        if (op_ == "=" and h == v) or (op_ == "^=" and h.startswith(v)) or (op_ == "*=" and v in h):
            return True
    return False


class FakeTeams:
    """One fake browser: http discovery + websocket CDP over a tiny Teams DOM."""

    def __init__(self, rail=(), search=(), compose="", teams_tab=True, old_chips=(), people=(), mention_dir=(),
                 history=(), no_root=False, remover_noop=False, drop_uploads=False, focus_miss=False,
                 fail_set_files=False, root_flicker=0, hidden=False, own=(), no_edit_item=False, titles=None, mention_lazy=False, targets=None, browser=None,
                 search_groups=None, search_stale="", app_toolbar=False):
        from websockets.sync.server import serve
        self.targets, self.browser = targets, browser  # targets: dicts of id/title/url; browser: /json/version Browser
        self.titles = dict(titles or {})  # rail name -> window title, when it isn't "Chat | <name> | ..."
        self.rail, self.search_dir, self.compose = list(rail), list(search), compose
        self.people_dir, self.mention_dir = list(people), list(mention_dir)
        # attachment cards in the chat HISTORY, outside the compose root, each with a Remove/Delete control
        self.history = list(history)
        self.no_root, self.remover_noop, self.drop_uploads = no_root, remover_noop, drop_uploads
        self.focus_miss, self.fail_set_files = focus_miss, fail_set_files
        # after a clear (Backspace) or a paste, the compose root is missing for this many reads (#172)
        self.root_flicker, self.root_gone = root_flicker, 0
        # a background tab (document.hidden): clicks do nothing until the tab is activated
        self.hidden = hidden
        # --edit-sent (#176): our sent messages, hover/menu/edit state
        self.own, self.no_edit_item = list(own), no_edit_item
        # the Teams desktop app: Edit is a button on the hover toolbar, not in the More menu, and every
        # message also carries a hidden 4px "More message options" button that opens nothing
        self.app_toolbar = app_toolbar
        self.hovered = self.more_menu = self.editing = None
        self.edit_text, self.edit_selected, self.edit_saved = "", False, False
        self.newchat_open = self.picker_focus = self.compose_focused = False
        self.picker_typed = self.mention_typed = ""
        self.mention_keyed = False
        # like Teams right after a clear: the first real key after "@..." opens no list (a retype does)
        self.mention_lazy, self.mention_keys = mention_lazy, 0
        self.to_chips: list[str] = []
        self.mentions: list[str] = []
        # elements with attributes, for the hit test: (x, y) -> attrs
        self.elements = {SEND_XY: {"data-tid": "sendMessageCommands-send", "aria-label": "Send"},
                         self.ATTACH_XY: {"data-tid": "sendMessageCommands-FilePicker",
                                          "aria-label": "Attach files"},
                         self.DONE_XY: {"aria-label": "Done"}}
        self.chat = None
        self.title = "Chat | Microsoft Teams"
        # Teams search keeps the last query in the box, so typing appends to it unless it is cleared first
        self.typed = search_stale
        self.search_sel = False
        # group chats Teams search lists only for a member's name, never for the rail name itself
        self.search_groups = dict(search_groups or {})
        self.search_open = False
        self.selected = False
        self.chips: list[str] = list(old_chips)
        self.menu_open = False
        self.calls: list[tuple[str, dict]] = []
        self.ops: list[str] = []
        self.clicks: list[tuple[float, float]] = []
        self.history_clicked: list[str] = []
        self.teams_tab = teams_tab
        self.srv = serve(self.handler, "127.0.0.1", 0)
        self.ws_port = self.srv.socket.getsockname()[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                doc = {"webSocketDebuggerUrl": f"ws://127.0.0.1:{fake.ws_port}/devtools/browser/x"}
                if fake.browser:
                    doc["Browser"] = fake.browser
                body = json.dumps(doc).encode()
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
    EDIT_ITEM_XY, DONE_XY = (740, 500), (760, 560)

    def own_xy(self, i):
        return (650, 100 + 40 * i)

    def more_xy(self, i):
        return (720, 100 + 40 * i)

    def edit_btn_xy(self, i):
        return (690, 100 + 40 * i)

    def hidden_more_xy(self, i):
        return (610, 100 + 40 * i)
    NEWCHAT_XY, PICKER_XY = (30, 20), (250, 60)

    def person_xy(self, i):
        return (300, 400 + 40 * i)

    def mention_xy(self, i):
        return (350, 600 + 40 * i)

    def remover_xy(self, i):
        return (600, 700 + 40 * i)

    def history_xy(self, i):
        return (800, 300 + 40 * i)  # the history card's Remove/Delete control, outside the compose root

    def near_history(self, x, y):
        return any(abs(self.history_xy(i)[0] - x) < 20 and abs(self.history_xy(i)[1] - y) < 20
                   for i in range(len(self.history)))

    def people_options(self):
        return [n for n in self.people_dir if self.newchat_open and self.picker_typed and self.picker_typed in n]

    def mention_options(self):
        t = self.mention_typed
        return [n for n in self.mention_dir if self.mention_keyed and t.startswith("@") and t[1:] in n]

    def click(self, x, y):
        self.clicks.append((x, y))
        near = lambda p: abs(p[0] - x) < 5 and abs(p[1] - y) < 5  # noqa: E731
        for i in range(len(self.own)):
            if near(self.more_xy(i)) and self.hovered == i:
                self.more_menu = i
        for i in range(len(self.own)):
            if self.app_toolbar and near(self.edit_btn_xy(i)) and self.hovered == i:
                self.editing, self.edit_text = i, self.own[i]
        if near(self.EDIT_ITEM_XY) and self.more_menu is not None:
            self.editing, self.edit_text, self.more_menu = self.more_menu, self.own[self.more_menu], None
        if near(self.DONE_XY):
            self.edit_saved = True
        self.compose_focused = False
        if near(self.NEWCHAT_XY):
            self.newchat_open = True
        if near(self.PICKER_XY) and self.newchat_open:
            self.picker_focus = True
        for i, n in enumerate(self.people_options()):
            if self.picker_focus and near(self.person_xy(i)):
                self.to_chips.append(n)
                self.chat, self.picker_focus = n, False  # title stays "Chat | Microsoft Teams"
                self.picker_typed = ""  # Teams clears the To input after a pick
        for i, n in enumerate(self.mention_options()):
            if near(self.mention_xy(i)):
                self.mentions.append(n)
                self.compose = self.compose[:len(self.compose) - len(self.mention_typed)] + n  # element replaces "@typed"
                self.mention_typed = ""
        for i in range(len(self.chips)):
            if near(self.remover_xy(i)) and self.chips[i].startswith("old") and not self.remover_noop:
                del self.chips[i]
                break
        for i in range(len(self.history)):
            if near(self.history_xy(i)):
                self.history_clicked.append(self.history[i])
                del self.history[i]
                break
        for i, n in enumerate(self.rail):
            if near(self.rail_xy(i)):
                self.chat, self.title = n, self.titles.get(n, f"Chat | {n} | Microsoft Teams")
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
        if not (self.search_open and self.typed):
            return []
        return ([n for n in self.search_dir if self.typed in n]
                + [n for n, members in self.search_groups.items() if self.typed in members])

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
        if name == "own_msgs":
            return [{"text": t} for t in self.own] if self.chat else []
        if name == "msg_more":
            norm = lambda t: " ".join(t.split())  # noqa: E731
            i = next((i for i, t in enumerate(self.own) if norm(t).startswith(args["key"])), None)
            if i is None:
                return None
            at = {"x": self.own_xy(i)[0], "y": self.own_xy(i)[1]}
            if self.app_toolbar:  # the old query found the hidden button, hovered or not
                return {"at": at, "more": {"x": self.hidden_more_xy(i)[0], "y": self.hidden_more_xy(i)[1]}}
            return {"at": at, "more": {"x": self.more_xy(i)[0], "y": self.more_xy(i)[1]} if self.hovered == i else None}
        if name == "msg_actions":
            norm = lambda t: " ".join(t.split())  # noqa: E731
            i = next((i for i, t in enumerate(self.own) if norm(t).startswith(args["key"])), None)
            if i is None:
                return None
            xy = lambda p: {"x": p[0], "y": p[1]}  # noqa: E731
            shown = self.hovered == i  # the toolbar shows for the hovered message only
            return {"at": xy(self.own_xy(i)), "more": xy(self.more_xy(i)) if shown else None,
                    "edit": xy(self.edit_btn_xy(i)) if shown and self.app_toolbar else None}
        if name == "menu_item":
            if self.more_menu is not None and args["label"] == "Edit" and not self.no_edit_item and not self.app_toolbar:
                return {"x": self.EDIT_ITEM_XY[0], "y": self.EDIT_ITEM_XY[1]}
            return None
        if name == "edit_ready":
            return {"editors": 1 if self.editing is not None else 0, "text": self.edit_text}
        if name == "edit_len":
            return len(self.edit_text.strip()) if self.editing is not None else -1
        if name == "edit_select":
            self.edit_selected = True
            return None
        if name == "edit_paste":
            self.edit_text += args["plain"]
            return {"len": len(self.edit_text.strip()), "links": args["html"].count("<a ")}
        if name == "editor_ready":
            return self.chat is not None
        if name == "compose_len":
            return len(self.compose.strip())
        if name == "editor_select":
            self.selected = True
            return None
        if name in ("editor_focus", "caret_end"):
            self.compose_focused = True
            return None
        if name == "chip_removers":  # LEGACY page-wide query (the old over-broad one): chips AND history
            return ([{"x": self.history_xy(i)[0], "y": self.history_xy(i)[1]} for i in range(len(self.history))]
                    + [{"x": self.remover_xy(i)[0], "y": self.remover_xy(i)[1]}
                       for i, n in enumerate(self.chips) if n.startswith("old")])  # history is above in the DOM
        if name == "root_chip_removers":
            return [{"x": self.remover_xy(i)[0], "y": self.remover_xy(i)[1]}
                    for i, n in enumerate(self.chips) if n.startswith("old")]
        if name == "root_chip_names":
            if self.root_gone > 0:
                self.root_gone -= 1
                return None
            return None if self.no_root else list(self.chips)
        if name == "in_root":
            return not self.no_root and not self.near_history(args["x"], args["y"])
        if name == "focus_is":
            if self.focus_miss:
                return False
            return self.search_open if args["sel"] == ds.SEARCH_SEL else self.picker_focus
        if name == "new_chat_btn":
            return {"x": self.NEWCHAT_XY[0], "y": self.NEWCHAT_XY[1]}
        if name == "people_picker":
            return {"x": self.PICKER_XY[0], "y": self.PICKER_XY[1]} if self.newchat_open else None
        if name == "people_find":
            opts = self.people_options()
            return {"total": len(opts), "matches": [{"x": self.person_xy(i)[0], "y": self.person_xy(i)[1]}
                                                    for i, n in enumerate(opts) if n == args["name"]]}
        if name == "to_chip":
            return args["name"] in self.to_chips
        if name == "mention_count":
            return len(self.mentions)
        if name == "mention_find":
            opts = self.mention_options()
            return {"total": len(opts), "matches": [{"x": self.mention_xy(i)[0], "y": self.mention_xy(i)[1]}
                                                    for i, n in enumerate(opts) if n == args["name"]]}
        if name == "paste":
            self.compose += args["plain"]  # at the caret, which is at the end
            self.root_gone = self.root_flicker
            self.pasted_html = args["html"]
            return {"len": len(self.compose.strip()), "links": args["html"].count("<a ")}
        if name == "search_box":
            if self.cur_session == "S0":  # the empty host page has no search box
                return None
            return {"x": self.SEARCH_XY[0], "y": self.SEARCH_XY[1]}
        if name == "search_select":
            self.search_sel = self.search_open
            return self.search_open
        if name == "search_find":
            opts = self.options()
            return {"total": len(opts), "matches": [{"x": self.opt_xy(i)[0], "y": self.opt_xy(i)[1]}
                                                    for i, n in enumerate(opts) if n == args["name"]]}
        if name == "hit_test":
            for xy, attrs in self.elements.items():
                if abs(args["x"] - xy[0]) < 20 and abs(args["y"] - xy[1]) < 20 and css_matches(args["sel"], attrs):
                    return True
            return False
        if name == "attach_btn":
            return {"x": self.ATTACH_XY[0], "y": self.ATTACH_XY[1]}
        if name == "menu_upload":
            return {"x": self.MENU_XY[0], "y": self.MENU_XY[1]} if self.menu_open else None
        if name == "chip_seen":  # LEGACY page-wide check: the name anywhere, history included
            return args["name"] in self.chips or args["name"] in self.history
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
        self.cur_session = None
        for raw in ws:
            msg = json.loads(raw)
            method, params = msg["method"], msg.get("params", {})
            self.calls.append((method, params))
            self.cur_session = msg.get("sessionId")
            result: dict = {}
            if method == "Target.getTargets":
                if self.targets is not None:
                    result = {"targetInfos": [{"targetId": t["id"], "type": "page", "title": t["title"], "url": t["url"]}
                                              for t in self.targets]}
                else:
                    result = {"targetInfos": [{"targetId": "T1", "type": "page",
                                               "url": "https://teams.cloud.microsoft/v2/" if self.teams_tab else "https://example.com/"}]}
            elif method == "Target.createTarget":
                result = {"targetId": "T1"}
                self.teams_tab = True
            elif method == "Target.attachToTarget":
                result = {"sessionId": "S" + params["targetId"][1:]}
            elif method == "Target.detachFromTarget":
                pass
            elif method == "Runtime.evaluate":
                result = {"result": {"type": "object", "value": self.evaluate(params["expression"])}}
            elif method in ("Target.activateTarget", "Page.bringToFront"):
                self.hidden = False
            elif method == "Input.dispatchMouseEvent":
                if params["type"] == "mouseMoved":
                    self.hovered = next((i for i in range(len(self.own)) if abs(self.own_xy(i)[0] - params["x"]) < 5
                                         and abs(self.own_xy(i)[1] - params["y"]) < 5), self.hovered)
                if params["type"] == "mousePressed" and not self.hidden:
                    self.click(params["x"], params["y"])
                    if self.pending_chooser:
                        self.pending_chooser = False
                        ws.send(json.dumps({"method": "Page.fileChooserOpened", "sessionId": "S1",
                                            "params": {"backendNodeId": 77, "mode": "selectMultiple"}}))
            elif method == "Input.dispatchKeyEvent":
                if params.get("key") == "Backspace" and params["type"] == "keyDown" and self.search_sel:
                    self.typed, self.search_sel = "", False
                if params.get("key") == "Backspace" and params["type"] == "keyDown" and self.edit_selected:
                    self.edit_text, self.edit_selected = "", False
                if params.get("key") == "Backspace" and params["type"] == "keyDown" and self.selected:
                    self.compose, self.selected = "", False
                    self.root_gone = self.root_flicker
                    self.mention_typed, self.mentions = "", []
                elif params.get("key") == "Backspace" and params["type"] == "keyDown" and self.compose_focused:
                    self.compose = self.compose[:-1]
                    self.mention_typed = self.mention_typed[:-1]
                if params["type"] == "keyDown" and params.get("text") and self.compose_focused:
                    self.compose += params["text"]
                    if self.mention_typed:
                        self.mention_typed += params["text"]
                        self.mention_keys += 1
                        # like Teams, the picker filters only after a real key
                        self.mention_keyed = not self.mention_lazy or self.mention_keys > 1
            elif method == "Input.insertText":
                t = params["text"]
                if self.picker_focus:
                    self.picker_typed += t
                elif self.compose_focused:
                    if t.startswith("@"):
                        self.mention_typed, self.mention_keyed, self.mention_keys = t, False, 0
                    self.compose += t  # the typed text is in the box until a suggestion replaces it
                else:
                    self.typed = t if self.search_sel else self.typed + t  # a selected query is replaced
                    self.search_sel = False
            elif method == "DOM.getDocument":
                result = {"root": {"nodeId": 1}}
            elif method == "DOM.querySelector":
                result = {"nodeId": 0}
            elif method == "DOM.setFileInputFiles":
                if self.fail_set_files:
                    ws.send(json.dumps({"id": msg["id"], "error": {"message": "boom"}}))
                    continue
                if not self.drop_uploads:
                    self.chips += [os.path.basename(f) for f in params["files"]]
            ws.send(json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}))  # event noise
            ws.send(json.dumps({"id": msg["id"], "result": result}))

    # assertions every case shares
    def never_sent(self, tc: unittest.TestCase):
        tc.assertFalse([p for m, p in self.calls if "commands" in p], "an editing command was sent")
        tc.assertFalse([p for m, p in self.calls if m == "Input.insertText" and re.search("[\r\n]", p["text"])],
                       "inserted text holds a newline")
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

    def run_stager(self, fake, *extra, lock=True, chat="Ann Lee", env_extra=None):
        env = {k: v for k, v in os.environ.items() if k != "TEAMS_OUTBOX_LOCK"}
        env["WHOSAID_STAGER_TIME_SCALE"] = "0.01"
        env.update(env_extra or {})
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

    def test_search_box_holding_an_old_query_is_cleared_first(self):
        f = self.fake(rail=["Zed"], search=["Ann Lee"], search_stale="Ann LeeAnn Lee")
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ann Lee", f.title)
        f.never_sent(self)

    def test_group_chat_not_in_rail_found_by_member_search(self):
        # an older group chat is off the rail, and Teams search lists it only under a member's name
        f = self.fake(rail=["Zed"], search=["Bo Chen"], search_groups={"Ann and Bo Chen": ["Bo Chen", "Ann"]},
                      search_stale="leftover")
        r = self.run_stager(f, chat="Ann and Bo Chen")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.chat, "Ann and Bo Chen")
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIs(out["sent"], False)
        self.assertGreaterEqual(f.ops.count("search_find"), 2)
        f.never_sent(self)

    def test_group_chat_ambiguous_by_member_search_refused(self):
        f = self.fake(rail=["Zed"])
        # two group chats with the same rail name, both listed under the member's name
        f.options = lambda: ["Ann and Bo Chen", "Ann and Bo Chen"] if f.search_open and f.typed == "Bo Chen" else []
        r = self.run_stager(f, chat="Ann and Bo Chen")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ambiguous: 2 chats named Ann and Bo Chen", r.stderr)
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_chat_not_found_leaves_the_search_box_empty(self):
        f = self.fake(rail=["Zed"], search=["Someone Else"])
        r = self.run_stager(f, chat="Ann and Bo Chen")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no chat named Ann and Bo Chen", r.stderr)
        self.assertIn("Bo Chen", r.stderr.split("no chat named Ann and Bo Chen", 1)[1])  # says what it searched
        self.assertEqual(f.typed, "")
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
        self.assertIn("whosaid drafts stage", r.stderr)
        self.assertIn("--replace", r.stderr)
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
        self.assertIn("root_chip_names", f.ops)
        f.never_sent(self)

    def test_chip_selector_matches_real_teams_markup(self):
        # Seen on real Teams 2026-11-06: <div data-tid="file-well"><div role="group" aria-label="<file>">...
        # <button aria-label="Remove attachment">. The old guesses matched none of it, so an attached page
        # read as "never showed up" and the stage failed after the attach had worked.
        self.assertEqual(ds.CHIP_PROVEN, '[data-tid="file-well"] > [role="group"]')
        self.assertTrue(ds.CHIP_SEL.startswith(ds.CHIP_PROVEN + ","))
        self.assertIn(json.dumps(ds.CHIP_SEL), ds.JS_ROOT_CHIP_NAMES)
        self.assertIn(json.dumps(ds.CHIP_SEL), ds.JS_ROOT_CHIP_REMOVERS)
        self.assertIn('button[aria-label^="Remove" i]', ds.CHIP_REMOVE_SEL)  # matches "Remove attachment"

    def test_rail_row_still_moving_is_reread_before_click(self):
        f = self.fake(rail=["Zed", "Ann Lee", "Bob"])
        orig, seen = f.evaluate, []

        def stale_first(e):
            r = orig(e)
            if "op:rail_find" in e and not seen:
                seen.append(1)
                return {"total": r["total"], "matches": [{"x": f.rail_xy(0)[0], "y": f.rail_xy(0)[1]}]}  # mid-scroll
            return r
        f.evaluate = stale_first
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ann Lee", f.title)
        self.assertNotIn(f.rail_xy(0), f.clicks)  # never clicked where the row used to be
        f.never_sent(self)

    def test_click_on_wrong_rail_chat_is_retried_once(self):
        f = self.fake(rail=["Zed", "Ann Lee", "Bob"])
        orig, misses = f.click, []

        def first_click_slips(x, y):
            if (x, y) == f.rail_xy(1) and not misses:
                misses.append(1)
                return orig(*f.rail_xy(0))  # the list re-rendered under the pointer
            return orig(x, y)
        f.click = first_click_slips
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ann Lee", f.title)
        self.assertEqual(misses, [1])
        f.never_sent(self)

    def test_click_on_wrong_rail_chat_twice_stops(self):
        f = self.fake(rail=["Zed", "Ann Lee", "Bob"])
        orig = f.click
        f.click = lambda x, y: orig(*f.rail_xy(0)) if (x, y) == f.rail_xy(1) else orig(x, y)
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 1)
        self.assertIn('not Ann Lee; stopping (left rail)', r.stderr)
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_attach_chip_never_shows(self):
        f = self.fake(rail=["Ann Lee"])
        orig = f.evaluate
        f.evaluate = lambda e: [] if "op:root_chip_names" in e else orig(e)
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 1)
        self.assertIn("never showed up", r.stderr)
        f.never_sent(self)

    def test_attach_chip_that_teams_drops_again_is_a_failure(self):
        """The Teams desktop app shows the chip for a moment, then removes it (the upload never lands).
        A chip seen once is not an attachment: it has to stay."""
        f = self.fake(rail=["Ann Lee"])
        orig = f.evaluate

        def flicker(e):
            out = orig(e)
            if "op:root_chip_names" in e and "page.html" in f.chips:
                f.chips.remove("page.html")  # gone right after this read
            return out
        f.evaluate = flicker
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("removed it again", r.stderr)
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

    # ---- Teams desktop app (#196): port order and chat-window pick ----
    T0 = {"id": "T0", "title": "Microsoft Teams", "url": "https://teams.microsoft.com/v2/"}
    T1 = {"id": "T1", "title": "Chat | Microsoft Teams", "url": "https://teams.microsoft.com/v2/"}

    def run_port(self, *, app_fake=None, app_port=None, browser_fake=None, env=None, extra=()):
        """Run with no --cdp-port: the Teams app port and/or the browser port point at fakes.
        The browser default (9222) is swapped for the browser fake by a thin wrapper around resolve_port."""
        e = {k: v for k, v in os.environ.items() if k not in ("TEAMS_OUTBOX_LOCK", "WHOSAID_CDP_PORT",
                                                              "WHOSAID_TEAMS_CDP_PORT")}
        e["WHOSAID_STAGER_TIME_SCALE"] = "0.01"
        e["TEAMS_OUTBOX_LOCK"] = str(self.lock)
        if app_fake is not None:
            e["WHOSAID_TEAMS_CDP_PORT"] = str(app_fake.port)
        elif app_port is not None:
            e["WHOSAID_TEAMS_CDP_PORT"] = str(app_port)
        e.update(env or {})
        bp = browser_fake.port if browser_fake else 0
        code = ("import sys,drafts_stager as d\n"
                "o=d.resolve_port\n"
                "def w(a):\n"
                "    r=o(a)\n"
                f"    if a.cdp_port==9222: a.cdp_port={bp}\n"
                "    return r\n"
                "d.resolve_port=w\n"
                "sys.exit(d.main(sys.argv[1:]))")
        return subprocess.run([sys.executable, "-c", code, "--chat", "Ann Lee", "--md", str(self.md), *extra],
                              capture_output=True, text=True, env=e, timeout=40, cwd=str(Path(SCRIPT).parent))

    def test_app_mode_skips_empty_host_page(self):
        f = self.fake(rail=["Ann Lee"], targets=[self.T0, self.T1], browser="Edg/150.0")
        r = self.run_port(app_fake=f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(("Target.activateTarget", {"targetId": "T1"}), f.calls)
        self.assertNotIn(("Target.activateTarget", {"targetId": "T0"}), f.calls)
        self.assertNotIn("Target.createTarget", [m for m, _ in f.calls])
        f.never_sent(self)

    def test_app_mode_prefers_chat_titled_for_the_requested_name(self):
        other = {"id": "T2", "title": "Chat | Zed Q | Microsoft Teams", "url": self.T1["url"]}
        mine = {"id": "T3", "title": "Chat | Ann Lee | Microsoft Teams", "url": self.T1["url"]}
        f = self.fake(rail=["Ann Lee"], targets=[self.T0, other, mine], browser="Edg/150.0")
        r = self.run_port(app_fake=f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(("Target.activateTarget", {"targetId": "T3"}), f.calls)

    def test_app_mode_with_the_desktop_title_form(self):
        """The real app window title is "Chat | NAME", no "| Microsoft Teams" (checked live 2026-11-10)."""
        other = {"id": "T2", "title": "Chat | Zed Q", "url": self.T1["url"]}
        mine = {"id": "T3", "title": "Chat | Ann Lee", "url": self.T1["url"]}
        f = self.fake(rail=["Ann Lee"], targets=[self.T0, other, mine], browser="Edg/150.0",
                      titles={"Ann Lee": "Chat | Ann Lee"})
        r = self.run_port(app_fake=f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(("Target.activateTarget", {"targetId": "T3"}), f.calls)
        self.assertIn("paste", f.ops)
        f.never_sent(self)

    def test_app_mode_probes_untitled_candidates_for_the_search_box(self):
        win = {"id": "T1", "title": "Microsoft Teams", "url": self.T1["url"]}  # no "Chat |" prefix
        f = self.fake(rail=["Ann Lee"], targets=[self.T0, win], browser="Edg/150.0")
        r = self.run_port(app_fake=f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(("Target.activateTarget", {"targetId": "T1"}), f.calls)
        detached = [m for m, _ in f.calls if m == "Target.detachFromTarget"]
        self.assertGreaterEqual(len(detached), 2, "probe sessions were left attached")

    def test_app_mode_with_only_the_host_page_is_exit_1_and_opens_nothing(self):
        f = self.fake(rail=["Ann Lee"], targets=[self.T0], browser="Edg/150.0")
        r = self.run_port(app_fake=f)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("Teams app is up but no chat window found", r.stderr)
        self.assertNotIn("Target.createTarget", [m for m, _ in f.calls])

    def test_browser_mode_without_a_teams_page_still_opens_one(self):
        f = self.fake(rail=["Ann Lee"], teams_tab=False)
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Target.createTarget", [m for m, _ in f.calls])

    def test_browser_mode_with_edge_takes_the_first_teams_page_unchanged(self):
        # an Edg/ browser reached through --cdp-port is NOT app mode: first page wins, no chat-window pick
        f = self.fake(rail=["Ann Lee"], targets=[self.T0, self.T1], browser="Edg/150.0")
        r = self.run_stager(f)
        self.assertNotIn("Teams app is up", r.stderr)
        self.assertIn(("Target.activateTarget", {"targetId": "T0"}), f.calls)
        self.assertNotIn("Target.detachFromTarget", [m for m, _ in f.calls])

    def test_port_table_app_port_answers_as_edge(self):
        app = self.fake(rail=["Ann Lee"], browser="Edg/150.0")
        br = self.fake(rail=["Ann Lee"])
        r = self.run_port(app_fake=app, browser_fake=br)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(app.calls and not br.calls, "app port first")

    def test_port_table_app_port_answers_as_non_edge(self):
        app = self.fake(rail=["Ann Lee"], browser="Chrome/150.0")
        br = self.fake(rail=["Ann Lee"])
        r = self.run_port(app_fake=app, browser_fake=br)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"port {app.port} is held by Chrome/150.0, not the Teams app; using the browser port", r.stderr)
        self.assertTrue(br.calls and not app.calls, "browser port used")

    def test_port_table_app_port_down(self):
        app = self.fake(rail=["Ann Lee"], browser="Edg/150.0")
        dead = app.port
        app.close()
        br = self.fake(rail=["Ann Lee"])
        r = self.run_port(app_port=dead, browser_fake=br)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(br.calls)
        self.assertNotIn("is held by", r.stderr)

    def test_port_table_explicit_flag_or_env_is_never_probed(self):
        # run the real script (no wrapper) with an Edg/ app fake on the app port: it must stay untouched
        app = self.fake(rail=["Ann Lee"], browser="Edg/150.0", targets=[self.T0])
        for how in ("flag", "env"):
            br = self.fake(rail=["Ann Lee"], targets=[self.T1])
            e = {k: v for k, v in os.environ.items() if k not in ("WHOSAID_CDP_PORT",)}
            e.update({"WHOSAID_STAGER_TIME_SCALE": "0.01", "TEAMS_OUTBOX_LOCK": str(self.lock),
                      "WHOSAID_TEAMS_CDP_PORT": str(app.port)})
            args = ["--cdp-port", str(br.port)] if how == "flag" else []
            if how == "env":
                e["WHOSAID_CDP_PORT"] = str(br.port)
            r = subprocess.run([sys.executable, SCRIPT, "--chat", "Ann Lee", "--md", str(self.md), *args],
                               capture_output=True, text=True, env=e, timeout=40)
            self.assertEqual(r.returncode, 0, f"{how}: {r.stderr}")
            self.assertTrue(br.calls, f"{how}: the explicit port was not used")
            self.assertFalse(app.calls, f"{how}: the app port was used")

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

    def test_attach_button_with_send_in_its_tid_still_works(self):
        f = self.fake(rail=["Ann Lee"])
        self.assertEqual(f.elements[f.ATTACH_XY]["data-tid"], "sendMessageCommands-FilePicker")
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f.ATTACH_XY, f.clicks)
        self.assertEqual(f.chips, ["page.html"])
        f.never_sent(self)

    def test_only_these_attachments_removes_old_chips(self):
        f = self.fake(rail=["Ann Lee"], compose="old text", old_chips=["old1.html", "old2.html"])
        r = self.run_stager(f, "--replace", "--only-these-attachments", "--attach", str(self.page))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.chips, ["page.html"])
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["attachments"], 1)
        self.assertLess(f.ops.index("editor_select"), f.ops.index("root_chip_removers"))
        self.assertLess(f.ops.index("root_chip_names"), f.ops.index("paste"))
        self.assertIn("in_root", f.ops)
        f.never_sent(self)

    def test_old_chips_stay_without_the_flag(self):
        f = self.fake(rail=["Ann Lee"], old_chips=["old1.html"])
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.chips, ["old1.html", "page.html"])
        f.never_sent(self)

    def test_only_these_attachments_fails_when_a_chip_will_not_go(self):
        f = self.fake(rail=["Ann Lee"], old_chips=["old1.html", "stuck.html"])  # no remover for "stuck"
        r = self.run_stager(f, "--replace", "--only-these-attachments")
        self.assertEqual(r.returncode, 1)
        self.assertIn("couldn't remove the old attachments", r.stderr)
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_only_these_attachments_without_replace_is_usage_error(self):
        f = self.fake(rail=["Ann Lee"])
        r = self.run_stager(f, "--only-these-attachments")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(f.calls, [])

    def test_new_chat(self):
        f = self.fake(people=["Ann Leeson", "Ann Lee"])
        r = self.run_stager(f, "--new-chat")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.to_chips, ["Ann Lee"])
        self.assertEqual(f.title, "Chat | Microsoft Teams")  # no NAME in the title: the To chip is the check
        self.assertIn("to_chip", f.ops)
        self.assertNotIn("rail_find", f.ops)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["chat"], "Ann Lee")
        self.assertTrue(f.compose.startswith("Hi **Ann**"))
        f.never_sent(self)

    def test_new_chat_with_two_people_picks_each(self):
        f = self.fake(people=["Ann Lee", "Bob Ray", "Bob Rayne"])
        r = self.run_stager(f, "--new-chat", chat="Ann Lee, Bob Ray")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.to_chips, ["Ann Lee", "Bob Ray"])
        f.never_sent(self)

    def test_one_to_one_refuses_a_group_window_that_lists_the_person(self):
        f = self.fake(rail=["Ann Lee"], titles={"Ann Lee": "Chat | Cy Dee, Ann Lee | Microsoft Teams"})
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not Ann Lee", r.stderr)
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_group_rail_name_matches_its_member_title(self):
        f = self.fake(rail=["Cy and Ann Lee"], titles={"Cy and Ann Lee": "Chat | Cy Dee, Ann Lee | Microsoft Teams"})
        r = self.run_stager(f, chat="Cy and Ann Lee")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("paste", f.ops)
        f.never_sent(self)

    def test_title_matches_rules(self):
        tm = ds.title_matches
        self.assertTrue(tm("Chat | Ann Lee | Microsoft Teams", "Ann Lee"))
        self.assertTrue(tm("Chat | Ann Lee", "Ann Lee"))  # the desktop app's window title
        self.assertFalse(tm("Chat | Cy Dee, Ann Lee", "Ann Lee"))
        self.assertTrue(tm("Chat | Cy Dee, Ann Lee", "Cy and Ann Lee"))
        self.assertFalse(tm("Chat | Cy Dee, Ann Lee | Microsoft Teams", "Ann Lee"))
        self.assertTrue(tm("Chat | Cy Dee, Ann Lee | Microsoft Teams", "Cy and Ann Lee"))
        self.assertTrue(tm("Chat | Cy Dee, Ann Lee | Microsoft Teams", "Ann Lee, Cy Dee"))
        self.assertFalse(tm("Chat | Cy Dee, Ann Lee, Bo Ray | Microsoft Teams", "Cy and Ann Lee"))
        self.assertFalse(tm("Chat | Ann Leeson | Microsoft Teams", "Ann Lee"))
        self.assertTrue(tm("Chat | Release crew | Microsoft Teams", "release crew"))
        self.assertFalse(tm("", "Ann Lee"))

    def test_new_chat_no_such_person(self):
        f = self.fake(people=["Ann Leeson"])
        r = self.run_stager(f, "--new-chat")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no person named Ann Lee", r.stderr)
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_new_chat_ambiguous(self):
        f = self.fake(people=["Ann Lee", "Ann Lee"])
        r = self.run_stager(f, "--new-chat")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ambiguous: 2 people named Ann Lee", r.stderr)
        self.assertEqual(f.to_chips, [])
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_two_mentions_in_order_then_body(self):
        f = self.fake(rail=["Ann Lee"], compose="old", mention_dir=["Bob Ray", "Bob Rayne", "Cy Dee"])
        r = self.run_stager(f, "--replace", "--mention", "Cy Dee", "--mention", "Bob Ray")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.mentions, ["Cy Dee", "Bob Ray"])
        self.assertTrue(f.compose.startswith("Cy Dee Bob Ray Hi **Ann**"), f.compose)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["mentions"], 2)
        self.assertLess(f.ops.index("editor_select"), f.ops.index("mention_find"))
        self.assertLess(len(f.ops) - 1 - f.ops[::-1].index("mention_find"), f.ops.index("paste"))
        f.never_sent(self)

    def test_mention_list_that_ignores_the_first_key_is_retyped(self):
        """Live: right after a clear the first key opens no @ list; Backspace and the letter again does."""
        f = self.fake(rail=["Ann Lee"], compose="old", mention_dir=["Bob Ray"], mention_lazy=True)
        r = self.run_stager(f, "--replace", "--mention", "Bob Ray")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.mentions, ["Bob Ray"])
        self.assertTrue(f.compose.startswith("Bob Ray Hi **Ann**"), f.compose)
        self.assertNotIn("@", f.compose)
        f.never_sent(self)

    def test_literal_at_name_is_stripped_from_the_body(self):
        f = self.fake(rail=["Ann Lee"], mention_dir=["Bob Ray"])
        self.md.write_text("Hey @Bob Ray please look at [this](https://example.com/d).\n")
        r = self.run_stager(f, "--mention", "Bob Ray")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.compose, "Bob Ray Hey please look at [this](https://example.com/d).\n")
        self.assertNotIn("@Bob Ray", f.pasted_html)
        self.assertEqual(f.mentions, ["Bob Ray"])
        f.never_sent(self)

    def test_mention_last_letter_is_a_real_key_event(self):
        """#183: the picker ignores a one-shot insertText, so the last letter goes as keyDown/keyUp."""
        f = self.fake(rail=["Ann Lee"], mention_dir=["Bob Ray"])
        r = self.run_stager(f, "--mention", "Bob Ray")
        self.assertEqual(r.returncode, 0, r.stderr)
        inserted = [p["text"] for m, p in f.calls if m == "Input.insertText"]
        self.assertIn("@Bob Ra", inserted)
        self.assertNotIn("@Bob Ray", inserted)
        keys = [(p["type"], p.get("text")) for m, p in f.calls if m == "Input.dispatchKeyEvent" and p.get("key") == "y"]
        self.assertEqual(keys, [("keyDown", "y"), ("keyUp", None)])
        self.assertEqual(f.mentions, ["Bob Ray"])
        f.never_sent(self)

    def test_mention_without_suggestion_refused(self):
        f = self.fake(rail=["Ann Lee"], mention_dir=["Someone Else"])
        r = self.run_stager(f, "--mention", "Bob Ray")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no mention suggestion for Bob Ray", r.stderr)
        self.assertNotIn("paste", f.ops)
        self.assertEqual((f.compose, f.mention_typed), ("", ""))  # the half-typed @ was cleaned up
        f.never_sent(self)

    def test_ambiguous_mention_refused(self):
        f = self.fake(rail=["Ann Lee"], mention_dir=["Bob Ray", "Bob Ray"])
        r = self.run_stager(f, "--mention", "Bob Ray")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ambiguous: 2 mention suggestions named Bob Ray", r.stderr)
        self.assertEqual(f.mentions, [])
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    # ---- review fixes -------------------------------------------------------------------------

    def test_restage_never_touches_history_attachments(self):
        """M1: history cards (outside the compose root) have Remove/Delete controls; none may be clicked."""
        f = self.fake(rail=["Ann Lee"], compose="old text", old_chips=["old1.html", "old2.html"],
                      history=["hist1.html", "hist2.html"])
        r = self.run_stager(f, "--replace", "--only-these-attachments", "--attach", str(self.page))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.chips, ["page.html"])
        self.assertEqual(f.history_clicked, [])
        self.assertEqual(f.history, ["hist1.html", "hist2.html"])
        self.assertFalse([c for c in f.clicks if f.near_history(*c)], f.clicks)
        f.never_sent(self)

    def test_remover_outside_the_root_is_never_clicked(self):
        """M1: even if the remover query returned a point outside the root, the hit test refuses it."""
        f = self.fake(rail=["Ann Lee"], old_chips=["old1.html"], history=["hist1.html"])
        orig = f.evaluate
        hx, hy = f.history_xy(0)
        f.evaluate = lambda e: [{"x": hx, "y": hy}] if "op:root_chip_removers" in e else orig(e)
        r = self.run_stager(f, "--replace", "--only-these-attachments")
        self.assertEqual(r.returncode, 1)
        self.assertIn("outside the compose box", r.stderr)
        self.assertEqual(f.history_clicked, [])
        self.assertNotIn("paste", f.ops)

    def test_remove_click_that_does_nothing_fails_after_one_click(self):
        """M1: the count must drop after a click, else exit 1 without clicking again."""
        f = self.fake(rail=["Ann Lee"], old_chips=["old1.html", "old2.html"], remover_noop=True)
        r = self.run_stager(f, "--replace", "--only-these-attachments")
        self.assertEqual(r.returncode, 1)
        self.assertIn("didn't remove it", r.stderr)
        self.assertEqual(len([c for c in f.clicks if abs(c[0] - 600) < 5 and c[1] >= 700]), 1, f.clicks)
        self.assertNotIn("paste", f.ops)

    def test_no_compose_root_text_only_restage_warns_and_goes_on(self):
        """M1: root not found, no pages: skip chip removal with a warning, stage the text."""
        f = self.fake(rail=["Ann Lee"], compose="old", no_root=True, history=["hist1.html"])
        r = self.run_stager(f, "--replace", "--only-these-attachments")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("warning", r.stderr)
        self.assertEqual(f.history_clicked, [])
        self.assertTrue(f.compose.startswith("Hi **Ann**"))
        f.never_sent(self)

    def test_no_compose_root_with_pages_fails(self):
        f = self.fake(rail=["Ann Lee"], compose="old", no_root=True)
        r = self.run_stager(f, "--replace", "--only-these-attachments", "--attach", str(self.page))
        self.assertEqual(r.returncode, 1)
        self.assertIn("couldn't find the compose area", r.stderr)
        self.assertNotIn("paste", f.ops)
        f.never_sent(self)

    def test_root_missing_for_a_few_reads_after_clear_and_paste_still_stages(self):
        """#172: Teams re-renders the compose area after the clear and the paste; the root comes back."""
        f = self.fake(rail=["Ann Lee"], compose="old", old_chips=["old1.html"], root_flicker=4)
        r = self.run_stager(f, "--replace", "--only-these-attachments", "--attach", str(self.page))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.chips, ["page.html"])
        self.assertIn('"attachments": 1', r.stdout)
        f.never_sent(self)

    def test_root_gone_for_good_after_paste_says_text_is_in_the_box(self):
        f = self.fake(rail=["Ann Lee"], root_flicker=10_000)
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 1)
        self.assertIn("couldn't find the compose area to check the attachments", r.stderr)
        self.assertIn("already in the compose box", r.stderr)
        f.never_sent(self)

    def test_background_tab_is_brought_to_front_before_the_rail_click(self):
        """A hidden tab ignores rail clicks; the stager activates it before opening the chat."""
        f = self.fake(rail=["Ann Lee"], hidden=True)
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 0, r.stderr)
        methods = [m for m, _ in f.calls]
        self.assertLess(methods.index("Target.activateTarget"), methods.index("Input.dispatchMouseEvent"))
        self.assertEqual(f.chips, ["page.html"])
        f.never_sent(self)

    # ---- --edit-sent (#176) ----
    OWN = ["morning, the build is green again", "so I'll file a follow-up to run the migration once per env",
           "thanks, that works"]

    def edit_run(self, f, sent_text, *extra):
        m = self.d / "match.txt"
        m.write_text(sent_text)
        self.md.write_text("so I filed a follow-up to run the migration once per env\n")
        return self.run_stager(f, "--edit-sent", "--match-file", str(m), *extra)

    def test_edit_sent_loads_new_text_into_that_message_only(self):
        f = self.fake(rail=["Ann Lee"], own=self.OWN)
        r = self.edit_run(f, self.OWN[1])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.editing, 1)
        self.assertEqual(f.edit_text.strip(), "so I filed a follow-up to run the migration once per env")
        self.assertFalse(f.edit_saved)
        self.assertEqual(f.compose, "")  # the compose box is never touched
        self.assertNotIn("paste", f.ops)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual((out["sent"], out["saved"], out["edit"]), (False, False, True))
        f.never_sent(self)

    def test_edit_sent_in_the_desktop_app_uses_the_toolbar_edit_button(self):
        """The app has no Edit in the More menu: it is a hover-toolbar button. The toolbar is showing for
        another message when the run starts, and a hidden 4px More button sits on every message."""
        f = self.fake(rail=["Ann Lee"], own=self.OWN, app_toolbar=True)
        f.hovered = 0
        r = self.edit_run(f, self.OWN[1])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.editing, 1)
        self.assertEqual(f.edit_text.strip(), "so I filed a follow-up to run the migration once per env")
        self.assertFalse(f.edit_saved)
        self.assertFalse([c for c in f.clicks if abs(c[0] - f.hidden_more_xy(1)[0]) < 5], "clicked the hidden button")
        self.assertFalse([c for c in f.clicks if abs(c[0] - f.edit_btn_xy(0)[0]) < 5 and abs(c[1] - f.edit_btn_xy(0)[1]) < 5],
                         "clicked Edit on another message")
        f.never_sent(self)

    def test_edit_sent_matches_a_lightly_edited_send(self):
        f = self.fake(rail=["Ann Lee"], own=self.OWN)
        r = self.edit_run(f, "so I'll file a follow up to run the migration once per env.")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.editing, 1)

    def test_edit_sent_no_matching_message(self):
        f = self.fake(rail=["Ann Lee"], own=self.OWN)
        r = self.edit_run(f, "a message that was never sent in this chat at all, nothing like the others")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no message of yours", r.stderr)
        self.assertIsNone(f.editing)

    def test_edit_sent_ambiguous(self):
        f = self.fake(rail=["Ann Lee"], own=[self.OWN[1], "x", self.OWN[1]])
        r = self.edit_run(f, self.OWN[1])
        self.assertEqual(r.returncode, 1)
        self.assertIn("ambiguous", r.stderr)
        self.assertIsNone(f.editing)

    def test_edit_sent_without_edit_item_stops(self):
        f = self.fake(rail=["Ann Lee"], own=self.OWN, no_edit_item=True)
        r = self.edit_run(f, self.OWN[1])
        self.assertEqual(r.returncode, 1)
        self.assertIn("no Edit item", r.stderr)

    def test_edit_sent_refuses_to_click_a_save_control(self):
        """The click guard in edit mode covers Done/Save, so a bad coordinate can't save the edit."""
        f = self.fake(rail=["Ann Lee"], own=self.OWN)
        orig = f.evaluate
        f.evaluate = (lambda e: {"x": f.DONE_XY[0], "y": f.DONE_XY[1]} if "op:menu_item" in e else orig(e))
        r = self.edit_run(f, self.OWN[1])
        self.assertEqual(r.returncode, 1)
        self.assertIn("refusing to click a save control", r.stderr)
        self.assertFalse(f.edit_saved)

    def test_edit_sent_usage_errors(self):
        f = self.fake(rail=["Ann Lee"], own=self.OWN)
        r = self.run_stager(f, "--edit-sent")
        self.assertEqual(r.returncode, 2)
        m = self.d / "match.txt"
        m.write_text("x")
        r = self.run_stager(f, "--edit-sent", "--match-file", str(m), "--attach", str(self.page))
        self.assertEqual(r.returncode, 2)
        r = self.run_stager(f, "--match-file", str(m))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(f.calls, [])

    def test_restage_chip_check_ignores_history(self):
        """M2: the file name is already in the chat history and the upload silently does nothing: exit 1."""
        f = self.fake(rail=["Ann Lee"], history=["page.html"], drop_uploads=True)
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 1)
        self.assertIn("never showed up", r.stderr)
        f.never_sent(self)

    def test_attachments_reported_is_the_measured_chip_count(self):
        f = self.fake(rail=["Ann Lee"], old_chips=["old1.html"])
        r = self.run_stager(f, "--attach", str(self.page))  # old chip stays: 2 in the box
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["attachments"], 2)

    def test_hang_hits_the_global_deadline_naming_the_step(self):
        """M3: one op never answers; the run ends with exit 1 and says which step it was in."""
        import time
        f = self.fake(rail=["Ann Lee"])
        orig = f.evaluate

        def slow(e):
            if "op:paste" in e:
                time.sleep(4)
            return orig(e)

        f.evaluate = slow
        r = self.run_stager(f, env_extra={"WHOSAID_STAGER_DEADLINE": "1.5"})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("timed out during pasting the draft", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_file_chooser_intercept_is_switched_off_after_a_failed_attach(self):
        f = self.fake(rail=["Ann Lee"], fail_set_files=True)
        r = self.run_stager(f, "--attach", str(self.page))
        self.assertEqual(r.returncode, 1)
        self.assertIn("boom", r.stderr)
        states = [p["enabled"] for m, p in f.calls if m == "Page.setInterceptFileChooserDialog"]
        self.assertEqual(states, [True, False])
        f.never_sent(self)

    def test_fast_rail_poll_does_not_fall_back_to_search_early(self):
        """m4: the rail shows other rows first; the exact row appears a few polls later."""
        f = self.fake(rail=["Zed", "Ann Lee"], search=["Ann Lee"])
        orig = f.evaluate
        seen = {"n": 0}

        def late(e):
            if "op:rail_find" in e:
                seen["n"] += 1
                if seen["n"] < 4:
                    return {"total": 1, "matches": []}
            return orig(e)

        f.evaluate = late
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("search_box", f.ops)
        f.never_sent(self)

    def test_search_box_focus_miss_types_nothing(self):
        f = self.fake(rail=["Zed"], search=["Ann Lee"], focus_miss=True)
        r = self.run_stager(f)
        self.assertEqual(r.returncode, 1)
        self.assertIn("couldn't focus the search box", r.stderr)
        self.assertEqual([p for m, p in f.calls if m == "Input.insertText"], [])
        self.assertNotIn("paste", f.ops)

    def test_to_field_focus_miss_types_nothing(self):
        f = self.fake(people=["Ann Lee"], focus_miss=True)
        r = self.run_stager(f, "--new-chat")
        self.assertEqual(r.returncode, 1)
        self.assertIn("couldn't focus the To field", r.stderr)
        self.assertEqual([p for m, p in f.calls if m == "Input.insertText"], [])

    def test_bad_mention_exits_2_before_any_browser_call(self):
        f = self.fake(rail=["Ann Lee"], compose="precious")
        r = self.run_stager(f, "--replace", "--mention", "Bob\nRay")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(f.calls, [])
        self.assertEqual(f.compose, "precious")


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
