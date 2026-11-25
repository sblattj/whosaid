#!/usr/bin/env python3
"""Gateway tool listing (GitHub issue #222), over the REAL entry point.

Spawns `python lib/mcp_server.py` (what `whosaid mcp` execs) and speaks raw stdio JSON-RPC,
so the wire handlers, profile parsing in main() and the SDK's own call path are what is
tested, not an in-process stand-in. Every server runs against temp dirs: a workspace with
`[summarizer] engine = "none"`, a temp drafts outbox (WHOSAID_DRAFTS_DIR) and a temp
sessions dir, so no write tool ever touches ~/.drafts.

Run: uv run --quiet --with "mcp[cli]>=2,<3" python test/mcp_gateway_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import mcp_server  # noqa: E402

PASSED = 0
FIVE = ["whosaid_search", "whosaid_context", "whosaid_meetings", "whosaid_person", "whosaid_live"]
META = ["whosaid_search_tools", "whosaid_call_tool"]


def check(cond: bool, msg: str) -> None:
    global PASSED
    if not cond:
        raise AssertionError(msg)
    PASSED += 1


def compact(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":")))


class Server:
    """One `python lib/mcp_server.py` child; every read has a deadline so a dead server fails."""

    def __init__(self, env: dict, profile: str | None):
        env = dict(env)
        if profile is None:
            env.pop("WHOSAID_TOOL_PROFILE", None)
        else:
            env["WHOSAID_TOOL_PROFILE"] = profile
        self.p = subprocess.Popen([sys.executable, str(REPO_DIR / "lib" / "mcp_server.py")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, env=env, cwd=env["WHOSAID_WORKSPACE"])
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.n = 0
        self.init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "gateway-test", "version": "0"}})["result"]
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _pump(self):
        for line in self.p.stdout:
            self.q.put(line)
        self.q.put(None)

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def rpc(self, method, params=None, timeout=120):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        while True:
            line = self.q.get(timeout=timeout)
            if line is None:
                raise AssertionError(f"server exited during {method}")
            resp = json.loads(line)
            if resp.get("id") == self.n:
                return resp

    def tools(self):
        return self.rpc("tools/list")["result"]["tools"]

    def call(self, name, arguments=None):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}})

    def via(self, name, arguments=None):
        args = {"name": name}
        if arguments is not None:
            args["arguments"] = arguments
        return self.call("whosaid_call_tool", args)

    def close(self):
        self.p.kill()
        self.p.wait()


def payload(resp) -> dict:
    """The tool's dict result, decoded from its text content."""
    return json.loads(resp["result"]["content"][0]["text"])


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="whosaid-gateway-"))
    try:
        run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"PASS: mcp_gateway_test ({PASSED} assertions)")


