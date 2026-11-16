#!/usr/bin/env python3
"""whosaid_decisions (issue #213) over the synthetic fixture workspace and a live session.

Run: uv run --with "mcp[cli]>=2,<3" python test/mcp_decisions_tool_test.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
os.environ["WHOSAID_SPEAKER_DB"] = str(Path(tempfile.gettempdir()) / "whosaid-no-registry.json")
os.environ.pop("WHOSAID_WORKSPACE", None)
import live  # noqa: E402
import mcp_server  # noqa: E402

WS = ROOT / "test" / "fixtures" / "decisions"
CHECKS = 0


def check(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


out = mcp_server.whosaid_decisions(meeting="2026-03-02-1000", workspace=str(WS))
check(out["ok"] and out["source"] == "meeting" and out["engine"] == "local", f"meeting ledger: {out.get('error')}")
split = [t for t in out["topics"] if "split across visits" in t["flags"]]
check(len(split) == 1 and len(split[0]["decisions"]) == 2, f"one split topic with two decisions: {out['topics']}")
ds = {d["id"]: d for d in out["decisions"]}
a, b = (ds[i] for i in split[0]["decisions"])
check(a["turns"][0]["t_str"] == "00:11:20" and b["turns"][0]["t_str"] == "00:39:20", "each cites its own proposal")
check(all(t["meeting"] == "2026-03-02-1000" for t in a["turns"] + b["turns"]), "turns carry the meeting")
json.dumps(out)  # the tool result is plain JSON

out = mcp_server.whosaid_decisions(meeting="03-02", workspace=str(WS))
check(out["ok"] and out["meeting"] == "2026-03-02-1000", "a unique substring resolves")
out = mcp_server.whosaid_decisions(meeting="../etc", workspace=str(WS))
check(not out["ok"] and out["hint"], "a path is refused")
out = mcp_server.whosaid_decisions(meeting="nope", workspace=str(WS))
check(not out["ok"] and "nope" in out["error"] and out["hint"], f"unknown meeting: {out}")

with tempfile.TemporaryDirectory() as d:
    out = mcp_server.whosaid_decisions(recordings=d)
    check(not out["ok"] and out["hint"], f"no live session: {out}")
    jl = Path(d) / live.LIVE_DIR / "20260302T100000.jsonl"
    jl.parent.mkdir()
    rows = [json.dumps({"meta": {"started": 0, "ended": False}})]
    lines = [(0, "them", "The cache layer for the pricing service keeps serving stale prices on checkout."),
             (20, "me", "Let's set the cache expiry for the pricing service to five minutes."),
             (40, "them", "Agreed, five minutes works for me.")]
    rows += [json.dumps({"t": t, "clock": f"10:00:{t:02d}", "who": who, "text": text}) for t, who, text in lines]
    jl.write_text("\n".join(rows) + "\n")
    out = mcp_server.whosaid_decisions(recordings=d)
    check(out["ok"] and out["source"] == "live" and out["session"] == "20260302T100000", f"live: {out.get('error')}")
    check(len(out["decisions"]) == 1 and out["decisions"][0]["status"] == "agreed"
          and out["decisions"][0]["turns"][0]["clock"] == "10:00:20", f"live decision: {out['decisions']}")

print(f"PASS: mcp_decisions_tool_test ({CHECKS} checks)")
