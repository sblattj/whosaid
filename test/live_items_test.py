#!/usr/bin/env python3
"""
Offline test for `whosaid live export-items` (lib/live_items.py, GitHub issue #227):
an ended live explainer stem becomes rows (stable id, kind, section and window, asker,
owner, a verbatim transcript line, every merged source), read only from top-level
arrays (a retracted item never resurfaces), stems whose .jsonl meta has ended:false
are skipped, near-duplicates within a stem merge, --since and csv work, an ended call
always gets a recap.json (a local, deterministic fallback), and the items.status.json
overlay (CLI and served-page writers) is merged into the rows. Temp dirs only.

Run: python3 test/live_items_test.py
"""
from __future__ import annotations

import csv
import http.client
import io
import json
import os
import re
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
import live_items as li  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


STEM = "20261116T100000"
STARTED = time.mktime((2026, 11, 16, 10, 0, 0, 0, 0, -1))
# (t, who, name, text, has_clock)
LINES = [
    (0, "me", None, "Let's go over the cache rollout today.", True),
    (30, "them", "Ana", "Can you share the load test numbers by Friday?", True),
    (60, "me", None, "Sure, I'll share the load test numbers by Friday.", True),
    (90, "them", "Ben", "How long does a cached copy live before it expires?", True),
    (120, "me", None, "We should ship the cache behind a feature flag.", True),
    (150, "them", "Ana", "Yes, agreed.", True),
    (240, "them", "Ben", "Who owns the rollback runbook after launch?", True),
    (270, "them", "Ben", "Could you send me the runbook link afterwards?", True),
    (300, "me", None, "Ben will write the rollback runbook this week.", False),  # no clock: computed
    (330, "them", "Ana", "Sounds good, thanks everyone.", True),
]


def write_jsonl(into: Path, stem: str, ended: bool, lines=LINES, started=STARTED) -> Path:
    jl = into / live.LIVE_DIR / f"{stem}.jsonl"
    jl.parent.mkdir(parents=True, exist_ok=True)
    rows = [json.dumps({"meta": {"started": started, "ended": ended, "updated": started + 400}})]
    for i, (t, who, name, text, has_clock) in enumerate(lines):
        e = {"n": i, "t": float(t), "who": who, "text": text}
        if name:
            e["name"] = name
        if has_clock:
            e["clock"] = time.strftime("%H:%M:%S", time.localtime(started + t))
        rows.append(json.dumps(e))
    jl.write_text("\n".join(rows) + "\n")
    return jl


def sec1_misheard():
    return {"title": "Cache rollout", "from": 0, "to": 200,
            "explain": ["Ana asked for the load test numbers."],
            "asks": [{"who": "Ana", "text": "send the invoice to Dana by Friday"}],
            "questions": ["How long does a cached copy live before it expires?"],
            "suggested": ["Ask for the cache hit rate before the rollout."],
            "low_confidence": ["'Ben' is a weak voice match (0.52)"]}


SEC2 = {"title": "Runbook", "from": 200, "to": 400, "explain": ["Ben owns the runbook."],
        "questions": ["Who owns the rollback runbook after launch?"],
        "for_you": [{"who": "Ben", "ask": "could you send me the runbook link?", "answer": "here it is",
                     "source": ""}]}
GOOD_ASK = {"who": "Ana", "text": "can you share the load test numbers by Friday?"}


def build_main(into: Path, with_recap=True, with_ai=True) -> Path:
    """Sections written while the call is live, then the call ends (meta ended: true)."""
    write_jsonl(into, STEM, ended=False)
    d = lx.store_dir(into, STEM)
    lx.add(d, sec1_misheard(), copy=lambda *_: False)
    lx.correct(d, "001", before="send the invoice to Dana by Friday",
               after="share the load test numbers by Friday", quote="the load test numbers", who="Ana", t=30,
               changes={"asks": [GOOD_ASK]})
    lx.add(d, SEC2, copy=lambda *_: False)
    write_jsonl(into, STEM, ended=True)
    if with_recap:
        lx.recap(d, {"decisions": ["Ship the cache behind a feature flag"],
                     "questions": ["How long does a cached copy live before expiring?"],
                     "owes": [{"who": "Ben", "text": "Ben will write the rollback runbook this week"}]})
    if with_ai:
        (into / live.LIVE_DIR / f"{STEM}.ai.json").write_text(json.dumps({
            "version": 1, "t": 330, "labels": {}, "recap": [],
            "nextSteps": [{"kind": "agreed", "who": "me", "text": "Share the load test numbers by Friday", "t": 60}]}))
    return d


def snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()}


def by_text(rows, needle):
    return [r for r in rows if needle.lower() in r["text"].lower()]


def run_cli(*args, env=None):
    return subprocess.run([sys.executable, str(ROOT / "lib" / "live_items.py"), *args],
                          capture_output=True, text=True, timeout=60, env=env)


# ---------------------------------------------------------------- rows, kinds, sources
with tempfile.TemporaryDirectory() as tmp:
    into = Path(tmp)
    d = build_main(into)
    before = snapshot(into)
    res = li.export(into, STEM)
    check(res["ok"] is True and res["stems"] == [STEM], f"export ok for the ended stem: {res.get('stems')} {res}")
    check(snapshot(into) == before, "export-items is read-only: no file written or touched")
    rows = res["rows"]
    keys = {"id", "stem", "kind", "text", "section", "from", "to", "window", "asker", "owner", "quote",
            "sources", "status"}
    check(rows and all(keys <= set(r) for r in rows), f"every row has the documented keys: {rows[:1]}")
    kinds = {r["kind"] for r in rows}
    check(kinds == {"ask", "question", "suggestion", "decision", "next step"}, f"all five kinds: {kinds}")
    check(all(r["stem"] == STEM and r["id"].startswith(STEM + "-") for r in rows), "ids carry the stem")
    check(all(re.fullmatch(STEM + r"-(ask|question|suggestion|decision|next-step)-[0-9a-f]{12}", r["id"])
              for r in rows), f"id = stem-kind-hash12: {[r['id'] for r in rows]}")
    check(len({r["id"] for r in rows}) == len(rows), "ids are unique")

    # retracted (the mis-heard ask lives only in revisions[] / corrections[])
    check(not by_text(rows, "invoice") and not by_text(rows, "Dana"),
          f"the retracted ask never resurfaces: {by_text(rows, 'invoice')}")
    check(all("revisions" not in s and "corrections" not in s for r in rows for s in r["sources"]),
          "no source points into revisions[] or corrections[]")
    raw = json.loads(lx.section_files(d)["001"].read_text())
    check(any("invoice" in json.dumps(rv) for rv in raw["revisions"]), "fixture: the mis-hear is in revisions[]")

    # the ask: section ask + ai next step merged (near-duplicate, across ask/next step)
    asks = by_text(rows, "load test numbers")
    check(len(asks) == 1, f"one row for the ask and its ai next step: {asks}")
    ask = asks[0]
    s1 = lx.section_files(d)["001"].name
    check(ask["kind"] == "ask" and ask["section"] == "001", f"ask kind/section: {ask}")
    check(ask["text"] == GOOD_ASK["text"], f"row text is the first (section) source's text: {ask['text']}")
    check(f"{s1}:asks[0]" in ask["sources"], f"ask source path {s1}:asks[0]: {ask['sources']}")
    check(f"{STEM}.ai.json:nextSteps[0]" in ask["sources"], f"merged ai source listed: {ask['sources']}")
    check(ask["from"] == 0 and ask["to"] == 200 and ask["window"] == "10:00–10:03", f"time window: {ask}")
    check(ask["asker"] == "Ana" and ask["owner"] == "me", f"asker Ana, owner me (asked of you): {ask}")
    q = ask["quote"]
    check(q == {"t": 30.0, "clock": "10:00:30", "speaker": "Ana",
                "text": "Can you share the load test numbers by Friday?"}, f"verbatim line with clock+speaker: {q}")

    # the question: section question + recap question merged
    qs = by_text(rows, "cached copy live")
    check(len(qs) == 1, f"a recap question repeating a section question is one row: {qs}")
    qr = qs[0]
    check(qr["kind"] == "question" and f"{s1}:questions[0]" in qr["sources"]
          and "recap.json:questions[0]" in qr["sources"], f"both question sources listed: {qr['sources']}")
    check(qr["asker"] == "Ben" and qr["quote"]["speaker"] == "Ben" and qr["quote"]["clock"] == "10:01:30",
          f"question asker from its transcript line: {qr}")

    # for_you is an ask, recap owes a next step with its owner, a computed clock
    s2 = lx.section_files(d)["002"].name
    fy = by_text(rows, "runbook link")
    check(len(fy) == 1 and fy[0]["kind"] == "ask" and fy[0]["sources"] == [f"{s2}:for_you[0]"]
          and fy[0]["asker"] == "Ben" and fy[0]["owner"] == "me", f"for_you row: {fy}")
    owe = by_text(rows, "write the rollback runbook")
    check(len(owe) == 1 and owe[0]["kind"] == "next step" and owe[0]["sources"] == ["recap.json:owes[0]"],
          f"recap owe row: {owe}")
    check(owe[0]["owner"] == "Ben", f"owner named by the owe: {owe[0]}")
    check(owe[0]["quote"] and owe[0]["quote"]["clock"] == "10:05:00" and owe[0]["quote"]["speaker"] == "me"
          and owe[0]["quote"]["text"] == "Ben will write the rollback runbook this week.",
          f"a line without a clock gets one from started + t: {owe[0]['quote']}")
    check(owe[0]["section"] == "002", f"a recap row lands in the section its line falls in: {owe[0]}")
    dec = [r for r in rows if r["kind"] == "decision"]
    check(len(dec) == 1 and dec[0]["sources"] == ["recap.json:decisions[0]"]
          and dec[0]["quote"]["text"] == "We should ship the cache behind a feature flag.", f"decision row: {dec}")
    sug = [r for r in rows if r["kind"] == "suggestion"]
    check(len(sug) == 1 and sug[0]["quote"] is None and sug[0]["sources"] == [f"{s1}:suggested[0]"],
          f"a suggestion (never said on the call) has no transcript line: {sug}")
    check(not by_text(rows, "weak voice match"), "low_confidence is not an item")
    check(all(r["status"] == "open" for r in rows), "no overlay: every row is open")

    # stable ids across reruns
    again = li.export(into, STEM)["rows"]
    check([r["id"] for r in again] == [r["id"] for r in rows], "ids are stable across reruns")
    check(json.dumps(again, sort_keys=True) == json.dumps(rows, sort_keys=True), "reruns are identical")

    # ---- csv
    out = li.to_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(out)))
    check(len(parsed) == len(rows), f"csv: one line per row: {len(parsed)}")
    check(list(parsed[0].keys()) == li.CSV_COLUMNS, f"csv header: {list(parsed[0].keys())}")
    crow = [p for p in parsed if p["id"] == ask["id"]][0]
    check(crow["quote_clock"] == "10:00:30" and crow["quote_speaker"] == "Ana"
          and crow["sources"] == " | ".join(ask["sources"]) and crow["kind"] == "ask", f"csv row: {crow}")

    # ---- CLI (module and launcher), json and csv
    r = run_cli("export-items", STEM, "--into", tmp)
    check(r.returncode == 0, f"cli export-items exit 0: {r.stderr}")
    cj = json.loads(r.stdout)
    check(cj["ok"] and [x["id"] for x in cj["rows"]] == [x["id"] for x in rows], "cli json == api rows")
    r = run_cli("export-items", STEM, "--into", tmp, "--format", "csv")
    check(r.returncode == 0 and r.stdout.splitlines()[0] == ",".join(li.CSV_COLUMNS), f"cli csv header: {r.stdout[:200]}")
    r = subprocess.run(["bash", str(ROOT / "whosaid"), "live", "export-items", STEM, "--into", tmp, "--format", "csv"],
                       capture_output=True, text=True, timeout=120)
    check(r.returncode == 0 and len(list(csv.DictReader(io.StringIO(r.stdout)))) == len(rows),
          f"launcher `whosaid live export-items` runs: rc={r.returncode} {r.stderr[-400:]}")
    check(snapshot(into) == before, "the CLI runs are read-only too")

    # ---- id stability when sources appear later: a recap/ai arriving never renames a section row
