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

    # ---- whosaid_live(since=cursor) (#187): only newer lines and a new cursor ----
    def write(ents, ended=False):
        live.write_session(jl.with_suffix(".md"), jl, now - 600, list(ents), ended)

    ents = [{"t": float(i * 10), "n": i, "who": "them", "voice": "v1", "name": "Alice", "text": f"point {i}",
             "clock": time.strftime("%H:%M:%S", time.localtime(now - 600 + i * 10))} for i in range(3)]
    write(ents)
    first = mcp_server.whosaid_live(recordings=d)
    check(first["ok"] and first["cursor"] == f"{jl.stem}:n2" and first["lines"] == 3,
          f"a snapshot carries a cursor: {first.get('cursor')}")
    ents[0]["name"] = "Alice"  # the rewrite: names move, two new lines (one in the same second)
    ents += [dict(ents[2], n=3, text="same second", voice="v2", name="Bob"),
             dict(ents[2], n=4, t=40.0, text="later", voice="v2", name="Bob")]
    write(ents)
    nxt = mcp_server.whosaid_live(since=first["cursor"], recordings=d)
    check([t["text"] for t in nxt["turns"]] == ["same second", "later"] and nxt["lines"] == 2,
          f"since=cursor: only the newer lines: {nxt}")
    check(nxt["cursor"] == f"{jl.stem}:n4" and nxt["reset"] is False and "later" in nxt["transcript"]
          and "point 0" not in nxt["transcript"], f"a new cursor, no overlap: {nxt}")
    again = mcp_server.whosaid_live(since=nxt["cursor"], recordings=d)
    check(again["ok"] and again["turns"] == [] and again["cursor"] == nxt["cursor"], f"nothing new: {again}")
    by_t = mcp_server.whosaid_live(since="20", recordings=d)
    check([t["text"] for t in by_t["turns"]] == ["later"], f"since=a bare t: lines after it: {by_t['turns']}")
    bob = mcp_server.whosaid_live(since=first["cursor"], speaker="^bob$", recordings=d)
    check([t["name"] for t in bob["turns"]] == ["Bob", "Bob"], f"speaker filter with since: {bob['turns']}")
    al = mcp_server.whosaid_live(since=first["cursor"], speaker="alice", recordings=d)
    check(al["turns"] == [] and al["cursor"] == nxt["cursor"], "filtered-out lines still move the cursor")
    bad = mcp_server.whosaid_live(since="what", recordings=d)
    check(bad["ok"] is False and bad["hint"], f"a bad cursor: ok=false with a hint: {bad}")
    check(mcp_server.whosaid_live(speaker="(", recordings=d)["ok"] is False, "a bad speaker regex refused")
    newer = into / live.LIVE_DIR / "20261030T160000.jsonl"
    live.write_session(newer.with_suffix(".md"), newer, now, [dict(ents[0], n=0, text="new call")], False)
    nc = mcp_server.whosaid_live(since=nxt["cursor"], recordings=d)
    check(nc["reset"] is True and [t["text"] for t in nc["turns"]] == ["new call"]
          and nc["cursor"] == "20261030T160000:n0", f"a cursor from the previous call: all of the new one: {nc}")

print(f"PASS: mcp_live_tools_test ({CHECKS} checks)")
