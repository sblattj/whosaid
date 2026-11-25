#!/usr/bin/env python3
"""lib/teams_live_transcript.py (#242): the read-only Teams transcript-panel source and the allow-listed
open-transcript action. No real Teams: a fake DevTools endpoint in front of either scripted reads or a
synthetic call window (test/fixtures/teams-call-dom/call.html) run in jsdom by the stager's bridge.

Run: uv run --quiet --with websockets python test/teams_live_transcript_test.py
The jsdom cases need bun (jsdom installs once into ~/.cache/whosaid-test-jsdom); without it they skip
loudly, and WHOSAID_REQUIRE_DOM_TEST=1 turns the skip into a failure. The Tracker and fake-CDP cases
are plain Python and always run.
"""

import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import teams_live_transcript as tlt  # noqa: E402

BRIDGE = ROOT / "test" / "fixtures" / "stager-edit-dom" / "bridge.mjs"
CALL = ROOT / "test" / "fixtures" / "teams-call-dom" / "call.html"
JSDOM_DIR = Path(os.environ.get("WHOSAID_TEST_JSDOM_DIR") or Path.home() / ".cache" / "whosaid-test-jsdom")
os.environ["WHOSAID_TEAMS_CDP_PORT"] = "9"  # never the real Teams app port


def jsdom_ready():
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
    print(f"SKIP teams_live_transcript_test DOM cases: {WHY_NOT}", file=sys.stderr)