with tempfile.TemporaryDirectory() as tmp:
    into = Path(tmp)
    d = build_main(into, with_recap=False, with_ai=False)
    lx.recap(d, {"decisions": ["Ship the cache behind a feature flag"]})  # a real recap, no question yet
    first = {r["text"]: r["id"] for r in li.export(into, STEM)["rows"]}
    lx.recap(d, {"decisions": ["Ship the cache behind a feature flag"],
                 "questions": ["How long does a cached copy live before expiring?"],
                 "owes": [{"who": "Ana", "text": "share the load test numbers by Friday"}]})
    (into / live.LIVE_DIR / f"{STEM}.ai.json").write_text(json.dumps({
        "version": 1, "nextSteps": [{"kind": "agreed", "who": "me", "text": "Share the load test numbers by Friday", "t": 60}]}))
    later = li.export(into, STEM)["rows"]
    lq = by_text(later, "cached copy live")[0]
    la = by_text(later, "load test numbers")
    check(lq["id"] == first[lq["text"]] and "recap.json:questions[0]" in lq["sources"],
          f"question id unchanged after a duplicate recap question arrived, sources grew: {lq}")
    check(len(la) == 1 and la[0]["id"] == first[GOOD_ASK["text"]] and len(la[0]["sources"]) == 3,
          f"ask id unchanged, owe + next step merged into it: {la}")

