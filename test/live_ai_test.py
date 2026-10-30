#!/usr/bin/env python3
"""
Offline test for lib/live_ai.py: the opt-in gate, when a call is due, the sidecar
(state survives a restart, a failure waits too), the overlay on a local build,
--if-stale seeing a new sidecar, the follow worker, and the output-token cap
reaching the `claude -p` child. Claude is faked; nothing leaves the machine.

Run: python3 test/live_ai_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
os.environ["WHOSAID_SPEAKER_DB"] = str(Path(tempfile.gettempdir()) / "whosaid-no-registry.json")
import claude_engine  # noqa: E402
import live  # noqa: E402
import live_ai  # noqa: E402
import live_insights as li  # noqa: E402

FIXTURE = ROOT / "test" / "fixtures" / "live" / "call.jsonl"
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


class FakeClient:
    def __init__(self, reply="", fail=False):
        self.reply, self.fail, self.tokens, self.resolved_model, self.prompts = reply, fail, 0, "", []

    def chat(self, system, user):
        self.prompts.append(user)
        self.tokens += 1000
        if self.fail:
            raise claude_engine.SummarizerError("`claude -p` failed twice: no reply within 90s", "")
        self.resolved_model = "claude-sonnet-5-5"
        return self.reply


def reply_for(ins):
    return "Here you go:\n" + json.dumps({
        "labels": {tp["id"]: f"Topic {n}" for n, tp in enumerate(ins["topics"], 1)} | {"made-up-9": "X"},
        "recap": ["Release notes go out Friday.", "Storage costs need a check."],
        "nextSteps": [{"kind": "agreed", "who": "me", "text": "Send the release notes.", "t": 30},
                      {"kind": "maybe", "who": "someone", "text": "  Check   the bucket. ", "t": "x"}]})


def ws_with(d: Path, toml: str) -> Path:
    ws = d / "ws"
    ws.mkdir()
    (ws / "whosaid.toml").write_text(toml)
    return ws


meta, entries = live.read_entries(FIXTURE)
ins = li.build(meta, entries, "s")

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    check(live_ai.config(None) is None, "no workspace: off")
    ws = ws_with(d, '[summarizer]\nengine = "claude"\n')
    check(live_ai.config(ws) is None, "[summarizer] engine = claude alone does not turn the live AI on")
    shutil.rmtree(ws)
    ws = ws_with(d, '[live]\nai = "claude"\nai_every = 60\nai_max_calls = 3\n')
    cfg = live_ai.config(ws)
    check(cfg and cfg["ai_every"] == 60 and cfg["ai_max_calls"] == 3 and cfg["ai_model"] == "sonnet"
          and cfg["ai_max_tokens"] == 1500 and cfg["ai_timeout"] == 90, f"[live] ai = claude: on, with defaults: {cfg}")

    # due: too short, a fresh call, waiting for more talk, the ended pass, the call cap
    check(not live_ai.due({}, {}, entries[:5], cfg), "fewer than MIN_LINES lines: not due")
    check(live_ai.due({}, {}, entries, cfg), "no sidecar yet: due")
    last = max(float(e["t"]) for e in entries)
    check(not live_ai.due({"t": last - 10}, {}, entries, cfg), "10 s of new talk < ai_every: not due")
    check(live_ai.due({"t": last - 10}, {"ended": True}, entries, cfg), "ended with unseen talk: one last pass")
    check(not live_ai.due({"t": last, "final": True}, {"ended": True}, entries, cfg), "final pass done: not due")
    check(not live_ai.due({"calls": 3}, {}, entries, cfg), "ai_max_calls reached: not due")

    # parse: unknown ids dropped, kinds and who normalized, bad t -> 0
    got = live_ai.parse(reply_for(ins), {tp["id"] for tp in ins["topics"]})
    check("made-up-9" not in got["labels"] and len(got["labels"]) == len(ins["topics"]), "labels only for real topic ids")
    check(got["nextSteps"][1] == {"kind": "suggested", "who": "them", "text": "Check the bucket.", "t": 0.0},
          f"next steps normalized: {got['nextSteps'][1]}")

    # prompt: topic ids and the newest lines, capped
    pr = live_ai.prompt(ins, entries)
    check(all(f"id {tp['id']}" in pr for tp in ins["topics"][:live_ai.TOPICS_SENT]) and "Transcript (newest part):" in pr,
          "prompt lists the topics and the transcript")
    check(len(pr) < live_ai.PROMPT_CHARS + 12000, f"prompt is bounded: {len(pr)} chars")

    # step: writes the sidecar, keeps state, a failure keeps the last answer and waits
    rec = d / "rec" / ".live"
    rec.mkdir(parents=True)
    jl = rec / "20261029T090000.jsonl"
    jl.write_text(FIXTURE.read_text().replace('"ended": true', '"ended": false', 1))  # still live
    fake = FakeClient(reply_for(ins))
    real_client = live_ai._client
    live_ai._client = lambda c: fake
    out = live_ai.step(jl, cfg, now=1.0)
    side = live_ai.read(jl)
    check(out and side["calls"] == 1 and side["tokens"] == 1000 and side["final"] is False
          and side["model"] == "claude-sonnet-5-5" and len(side["recap"]) == 2 and not side["error"],
          f"step writes the sidecar: {side.get('calls')}, {side.get('error')}")
    check(live_ai.step(jl, cfg) is None and len(fake.prompts) == 1, "nothing new: no second call (state survives)")
    with open(jl, "a") as fh:
        fh.write(json.dumps({"t": last + 200, "who": "them", "text": "One more thing about the storage bucket."}) + "\n")
    fake.fail, fake.tokens = True, 0  # a new client per call, as _client makes
    out = live_ai.step(jl, cfg)
    side = live_ai.read(jl)
    check(out and side["error"].startswith("`claude -p` failed") and side["calls"] == 2 and len(side["recap"]) == 2
          and side["tokens"] == 2000, "a failed call is recorded and keeps the last good answer")
    check(live_ai.step(jl, cfg) is None, "after a failure: waits for new talk, no hot retry")
    check(side["failures"] == 1, "a failure is counted")
    check(not live_ai.due({"failures": live_ai.MAX_FAILURES}, {}, entries, cfg), "MAX_FAILURES in a row: no more calls")
    check(live_ai.due({"failures": live_ai.MAX_FAILURES - 1}, {}, entries, cfg), "fewer failures: still due")

    # overlay through build_file, with localLabel kept; --if-stale sees a newer sidecar
    path, res = li.build_file(jl)
    t0 = res["topics"][0]
    check(t0.get("labelSource") == "ai" and t0["label"].startswith("Topic ") and t0.get("localLabel"),
          f"topic label from the sidecar, local one kept: {t0.get('label')} / {t0.get('localLabel')}")
    roots = {n["id"]: n["label"] for n in res["nodes"] if n["depth"] == 0}
    check(roots[t0["id"]] == t0["label"], "wordgraph root carries the AI label")
    check(res["nextSteps"][0]["source"] == "ai" and res["localNextSteps"], "AI next steps, local ones kept aside")
    check(res["ai"]["recap"] and res["ai"]["tokens"] == 2000 and res["ai"]["error"], "ai block: recap, tokens, last error")
    local = li.build(*live.read_entries(jl), jl.name[:-6])
    check("ai" not in local and all("localLabel" not in tp for tp in local["topics"]), "build() itself stays local")
    check(not li.stale(jl), "insights current after build_file")
    future = time.time() + 5
    os.utime(live_ai.ai_path(jl), (future, future))
    check(li.stale(jl), "a newer sidecar makes the insights stale")

    # the worker runs step() in its own thread and stops when told
    live_ai.ai_path(jl).unlink()
    fake.fail = False
    stop = threading.Event()
    w = live_ai.Worker(d / "rec", cfg, stop=stop.is_set, poll=0.05)
    w.start()
    for _ in range(100):
        if live_ai.read(jl).get("calls"):
            break
        time.sleep(0.05)
    stop.set()
    w.join(2)
    check(live_ai.read(jl).get("calls") == 1 and not w.is_alive(), "worker made one call and stopped")
    live_ai._client = real_client

    # follow without an opted-in workspace never starts the worker
    started = []
    real_worker = live_ai.Worker
    live_ai.Worker = lambda *a, **k: started.append(1) or real_worker(*a, **k)
    li.follow(d / "rec", poll=0.01, once=True, max_hours=0.001, ws=None)
    check(not started, "follow without --ws: no AI worker")
    li.follow(d / "rec", poll=0.01, once=True, max_hours=0.001, ws=str(ws))
    check(started, "follow --ws <opted-in workspace>: AI worker started")
    live_ai.Worker = real_worker

# the output cap reaches the claude child's env; the CLI counts tokens
seen = {}


def fake_run(argv, **kw):
    seen.update(kw.get("env") or {})
    return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(
        {"type": "result", "result": "{}", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                     "cache_read_input_tokens": 100}}), stderr="")


real_run = claude_engine.subprocess.run
claude_engine.subprocess.run = fake_run
try:
    c = claude_engine.ClaudeCLI("sonnet", binary=sys.executable, timeout=5, max_tokens=1500)
    c.chat("s", "u")
finally:
    claude_engine.subprocess.run = real_run
check(seen.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") == "1500", "max_tokens -> CLAUDE_CODE_MAX_OUTPUT_TOKENS")
check(c.tokens == 115, f"ClaudeCLI.tokens sums usage: {c.tokens}")

print(f"PASS: live_ai_test ({CHECKS} checks)")
