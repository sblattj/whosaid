#!/usr/bin/env python3
"""whosaid_live_insights / whosaid_live_ask over a synthetic live session."""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import live  # noqa: E402
import mcp_server  # noqa: E402

CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


LINES = [
    ("them", "Morning, let's start with the release plan for the mobile build."),
    ("me", "The mobile build passed the smoke tests yesterday."),
    ("them", "Great, then the release plan needs a date for the mobile build."),
    ("me", "I'll update the release notes and send them to the team today."),
    ("them", "Next, the billing export keeps timing out on large accounts."),
    ("me", "The billing export times out after thirty seconds on large accounts."),
    ("them", "Let's raise the billing export timeout and add paging."),
    ("me", "Maybe we could cache the billing export too?"),
]

with tempfile.TemporaryDirectory() as d:
    into = Path(d)
    out = mcp_server.whosaid_live_insights(recordings=d)
    check(out["ok"] is False and out["hint"], f"no session: ok=false with a hint: {out}")
    out = mcp_server.whosaid_live_ask("billing", recordings=d)
    check(out["ok"] is False and out["hint"], f"ask, no session: {out}")

    jl = into / live.LIVE_DIR / "20261030T150000.jsonl"
    jl.parent.mkdir()
    now = time.time()
    rows = [json.dumps({"meta": {"started": now - 600, "ended": False, "updated": now}})]
    for i, (who, text) in enumerate(LINES):
        t = i * 60
        rows.append(json.dumps({"t": t, "clock": time.strftime("%H:%M:%S", time.localtime(now - 600 + t)),
                                "who": who, "text": text}))
    jl.write_text("\n".join(rows) + "\n")

    out = mcp_server.whosaid_live_insights(recordings=d)
    check(out["ok"] and out["active"] and out["lines"] == len(LINES), f"insights header: {out}")
    check("transcript" not in out and "nodes" not in out, "insights drops the transcript and wordgraph nodes")
    check(out["topics"] and all({"label", "share", "terms", "first", "last"} <= set(t) for t in out["topics"]),
          f"topics shape: {out['topics']}")
    check(all(len(t["first"]) == 8 for t in out["topics"]), "topic clocks are HH:MM:SS")
    check(out["summaries"] and out["summaries"][0]["bullets"], f"summaries: {out['summaries']}")
    kinds = {(n["kind"], n["who"]) for n in out["next_steps"]}
    check(("agreed", "me") in kinds, f"the 'I'll update ... send' line is an agreed next step: {out['next_steps']}")
    check(all(isinstance(n["topic"], str) and n["topic"] for n in out["next_steps"]), "next steps carry a topic label")

    out = mcp_server.whosaid_live_ask("billing export timeout", recordings=d)
    check(out["ok"] and out["matches"], f"ask finds matches: {out}")
    check("billing" in out["matches"][0]["text"].lower() and out["matches"][0]["clock"], "best match is about billing")
    check("t" not in out["matches"][0] and "mode" not in out, "ask returns clock, not raw offsets or mode")
    out = mcp_server.whosaid_live_ask("mobile build", minutes=2, recordings=d)
    check(out["ok"] and not any("mobile" in m["text"].lower() for m in out["matches"]),
          f"minutes limits the search window: {out}")
    check(mcp_server.whosaid_live_ask("  ", recordings=d)["ok"] is False, "empty question refused")
    check(mcp_server.whosaid_live_ask("x", minutes=0, recordings=d)["ok"] is False, "minutes <= 0 refused")

print(f"PASS: mcp_live_tools_test ({CHECKS} checks)")