# ---------------------------------------------------------------- ended:false, --since
with tempfile.TemporaryDirectory() as tmp:
    into = Path(tmp)
    build_main(into)
    write_jsonl(into, "20261117T090000", ended=False)
    lx.add(lx.store_dir(into, "20261117T090000"), {"title": "Live one", "questions": ["Is the live call done?"]})
    write_jsonl(into, "20261115T090000", ended=True)
    lx.add(lx.store_dir(into, "20261115T090000"), {"title": "Older", "questions": ["What is the pilot budget?"]})

    one = li.export(into, "20261117T090000")
    check(one["ok"] is False and "ended" in one["error"], f"an explicit stem still live is refused: {one}")
    check(run_cli("export-items", "20261117T090000", "--into", tmp).returncode == 1, "cli: not ended exits 1")
    allr = li.export(into, since="20261101T000000")
    check(allr["ok"] and allr["stems"] == ["20261115T090000", STEM], f"--since: every ended stem after it: {allr['stems']}")
    check({"stem": "20261117T090000", "reason": "not ended"} in allr["skipped"], f"skipped lists the live one: {allr['skipped']}")
    check(not by_text(allr["rows"], "live call done"), "rows of an ended:false stem never appear")
    newer = li.export(into, since="20261116T000000")
    check(newer["stems"] == [STEM] and not by_text(newer["rows"], "pilot budget"),
          f"--since excludes the stem itself and older ones: {newer['stems']}")
    both = li.export(into, "20261116T235959", since="20261115T090000")
    check(both["ok"] and both["stems"] == [STEM], f"stem + --since: the range (since, stem]: {both}")
    r = run_cli("export-items", "--since", "20261116T000000", "--into", tmp)
    check(r.returncode == 0 and json.loads(r.stdout)["stems"] == [STEM], f"cli --since: {r.stdout[:300]} {r.stderr}")
    check(run_cli("export-items", "--into", tmp).returncode == 2, "cli: neither a stem nor --since is a usage error")