class FakeTeams:
    """A DevTools endpoint: /json/list from self.pages, one websocket per page id. Each page is
    {"id", "title", "eval": callable(expression) -> value}; every CDP call is logged in self.calls."""

    def __init__(self, pages=None):
        from websockets.sync.server import serve
        self.pages = pages or []
        self.calls: list[tuple[str, str, dict]] = []
        self.srv = serve(self.handler, "127.0.0.1", 0)
        self.ws_port = self.srv.socket.getsockname()[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps([{"id": p["id"], "type": "page", "title": p["title"], "url": "https://teams.microsoft.com/v2/",
                                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{fake.ws_port}/devtools/page/{p['id']}"}
                                   for p in fake.pages]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def handler(self, ws):
        pid = ws.request.path.rsplit("/", 1)[-1]
        try:
            for raw in ws:
                msg = json.loads(raw)
                method, params = msg["method"], msg.get("params", {})
                self.calls.append((pid, method, params))
                page = next((p for p in self.pages if p["id"] == pid), None)
                if page is None:
                    return  # the page went away: drop the socket
                result = {}
                if method == "Runtime.evaluate":
                    v = page["eval"](params["expression"], page)
                    if isinstance(v, Exception):
                        result = {"exceptionDetails": {"text": str(v)}}
                    else:
                        result = {"result": {"type": "object", "value": v}}
                elif method == "Input.dispatchMouseEvent" and page.get("mouse"):
                    page["mouse"](params)
                ws.send(json.dumps({"id": msg["id"], "result": result}))
        except Exception as e:  # noqa: BLE001 - client hang-up is the normal end
            if "ConnectionClosed" not in type(e).__name__:
                raise

    def inputs(self):
        return [c for c in self.calls if c[1].startswith("Input.")]

    def close(self):
        self.srv.shutdown()
        self.http.shutdown()
        self.http.server_close()


class Dom:
    """call.html in jsdom, as a FakeTeams page."""

    def __init__(self, mode=""):
        env = {**os.environ, "NODE_PATH": str(JSDOM_DIR / "node_modules")}
        self.p = subprocess.Popen(["bun", str(BRIDGE), str(CALL)] + ([mode] if mode else []),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.lock = threading.Lock()

    def ask(self, q):
        with self.lock:
            self.p.stdin.write(json.dumps(q) + "\n")
            self.p.stdin.flush()
            return json.loads(self.p.stdout.readline())

    def js(self, expr):
        r = self.ask({"eval": expr})
        if "error" in r:
            raise AssertionError(r["error"])
        return r["value"]

    def page(self, pid="P1", title="Sync call | Microsoft Teams"):
        def ev(expr, _page):
            r = self.ask({"eval": expr})
            return RuntimeError(r["error"]) if "error" in r else r["value"]
        return {"id": pid, "title": title, "eval": ev, "mouse": lambda p: self.ask({"mouse": p})}

    def fx(self):
        return self.js("({clicks: window.__fx.clicks, hits: window.__fx.hits, bad: window.__fx.bad})")

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


def scripted(reads):
    """A page whose JS_READ answers come from a list (the last one repeats)."""
    seq = list(reads)

    def ev(expr, _page):
        if expr != tlt.JS_READ:
            return RuntimeError("unexpected expression")
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return ev


def rows(*pairs, panel=True):
    return {"panel": panel, "inCall": True, "rows": [{"id": i, "author": "", "text": t} for i, t in pairs]}


# ---- Tracker: pure Python ----------------------------------------------------------------------------------
class TrackerTest(unittest.TestCase):
    def test_overlapping_polls_emit_each_row_id_once(self):
        tr = tlt.Tracker(settle=False)
        evs = tr.step(rows(("a", "one"), ("b", "two")), "T1")
        evs += tr.step(rows(("b", "two"), ("c", "three")), "T2")
        evs += tr.step(rows(("b", "two"), ("c", "three"), ("d", "four")), "T3")
        got = [e["id"] for e in evs if "id" in e]
        self.assertEqual(got, ["a", "b", "c", "d"])
        self.assertEqual([e for e in evs if "status" in e], [{"status": "ok", "at": "T1"}], "status once, on change")
        self.assertFalse([e for e in evs if e.get("gap")])

    def test_a_row_shape_matches_the_contract_and_speaker_defaults_to_empty(self):
        tr = tlt.Tracker(settle=False)
        ev = [e for e in tr.step({"panel": True, "rows": [{"id": "x", "author": "", "text": "hello there"}]}, "T") if "id" in e]
        self.assertEqual(ev, [{"id": "x", "at": "T", "speaker": "", "text": "hello there", "final": True}])

    def test_settle_holds_the_growing_tail_row_and_emits_it_once_with_its_last_text(self):
        tr = tlt.Tracker(settle=True)
        self.assertEqual([e for e in tr.step(rows(("a", "we should")), "T1") if "id" in e], [])
        self.assertEqual([e for e in tr.step(rows(("a", "we should ship it")), "T2") if "id" in e], [])
        out = [e for e in tr.step(rows(("a", "we should ship it")), "T3") if "id" in e]
        self.assertEqual([(e["id"], e["text"], e["at"]) for e in out], [("a", "we should ship it", "T1")])
        self.assertEqual([e for e in tr.step(rows(("a", "we should ship it")), "T4") if "id" in e], [])

    def test_a_row_that_scrolls_away_before_it_settles_is_still_emitted(self):
        tr = tlt.Tracker(settle=True)
        tr.step(rows(("a", "first")), "T1")
        out = tr.step(rows(("b", "second")), "T2")
        self.assertEqual([e["id"] for e in out if "id" in e], ["a"])
        self.assertEqual([e["id"] for e in tr.flush()], ["b"])

    def test_a_poll_with_no_row_in_common_reports_a_gap(self):
        tr = tlt.Tracker(settle=False)
        tr.step(rows(("a", "one"), ("b", "two")), "T1")
        evs = tr.step(rows(("x", "later"), ("y", "later still")), "T2")
        gaps = [e for e in evs if e.get("gap")]
        self.assertEqual(gaps, [{"gap": True, "at": "T2", "after": "b", "before": "x"}])
        self.assertEqual([e["id"] for e in evs if "id" in e], ["x", "y"])

    def test_panel_closed_and_window_lost_are_reported_once_each_and_recovery_says_ok(self):
        tr = tlt.Tracker(settle=False)
        evs = tr.step(rows(("a", "one")), "T1")
        evs += tr.step({"panel": False, "rows": []}, "T2")
        evs += tr.step({"panel": False, "rows": []}, "T3")
        evs += tr.step(None, "T4")
        evs += tr.step(None, "T5")
        evs += tr.step(rows(("a", "one"), ("b", "two")), "T6")
        self.assertEqual([e["status"] for e in evs if "status" in e], ["ok", "panel closed", "window not found", "ok"])
        self.assertEqual([e["id"] for e in evs if "id" in e], ["a", "b"], "the row seen before the rejoin is not re-emitted")

    def test_rows_without_id_or_text_are_dropped(self):
        tr = tlt.Tracker(settle=False)
        evs = tr.step({"panel": True, "rows": [{"id": "", "text": "x"}, {"id": "a", "text": ""}, {"id": "b", "text": "ok"}]}, "T")
        self.assertEqual([e["id"] for e in evs if "id" in e], ["b"])

    def test_deny_list_catches_call_controls_but_not_the_allow_list(self):
        for bad in ("Stop transcription", "Leave", "Hang up", "Mute", "Unmute", "End call", "Turn off live captions",
                    "Hide transcript", "Close", "Start recording"):
            self.assertTrue(tlt.denied([bad]), bad)
        for ok in tlt.OPEN_NAMES + tlt.MENU_PATH[0] + tlt.MENU_PATH[1] + ("Send", "Calendar", "Attendees"):
            self.assertIsNone(tlt.denied([ok]), ok)


# ---- fake CDP, scripted reads: read-only, rejoin, window re-found by title ----------------------------------
class SourceOverFakeCdpTest(unittest.TestCase):
    def run_source(self, f, polls, title=None, between=None):
        out = io.StringIO()
        n = [0]

        def sleep(_s):
            n[0] += 1
            if between:
                between(n[0])

        ticks = iter(range(10_000))
        tlt.run_source(f.port, title, 0.01, True, max_seconds=polls - 0.5, settle=False, out=out, sleep=sleep,
                       clock=lambda: next(ticks))
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_source_sends_nothing_but_runtime_evaluate(self):
        f = FakeTeams([{"id": "P1", "title": "Sync call | Microsoft Teams", "eval": scripted([rows(("a", "one"))])}])
        try:
            evs = self.run_source(f, 3)
        finally:
            f.close()
        self.assertEqual({c[1] for c in f.calls}, {"Runtime.evaluate"})
        self.assertEqual([e["id"] for e in evs if "id" in e], ["a"])

    def test_read_only_wrapper_refuses_input(self):
        f = FakeTeams([{"id": "P1", "title": "t", "eval": scripted([rows()])}])
        try:
            c = tlt.ReadOnlyCDP(f'ws://127.0.0.1:{f.ws_port}/devtools/page/P1')
            for m in ("Input.dispatchMouseEvent", "Input.dispatchKeyEvent", "Input.insertText", "Page.navigate"):
                with self.assertRaises(tlt.LiveError):
                    c.send(m, {})
            c.close()
        finally:
            f.close()
        self.assertEqual(f.inputs(), [])

    def test_rejoin_new_page_id_is_refound_by_title_and_reported_not_crashed(self):
        """Poll 1-2 on page P1 with rows; then P1 goes away (the call dropped) and nothing matches: window
        not found; then the rejoined window comes back as P2 with the panel closed; then open again."""
        title = "Sync call"
        f = FakeTeams([{"id": "P1", "title": "Sync call | Microsoft Teams", "eval": scripted([rows(("a", "one"))])},
                       {"id": "M", "title": "Chat | Microsoft Teams", "eval": scripted([{"panel": False, "inCall": False, "rows": []}])}])
        reads_p2 = [{"panel": False, "inCall": True, "rows": []}] * 2 + [rows(("a", "one"), ("b", "two"))]

        def between(n):
            if n == 2:
                f.pages[:] = [p for p in f.pages if p["id"] != "P1"]
            if n == 4:
                f.pages.append({"id": "P2", "title": "Sync call | Microsoft Teams", "eval": scripted(reads_p2)})

        try:
            evs = self.run_source(f, 8, title=title, between=between)
        finally:
            f.close()
        self.assertEqual([e["status"] for e in evs if "status" in e], ["ok", "window not found", "panel closed", "ok"])
        self.assertEqual([e["id"] for e in evs if "id" in e], ["a", "b"])
        self.assertIn("P2", {c[0] for c in f.calls})
        self.assertEqual(f.inputs(), [])

    def test_without_a_title_a_non_call_page_is_not_taken(self):
        f = FakeTeams([{"id": "M", "title": "Microsoft Teams", "eval": scripted([{"panel": False, "inCall": False, "rows": []}])}])
        try:
            evs = self.run_source(f, 2)
        finally:
            f.close()
        self.assertEqual([e["status"] for e in evs if "status" in e], ["window not found"])

    def test_a_down_port_is_window_not_found_not_a_crash(self):
        out = io.StringIO()
        ticks = iter(range(100))
        rc = tlt.run_source(9, None, 0.01, True, max_seconds=1.5, out=out, sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertEqual(rc, 0)
        self.assertEqual([json.loads(x)["status"] for x in out.getvalue().splitlines()], ["window not found"])

    def test_cli_once_prints_jsonl(self):
        f = FakeTeams([{"id": "P1", "title": "Sync call | Microsoft Teams", "eval": scripted([rows(("a", "one"), ("b", "two"))])}])
        try:
            r = subprocess.run([sys.executable, str(ROOT / "lib" / "teams_live_transcript.py"), "source", "--port", str(f.port),
                                "--once", "--jsonl"], capture_output=True, text=True, timeout=60)
        finally:
            f.close()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual(lines[0]["status"], "ok")
        self.assertEqual([(x["id"], x["text"], x["final"], x["speaker"]) for x in lines[1:]],
                         [("a", "one", True, ""), ("b", "two", True, "")])


class McpToolTest(unittest.TestCase):
    def test_mcp_open_transcript_is_registered_in_teams_and_reports_a_down_port(self):
        try:
            import asyncio
            import mcp_server
        except ImportError as e:  # run_all.sh brings mcp; a bare python3 may not
            self.skipTest(f"mcp not importable: {e}")
        self.assertIn("whosaid_teams_open_transcript", mcp_server.TOOL_GROUPS["teams"])
        desc = mcp_server._DESC_TEAMS_OPEN_TRANSCRIPT
        for word in ("allow-list", "deny-list", "Stop transcription", "Leave", "Mute", "already open"):
            self.assertIn(word, desc)
        self.assertNotIn("\u2014", desc)
        out = asyncio.run(mcp_server.whosaid_teams_open_transcript(port=9))
        self.assertFalse(out["ok"])
        self.assertEqual(out.get("action"), "failed", out)


# ---- the synthetic call window in jsdom ----------------------------------------------------------------------
@unittest.skipIf(WHY_NOT and not os.environ.get("WHOSAID_REQUIRE_DOM_TEST"), f"no DOM engine: {WHY_NOT}")
class AgainstFixtureDomTest(unittest.TestCase):
    def setUp(self):
        if WHY_NOT:
            self.fail(f"WHOSAID_REQUIRE_DOM_TEST is set but {WHY_NOT}")
        self.doms = []
        self.fakes = []

    def tearDown(self):
        for f in self.fakes:
            f.close()
        for d in self.doms:
            d.close()

    def world(self, mode=""):
        d = Dom(mode)
        self.doms.append(d)
        f = FakeTeams([d.page()])
        self.fakes.append(f)
        return d, f

    def open_(self, f, **kw):
        return tlt.open_transcript(f.port, None, sleep=lambda s: None, wait=0.5, **kw)

    def test_the_read_takes_the_last_segment_as_id_and_the_author_only_where_present(self):
        d, _ = self.world("open=1")
        d.js("window.__fx.add('first line', 'Speaker A'); window.__fx.add('second line'); window.__fx.add('third  line\\n wraps', '')")
        r = d.js(tlt.JS_READ)
        self.assertTrue(r["panel"])
        self.assertTrue(r["inCall"])
        self.assertEqual(r["rows"], [{"id": "row-1", "author": "Speaker A", "text": "first line"},
                                     {"id": "row-2", "author": "", "text": "second line"},
                                     {"id": "row-3", "author": "", "text": "third line wraps"}])

    def test_source_over_overlapping_virtualized_polls_emits_each_row_once_and_reports_the_gap(self):
        d, f = self.world("open=1")
        d.js("window.__fx.add('one'); window.__fx.add('two')")
        script = {1: "window.__fx.add('three'); window.__fx.keep(2)",          # overlap: two, three
                  2: "window.__fx.add('four'); window.__fx.keep(3)",           # overlap: two, three, four
                  3: "['five','six','seven'].forEach(t=>window.__fx.add(t)); window.__fx.keep(2)",  # six, seven: gap
                  4: "window.__fx.closePanel()"}
        out = io.StringIO()
        n = [0]

        def sleep(_s):
            n[0] += 1
            if n[0] in script:
                d.js(script[n[0]])

        ticks = iter(range(1000))
        tlt.run_source(f.port, None, 0.01, True, max_seconds=5.5, settle=False, out=out, sleep=sleep, clock=lambda: next(ticks))
        evs = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([e["text"] for e in evs if "id" in e], ["one", "two", "three", "four", "six", "seven"])
        self.assertEqual(len({e["id"] for e in evs if "id" in e}), 6)
        self.assertEqual([(e["after"], e["before"]) for e in evs if e.get("gap")], [("row-4", "row-6")])
        self.assertEqual([e["status"] for e in evs if "status" in e], ["ok", "panel closed"])
        self.assertEqual(f.inputs(), [], "the source never sends an input event")

    def test_open_transcript_goes_more_then_record_and_transcribe_then_show_transcript_only(self):
        d, f = self.world()
        res = self.open_(f)
        fx = d.fx()
        self.assertEqual(res["action"], "opened", res)
        self.assertEqual(res["clicked"], ["more", "record and transcribe", "show transcript"])
        self.assertEqual(fx["hits"], ["More", "Record and transcribe", "Show transcript"])
        self.assertEqual(fx["bad"], [])
        presses = [c for c in f.inputs() if c[2]["type"] == "mousePressed"]
        self.assertEqual(len(presses), 3)
        self.assertEqual({c[1] for c in f.inputs()}, {"Input.dispatchMouseEvent"}, "no key or text input")
        self.assertTrue(d.js(tlt.JS_READ)["panel"])

    def test_open_transcript_uses_a_toolbar_control_directly_when_there_is_one(self):
        d, f = self.world("direct=1")
        res = self.open_(f)
        self.assertEqual((res["action"], res["clicked"]), ("opened", ["show transcript"]))
        self.assertEqual(d.fx()["hits"], ["Show transcript"])
        self.assertEqual(len([c for c in f.inputs() if c[2]["type"] == "mousePressed"]), 1)

    def test_refused_when_the_panel_is_already_open_zero_mouse_events(self):
        d, f = self.world("open=1&direct=1")
        with self.assertRaises(tlt.LiveError) as cm:
            self.open_(f)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(f.inputs(), [])
        self.assertEqual(d.fx()["hits"], [])

    def test_refused_when_the_control_reads_as_on(self):
        d, f = self.world("pressed=1")
        with self.assertRaises(tlt.LiveError) as cm:
            self.open_(f)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(f.inputs(), [])

    def test_drift_allow_listed_name_on_a_stop_transcription_control_is_refused(self):
        """The submenu's allow-listed name now sits on a control that reads Stop transcription: the deny-list
        refuses it before any mouse event for that hop; Stop is never clicked."""
        d, f = self.world("drift=1")
        with self.assertRaises(tlt.LiveError) as cm:
            self.open_(f)
        self.assertEqual(cm.exception.code, 4)
        self.assertIn("deny-list", str(cm.exception))
        fx = d.fx()
        self.assertEqual(fx["bad"], [])
        self.assertNotIn("Show transcript", fx["hits"])
        self.assertEqual(fx["hits"], ["More", "Record and transcribe"])

    def test_click_allowed_refuses_every_denied_control_on_the_page(self):
        """Point the choke point straight at Stop, Leave, Mute and the camera, each with the full allow-list."""
        d, f = self.world()
        d.js("document.getElementById('more').click(); [...document.querySelectorAll('[role=menuitem]')][1].click()")
        d.js("window.__fx.hits = []")
        allow = tlt.OPEN_NAMES + tlt.MENU_PATH[0] + tlt.MENU_PATH[1]
        c = tlt.ActionCDP(f'ws://127.0.0.1:{f.ws_port}/devtools/page/P1')
        try:
            targets = d.js("[...document.querySelectorAll('button,[role=menuitem]')].filter(b=>/stop|leave|mute|camera|start recording/i"
                           ".test(b.getAttribute('aria-label'))).map(b=>{const r=b.getBoundingClientRect();"
                           "return {name:b.getAttribute('aria-label'),x:r.x+r.width/2,y:r.y+r.height/2}})")
            self.assertEqual(len(targets), 5, targets)
            for t in targets:
                with self.assertRaises(tlt.LiveError, msg=t["name"]) as cm:
                    tlt.click_allowed(c, t["x"], t["y"], allow)
                self.assertEqual(cm.exception.code, 4)
            with self.assertRaises(tlt.LiveError):
                c.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 1, "y": 1})
        finally:
            c.close()
        self.assertEqual(f.inputs(), [])
        self.assertEqual(d.fx()["hits"], [])

    def test_dry_run_clicks_nothing(self):
        d, f = self.world("direct=1")
        res = self.open_(f, dry_run=True)
        self.assertEqual(res["would_click"], ["show transcript"])
        self.assertEqual(f.inputs(), [])

    def test_no_control_found_is_exit_5_and_no_click(self):
        d, f = self.world("nomenu=1")
        with self.assertRaises(tlt.LiveError) as cm:
            self.open_(f)
        self.assertEqual(cm.exception.code, 5)
        self.assertEqual(d.fx()["hits"], ["More"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
