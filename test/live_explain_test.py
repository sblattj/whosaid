#!/usr/bin/env python3
"""
Offline test for lib/live_explain.py (`whosaid live explain`, issue #220): sections as
NNN-<slug> files with stable ids, the static page (newest first, recap on top, 30 s
meta refresh, dark by default, no remote resources), the sketch sanitizer, corrections
as before/after boxes that keep the earlier revision, the served copy (client injected,
refresh removed, file on disk unchanged), SSE pushes, the LAN token (loopback only by
default, 403 without it, HttpOnly cookie, mode 600), the rich-text clipboard hook, the
MCP tool pair and the opt-in `[live] explain = "claude"` engine with its budget.
Claude is faked and the clipboard is stubbed: nothing leaves the machine.

Run: python3 test/live_explain_test.py
"""
from __future__ import annotations

import http.client
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
os.environ["WHOSAID_SPEAKER_DB"] = str(Path(tempfile.gettempdir()) / "whosaid-no-registry.json")
os.environ["WHOSAID_EXPLAIN_CLIPBOARD"] = "0"
import live  # noqa: E402
import live_explain as lx  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


SKETCH = ('<svg viewBox="0 0 600 200" xmlns="http://www.w3.org/2000/svg">'
          '<rect x="10" y="40" width="160" height="70" rx="12" fill="none" stroke="currentColor"/>'
          '<text x="30" y="80">client</text>'
          '<path d="M180 75 L380 75" stroke="var(--accent)" marker-end="url(#arr)"/>'
          '<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="4" refY="4">'
          '<path d="M0 0 L8 4 L0 8z" fill="currentColor"/></marker></defs>'
          '<rect x="390" y="40" width="160" height="70" rx="12" fill="none" stroke="currentColor" onclick="alert(1)"/>'
          '<text x="410" y="80">cache</text></svg>')

SEC1 = {"title": "Why the cache sits in front", "from": 60, "to": 300,
        "explain": "Ben explained that every read first asks the cache.\n\nOnly a miss goes to the database.",
        "sketch": SKETCH,
        "claims": [{"who": "Ben", "text": "the fallback to the old path is on by default"}],
        "jargon": [{"term": "cache miss", "st": "the copy was not there, so it is fetched from the source"}],
        "questions": ["How long does a cached copy live?"],
        "asks": [{"who": "Ana", "text": "can you share the load test numbers?"}],
        "suggested": ["Ask for the hit rate before the rollout."],
        "low_confidence": ["'Ben' is a weak voice match (0.52)"]}
SEC2 = {"title": "Rollout plan", "from": 300, "to": 600, "explain": ["Ana wants a staged rollout."],
        "for_you": [{"who": "Ana", "ask": "can you share that query again?",
                     "answer": "Here it is:\n\n- `select count(*) from reads`\n- run it per region",
                     "source": "planning call, last week"}]}


def make_session(into: Path, ended=False) -> Path:
    jl = into / live.LIVE_DIR / "20261116T100000.jsonl"
    jl.parent.mkdir(parents=True, exist_ok=True)
    started = time.mktime((2026, 11, 16, 10, 0, 0, 0, 0, -1))
    rows = [json.dumps({"meta": {"started": started, "ended": ended, "updated": time.time()}})]
    for i in range(12):
        rows.append(json.dumps({"n": i + 1, "t": i * 30.0, "who": "them" if i % 2 else "me",
                                "text": f"synthetic line {i} about the cache and the rollout"}))
    jl.write_text("\n".join(rows) + "\n")
    return jl


def get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    return r.status, dict(r.getheaders()), body