# ---------------------------------------------------------------- recap fallback
with tempfile.TemporaryDirectory() as tmp:
    into = Path(tmp)
    d = build_main(into, with_recap=False, with_ai=False)
    rc_path = d / "recap.json"
    check(not rc_path.exists(), "fixture: an ended call without a recap")
    derived = lx.derive_recap(d)
    check(derived and derived["derived"] is True, f"derive_recap: {derived}")
    check({"who": "me", "text": GOOD_ASK["text"]} in derived["owes"]
          and "How long does a cached copy live before it expires?" in derived["questions"],
          f"derived from top-level section items: {derived}")
    check("We should ship the cache behind a feature flag." in derived["decisions"],
          f"decisions from the local decisions ledger (agreed only): {derived['decisions']}")
    check(not any("invoice" in json.dumps(derived) for _ in [0]), "derived recap skips the retracted ask")
    check(not rc_path.exists(), "derive_recap writes nothing")
    rows_mem = li.export(into, STEM)["rows"]
    check(not rc_path.exists(), "export on an ended stem without a recap stays read-only")
    check([r for r in rows_mem if r["kind"] == "decision"], "export still has decision rows (in-memory fallback)")
    check(all("recap.json:" not in s for r in rows_mem for s in r["sources"]),
          f"in-memory fallback rows never cite a recap.json that is not on disk: {[r['sources'] for r in rows_mem]}")

    check(lx.ensure_recap(d) is True and rc_path.is_file(), "ensure_recap writes recap.json on an ended call")
    disk = json.loads(rc_path.read_text())
    check(disk["derived"] is True and disk["decisions"] == derived["decisions"], f"recap.json derived: {disk}")
    check((d / "recap.html").is_file() and 'id="s-recap"' in (d / "index.html").read_text(), "recap rendered")
    check(lx.ensure_recap(d) is False, "ensure_recap is idempotent (nothing changed: no rewrite)")
    rows_disk = li.export(into, STEM)["rows"]
    da = by_text(rows_disk, "load test numbers")
    check(len(da) == 1 and da[0]["owner"] == "me" and da[0]["asker"] == "Ana"
          and "recap.json:owes[0]" in da[0]["sources"], f"a derived owe is owed by me, asked by Ana: {da}")
    check(any(s.startswith("recap.json:decisions[") for r in rows_disk for s in r["sources"]),
          "a fresh derived recap on disk is cited as recap.json")

    # a later correction that retracts an ask refreshes the derived recap, and export never shows it
    lx.correct(d, "001", before=GOOD_ASK["text"], after="nothing was asked", changes={"asks": []})
    disk = json.loads(rc_path.read_text())
    check(not any("load test" in o["text"] for o in disk["owes"]), f"correct() refreshed the derived recap: {disk}")
    rows_after = li.export(into, STEM)["rows"]
    check(not [r for r in rows_after if r["kind"] in ("ask", "next step") and "load test" in r["text"]],
          f"the retracted ask is gone from rows: {by_text(rows_after, 'load test')}")

    # a real recap is never replaced by the fallback
    lx.recap(d, {"decisions": ["A real decision"]})
    check(lx.ensure_recap(d) is False and json.loads(rc_path.read_text()).get("derived") is not True,
          "a producer's recap is kept")

with tempfile.TemporaryDirectory() as tmp:
    into = Path(tmp)
    write_jsonl(into, "20261118T090000", ended=False)
    d = lx.store_dir(into, "20261118T090000")
    lx.add(d, {"title": "Still live", "questions": ["When do we ship?"]})
    check(lx.ensure_recap(d) is False and not (d / "recap.json").exists(), "no fallback recap while the call is live")
    # the capture loop's end hook
    jl = write_jsonl(into, "20261118T090000", ended=True)
    check(live.finish_explainer(jl) is True and (d / "recap.json").is_file(),
          "live.finish_explainer writes the fallback recap when capture ends")
    check(live.finish_explainer(into / live.LIVE_DIR / "nope.jsonl") is False, "no explainer: nothing to do")

with tempfile.TemporaryDirectory() as tmp:
    into = Path(tmp)
    jl = write_jsonl(into, "20261119T090000", ended=True)
    d = lx.store_dir(into, "20261119T090000")
    lx.add(d, {"title": "Ended", "asks": [{"who": "Ana", "text": "can you send the deck?"}]})
    # an ended call whose recap is written by add(): the store had none before
    check((d / "recap.json").is_file(), "add() on an ended call writes the fallback recap")
    (d / "recap.json").unlink()
    (d / "recap.html").unlink()

    class Fake:
        tokens = 10

        def chat(self, system, prompt):
            return json.dumps({"sections": [], "corrections": [], "recap": None})
    orig = lx._engine_client
    lx._engine_client = lambda cfg: Fake()
    try:
        cfg = dict(lx.ENGINE_DEFAULTS, explain="claude", ws="", claude_bin="")
        out = lx.engine_step(d, jl, cfg)
    finally:
        lx._engine_client = orig
    check(out is not None and out["final"] and (d / "recap.json").is_file()
          and json.loads((d / "recap.json").read_text())["derived"] is True,
          f"the engine's ended pass without a model recap still leaves recap.json: {out}")