def run(tmp: Path) -> None:
    ws, drafts_dir, sessions = tmp / "ws", tmp / "drafts", tmp / "sessions"
    for d in (ws, drafts_dir, sessions, tmp / "rec"):
        d.mkdir()
    (ws / "whosaid.toml").write_text('[summarizer]\nengine = "none"\n')
    env = dict(os.environ, WHOSAID_WORKSPACE=str(ws), WHOSAID_DRAFTS_DIR=str(drafts_dir),
               WHOSAID_SESSIONS_DIR=str(sessions), WHOSAID_TEAMS_CACHE="off")

    # ---- in-process: group map, profile parsing, instructions ----
    registered = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    check(len(registered) == 48, f"46 tools + 2 meta-tools registered: {len(registered)}")
    check(mcp_server._ungrouped_tools() == [], f"group map covers every tool: {mcp_server._ungrouped_tools()}")
    mapped = [t for names in mcp_server.TOOL_GROUPS.values() for t in names]
    check(len(mapped) == len(set(mapped)), "no tool is in two groups")
    check(set(mapped) == registered - set(META), "groups hold exactly the non-meta tools")
    check(set(mcp_server.TOOL_GROUPS) == {"search", "live", "drafts", "teams", "workspace", "voices"},
          f"groups: {list(mcp_server.TOOL_GROUPS)}")
    check(set(FIVE) <= set(mapped), "the gateway five are grouped tools")

    rp = mcp_server.resolve_tool_profile
    check(rp(None) == rp("") == rp(" gateway ") == ("gateway", frozenset(META + FIVE)), "unset = gateway")
    check(rp("full") == ("full", None) and rp("gateway,full") == ("full", None), "full lists everything")
    label, names = rp("gateway,drafts")
    check(label == "gateway,drafts" and names == frozenset(META + FIVE) | set(mcp_server.TOOL_GROUPS["drafts"]),
          f"gateway,drafts: {label}")
    check(rp("drafts") == rp("Drafts , gateway"), "a group list always keeps the gateway five")
    check(rp(",".join(mcp_server.TOOL_GROUPS)) == ("full", None), "every group = full")
    try:
        rp("gateway,bogus")
        check(False, "unknown group must raise")
    except mcp_server.ToolProfileError as exc:
        check("bogus" in str(exc) and "voices" in str(exc), f"unknown group error names it and the known ones: {exc}")

    instr = mcp_server.SERVER_INSTRUCTIONS
    check(len(instr) < 2048 and instr.isascii(), f"instructions under 2,048 chars: {len(instr)}")
    for term in META + FIVE + list(mcp_server.TOOL_GROUPS) + [
            "whosaid_draft_stage", "whosaid_worklist", "whosaid_transcribe", "WHOSAID_TOOL_PROFILE=full",
            "whosaid://guide", "ANY other whosaid_* tool"]:
        check(term in instr, f"instructions mention {term}")
    check(mcp_server._INSTRUCTIONS_DETAIL in mcp_server._GUIDE, "the cut instructions text moved into whosaid://guide")
    check("WHOSAID_TOOL_PROFILE" in mcp_server.guide(), "guide documents WHOSAID_TOOL_PROFILE")

    # ---- unknown group: loud startup failure ----
    bad = subprocess.run([sys.executable, str(REPO_DIR / "lib" / "mcp_server.py")], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=120,
                         env=dict(env, WHOSAID_TOOL_PROFILE="gateway,bogus"))
    check(bad.returncode == 2 and "unknown tool group 'bogus'" in bad.stderr,
          f"unknown group exits 2 with a stderr message: rc={bad.returncode} {bad.stderr[-300:]}")

    # ---- profiles over the wire ----
    measured = {}
    listings = {}
    for profile in (None, "gateway", "gateway,drafts", "full"):
        s = Server(env, profile)
        try:
            tools = s.tools()
            listings[profile] = [t["name"] for t in tools]
            measured[profile or "unset"] = (len(tools), compact(tools))
            caps = s.init["capabilities"].get("tools") or {}
            check(not caps.get("listChanged"), f"{profile}: listChanged false/absent: {caps}")
            check(len(s.init.get("instructions") or "") < 2048, f"{profile}: wire instructions < 2048")
        finally:
            s.close()
    check(listings[None] == META + FIVE, f"unset lists exactly the 7, in order: {listings[None]}")
    check(measured["unset"][1] < 8192, f"default tools/list under 8,192 bytes: {measured['unset']}")
    check(listings["gateway"] == listings[None], "gateway == unset")
    check(listings["full"][:2] == META and set(listings["full"]) == registered and len(listings["full"]) == 48,
          f"full lists all 48, meta first: {len(listings['full'])}")
    check(listings["full"] != listings[None], "control: full differs from the default")
    check(listings["gateway,drafts"][:7] == META + FIVE
          and set(listings["gateway,drafts"][7:]) == set(mcp_server.TOOL_GROUPS["drafts"]),
          f"gateway,drafts: {listings['gateway,drafts']}")
    print("measured (count, compact bytes):", json.dumps(measured))

    # ---- the default server: direct vs whosaid_call_tool ----
    s = Server(env, None)
    try:
        before = s.tools()

        def same(name, arguments, why):
            direct, wrapped = s.call(name, arguments), s.via(name, arguments)
            check("result" in direct, f"{why}: direct call answered with a result: {str(direct)[:200]}")
            check(direct["result"] == wrapped["result"],
                  f"{why}: whosaid_call_tool result equals the direct call\n A={direct}\n B={wrapped}")
            return direct["result"]

        # sync tool, unlisted, success with the workspace_source stamp (workspace group)
        r = same("whosaid_workspace_status", {}, "sync workspace_status")
        check(r.get("isError") is not True and payload({"result": r})["ok"] is True
              and payload({"result": r}).get("workspace_source") == "WHOSAID_WORKSPACE",
              f"unlisted whosaid_workspace_status succeeds directly, stamped: {str(r)[:300]}")
        # async tool (drafts group): its early argument check, never reaches the outbox
        r = same("whosaid_draft_stage", {}, "async draft_stage")
        check("pass id, or all=true" in payload({"result": r})["error"], "draft_stage early error")
        # pydantic argument validation error (search group)
        r = same("whosaid_context", {"meeting": 5}, "validation error")
        check(r.get("isError") is True and "validation error" in r["content"][0]["text"], f"validation: {r}")
        # one per remaining group
        same("whosaid_live", {"recordings": str(tmp / "rec")}, "live group")
        same("whosaid_teams_live", {"action": "bogus"}, "teams group (validation, never reads the cache)")
        same("whosaid_relabel", {}, "voices group (missing required args)")
        same("whosaid_drafts", {}, "drafts read (temp outbox)")
        same("whosaid_meetings", {}, "search group, listed tool")
        r = same("whosaid_no_such_tool", {}, "unknown tool")
        check(r.get("isError") is True and "Unknown tool" in r["content"][0]["text"], f"unknown tool: {r}")

        # a write tool through the gateway: lands in the temp outbox, same shape as a direct call.
        # Results differ by design (id, path, recipient), so this compares the key set + location.
        w1 = payload(s.via("whosaid_draft_new", {"to": "Gateway Probe", "topic": "via call_tool"}))
        w2 = payload(s.call("whosaid_draft_new", {"to": "Direct Probe", "topic": "direct"}))
        check(w1["ok"] is True and w2["ok"] is True and set(w1) == set(w2), f"draft_new shapes: {w1} {w2}")
        check(all(Path(w["path"]).resolve().is_relative_to(drafts_dir.resolve()) for w in (w1, w2)),
              "draft_new wrote under the temp outbox, never ~/.drafts")

        # meta-tools refuse to be wrapped; malformed wrapper args get the SDK's validation error
        for meta in META:
            r = s.via(meta, {})["result"]
            check(r.get("isError") is True and "is a meta-tool" in r["content"][0]["text"], f"refuses {meta}: {r}")
        r = s.call("whosaid_call_tool", {})["result"]
        check(r.get("isError") is True and "name" in r["content"][0]["text"], f"call_tool without name: {r}")

        # search_tools: catalog, exact name, ranked
        cat = payload(s.call("whosaid_search_tools"))
        groups = [g["group"] for g in cat["groups"]]
        check(groups == ["meta", "search", "live", "drafts", "teams", "workspace", "voices"], f"catalog groups: {groups}")
        seen = [t["name"] for g in cat["groups"] for t in g["tools"]]
        check(sorted(seen) == sorted(registered) and len(seen) == len(set(seen)), "catalog lists every tool once")
        check(all("description" not in t and "\n" not in t["summary"] and len(t["summary"]) <= 140
                  for g in cat["groups"] for t in g["tools"]), "catalog carries one-line summaries only")
        summaries = {t["name"]: t["summary"] for g in cat["groups"] for t in g["tools"]}
        check(all(not x.endswith((":", " e.g.", " i.e.")) and len(x) >= 20 for x in summaries.values()),
              f"summaries are whole sentences: {[x for x in summaries.values() if x.endswith(':') or len(x) < 20]}")
        check("whosaid_draft_stage" in summaries["whosaid_call_tool"], f"call_tool summary: {summaries['whosaid_call_tool']}")
        check(compact(cat) < 16384, f"catalog stays small: {compact(cat)} bytes")
        ex = payload(s.call("whosaid_search_tools", {"query": "whosaid_draft_stage"}))
        m = ex["matches"]
        check(ex["total"] == 1 and m[0]["name"] == "whosaid_draft_stage" and m[0]["listed"] is False
              and m[0]["group"] == "drafts" and "properties" in m[0]["inputSchema"]
              and m[0]["annotations"]["readOnlyHint"] is False, f"exact name: {str(ex)[:300]}")
        check(payload(s.call("whosaid_search_tools", {"query": "draft_stage"}))["matches"][0]["name"]
              == "whosaid_draft_stage", "exact name without the whosaid_ prefix")
        rk = payload(s.call("whosaid_search_tools", {"query": "calendar", "limit": 50}))
        check(rk["matches"][0]["name"] == "whosaid_teams_calendar" and len(rk["matches"]) <= 20
              and {"name", "group", "listed", "description", "inputSchema"} <= set(rk["matches"][0]),
              f"ranked: {[x['name'] for x in rk['matches']]}")
        check(len(payload(s.call("whosaid_search_tools", {"query": "draft"}))["matches"]) == 8, "limit default 8")
        check(payload(s.call("whosaid_search_tools", {"query": "zzqqxx"}))["total"] == 0, "no match is empty")

        ann = {t["name"]: t.get("annotations") or {} for t in before}
        check(ann["whosaid_search_tools"].get("readOnlyHint") is True, "search_tools is read-only")
        check(ann["whosaid_call_tool"].get("readOnlyHint") is False, "call_tool is NOT read-only")

        after = s.tools()
        check(after == before, "tools/list identical before and after whosaid_call_tool calls")
    finally:
        s.close()


if __name__ == "__main__":
    main()