with tempfile.TemporaryDirectory() as tmp:
    into = Path(tmp)
    # ---- no session: a clear error, nothing created
    try:
        lx.store_dir(into)
        check(False, "store_dir without a session raises")
    except lx.ExplainError as err:
        check("no live transcript" in str(err), f"no-session error: {err}")
    jl = make_session(into)
    d = lx.store_dir(into)
    check(d == jl.with_name("20261116T100000.explain"), f"store next to the transcript: {d}")

    # ---- empty view by default (no engine): page exists, says so, no sections
    page = lx.render_page(d)
    check('class="xp-empty"' in page and "nothing is sent anywhere" in page, "empty page explains itself")
    check(lx.manifest(d)["sections"] == [], "no sections without a producer")

    # ---- add: NNN-<slug> files, stable ids, sanitized sketch
    copied = []
    r1 = lx.add(d, SEC1, copy=lambda h, p: copied.append((h, p)) or True)
    check(r1["id"] == "001" and Path(r1["file"]).name == "001-why-the-cache-sits-in-front.html", f"add 1: {r1}")
    check(copied == [], "no clipboard write without a For-you answer")
    r2 = lx.add(d, SEC2, copy=lambda h, p: copied.append((h, p)) or True)
    check(r2["id"] == "002" and r2["copied"], f"add 2 copies the For-you answer: {r2}")
    check(copied and "<code>select count(*) from reads</code>" in copied[0][0]
          and copied[0][1].startswith("Here it is:"), f"clipboard got rich html + plain: {copied}")
    files = sorted(p.name for p in d.iterdir())
    check({"001-why-the-cache-sits-in-front.json", "001-why-the-cache-sits-in-front.html",
           "002-rollout-plan.json", "002-rollout-plan.html", "index.html"} <= set(files), f"files: {files}")
    frag1 = (d / "001-why-the-cache-sits-in-front.html").read_text()
    check("onclick" not in frag1 and "alert" not in frag1, "event handlers stripped from the sketch")
    check('marker-end="url(#arr)"' in frag1 and "<svg" in frag1, "local url(#id) kept")
    for bad in ('<svg><script>alert(1)</script></svg>', '<svg><image href="http://x/y.png"/></svg>',
                '<svg><use href="http://evil/x.svg#a"/></svg>', '<svg><rect fill="url(http://x)"/></svg>',
                '<svg><foreignObject><div/></foreignObject></svg>', '<div>no svg</div>', '<svg><rect'):
        try:
            lx.sanitize_svg(bad)
            check(False, f"unsafe sketch accepted: {bad}")
        except lx.ExplainError:
            check(True, "")

    # ---- section content (the DOM contract)
    for needle in ('id="s-001"', 'data-id="001"', 'data-rev="1"', 'class="xs-time">10:01–10:05',
                   '<b class="xs-who">Ben</b>: the fallback', '<b>S-T:</b> <dfn>cache miss</dfn>',
                   'class="xs-q" data-key="001-q0"', 'class="xs-ask" data-key="001-a0"',
                   'Suggested <small>my ideas, not said on the call</small>', 'class="xs-lowconf"',
                   'data-flags="questions asks suggested"', "<p>Ben explained that every read first asks the cache.</p>",
                   "<p>Only a miss goes to the database.</p>"):
        check(needle in frag1, f"section 1 has {needle!r}")
    frag2 = (d / "002-rollout-plan.html").read_text()
    for needle in ('class="xs-fy" data-key="002-f0"', '<b class="xs-who">Ana</b> asked: can you share',
                   'class="xs-copy"', "From: planning call, last week", "foryou"):
        check(needle in frag2, f"section 2 has {needle!r}")

    # ---- static page: newest first, refresh, dark, CSP, no remote resources
    page = (d / "index.html").read_text()
    check(page.index('id="s-002"') < page.index('id="s-001"'), "newest section first")
    check('<meta http-equiv="refresh" content="30" data-xp-refresh>' in page, "30 s meta refresh on disk")
    check('<html lang="en" data-theme="dark">' in page, "dark by default")
    check("Content-Security-Policy" in page and "default-src 'none'" in page, "CSP in the page")
    check(not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", page) and "@import" not in page,
          "no remote resource references")
    check("/live.js" not in page, "the file on disk carries no live client")

    # ---- correction: before/after, earlier revision kept, id stable
    rc = lx.correct(d, "001", before="the fallback to the old path is on by default",
                    after="the fallback is off by default; it is turned on per region",
                    quote="no, that fallback is not on by default", who="Ana", t="10:07",
                    changes={"claims": [{"who": "Ana", "text": "the fallback is off by default"}]})
    check(rc["id"] == "001" and rc["rev"] == 2, f"correct keeps the id, bumps rev: {rc}")
    src = json.loads((d / "001-why-the-cache-sits-in-front.json").read_text())
    check(src["revisions"][0]["claims"][0]["text"] == "the fallback to the old path is on by default",
          "the earlier revision is kept")
    frag1b = (d / "001-why-the-cache-sits-in-front.html").read_text()
    for needle in ('class="xs-correction" data-rev="2"', "<s>the fallback to the old path is on by default</s>",
                   "Corrected at 10:07", "“no, that fallback is not on by default” <cite>Ana, 10:07</cite>",
                   'data-rev="2"', "corrected", "<b class=\"xs-who\">Ana</b>: the fallback is off by default"):
        check(needle in frag1b, f"corrected section has {needle!r}")
    check(len(list(d.glob("001-*.html"))) == 1, "still one file for section 001")
    for bad in (("009", "a", "b"), ("001", "", "b")):
        try:
            lx.correct(d, *bad)
            check(False, f"bad correction accepted: {bad}")
        except lx.ExplainError:
            check(True, "")

    # ---- served copy vs file on disk
    disk = (d / "index.html").read_text()
    served = lx.served_page(lx.render_page(d))
    check("data-xp-refresh" not in served and '<script src="/live.js"></script>\n</body>' in served,
          "served copy: refresh stripped, client injected")
    check((d / "index.html").read_text() == disk and "data-xp-refresh" in disk, "file on disk unchanged")

    # ---- LAN auth: a pure function
    check(lx.authorize("127.0.0.1", None, None, None) == "ok", "loopback ok without a token")
    check(lx.authorize("::1", None, None, None) == "ok", "ipv6 loopback ok")
    check(lx.authorize("::ffff:127.0.0.1", None, None, None) == "ok", "mapped loopback ok")
    check(lx.authorize("192.168.1.20", None, None, None) == "forbidden", "LAN off: non-loopback 403")
    check(lx.authorize("192.168.1.20", "abc", None, None) == "forbidden", "LAN off: a token does not help")
    check(lx.authorize("192.168.1.20", None, None, "tok") == "forbidden", "LAN on: no token = 403")
    check(lx.authorize("192.168.1.20", "nope", None, "tok") == "forbidden", "LAN on: wrong token = 403")
    check(lx.authorize("192.168.1.20", "tok", None, "tok") == "set-cookie", "LAN on: ?token= sets the cookie")
    check(lx.authorize("192.168.1.20", None, "tok", "tok") == "ok", "LAN on: cookie ok")
    tok = lx.page_token(d)
    mode = stat.S_IMODE((d / lx.TOKEN_FILE).stat().st_mode)
    check(mode == 0o600 and len(tok) >= 24 and lx.page_token(d) == tok, f"token file mode {oct(mode)}")

    # ---- the server: GET routes, SSE pushes, 403 for non-loopback (handler level)
    stop = threading.Event()
    srv, url, token = lx.serve(d, port=0, stop=stop)
    check(token is None and srv.server_address[0] == "127.0.0.1", "loopback only by default")
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    th.start()
    st, hd, body = get(port, "/")
    check(st == 200 and b"/live.js" in body and b"data-xp-refresh" not in body, "GET / is the served copy")
    check("default-src 'none'" in hd.get("Content-Security-Policy", "") and hd.get("Cache-Control") == "no-store",
          f"headers: {hd}")
    st, _, js = get(port, "/live.js")
    check(st == 200, "GET /live.js")
    st, _, m = get(port, "/manifest.json")
    man = json.loads(m)
    check([s["id"] for s in man["sections"]] == ["002", "001"] and man["sections"][1]["rev"] == 2, f"manifest: {man}")
    st, _, raw = get(port, "/section/001")
    check(st == 200 and raw == (d / "001-why-the-cache-sits-in-front.html").read_bytes(), "raw stored fragment")
    check(get(port, "/section/042")[0] == 404 and get(port, "/../etc/passwd")[0] == 404, "unknown paths 404")

    events = []
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    check(resp.getheader("Content-Type") == "text/event-stream", "SSE content type")

    def read_events():
        buf = b""
        try:
            while len(events) < 2:
                ch = resp.fp.readline()
                if not ch:
                    return
                buf += ch
                if ch == b"\n" and buf.strip():
                    if buf.startswith(b"event: manifest"):
                        events.append(json.loads(buf.split(b"data: ", 1)[1]))
                    buf = b""
        except OSError:
            return
    reader = threading.Thread(target=read_events, daemon=True)
    reader.start()
    deadline = time.time() + 5
    while not events and time.time() < deadline:
        time.sleep(0.05)
    check(len(events) == 1 and len(events[0]["sections"]) == 2, f"SSE first manifest: {events}")
    lx.add(d, {"title": "Third", "from": 600, "to": 700, "explain": "x"}, copy=lambda *_: True)
    deadline = time.time() + 5
    while len(events) < 2 and time.time() < deadline:
        time.sleep(0.05)
    check(len(events) == 2 and events[1]["sections"][0]["id"] == "003", f"SSE pushed the new section: {events[1:]}")
    conn.close()

    # a non-loopback request, through the real handler, with and without the token
    class FakeSock:
        def __init__(self, req):
            import io
            self.rfile, self.wfile = io.BytesIO(req), io.BytesIO()

        def makefile(self, mode, *a, **k):
            return self.rfile if "r" in mode else self.wfile

        def sendall(self, b):
            self.wfile.write(b)

    def lan_get(tok_cfg, path, cookie=""):
        H = lx.make_handler(d, tok_cfg)
        req = f"GET {path} HTTP/1.1\r\nHost: x\r\n" + (f"Cookie: {cookie}\r\n" if cookie else "") + "\r\n"
        s = FakeSock(req.encode())

        class H2(H):
            def finish(self):
                pass
        h = H2.__new__(H2)
        h.rfile, h.wfile = s.rfile, s.wfile
        h.client_address, h.server, h.request = ("192.168.1.20", 5555), None, s
        h.handle_one_request()
        return s.wfile.getvalue().decode(errors="replace")
    out = lan_get(None, "/")
    check(out.startswith("HTTP/1.0 403") or " 403 " in out.split("\r\n")[0], f"LAN off: 403: {out[:80]}")
    out = lan_get("tok", "/manifest.json")
    check(" 403 " in out.split("\r\n")[0], "LAN on, no token: 403")
    out = lan_get("tok", "/manifest.json?token=tok")
    check(" 200 " in out.split("\r\n")[0] and "Set-Cookie: xp_token=tok; HttpOnly; SameSite=Strict; Path=/" in out,
          f"LAN on, token: 200 + HttpOnly cookie: {out[:300]}")
    out = lan_get("tok", "/manifest.json", cookie="xp_token=tok")
    check(" 200 " in out.split("\r\n")[0] and "Set-Cookie" not in out, "LAN on, cookie: 200")
    stop.set()
    srv.shutdown()
    srv.server_close()

    # ---- recap on top when the call ends
    make_session(into, ended=True)
    rr = lx.recap(d, {"decisions": ["Stage the rollout by region"], "questions": ["Who owns the dashboard?"],
                      "owes": [{"who": "me", "text": "send the load test numbers"}]})
    page = (d / "index.html").read_text()
    check(rr["rev"] == 1 and page.index('id="s-recap"') < page.index('id="s-003"'), "recap on top")
    check('data-state="ended"' in page and "Who owes what" in page and "Decisions" in page, "recap blocks")
    check(lx.manifest(d)["ended"] and lx.manifest(d)["recap"]["id"] == "recap", "manifest carries the recap")

    # ---- glossary: configurable term map
    ws = into / "ws"
    ws.mkdir()
    (ws / "whosaid.toml").write_text('[live.explain_glossary]\nSSE = "server-sent events: the server pushes updates"\n')
    (d / "glossary.json").write_text(json.dumps({"canary": "a small first rollout"}))
    page = lx.render_page(d, ws)
    gl = json.loads(re.search(r'<script type="application/json" id="xp-glossary">(.*?)</script>', page, re.S).group(1))
    check(gl == {"cache miss": "the copy was not there, so it is fetched from the source",
                 "SSE": "server-sent events: the server pushes updates", "canary": "a small first rollout"},
          f"glossary merged (section S-T terms + toml + glossary.json): {gl}")
    (d / "glossary.json").write_text(json.dumps({"cache miss": "override from glossary.json"}))
    gl = json.loads(re.search(r'id="xp-glossary">(.*?)</script>', lx.render_page(d), re.S).group(1))
    check(gl.get("cache miss") == "override from glossary.json", f"glossary.json wins over a section term: {gl}")
    (d / "glossary.json").write_text(json.dumps({"canary": "a small first rollout"}))

    # ---- CLI: add / correct / status through `whosaid live explain`
    env = dict(os.environ, WHOSAID_EXPLAIN_CLIPBOARD="0")
    cli = [str(ROOT / "whosaid"), "live", "explain"]
    r = subprocess.run(cli + ["add", "--into", tmp], input=json.dumps({"title": "From the CLI", "explain": "y"}),
                       capture_output=True, text=True, env=env, timeout=60)
    check(r.returncode == 0 and json.loads(r.stdout)["id"] == "004", f"CLI add: {r.stdout} {r.stderr}")
    r = subprocess.run(cli + ["correct", "004", "--into", tmp, "--before", "y", "--after", "z", "--at", "120"],
                       capture_output=True, text=True, env=env, timeout=60)
    check(r.returncode == 0 and json.loads(r.stdout)["rev"] == 2, f"CLI correct: {r.stdout} {r.stderr}")
    r = subprocess.run(cli + ["status", "--into", tmp], capture_output=True, text=True, env=env, timeout=60)
    st = json.loads(r.stdout)
    check(r.returncode == 0 and st["sections"] == 4 and st["recap"] and st["engine"] == "none", f"CLI status: {st}")
    r = subprocess.run(cli + ["add", "--into", tmp], input='{"title": ""}', capture_output=True, text=True,
                       env=env, timeout=60)
    check(r.returncode == 2 and "needs a title" in r.stderr, f"CLI bad input: {r.returncode} {r.stderr}")