# ---------------------------------------------------------------- overlay
with tempfile.TemporaryDirectory() as tmp:
    into = Path(tmp)
    d = build_main(into)
    rows = li.export(into, STEM)["rows"]
    ask = by_text(rows, "load test numbers")[0]
    q = by_text(rows, "cached copy live")[0]
    res = li.set_status(d, ask["id"], "done", note="sent the numbers", by="test")
    check(res["ok"] and (d / li.STATUS_FILE).is_file(), f"set_status writes {li.STATUS_FILE}: {res}")
    ov = json.loads((d / li.STATUS_FILE).read_text())
    check(ov["items"][ask["id"]]["status"] == "done" and ov["items"][ask["id"]]["note"] == "sent the numbers",
          f"overlay keyed by row id: {ov}")
    merged = li.export(into, STEM)["rows"]
    check([r["status"] for r in merged if r["id"] == ask["id"]] == ["done"], "export merges the overlay status")
    check(all(r["status"] == "open" for r in merged if r["id"] != ask["id"]), "other rows stay open")
    check(li.set_status(d, f"{STEM}-ask-000000000000", "done")["ok"] is False, "an unknown row id is refused")
    check(li.set_status(d, ask["id"], "maybe")["ok"] is False, "an unknown status is refused")
    keys = li.page_keys(d)
    check(keys.get("001-a0") == {"id": ask["id"], "status": "done"}, f"page key 001-a0 -> the ask row: {keys}")
    check(keys.get("001-q0", {}).get("id") == q["id"] and keys.get("recap-q0", {}).get("id") == q["id"],
          f"a section and a recap checkbox share one merged row: {keys}")
    check(keys.get("002-f0", {}).get("id") == by_text(rows, "runbook link")[0]["id"], "for_you key 002-f0")
    r = run_cli("mark-item", STEM, q["id"], "--status", "done", "--into", tmp)
    check(r.returncode == 0 and json.loads((d / li.STATUS_FILE).read_text())["items"][q["id"]]["status"] == "done",
          f"cli mark-item: {r.stdout} {r.stderr}")
    check(run_cli("mark-item", STEM, "bogus", "--status", "done", "--into", tmp).returncode == 1, "cli: bad id exits 1")
    li.set_status(d, q["id"], "open")
    check(li.page_keys(d)["001-q0"]["status"] == "open", "reopen works")

    # served page: GET /items.json, POST /item-status (same-origin JSON only)
    stop = threading.Event()
    srv, url, _tok = lx.serve(d, port=0, stop=stop)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    th.start()
    port = srv.server_address[1]
    host = f"127.0.0.1:{port}"
    try:
        def req(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request(method, path, body=body, headers=headers or {})
            rr = c.getresponse()
            data = rr.read()
            c.close()
            return rr.status, data
        st, data = req("GET", "/items.json")
        got = json.loads(data)
        check(st == 200 and got["items"]["001-a0"] == {"id": ask["id"], "status": "done"}, f"GET /items.json: {got}")
        body = json.dumps({"id": q["id"], "status": "done"})
        st, _ = req("POST", "/item-status", body, {"Content-Type": "text/plain"})
        check(st == 415, f"POST without a JSON content type is refused: {st}")
        st, _ = req("POST", "/item-status", body, {"Content-Type": "application/json", "Origin": "http://evil.example"})
        check(st == 403, f"POST from another origin is refused: {st}")
        st, _ = req("POST", "/item-status", json.dumps({"id": "nope", "status": "done"}),
                    {"Content-Type": "application/json", "Origin": f"http://{host}"})
        check(st == 400, f"POST with an unknown id: 400: {st}")
        st, data = req("POST", "/item-status", body, {"Content-Type": "application/json", "Origin": f"http://{host}"})
        check(st == 200 and json.loads(data)["ok"], f"POST same-origin JSON: {st} {data}")
        check(json.loads((d / li.STATUS_FILE).read_text())["items"][q["id"]]["status"] == "done"
              and json.loads((d / li.STATUS_FILE).read_text())["items"][q["id"]]["by"] == "page",
              "the page's checkbox lands in the overlay")
        st, page = req("GET", "/")
        check(b"/items.json" in page and b"/item-status" in page, "the served page script syncs with the overlay")
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()

print(f"OK live_items_test: {CHECKS} checks")
