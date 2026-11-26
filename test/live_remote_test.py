#!/usr/bin/env python3
"""
Offline test for `whosaid live serve` / `live pull` (issue #244): bind refusals (wildcard,
non-loopback without a token), the bearer token (none or wrong = 401, and pull exits 2),
read-only (POST = 405), GET /sessions, cursor paging with no repeated line, long-poll,
a pull mirror that gets new lines while the call goes on, keeps the remote's times, picks up
a name given later to an earlier line, gets the `ended` marker, and says where it came from
("source" in the meta and in latest_session). Real HTTP on 127.0.0.1, temp dirs, no audio.

Run: python3 test/live_remote_test.py
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import live  # noqa: E402
import live_remote as lr  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def turn(n, t, who, text, started, **kw):
    return {"n": n, "t": t, "who": who, "text": text,
            "clock": time.strftime("%H:%M:%S", time.localtime(started + t)), **kw}


def write(into, stem, started, entries, ended=False):
    d = into / live.LIVE_DIR
    live.write_session(d / f"{stem}.md", d / f"{stem}.jsonl", started, list(entries), ended)


def get(url, token=None, method="GET"):
    req = urllib.request.Request(url, method=method,
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def test_bind_rules():
    check(lr.check_bind("0.0.0.0", "tok") is not None, "wildcard 0.0.0.0 refused even with a token")
    check(lr.check_bind("::", "tok") is not None, "wildcard :: refused")
    check(lr.check_bind("100.64.1.2", None) is not None, "non-loopback without a token refused")
    check(lr.check_bind("100.64.1.2", "tok") is None, "non-loopback with a token allowed")
    check(lr.check_bind("127.0.0.1", None) is None, "loopback without a token allowed")


def test_token_file():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sub" / "live-token"
        tok = lr.ensure_token(p)
        check(len(tok) >= 32 and lr.read_token(p) == tok, "token created and read back")
        check(p.stat().st_mode & 0o777 == 0o600, f"token file is 0600 (got {oct(p.stat().st_mode & 0o777)})")
        check(lr.ensure_token(p) == tok, "an existing token is kept")


def test_serve_and_pull():
    with tempfile.TemporaryDirectory() as td:
        src, dst = Path(td) / "src", Path(td) / "dst"
        started = time.time() - 600  # the remote's clock; the mirror must not restamp it
        stem = "20261126T101500"
        lines = [turn(0, 1.0, "me", "hello there", started), turn(1, 4.0, "them", "hi", started, voice="v1")]
        write(src, stem, started, lines)
        srv = lr.start_server(src, "127.0.0.1", 0, "s3cret")
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            code, _ = get(f"{url}/sessions")
            check(code == 401, f"no token: 401 (got {code})")
            code, _ = get(f"{url}/sessions", "wrong")
            check(code == 401, f"wrong token: 401 (got {code})")
            code, _ = get(f"{url}/sessions", "s3cret", method="POST")
            check(code in (401, 405), f"POST is never served (got {code})")
            code, body = get(f"{url}/sessions", "s3cret")
            check(code == 200 and body["sessions"][0]["stem"] == stem and body["sessions"][0]["lines"] == 2,
                  f"GET /sessions lists the session: {body}")
            code, body = get(f"{url}/sessions/..%2Fetc", "s3cret")
            check(code in (400, 404), f"a path outside .live is refused (got {code})")
            code, r1 = get(f"{url}/sessions/{stem}", "s3cret")
            check(code == 200 and len(r1["entries"]) == 2 and r1["cursor"] == f"{stem}:n1", f"whole session + cursor: {r1}")
            code, r2 = get(f"{url}/sessions/{stem}?cursor={r1['cursor']}", "s3cret")
            check(code == 200 and r2["entries"] == [] and r2["cursor"] == r1["cursor"], "nothing new after the cursor")

            # long-poll: a line written 1 s into a 10 s wait comes back early
            def later():
                time.sleep(1.0)
                write(src, stem, started, lines + [turn(2, 9.0, "them", "new line", started, voice="v1")])
            threading.Thread(target=later).start()
            t0 = time.monotonic()
            code, r3 = get(f"{url}/sessions/{stem}?cursor={r1['cursor']}&wait=10", "s3cret")
            took = time.monotonic() - t0
            check(code == 200 and [e["text"] for e in r3["entries"]] == ["new line"] and took < 5,
                  f"long-poll returns the new line early ({took:.1f}s): {r3}")
            lines.append(turn(2, 9.0, "them", "new line", started, voice="v1"))

            # a pull with the wrong token exits 2 and writes nothing
            rc = lr.pull(url, dst, "wrong", once=True, wait=1)
            check(rc == 2 and not (dst / live.LIVE_DIR).exists(), "pull with a wrong token: exit 2, no mirror")

            # the mirror follows the call: new lines, a later name, then ended
            box = {}
            th = threading.Thread(target=lambda: box.setdefault("rc", lr.pull(url, dst, "s3cret", once=True,
                                                                               wait=2, resync=1.5)))
            th.start()
            mjl = dst / live.LIVE_DIR / f"{stem}.jsonl"
            for _ in range(50):
                if mjl.exists() and len(live.read_entries(mjl)[1]) == 3:
                    break
                time.sleep(0.1)
            check(mjl.exists() and len(live.read_entries(mjl)[1]) == 3, "mirror has the 3 lines so far")
            lines.append(turn(3, 12.0, "me", "fourth", started))
            write(src, stem, started, lines)
            for _ in range(60):
                if len(live.read_entries(mjl)[1]) == 4:
                    break
                time.sleep(0.1)
            check(len(live.read_entries(mjl)[1]) == 4, "a line written during the call reaches the mirror")
            lines[1] = {**lines[1], "name": "B"}  # the voice got a name later (voices.annotate)
            write(src, stem, started, lines, ended=True)
            th.join(timeout=20)
            check(box.get("rc") == 0, f"pull --once exits 0 after the call ends (got {box.get('rc')})")
            meta, ents = live.read_entries(mjl)
            check(meta.get("ended") is True, "the mirror has the ended marker")
            check(meta.get("source") == f"remote:{url}", f"meta names the source: {meta}")
            check(abs(float(meta["started"]) - started) < 1e-6, "started is the remote's, not restamped")
            check([e["n"] for e in ents] == [0, 1, 2, 3], f"each line once, in order: {[e['n'] for e in ents]}")
            check([e["clock"] for e in ents] == [e["clock"] for e in lines], "line times are the remote's")
            check(ents[1].get("name") == "B", "a name given later reaches the earlier line")
            r = live.latest_session(dst)
            check(r["ok"] and r["source"] == f"remote:{url}" and r["ended"], f"latest_session reads the mirror: {r}")
            check(live.latest_session(src)["source"] == "local", "a local session says source local")

            # resume: a second pull of the same ended session duplicates nothing
            rc = lr.pull(url, dst, "s3cret", stem=stem, wait=1)
            check(rc == 0 and [e["n"] for e in live.read_entries(mjl)[1]] == [0, 1, 2, 3], "re-pull: no duplicate line")
        finally:
            srv.shutdown()


def test_cli_refuses():
    with tempfile.TemporaryDirectory() as td:
        tf = Path(td) / "none"
        rc = live.main(["serve", "--serve-only", "--bind", "0.0.0.0", "--into", td, "--token-file", str(tf)])
        check(rc == 2, f"serve --bind 0.0.0.0 exits 2 (got {rc})")
        rc = live.main(["serve", "--serve-only", "--bind", "192.0.2.7", "--into", td, "--token-file", str(tf)])
        check(rc == 2, f"serve on a LAN address without a token exits 2 (got {rc})")
        rc = live.main(["pull", "--from", "192.0.2.7:8766", "--into", td, "--token-file", str(tf), "--once"])
        check(rc == 2, f"pull from a remote host without a token exits 2 (got {rc})")


if __name__ == "__main__":
    test_bind_rules()
    test_token_file()
    test_serve_and_pull()
    test_cli_refuses()
    print(f"live_remote_test: {CHECKS} checks passed")