# ---- the opt-in engine: off by default, budgeted, failures wait
with tempfile.TemporaryDirectory() as tmp:
    into = Path(tmp)
    jl = make_session(into)
    d = lx.store_dir(into)
    ws = into / "ws"
    ws.mkdir()
    check(lx.engine_config(None) is None and lx.engine_config(ws) is None, "engine off without opt-in")
    (ws / "whosaid.toml").write_text('[live]\nai = "claude"\n')
    check(lx.engine_config(ws) is None, "[live] ai alone does not turn the explainer on")
    (ws / "whosaid.toml").write_text('[live]\nexplain = "claude"\nexplain_every = 60\nexplain_max_calls = 2\n')
    cfg = lx.engine_config(ws)
    check(cfg and cfg["explain_every"] == 60 and cfg["explain_max_calls"] == 2 and cfg["explain_timeout"] == 120,
          f"engine config: {cfg}")

    class Fake:
        def __init__(self, reply, fail=False):
            self.reply, self.fail, self.tokens, self.prompts = reply, fail, 0, []

        def chat(self, system, user):
            self.prompts.append(user)
            self.tokens += 500
            if self.fail:
                raise RuntimeError("no reply")
            return self.reply
    reply = json.dumps({"sections": [{"title": "Engine section", "from": 0, "to": 300, "explain": ["e"],
                                      "sketch": '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'},
                                     {"title": "Bad sketch", "sketch": "<svg><script/></svg>"}],
                        "corrections": [{"id": "077", "before": "a", "after": "b"}]})
    fake = Fake(reply)
    lx._engine_client = lambda c: fake
    res = lx.engine_step(d, jl, cfg, copy=lambda *_: True)
    check(res and res["added"] == ["001"] and len(res["skipped"]) == 2 and res["calls"] == 1,
          f"engine adds the good section, skips the bad ones: {res}")
    check("The call is in progress." in fake.prompts[0] and "[330] them: synthetic line 11" in fake.prompts[0],
          "engine prompt carries the new lines")
    check(lx.engine_step(d, jl, cfg) is None, "not due again without new talk")
    fake.fail, fake.tokens = True, 0  # a fresh client per call, like _engine_client
    rows = jl.read_text().splitlines()
    rows += [json.dumps({"n": 100 + i, "t": 400.0 + i * 10, "who": "them", "text": f"more talk {i}"}) for i in range(3)]
    jl.write_text("\n".join(rows) + "\n")
    res = lx.engine_step(d, jl, cfg)
    check(res and res["failures"] == 1 and res["error"] == "no reply" and res["calls"] == 2, f"failure recorded: {res}")
    check(lx.engine_step(d, jl, cfg) is None, f"max_calls={cfg['explain_max_calls']} reached: no more calls")
    check(json.loads((d / lx.STATE_FILE).read_text())["tokens"] == 1000, "tokens counted across calls")

print(f"OK live_explain_test: {CHECKS} checks")
