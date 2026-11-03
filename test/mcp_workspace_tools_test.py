#!/usr/bin/env python3
"""
Offline unit tests for the meeting-workspace tools and resources on the whosaid
MCP server (GitHub issue #14).

Nothing real is executed: the private runner `mcp_server._run_ws` is
monkeypatched to record argv and return canned JSON, so every tool's argument
construction, validation, and error-dict path is asserted in well under a
second. The runner itself is exercised separately against tiny fake
`search.py`/`graph.py` scripts in a temp dir (exit 0/1/2, bad JSON, timeout,
missing script). Resources are read through the module functions with
WHOSAID_WORKSPACE pointed at a temp workspace. whosaid_relabel's flag
construction (GitHub issue #19) is covered the same way: `_run_cli` is
monkeypatched to record argv, so no real relabel ever runs.

If the `mcp` SDK is not importable, a minimal stub of the names mcp_server uses
(MCPServer.tool/.resource decorators and ToolAnnotations) is installed first, so
this file runs with plain python3 and needs no `uv`.

Run:
    python3 test/mcp_workspace_tools_test.py
    uv run --with "mcp[cli]>=2,<3" python test/mcp_workspace_tools_test.py   # real SDK
"""

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))


def _install_sdk_stub() -> None:
    """Register just enough of `mcp` for lib/mcp_server.py to import."""

    class ToolAnnotations:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class MCPServer:
        def __init__(self, name=None, instructions=None, **kw):
            self.name = name
            self.instructions = instructions
            self.tools = {}
            self.resources = {}

        def tool(self, name=None, description=None, annotations=None, **kw):
            def deco(fn):
                self.tools[name or fn.__name__] = (fn, description, annotations)
                return fn
            return deco

        def resource(self, uri, **kw):
            def deco(fn):
                self.resources[uri] = fn
                return fn
            return deco

        def run(self):  # pragma: no cover
            raise RuntimeError("stub server cannot run")

    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    mcpserver_mod = types.ModuleType("mcp.server.mcpserver")
    mcpserver_mod.MCPServer = MCPServer
    types_mod = types.ModuleType("mcp.types")
    types_mod.ToolAnnotations = ToolAnnotations
    mcp_mod.server = server_mod
    mcp_mod.types = types_mod
    server_mod.mcpserver = mcpserver_mod
    sys.modules.update({
        "mcp": mcp_mod,
        "mcp.server": server_mod,
        "mcp.server.mcpserver": mcpserver_mod,
        "mcp.types": types_mod,
    })


try:
    import mcp.types  # noqa: F401
    try:
        from mcp.server.mcpserver import MCPServer  # noqa: F401
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # noqa: F401
    SDK = "real"
except ImportError:
    _install_sdk_stub()
    SDK = "stub"

import mcp_server  # noqa: E402

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def clear_env() -> None:
    for var in ("WHOSAID_WORKSPACE", "WHOSAID_OWNER", "WHOSAID_OLLAMA", "WHOSAID_SUMMARIZER_MODEL"):
        os.environ.pop(var, None)


class FakeRunner:
    """Stands in for mcp_server._run_ws: records calls, returns queued results."""

    def __init__(self):
        self.calls = []
        self.queue = []

    def __call__(self, script, args, ws):
        self.calls.append((Path(script).name, [str(a) for a in args], Path(ws)))
        if self.queue:
            return self.queue.pop(0)
        return [], None

    def last(self):
        return self.calls[-1]


def make_workspace(root: Path) -> Path:
    """A tiny workspace: config, one meeting with two transcripts, one rendered file."""
    ws = root / "ws"
    ws.mkdir(parents=True)
    (ws / "whosaid.toml").write_text(
        '[workspace]\nowner = "Alice_Example"\n\n'
        '[groups]\nleadership = ["Bob_Example"]\nteam = ["Carol_Example", "Dan_Example"]\n'
    )
    meeting = ws / "2026-01-05-0900"
    meeting.mkdir()
    (meeting / "a.speakers.txt").write_text(
        "[00:00:01] Alice_Example: Let us start with the rollout.\n"
        "[00:00:09] Bob_Example: I will own the rollout checklist.\n"
    )
    (meeting / "b.speakers.txt").write_text("[00:00:02] Carol_Example: Second recording.\n")
    (meeting / "action-items.md").write_text("# Action items\n- **AI-001** [open] rollout checklist\n")
    (ws / "_WIKI.md").write_text("# Wiki\n\nHello.\n")
    return ws


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------
def test_workspace_resolution(tmp: Path, fake: FakeRunner) -> None:
    ws = make_workspace(tmp / "res")
    clear_env()

    out = mcp_server.whosaid_search("rollout")
    check(out["ok"] is False and "workspace" in out["error"], f"no workspace must error: {out}")
    check("WHOSAID_WORKSPACE" in out["hint"] and "workspace=" in out["hint"], f"hint must name both options: {out}")
    check(fake.calls == [], "no runner call without a workspace")

    out = mcp_server.whosaid_search("rollout", workspace=str(tmp / "nope"))
    check(out["ok"] is False and "not a directory" in out["error"], f"bad dir must error: {out}")

    os.environ["WHOSAID_WORKSPACE"] = str(ws)
    out = mcp_server.whosaid_search("rollout")
    check(out["ok"] is True, f"env workspace must resolve: {out}")
    check(fake.last()[2] == ws.resolve(), f"env workspace must be passed to the runner: {fake.last()}")

    other = make_workspace(tmp / "other")
    out = mcp_server.whosaid_search("rollout", workspace=str(other))
    check(fake.last()[2] == other.resolve(), "explicit workspace argument must beat WHOSAID_WORKSPACE")

    # Never cwd: run from inside a workspace with no env and no argument.
    clear_env()
    cwd = os.getcwd()
    os.chdir(ws)
    try:
        out = mcp_server.whosaid_meetings()
    finally:
        os.chdir(cwd)
    check(out["ok"] is False, "cwd must never be used as the workspace")


# ---------------------------------------------------------------------------
# Tools: argv construction, validation, pass-through
# ---------------------------------------------------------------------------
def test_search(ws: Path, fake: FakeRunner) -> None:
    w = str(ws.resolve())
    fake.queue.append(([{"meeting": "2026-01-05-0900", "t_sec": 9, "t_str": "00:00:09",
                         "speaker": "Bob_Example", "text": "I will own the rollout checklist.",
                         "score": 1.5, "source": "exact"}], None))
    out = mcp_server.whosaid_search("rollout checklist")
    script, args, _ = fake.last()
    check(script == "search.py", f"search must shell search.py: {script}")
    check(args == ["query", w, "rollout checklist", "--mode", "hybrid", "-k", "10"], f"default argv: {args}")
    check(out["count"] == 1 and out["hits"][0]["speaker"] == "Bob_Example", f"hits pass through: {out}")
    check(out["mode"] == "hybrid" and "engine" not in out, f"engine only when reported: {out}")
    check("whosaid_context" in out["next_step"], "next_step must point at whosaid_context")

    fake.queue.append(({"engine": "fts5", "hits": []}, None))
    out = mcp_server.whosaid_search(" rollout ", mode="exact", speaker="Bob_Example",
                                    meeting="2026-01-05-0900", k=3)
    _, args, _ = fake.last()
    check(args == ["query", w, "rollout", "--mode", "exact", "-k", "3",
                   "--speaker", "Bob_Example", "--meeting", "2026-01-05-0900"], f"filter argv: {args}")
    check(out["engine"] == "fts5" and out["count"] == 0 and out["hits"] == [], f"dict payload: {out}")
    check("whosaid_workspace_status" in out["next_step"], "empty result must suggest the status tool")

    before = len(fake.calls)
    for bad_kwargs in (
        {"query": "   "},
        {"query": "x", "mode": "fuzzy"},
        {"query": "x", "k": 0},
        {"query": "x", "k": 101},
        {"query": "x", "k": "ten"},
        {"query": "x", "meeting": "../etc"},
        {"query": "x", "meeting": "a/b"},
    ):
        out = mcp_server.whosaid_search(**bad_kwargs)
        check(out["ok"] is False and "error" in out and "hint" in out, f"must reject {bad_kwargs}: {out}")
    check(len(fake.calls) == before, "invalid input must not reach the runner")

    fake.queue.append(([], None))
    out = mcp_server.whosaid_search("x", k="5")
    check(fake.last()[1][-1] == "5" and out["ok"], "numeric-string k is coerced")

    err = {"ok": False, "error": "whosaid: no index in ws", "hint": f"run: whosaid index {w}"}
    fake.queue.append((None, err))
    out = mcp_server.whosaid_search("rollout")
    check(out == err, f"runner error dict must pass through untouched: {out}")


def test_context(ws: Path, fake: FakeRunner) -> None:
    w = str(ws.resolve())
    turns = [{"t_sec": 1, "t_str": "00:00:01", "speaker": "Alice_Example", "text": "Let us start."}]
    fake.queue.append((turns, None))
    out = mcp_server.whosaid_context("2026-01-05-0900", "00:00:09")
    script, args, _ = fake.last()
    check(script == "search.py", "context must shell search.py")
    check(args == ["context", w, "2026-01-05-0900", "00:00:09", "--before", "60", "--after", "120"], f"argv: {args}")
    check(out["turns"] == turns and out["count"] == 1 and out["meeting"] == "2026-01-05-0900", f"shape: {out}")
    check(out["at"] == "00:00:09" and out["before"] == 60 and out["after"] == 120, f"echo: {out}")

    fake.queue.append(([], None))
    mcp_server.whosaid_context("2026-01-05-0900", "12:34", before=0, after=30)
    _, args, _ = fake.last()
    check(args[3] == "12:34" and args[5] == "0" and args[7] == "30", f"MM:SS and window: {args}")

    before = len(fake.calls)
    for meeting, at, kw in (
        ("../x", "00:00:09", {}),
        ("a/b", "00:00:09", {}),
        ("", "00:00:09", {}),
        (".hidden", "00:00:09", {}),
        ("ok", "9", {}),
        ("ok", "1:2", {}),
        ("ok", "abc", {}),
        ("ok", "00:00:09", {"before": -1}),
        ("ok", "00:00:09", {"after": 99999}),
        ("ok", "00:00:09", {"after": "soon"}),
    ):
        out = mcp_server.whosaid_context(meeting, at, **kw)
        check(out["ok"] is False and "hint" in out, f"must reject ({meeting!r}, {at!r}, {kw}): {out}")
    check(len(fake.calls) == before, "invalid context input must not reach the runner")


def test_graph_tools(ws: Path, fake: FakeRunner) -> None:
    w = str(ws.resolve())

    fake.queue.append(([{"id": "AI-001"}], None))
    out = mcp_server.whosaid_items()
    script, args, _ = fake.last()
    check(script == "graph.py" and args == ["items", w], f"items argv: {script} {args}")
    check(out["items"] == [{"id": "AI-001", "stale": False}] and out["count"] == 1, f"items shape: {out}")

    fake.queue.append(([], None))
    mcp_server.whosaid_items(owner="Bob_Example", requester="Alice_Example", status="open", type="task")
    _, args, _ = fake.last()
    check(args == ["items", w, "--owner", "Bob_Example", "--requester", "Alice_Example",
                   "--status", "open", "--type", "task"], f"items filters: {args}")

    fake.queue.append(([], None))
    mcp_server.whosaid_items(owner="  ", status="")
    check(fake.last()[1] == ["items", w], "blank filters are dropped")

    # stale: open, untouched, newest meeting older than 14 days before as_of (folder dates)
    rows = [{"id": "AI-001", "status": "open", "status_source": "corpus", "last_seen": "2026-01-01-0900"},
            {"id": "AI-002", "status": "open", "status_source": "corpus", "last_seen": "2026-03-10-0900"},
            {"id": "AI-003", "status": "delivered", "status_source": "corpus", "last_seen": "2026-01-02-0900"},
            {"id": "AI-004", "status": "open", "status_source": "tracking", "last_seen": "2026-01-03-0900",
             "tracking_history": [{"status": "open"}]},
            {"id": "AI-005", "status": "open", "status_source": "corpus", "last_seen": "Hand named"}]
    fake.queue.append(([dict(r) for r in rows], None))
    out = mcp_server.whosaid_items(as_of="2026-03-12")
    flags = {i["id"]: i["stale"] for i in out["items"]}
    check(flags == {"AI-001": True, "AI-002": False, "AI-003": False, "AI-004": False, "AI-005": False}
          and out["stale_count"] == 1 and out["stale_after_days"] == 14, f"stale flags: {flags}, {out.get('stale_count')}")
    fake.queue.append(([dict(r) for r in rows], None))
    out = mcp_server.whosaid_items(as_of="2026-03-12", stale=False, sort="newest", compact=True)
    check([i["id"] for i in out["items"]] == ["AI-005", "AI-002", "AI-004", "AI-003"]
          and out["stale_count"] == 1 and out["total"] == 4 and "stale" in out["items"][0],
          f"stale=false drops stale items, newest first, compact keeps the flag: {out['items']}")
    fake.queue.append(([dict(r) for r in rows], None))
    check([i["id"] for i in mcp_server.whosaid_items(as_of="2026-03-12", stale=True)["items"]] == ["AI-001"],
          "stale=true keeps only stale items")
    (ws / "whosaid.toml").write_text((ws / "whosaid.toml").read_text() + "\n[tracking]\nstale_after_days = 0\n"
                                     if (ws / "whosaid.toml").exists() else "[tracking]\nstale_after_days = 0\n")
    fake.queue.append(([dict(r) for r in rows], None))
    check(mcp_server.whosaid_items(as_of="2026-03-12")["stale_count"] == 0, "stale_after_days = 0 turns stale off")

    fake.queue.append(({"id": "AI-001", "text": "rollout checklist"}, None))
    out = mcp_server.whosaid_item(" AI-001 ")
    script, args, _ = fake.last()
    check(script == "graph.py" and args == ["item", w, "AI-001"], f"item argv: {args}")
    check(out["id"] == "AI-001" and out["item"]["text"] == "rollout checklist", f"item shape: {out}")
    before = len(fake.calls)
    for bad in ("", "AI 001", "../AI-001", "-x", "a/b"):
        out = mcp_server.whosaid_item(bad)
        check(out["ok"] is False, f"item must reject {bad!r}: {out}")
    check(len(fake.calls) == before, "bad ids must not reach the runner")

    fake.queue.append(({"name": "Bob_Example", "owns": [{"id": "AI-001"}]}, None))
    out = mcp_server.whosaid_person("Bob_Example")
    script, args, _ = fake.last()
    check(script == "graph.py" and args == ["person", w, "Bob_Example"], f"person argv: {args}")
    check(out["name"] == "Bob_Example" and out["person"]["owns"][0]["id"] == "AI-001", f"person shape: {out}")

    fake.queue.append(([{"name": "Alice_Example"}, {"name": "Bob_Example"}], None))
    out = mcp_server.whosaid_person()
    check(fake.last()[1] == ["person", w], f"person list argv: {fake.last()[1]}")
    check(out["count"] == 2 and "person" not in out and len(out["people"]) == 2, f"people shape: {out}")

    for fn, sub, script, key in (
        (mcp_server.whosaid_meetings, "meetings", "graph.py", "meetings"),
        (mcp_server.whosaid_prs, "prs", "graph.py", "prs"),
        (mcp_server.whosaid_speakers, "speakers", "search.py", "speakers"),
    ):
        fake.queue.append(([{"x": 1}, {"x": 2}], None))
        out = fn()
        s, args, _ = fake.last()
        check(s == script and args == [sub, w], f"{sub} argv: {s} {args}")
        check(out[key] == [{"x": 1}, {"x": 2}] and out["count"] == 2, f"{sub} shape: {out}")

        fake.queue.append((None, {"ok": False, "error": "boom", "hint": "run: whosaid index x"}))
        out = fn()
        check(out["ok"] is False and out["error"] == "boom", f"{sub} error pass-through: {out}")


def test_workspace_status(ws: Path, fake: FakeRunner) -> None:
    fake.queue.append(({"meetings": 1, "turns": 3, "embeddings": 0}, None))
    out = mcp_server.whosaid_workspace_status()
    script, args, _ = fake.last()
    check(script == "search.py" and args == ["status", str(ws.resolve())], f"status argv: {args}")
    check(out["ok"] is True and out["search"] == {"meetings": 1, "turns": 3, "embeddings": 0}, f"status: {out}")
    check(out["owner"] == "Alice_Example", f"owner from whosaid.toml: {out}")
    check(out["groups"] == ["leadership", "team"], f"group names only, in file order: {out}")
    check("Bob_Example" not in json.dumps(out), "group members must not be reported")
    check(out["files"] == {"_WIKI.md": True, "_ACTION-ITEMS.md": False, "_INDEX.md": False}, f"files: {out['files']}")
    check(out["index_present"] is False and out["index_path"].endswith("_search.db"), f"index: {out}")
    check(out["meeting_folders"] == 1 and out["config_present"] is True, f"counts: {out}")
    check("MISSING" in out["summary"] and "Alice_Example" in out["summary"], f"summary: {out['summary']}")

    (ws / "_search.db").write_bytes(b"")
    fake.queue.append((None, {"ok": False, "error": "no index", "hint": "run: whosaid index x"}))
    out = mcp_server.whosaid_workspace_status()
    check(out["ok"] is True and out["search"] is None, "status stays ok when search.py fails")
    check(out["search_error"] == "no index" and out["hint"].startswith("run: whosaid index"), f"error surfaced: {out}")
    check(out["index_present"] is True, "index file presence is a plain file check")
    (ws / "_search.db").unlink()

    bare = ws.parent / "bare"
    bare.mkdir()
    fake.queue.append(({}, None))
    out = mcp_server.whosaid_workspace_status(workspace=str(bare))
    check(out["owner"] is None and out["groups"] == [] and out["config_present"] is False, f"no config: {out}")
    check(out["meeting_folders"] == 0 and "no owner" in out["summary"], f"bare summary: {out['summary']}")


def test_worklist(ws: Path, fake: FakeRunner) -> None:
    w = str(ws.resolve())
    # The tool checks the corpora exist before shelling out (GitHub issue #113).
    (ws / "_commitments.json").write_text('{"items": []}\n')
    payload = {"owner": "Alice_Example", "generated_from": ["2026-01-05-0900"],
               "items": [{"id": "CM-001", "source": "commitments", "tier": "P1", "score": 5,
                          "why": ["boss"], "text": "send the deck", "status": "open"}]}
    fake.queue.append((payload, None))
    out = mcp_server.whosaid_worklist()
    script, args, _ = fake.last()
    check(script == "workspace.py" and args == ["worklist", w, "--owner", "me"], f"worklist argv: {script} {args}")
    check(out["ok"] is True and out["owner"] == "Alice_Example", f"worklist owner: {out}")
    check(out["items"] == payload["items"] and out["count"] == 1, f"worklist items: {out}")
    check(out["generated_from"] == ["2026-01-05-0900"], f"worklist generated_from: {out}")

    fake.queue.append(({"owner": "Bob_Example", "generated_from": [], "items": []}, None))
    out = mcp_server.whosaid_worklist(owner=" Bob_Example ")
    check(fake.last()[1] == ["worklist", w, "--owner", "Bob_Example"], f"worklist owner argv: {fake.last()[1]}")
    check(out["count"] == 0 and out["items"] == [], f"empty worklist: {out}")

    fake.queue.append(({"owner": "Alice_Example", "generated_from": [], "items": []}, None))
    mcp_server.whosaid_worklist(owner="   ")
    check(fake.last()[1][-1] == "me", "blank owner falls back to me")

    fake.queue.append((None, {"ok": False, "error": "worklist: no owner", "hint": "run: whosaid index x"}))
    out = mcp_server.whosaid_worklist()
    check(out["ok"] is False and out["error"] == "worklist: no owner", f"worklist error passthrough: {out}")
    check("owner=" in out["hint"] and "whosaid index" not in out["hint"], f"worklist hint names owner=, not the index: {out['hint']}")
    check("builds the corpora" not in out["hint"], f"corpora exist: no rebuild advice (#113): {out['hint']}")

    before = len(fake.calls)
    out = mcp_server.whosaid_worklist(workspace=str(ws.parent / "nope"))
    check(out["ok"] is False and len(fake.calls) == before, "bad workspace never reaches the runner")


def test_list_paging(ws: Path, fake: FakeRunner) -> None:
    """meetings/prs/speakers: newest-first order, filters, limit/offset/compact and the
    50-row default cap (GitHub issue #90)."""
    w = str(ws.resolve())
    rows = [{"folder": f"2026-01-{d:02d}-0900", "source_name": f"call {d}.m4a",
             "created": f"2026-01-{d:02d}T09:00:00Z", "duration_s": 60.0, "minutes": 1,
             "dated": True, "has_action_items": False, "segments": 3, "kind": "audio"}
            for d in (3, 1, 5, 2, 4)]
    rows.append({"folder": "Standup notes", "source_name": "notes.txt", "created": None,
                 "duration_s": None, "minutes": None, "dated": False, "has_action_items": False,
                 "segments": 1, "kind": "audio"})

    fake.queue.append((rows, None))
    out = mcp_server.whosaid_meetings(kind="audio")
    check(fake.last()[1] == ["meetings", w, "--kind", "audio"], f"kind still goes to the CLI: {fake.last()[1]}")
    check([m["folder"] for m in out["meetings"]] == [
        "2026-01-05-0900", "2026-01-04-0900", "2026-01-03-0900", "2026-01-02-0900", "2026-01-01-0900",
        "Standup notes"], f"newest first, undated last: {out['meetings']}")
    check(out["total"] == 6 and out["count"] == 6 and out["limit"] == 50 and "omitted" not in out, f"fits: {out}")

    fake.queue.append((rows, None))
    out = mcp_server.whosaid_meetings(since="2026-01-02", until="2026-01-04")
    check([m["folder"] for m in out["meetings"]] == ["2026-01-04-0900", "2026-01-03-0900", "2026-01-02-0900"],
          f"since/until inclusive, undated dropped: {out['meetings']}")

    fake.queue.append((rows, None))
    out = mcp_server.whosaid_meetings(query="NOTES")
    check([m["folder"] for m in out["meetings"]] == ["Standup notes"], f"query matches source_name: {out}")

    fake.queue.append((rows, None))
    out = mcp_server.whosaid_meetings(limit=2, offset=1, compact=True)
    check([m["folder"] for m in out["meetings"]] == ["2026-01-04-0900", "2026-01-03-0900"], f"page: {out}")
    check(out["omitted"] == 3 and "offset=3" in out["hint"] and out["offset"] == 1, f"omitted/hint: {out}")
    check(out["next_offset"] == 3, f"next_offset on a partial page: {out}")
    check(out["compact"] is True and set(out["meetings"][0]) == {"folder", "kind", "created", "minutes"},
          f"compact keys: {out['meetings'][0]}")

    for bad in ({"since": "Jan 2"}, {"until": "2026-1-2"}, {"limit": 0}, {"offset": -1}, {"limit": "x"}):
        n = len(fake.calls)
        out = mcp_server.whosaid_meetings(**bad)
        check(out["ok"] is False and len(fake.calls) == n, f"{bad} must error before shelling: {out}")

    # the default cap: 120 rows -> newest 50 plus omitted/hint; prs and speakers page the same way
    many = [{"folder": f"2026-03-{i // 4 + 1:02d}-{i:04d}", "created": None, "kind": "teams-chat"} for i in range(120)]
    fake.queue.append((many, None))
    out = mcp_server.whosaid_meetings()
    check(out["count"] == 50 and out["total"] == 120 and out["omitted"] == 70, f"default cap: {out['count']}")
    check(out["meetings"][0]["folder"] == "2026-03-30-0119", f"dated folder names sort too: {out['meetings'][0]}")
    for fn, key in ((mcp_server.whosaid_prs, "prs"), (mcp_server.whosaid_speakers, "speakers")):
        fake.queue.append(([{"x": i} for i in range(120)], None))
        out = fn()
        check(out["count"] == 50 and out["omitted"] == 70 and out[key][0] == {"x": 0}, f"{key} default cap: {out['count']}")
        fake.queue.append(([{"x": i} for i in range(120)], None))
        out = fn(limit=10, offset=110)
        check([r["x"] for r in out[key]] == list(range(110, 120)) and "omitted" not in out, f"{key} last page")
        n = len(fake.calls)
        check(fn(limit=0)["ok"] is False and len(fake.calls) == n, f"{key} limit=0 errors")


def test_items_paging(ws: Path, fake: FakeRunner) -> None:
    """whosaid_items: bounded default page, offset paging, compact, stable order (GitHub issue #112)."""
    w = str(ws.resolve())
    db = mcp_server.wsconfig.search_db(ws)
    # 250 generated items (AI-001..AI-250) plus two past 999, shuffled so order must come from the tool
    ids = [f"AI-{i:03d}" for i in range(1, 251)] + ["AI-1000", "AI-1001"]
    expect = sorted(ids, key=lambda s: int(s.split("-")[1]))
    check(expect[-2:] == ["AI-1000", "AI-1001"], "fixture: numeric order puts AI-1000 after AI-250")
    rows = [{"id": i, "text": f"do thing {i}", "owner": "Bob_Example", "status": "open", "type": "task",
             "first_seen": "2026-01-05", "last_seen": "2026-01-06", "span": 1, "merged_into": None,
             "occurrences": 1 + n % 3, "requesters": ["Alice_Example"]}
            for n, i in enumerate(ids)]
    shuffled = rows[::-1]

    if db.exists():
        db.unlink()
    fake.queue.append((list(shuffled), None))
    out = mcp_server.whosaid_items()
    check(out["ok"] is True and out["index_mtime"] is None, f"index_mtime null without an index: {out.get('index_mtime')}")
    check(out["count"] == 50 and len(out["items"]) == 50 and out["total"] == 252 and out["omitted"] == 202,
          f"default call is bounded: count={out['count']} total={out['total']} omitted={out.get('omitted')}")
    check(out["limit"] == 50 and out["next_offset"] == 50, f"next-page indicator: {out}")
    check([r["id"] for r in out["items"]] == expect[:50], "default page is the first 50 in numeric id order")
    check(set(out["items"][0]) >= {"requesters", "first_seen", "span"}, "non-compact keeps full item dicts")
    check("compact" not in out, "compact flag absent by default")

    db.write_bytes(b"")
    m = __import__("datetime").datetime.fromtimestamp(db.stat().st_mtime, __import__("datetime").timezone.utc).isoformat()
    fake.queue.append((list(shuffled), None))
    out = mcp_server.whosaid_items()
    check(out["index_mtime"] is not None and out["index_mtime"].replace("Z", "+00:00")[:19] == m[:19],
          f"index_mtime is the db mtime in UTC: {out['index_mtime']} vs {m}")
    db.unlink()

    # walk every page with next_offset: each id exactly once, in order
    seen, off, pages = [], 0, 0
    while off is not None:
        fake.queue.append((list(shuffled), None))
        out = mcp_server.whosaid_items(limit=40, offset=off)
        seen += [r["id"] for r in out["items"]]
        off = out.get("next_offset")
        pages += 1
        check(pages < 20, "paging must terminate")
        check(off is None or off == len(seen), f"next_offset follows the rows returned: {off} vs {len(seen)}")
    check(seen == expect, f"paging retrieves all 252 without duplicates or omissions ({len(seen)} rows)")
    check(pages == 7 and "next_offset" not in out and "omitted" not in out, f"last page: pages={pages} {out.get('omitted')}")

    # ties on the numeric suffix fall back to the id string, so the order is total
    fake.queue.append(([{"id": "AI-5b"}, {"id": "AI-5a"}, {"id": "AI-4"}], None))
    out = mcp_server.whosaid_items()
    check([r["id"] for r in out["items"]] == ["AI-4", "AI-5a", "AI-5b"], f"tie-breaker: {out['items']}")

    # Compact keeps extraction type, obligation kind and completion evidence distinct.
    fake.queue.append((list(shuffled), None))
    out = mcp_server.whosaid_items(compact=True, limit=3)
    check(out["compact"] is True and all(set(r) == set(mcp_server._ITEMS_COMPACT_KEYS)
                                         for r in out["items"]), f"compact keys: {out['items'][0]}")
    check(out["items"][0]["id"] == "AI-001" and out["items"][0]["occurrences"] == rows[0]["occurrences"],
          f"compact row values: {out['items'][0]}")

    fake.queue.append((list(shuffled), None))
    out = mcp_server.whosaid_items(offset=50)
    check(out["count"] == 50 and out["limit"] == 50 and out["next_offset"] == 100,
          f"an offset-only page remains bounded: {out['count']}")
    check([r["id"] for r in out["items"]] == expect[50:100], "offset-only page follows the first page")

    # filters go to the CLI before paging; paging args never do
    fake.queue.append((list(shuffled), None))
    mcp_server.whosaid_items(owner="Bob_Example", status="open", type="task", limit=5, offset=10, compact=True)
    check(fake.last()[1] == ["items", w, "--owner", "Bob_Example", "--status", "open", "--type", "task"],
          f"filters pass through, paging stays in Python: {fake.last()[1]}")

    # offset past the end: empty page, no next_offset
    fake.queue.append((list(shuffled), None))
    out = mcp_server.whosaid_items(offset=1000)
    check(out["items"] == [] and out["count"] == 0 and out["total"] == 252 and "next_offset" not in out,
          f"offset past the end: {out}")

    for bad in ({"limit": 0}, {"limit": "x"}, {"offset": -1}, {"offset": "y"}):
        n = len(fake.calls)
        out = mcp_server.whosaid_items(**bad)
        check(out["ok"] is False and len(fake.calls) == n, f"{bad} must error before shelling: {out}")


def test_worklist_paging(ws: Path, fake: FakeRunner) -> None:
    """tier/limit/offset/compact + the 50-item default cap (GitHub issue #30)."""
    w = str(ws.resolve())
    items = [
        {"id": f"CM-{i:03d}", "source": "commitments", "text": f"do thing {i}", "status": "open",
         "tier": "P1" if i < 2 else ("P2" if i < 4 else "P3"), "score": 10 - i, "why": ["boss"],
         "first_seen": "2026-01-05", "last_seen": "2026-01-05", "occurrences": 1,
         "requested_by": None, "negative": False, "also": None}
        for i in range(6)
    ]
    payload = {"owner": "Alice_Example", "generated_from": ["2026-01-05-0900"], "items": items}

    # tier filter: normalized case-insensitively, applied in the tool (CLI argv unchanged)
    fake.queue.append((payload, None))
    out = mcp_server.whosaid_worklist(tier="p1")
    check(fake.last()[1] == ["worklist", w, "--owner", "me"], f"tier filters in the tool, not the CLI: {fake.last()[1]}")
    check(out["ok"] is True and out["total"] == 2 and out["count"] == 2 and out["tier"] == "P1",
          f"tier filter: {out}")
    check([it["id"] for it in out["items"]] == ["CM-000", "CM-001"], f"tier keeps shell order: {out['items']}")
    check("omitted" not in out and "hint" not in out, f"everything fit, no omitted/hint: {out}")

    # limit + offset walk the ranked list in order, one page at a time
    fake.queue.append((payload, None))
    out = mcp_server.whosaid_worklist(limit=2)
    check([it["id"] for it in out["items"]] == ["CM-000", "CM-001"] and out["total"] == 6 and out["limit"] == 2,
          f"first page: {out}")
    check(out["omitted"] == 4 and "offset=2" in out["hint"], f"omitted + hint: {out}")
    fake.queue.append((payload, None))
    out = mcp_server.whosaid_worklist(limit=2, offset=2)
    check([it["id"] for it in out["items"]] == ["CM-002", "CM-003"] and out["offset"] == 2 and out["omitted"] == 2,
          f"second page: {out}")
    fake.queue.append((payload, None))
    out = mcp_server.whosaid_worklist(offset=4)
    check([it["id"] for it in out["items"]] == ["CM-004", "CM-005"] and "omitted" not in out and out["limit"] == 50,
          f"offset alone uses the default cap: {out}")
    fake.queue.append((payload, None))
    out = mcp_server.whosaid_worklist(offset=6)
    check(out["items"] == [] and out["count"] == 0 and out["total"] == 6 and "omitted" not in out,
          f"offset past the end: {out}")

    # compact drops the verbose keys
    fake.queue.append((payload, None))
    out = mcp_server.whosaid_worklist(limit=1, compact=True)
    check(out["compact"] is True and set(out["items"][0]) == set(mcp_server._WORKLIST_COMPACT_KEYS),
          f"compact item keys: {out['items'][0]}")
    check("first_seen" not in json.dumps(out["items"]), f"compact drops the verbose fields: {out['items']}")

    # default cap: a 60-item worklist returns the top 50 with omitted/hint (GitHub issue #30)
    big = {"owner": "Alice_Example", "generated_from": [],
           "items": [dict(items[4], id=f"CM-{i:03d}") for i in range(60)]}
    fake.queue.append((big, None))
    out = mcp_server.whosaid_worklist()
    check(out["count"] == 50 and out["omitted"] == 10 and out["total"] == 60 and out["limit"] == 50,
          f"default cap: count={out['count']} total={out['total']} omitted={out.get('omitted')}")
    check("offset=50" in out["hint"], f"hint must point at the next page: {out.get('hint')}")
    check(out["items"][0]["id"] == "CM-000" and out["items"][-1]["id"] == "CM-049",
          "the cap keeps the top of the ranking")

    big["items"] *= 3
    fake.queue.append((big, None))
    out = mcp_server.whosaid_worklist(offset=50)
    check(out["count"] == 50 and out["next_offset"] == 100 and out["omitted"] == 80,
          f"later worklist pages are also bounded: {out['count']}")

    # invalid params: clean error dicts, never reaching the runner
    before = len(fake.calls)
    out = mcp_server.whosaid_worklist(tier="urgent")
    check(out["ok"] is False and "tier" in out["error"] and "P1" in out["hint"], f"invalid tier: {out}")
    out = mcp_server.whosaid_worklist(limit=0)
    check(out["ok"] is False and "limit" in out["error"], f"limit=0: {out}")
    out = mcp_server.whosaid_worklist(offset=-3)
    check(out["ok"] is False and "offset" in out["error"], f"negative offset: {out}")
    check(len(fake.calls) == before, "invalid params never reach the runner")


def test_tracking_filters(ws: Path, fake: FakeRunner) -> None:
    """Filters, counts and compact evidence survive the MCP boundary."""
    w = str(ws.resolve())
    accepted = {"id": "CM-001", "text": "Send a checklist", "status": "partial",
                "kind": "explicit_commitment", "accepted_owner": "Alice_Example",
                "evidence_available": True, "remaining_milestones": ["CM-001-publish"],
                "tier": "P1", "milestones": [{"id": "CM-001-publish", "text": "Publish it"}]}
    rows = [accepted, dict(accepted, id="CM-002", accepted_owner=None, kind="request"),
            dict(accepted, id="CM-003", status="delivered")]
    fake.queue.append(({"items": rows, "owner": "Alice_Example"}, None))
    out = mcp_server.whosaid_worklist(kind="explicit_commitment", status="partial",
                                    accepted_only=True, as_of="2026-01-06", compact=True, limit=1)
    check(fake.last()[1] == ["worklist", w, "--owner", "me", "--kind", "explicit_commitment",
                            "--status", "partial", "--as-of", "2026-01-06", "--accepted-only"],
          f"worklist tracking flags reach the CLI: {fake.last()}")
    check(out["total"] == 1 and out["kind_counts"] == {"explicit_commitment": 1}
          and out["status_counts"] == {"partial": 1} and out["accepted_count"] == 1,
          f"filtered counts are before paging: {out}")
    check(out["items"][0]["remaining_milestones"] == ["CM-001-publish"]
          and out["items"][0]["evidence_available"] is True and "milestones" not in out["items"][0],
          f"compact preserves evidence and remaining work: {out['items']}")

    fake.queue.append(([dict(accepted, id="AI-001"), dict(accepted, id="AI-002")], None))
    out = mcp_server.whosaid_items(kind="explicit_commitment", accepted_only=True,
                                 status="partial", as_of="2026-01-06", compact=True, limit=1)
    check(fake.last()[1] == ["items", w, "--status", "partial", "--kind", "explicit_commitment",
                            "--as-of", "2026-01-06", "--accepted-only"], f"items flags: {fake.last()}")
    check(out["total"] == 2 and out["count"] == 1 and out["accepted_count"] == 2
          and out["evidence_available_count"] == 2, f"item counts describe all matches: {out}")
    check(out["items"][0]["kind"] == "explicit_commitment"
          and out["items"][0]["accepted_owner"] == "Alice_Example", f"compact distinctions: {out}")

    fake.queue.append(({}, None))
    mcp_server.whosaid_person("Alice_Example", as_of="2026-01-06")
    check(fake.last()[1] == ["person", w, "Alice_Example", "--as-of", "2026-01-06"], "person date forwarded")
    fake.queue.append(({}, None))
    mcp_server.whosaid_item("AI-001", as_of="2026-01-06")
    check(fake.last()[1] == ["item", w, "AI-001", "--as-of", "2026-01-06"], "item date forwarded")
    before = len(fake.calls)
    for fn, args in ((mcp_server.whosaid_items, {"kind": "todo"}),
                     (mcp_server.whosaid_worklist, {"as_of": "2026-02-31"}),
                     (mcp_server.whosaid_person, {"as_of": "2026-01-06"}),
                     (mcp_server.whosaid_item, {"id": "CM-001", "as_of": "yesterday"})):
        out = fn(**args)
        check(not out["ok"] and out.get("category") == "invalid_arguments", f"structured argument error: {out}")
    check(len(fake.calls) == before, "invalid tracking filters do not invoke subprocesses")


def test_tracking_reads_and_cache(tmp: Path, fake: FakeRunner) -> None:
    """A real synthetic ledger proves temporal reads and cache invalidation."""
    import tracking
    import workspace
    ws = make_workspace(tmp / "tracking")
    (ws / "_commitments.json").write_text(json.dumps({"items": [{
        "id": "CM-001", "speaker": "Alice_Example", "text": "I will send the checklist.",
        "status": "done", "first_seen": "2026-01-01", "occurrences": []}]}))
    evidence = {"kind": "manual", "ref": "fixture:reviewed-note"}
    tracking.record_events(ws, [
        {"item_id": "CM-001", "type": "status", "occurred_at": "2026-01-01",
         "status": "open", "evidence": evidence},
        {"item_id": "CM-001", "type": "milestones", "occurred_at": "2026-01-02",
         "milestones": [{"id": "M-001", "text": "Draft checklist", "acceptance": "Draft shared"},
                        {"id": "M-002", "text": "Publish checklist", "acceptance": "Published link"}],
         "evidence": evidence},
        {"item_id": "M-001", "type": "status", "occurred_at": "2026-01-03",
         "status": "delivered", "evidence": evidence},
        {"item_id": "M-002", "type": "status", "occurred_at": "2026-01-05",
         "status": "delivered", "evidence": evidence},
        {"item_id": "CM-001", "type": "priority", "occurred_at": "2026-01-02",
         "speaker": "Bob_Example", "scope": "planning", "text": "Checklist first", "rank": 1,
         "state": "active", "evidence": evidence},
        {"item_id": "CM-001", "type": "priority", "occurred_at": "2026-01-05",
         "speaker": "Bob_Example", "scope": "planning", "text": "Checklist can wait", "rank": 2,
         "state": "deferred", "evidence": evidence},
    ])
    before = {p.name: p.read_bytes() for p in ws.iterdir() if p.is_file()}
    calls = len(fake.calls)
    out = mcp_server.whosaid_item("CM-001", workspace=str(ws), as_of="2026-01-03")
    check(out["ok"] and out["item"]["status"] == "partial"
          and out["item"]["remaining_milestones"] == ["M-002"], f"CM historical milestones: {out}")
    out = mcp_server.whosaid_item("CM-001", workspace=str(ws))
    check(out["item"]["status"] == "delivered" and out["item"]["evidence_available"], f"CM current evidence: {out}")
    out = mcp_server.whosaid_item("CM-001", workspace=str(ws), as_of="2025-12-31")
    check(out.get("category") == "not_found", f"no item before its first occurrence: {out}")
    out = mcp_server.whosaid_priorities(group="leadership", workspace=str(ws), as_of="2026-01-03")
    check(out["ok"] and out["members"] == ["Bob_Example"] and out["count"] == 1
          and out["items"][0]["rank"] == 1 and out["items"][0]["state"] == "active",
          f"priority history as of date: {out}")
    out = mcp_server.whosaid_priorities(group="leadership", workspace=str(ws))
    check(out["items"][0]["state"] == "deferred" and len(out["history"]) == 2, f"latest priority: {out}")
    out = mcp_server.whosaid_priorities(group="unknown", workspace=str(ws))
    check(out.get("category") == "tracking_error", f"unknown group is structured: {out}")
    check(len(fake.calls) == calls, "CM and priority reads use the shared local projection")
    check(before == {p.name: p.read_bytes() for p in ws.iterdir() if p.is_file()}, "tracking reads never write")

    cached_row = {"id": "CM-001", "kind": "explicit_commitment", "status": "delivered",
                  "accepted_owner": "Alice_Example", "evidence_available": True}
    cache = ws / "_WORKLIST-Alice_Example.json"
    def save_cache(row):
        cache.write_text(json.dumps({"owner": "Alice_Example", "items": [row],
                                     "corpora": workspace.corpora_digests(ws)}))
    save_cache(cached_row)
    out = mcp_server.whosaid_worklist(workspace=str(ws), use_cache=True, accepted_only=True,
                                    kind="explicit_commitment", status="delivered")
    check(out["ok"] and not out["stale"] and out["accepted_count"] == 1, f"fresh tracking cache: {out}")
    token = out["snapshot_token"]
    ledger = ws / "_tracking.json"
    old_stat = ledger.stat()
    ledger.write_text(ledger.read_text() + "\n")
    os.utime(ledger, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    out = mcp_server.whosaid_worklist(workspace=str(ws), use_cache=True)
    check(out["stale"] and out["snapshot_token"] != token, "ledger content invalidates cache even with unchanged mtime")
    save_cache(cached_row)
    token = mcp_server._snapshot_token(ws)
    config = ws / "whosaid.toml"
    old_stat = config.stat()
    config.write_text(config.read_text() + "\n# changed\n")
    os.utime(config, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    out = mcp_server.whosaid_worklist(workspace=str(ws), use_cache=True)
    check(out["stale"] and mcp_server._snapshot_token(ws) != token, "config content changes freshness and page snapshot")

    out = mcp_server.whosaid_worklist(workspace=str(ws), use_cache=True, as_of="2026-01-03")
    check(out.get("category") == "incompatible_cache", f"current cache cannot answer historical status: {out}")
    fake.queue.append((None, {"ok": False, "category": "timeout", "error": "timed out", "phase": "rank", "hint": ""}))
    out = mcp_server.whosaid_worklist(workspace=str(ws), as_of="2026-01-03")
    check(out.get("category") == "timeout" and "cached" not in out
          and out["cache_error"]["category"] == "incompatible_cache", f"historical timeout never attaches current state: {out}")
    save_cache({"id": "CM-001", "status": "open"})
    out = mcp_server.whosaid_worklist(workspace=str(ws), use_cache=True, accepted_only=True)
    check(out.get("category") == "incompatible_cache", f"old extraction cache cannot assert accepted ownership: {out}")


def test_tracking_suggestions(tmp: Path, fake: FakeRunner) -> None:
    """Indexed completion text stays a review candidate, with quote/dedup guards."""
    import sqlite3
    ws = make_workspace(tmp / "suggestions")
    out = mcp_server.whosaid_tracking_suggestions(workspace=str(ws))
    check(out.get("category") == "missing_index", f"missing suggestion index: {out}")
    items = [{"id": "CM-001", "speaker": "Alice_Example", "text": "I will send the project checklist.",
              "status": "open", "first_seen": "2026-01-01", "occurrences": []},
             {"id": "CM-002", "speaker": "Bob_Example", "text": "I will send the project checklist.",
              "status": "open", "first_seen": "2026-01-01", "occurrences": []}]
    (ws / "_commitments.json").write_text(json.dumps({"items": items}))
    conn = sqlite3.connect(ws / "_search.db")
    conn.execute("CREATE TABLE seg(meeting, source, speaker, t_sec, line, text)")
    conn.execute("CREATE TABLE seg_message(rowid INTEGER PRIMARY KEY, message_id, attribution)")
    turns = [
        ("2026-01-03", "Alice_Example", "I sent the project checklist.", "msg-one", {}),
        ("2026-01-04", "Alice_Example", "I sent the project checklist.", "msg-one", {}),
        ("2026-01-04", "Alice_Example", "I sent the project checklist.", "msg-quote", {"quoted": True}),
        ("2026-01-04", "Alice_Example", "I sent the project checklist.", "msg-uncertain", {"attribution_uncertain": True}),
        ("2026-01-04", "Alice_Example", "I have not sent the project checklist.", "msg-negative", {}),
        ("2026-01-04", "Alice_Example", "I will send the project checklist.", "msg-future", {}),
        ("2026-01-04", "Bob_Example", "I sent the project checklist.", "msg-other", {}),
    ]
    for n, (meeting, speaker, text, message_id, attribution) in enumerate(turns, 1):
        conn.execute("INSERT INTO seg VALUES (?,?,?,?,?,?)", (meeting, "chat.speakers.txt", speaker, n, n, text))
        conn.execute("INSERT INTO seg_message VALUES (?,?,?)", (n, message_id, json.dumps(attribution)))
    conn.commit()
    conn.close()
    before = {p.name: p.read_bytes() for p in ws.iterdir() if p.is_file()}
    calls = len(fake.calls)
    out = mcp_server.whosaid_tracking_suggestions(workspace=str(ws))
    check(out["ok"] and out["owner"] == "Alice_Example" and out["count"] == 1,
          f"one completion candidate for configured owner: {out}")
    check(out["items"][0]["item_id"] == "CM-001" and out["items"][0]["review_required"]
          and out["items"][0]["evidence"]["speaker"] == "Alice_Example", f"candidate preserves original speaker: {out}")
    check(out["items_scanned"] == 1 and out["turns_scanned"] == 6 and out["turns_excluded"] == 3,
          f"dedup and uncertain/quoted exclusions: {out}")
    check(out["review_required"] and out["has_more"] is False and out["limitations"], "review and scope disclosed")
    check(before == {p.name: p.read_bytes() for p in ws.iterdir() if p.is_file()}, "suggestions never write files")
    check(len(fake.calls) == calls, "suggestions do not run remote or CLI calls")
    import tracking
    import time
    real_suggest = tracking.suggest_evidence
    def expire_during_match(items, turns, limit=100, *, deadline=None):
        check(deadline is not None and deadline > time.monotonic(), "MCP passes an absolute future deadline to matcher")
        return real_suggest(items, turns, limit, deadline=time.monotonic() - 1)
    try:
        tracking.suggest_evidence = expire_during_match
        out = mcp_server.whosaid_tracking_suggestions(workspace=str(ws))
    finally:
        tracking.suggest_evidence = real_suggest
    check(out.get("category") == "timeout" and out.get("phase") == "suggestions"
          and 0 <= out["elapsed_s"] < 1, f"expired matcher deadline becomes a prompt structured timeout: {out}")
    for limit in (0, 1001, True, "50"):
        out = mcp_server.whosaid_tracking_suggestions(workspace=str(ws), limit=limit)
        check(out.get("category") == "invalid_arguments", f"bounded suggestion limit {limit!r}: {out}")


# ---------------------------------------------------------------------------
# The real runner against fake CLIs
# ---------------------------------------------------------------------------
def test_runner(tmp: Path) -> None:
    ws = tmp / "ws"
    fakes = tmp / "fakelib"
    fakes.mkdir()

    echo = fakes / "echo.py"
    echo.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n"
    )
    payload, err = mcp_server._run_ws(echo, ["query", ws, "hello world", "-k", 3], ws)
    check(err is None, f"exit 0 must not error: {err}")
    check(payload["argv"] == ["query", str(ws), "hello world", "-k", "3", "--json"], f"argv + trailing --json: {payload}")
    check(Path(payload["cwd"]).resolve() == mcp_server.REPO_DIR.resolve(), f"cwd must be the repo dir: {payload['cwd']}")

    missing = fakes / "missing.py"
    missing.write_text(
        "import sys\n"
        "print('whosaid: no _search.db in the workspace', file=sys.stderr)\n"
        "print('run: whosaid index ' + sys.argv[2], file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    payload, err = mcp_server._run_ws(missing, ["status", ws], ws)
    check(payload is None and err["ok"] is False, f"exit 1 must error: {err}")
    check(err["error"] == f"run: whosaid index {ws}", f"error must be the LAST stderr line: {err}")
    check(err["hint"] == f"run: whosaid index {ws}", f"hint must name the index command: {err}")

    usage = fakes / "usage.py"
    usage.write_text("import sys\nprint('usage: search.py query <ws> <query>', file=sys.stderr)\nsys.exit(2)\n")
    _, err = mcp_server._run_ws(usage, ["query", ws], ws)
    check(err["error"].startswith("usage:") and "argument" in err["hint"], f"exit 2 is a usage error: {err}")

    quiet = fakes / "quiet.py"
    quiet.write_text("import sys\nsys.exit(3)\n")
    _, err = mcp_server._run_ws(quiet, ["prs", ws], ws)
    check("exited 3" in err["error"], f"silent failure still explains itself: {err}")
    check(err["hint"] == f"run: whosaid index {ws}", f"no _search.db: hint is the index command: {err}")

    notfound = fakes / "notfound.py"
    notfound.write_text("import sys\nprint('whosaid: item: AI-999 not found', file=sys.stderr)\nsys.exit(1)\n")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "_search.db").write_bytes(b"")
    try:
        _, err = mcp_server._run_ws(notfound, ["item", ws, "AI-999"], ws)
        check(err["error"] == "whosaid: item: AI-999 not found", f"last stderr line: {err}")
        check("index exists" in err["hint"] and "whosaid index" in err["hint"], f"indexed ws + no rebuild ask: argument hint: {err}")
        _, err = mcp_server._run_ws(missing, ["status", ws], ws)
        check(err["hint"] == f"run: whosaid index {ws}", f"CLI asking for a rebuild wins even when the db exists: {err}")
    finally:
        (ws / "_search.db").unlink()

    badjson = fakes / "badjson.py"
    badjson.write_text("print('not json')\n")
    _, err = mcp_server._run_ws(badjson, ["prs", ws], ws)
    check(err["ok"] is False and "unparsable JSON" in err["error"], f"bad JSON must error, not raise: {err}")

    _, err = mcp_server._run_ws(fakes / "absent.py", ["prs", ws], ws)
    check(err["ok"] is False and "missing" in err["error"], f"missing script must error, not raise: {err}")

    slow = fakes / "slow.py"
    slow.write_text("import time\ntime.sleep(5)\n")
    saved = mcp_server.WS_TIMEOUT
    mcp_server.WS_TIMEOUT = 0.3
    try:
        _, err = mcp_server._run_ws(slow, ["query", ws, "x"], ws)
    finally:
        mcp_server.WS_TIMEOUT = saved
    check(err["ok"] is False and "timed out" in err["error"], f"timeout must error, not raise: {err}")
    check(err.get("category") == "timeout" and err.get("phase") is None and err.get("elapsed_s", 0) >= 0.3,
          f"structured timeout, no phase line seen (#113): {err}")
    for name, e in (("missing", mcp_server._run_ws(missing, ["status", ws], ws)[1]),
                    ("badjson", mcp_server._run_ws(badjson, ["prs", ws], ws)[1]),
                    ("absent", mcp_server._run_ws(fakes / "absent.py", ["prs", ws], ws)[1])):
        want = {"missing": "exit_1", "badjson": "bad_json", "absent": "missing_script"}[name]
        check(e.get("category") == want, f"{name} category {want}: {e}")


def test_worklist_diagnostics(tmp: Path) -> None:
    """GitHub issue #113 acceptance: missing corpora, a deliberately delayed
    ranking run, and the stale/fresh cached worklist each give a distinct,
    structured answer; use_cache reads the cache without shelling out."""
    root = tmp / "wl113"
    root.mkdir()

    def new_ws(name: str, corpora: bool) -> Path:
        ws = root / name
        ws.mkdir()
        (ws / "whosaid.toml").write_text('[workspace]\nowner = "Alice_Example"\n')
        if corpora:
            (ws / "_commitments.json").write_text('{"items": []}\n')
            (ws / "_action-items.json").write_text('{"items": []}\n')
        return ws.resolve()

    cache_items = [{"id": f"CM-{i:03d}", "source": "commitments", "text": f"send deck {i}",
                    "status": "open", "tier": "P1" if i == 0 else "P3", "score": 9 - i,
                    "why": ["boss"], "first_seen": "2026-01-05", "last_seen": "2026-01-05",
                    "occurrences": 1, "requested_by": "", "negative": False, "also": [],
                    "merged_into": ""} for i in range(3)]

    def write_cache(ws: Path, age_vs_corpora: float, digests: str = "") -> Path:
        """digests: '' = legacy, 'match' / 'mismatch' = current source fingerprints."""
        cache = ws / "_WORKLIST-Alice_Example.json"
        body = {"owner": "Alice_Example", "generated_from": ["2026-01-05-0900"],
                "items": cache_items, "generated_at": "2026-01-06T10:00:00+00:00"}
        if digests:
            import workspace
            body["corpora"] = workspace.corpora_digests(ws)
            if digests == "mismatch":
                body["corpora"]["_action-items.json"] = "0" * 64
        cache.write_text(json.dumps(body))
        base = max((ws / n).stat().st_mtime for n in ("_commitments.json", "_action-items.json"))
        os.utime(cache, (base + age_vs_corpora, base + age_vs_corpora))
        return cache

    def never(*a, **k):
        raise AssertionError(f"_run_ws must not be called: {a}")

    saved_runner, saved_py, saved_timeout = mcp_server._run_ws, mcp_server.WORKSPACE_PY, mcp_server.WS_TIMEOUT
    try:
        # 1) missing corpora: its own category, the only path that says roll-up, no subprocess
        mcp_server._run_ws = never
        bare = new_ws("bare", corpora=False)
        out = mcp_server.whosaid_worklist(workspace=str(bare))
        print("example missing_corpora:", json.dumps(out))
        check(out["ok"] is False and out.get("category") == "missing_corpora", f"missing corpora category: {out}")
        check("whosaid roll-up" in out["hint"] and "--action-items" in out["hint"], f"missing corpora hint: {out}")
        out = mcp_server.whosaid_worklist(workspace=str(bare), use_cache=True)
        check(out.get("category") == "missing_corpora", f"use_cache without corpora is missing_corpora too: {out}")
        mcp_server._run_ws = saved_runner

        # 2) delayed ranking: a fake workspace.py that reaches the match phase then hangs
        fakes = root / "fakelib"
        fakes.mkdir()
        slow = fakes / "workspace.py"
        slow.write_text(
            "import sys, time\n"
            "print('whosaid: phase: load', file=sys.stderr, flush=True)\n"
            "print('whosaid: dedupe: difflib >= 0.82 (embeddings off)', file=sys.stderr, flush=True)\n"
            "print('whosaid: phase: match ' + sys.argv[4], file=sys.stderr, flush=True)\n"
            "time.sleep(6)\n"
        )
        mcp_server.WORKSPACE_PY = slow
        mcp_server.WS_TIMEOUT = 0.8
        ws = new_ws("delayed", corpora=True)
        out = mcp_server.whosaid_worklist(workspace=str(ws), owner="Alice_Example")
        print("example timeout (no cache):", json.dumps(out))
        check(out["ok"] is False and out.get("category") == "timeout", f"timeout category: {out}")
        check(out.get("phase") == "match Alice_Example", f"timeout names the last phase: {out}")
        check(isinstance(out.get("elapsed_s"), (int, float)) and 0.8 <= out["elapsed_s"] < 6, f"elapsed_s: {out}")
        check("builds the corpora" not in out["hint"] and "large" in out["hint"], f"match-phase hint, no rebuild: {out}")
        check("cached" not in out and "no cached worklist" in out["hint"], f"no cache yet: {out}")

        # prime/matcher timeouts describe source identity work, with no model dependency
        slow.write_text(
            "import sys, time\n"
            "print('whosaid: phase: prime ' + sys.argv[4], file=sys.stderr, flush=True)\n"
            "time.sleep(6)\n"
        )
        out = mcp_server.whosaid_worklist(workspace=str(ws), owner="Alice_Example")
        check(out.get("category") == "timeout" and out.get("phase") == "prime Alice_Example", f"prime phase: {out}")
        check("source identity" in out["hint"] and "do not call an embedding model" in out["hint"], f"prime-phase hint: {out}")

        # 3) timeout + a cache with matching input fingerprints: attached, fresh and sliced
        write_cache(ws, +60, digests="match")
        out = mcp_server.whosaid_worklist(workspace=str(ws), owner="me", limit=2, compact=True)
        print("example timeout + fresh cache:", json.dumps(out))
        cached = out.get("cached") or {}
        check(out.get("category") == "timeout" and cached.get("stale") is False, f"fresh cache: {out}")
        check(cached.get("generated_at") == "2026-01-06T10:00:00+00:00" and cached.get("corpora_mtime"), f"freshness: {cached}")
        check([it["id"] for it in cached.get("items", [])] == ["CM-000", "CM-001"] and cached.get("total") == 3,
              f"cache sliced like a fresh result: {cached}")
        check(set(cached["items"][0]) == set(mcp_server._WORKLIST_COMPACT_KEYS), f"compact applies: {cached['items'][0]}")
        check("use_cache=true" in out["hint"], f"timeout hint points at the cache: {out['hint']}")

        # recorded digests win over mtimes: same content but an older file is still fresh,
        # a newer file built from other content is stale
        write_cache(ws, -60, digests="match")
        out = mcp_server.whosaid_worklist(workspace=str(ws), owner="Alice_Example")
        check((out.get("cached") or {}).get("stale") is False, f"matching digests: fresh despite mtime: {out}")
        write_cache(ws, +60, digests="mismatch")
        out = mcp_server.whosaid_worklist(workspace=str(ws), owner="Alice_Example")
        check((out.get("cached") or {}).get("stale") is True, f"other digests: stale despite mtime: {out}")

        # 4) timeout + a legacy cache without complete source coverage: stale true
        write_cache(ws, -60)
        out = mcp_server.whosaid_worklist(workspace=str(ws), owner="Alice_Example", tier="p1")
        print("example timeout + stale cache:", json.dumps(out))
        cached = out.get("cached") or {}
        check(cached.get("stale") is True, f"stale cache: {out}")
        check([it["id"] for it in cached.get("items", [])] == ["CM-000"] and cached.get("tier") == "P1", f"tier on cache: {cached}")

        # 5) use_cache=true: the cached result directly, never the subprocess
        mcp_server._run_ws = never
        out = mcp_server.whosaid_worklist(workspace=str(ws), use_cache=True, limit=1)
        print("example use_cache:", json.dumps(out))
        check(out["ok"] is True and out.get("source") == "cache" and out.get("stale") is True, f"use_cache: {out}")
        check(out["generated_at"] == "2026-01-06T10:00:00+00:00" and out["owner"] == "Alice_Example", f"use_cache meta: {out}")
        check(out["count"] == 1 and out["total"] == 3 and out["omitted"] == 2, f"use_cache pages: {out}")

        # 6) use_cache=true with no cache file: no_cache, still no subprocess
        (ws / "_WORKLIST-Alice_Example.json").unlink()
        out = mcp_server.whosaid_worklist(workspace=str(ws), use_cache=True)
        print("example no_cache:", json.dumps(out))
        check(out["ok"] is False and out.get("category") == "no_cache", f"no_cache: {out}")
        check("_WORKLIST-Alice_Example.json" in out["error"], f"no_cache names the file: {out}")
    finally:
        mcp_server._run_ws, mcp_server.WORKSPACE_PY, mcp_server.WS_TIMEOUT = saved_runner, saved_py, saved_timeout


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# whosaid_relabel flags (GitHub issues #19 and #38): offline via a stubbed _run_cli
# ---------------------------------------------------------------------------
def test_relabel_flags() -> None:
    calls: list[list] = []

    def fake_cli(args, timeout=None):
        calls.append([str(a) for a in args])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    saved = mcp_server._run_cli
    mcp_server._run_cli = fake_cli
    try:
        out = mcp_server.whosaid_relabel("rec", {"SPEAKER_00": "Jane"})
        check(out["ok"] is True, f"plain relabel must succeed offline: {out}")
        check(calls[-1] == ["relabel", "rec", "SPEAKER_00=Jane"],
              f"plain relabel must not gain the new flags: {calls[-1]}")

        out = mcp_server.whosaid_relabel(
            "rec", {"SPEAKER_01": "Bob"},
            no_save=True, force=True, note="from standup 2026-09-18",
            match_threshold=0.4, outdir="/tmp/whosaid-test-out",
        )
        check(out["ok"] is True, f"flagged relabel must succeed offline: {out}")
        check(calls[-1] == [
            "relabel", "rec", "SPEAKER_01=Bob",
            "--match-threshold", "0.4", "--no-save", "--force",
            "--note", "from standup 2026-09-18", "-o", "/tmp/whosaid-test-out",
        ], f"new flags must mirror the CLI surface in order: {calls[-1]}")
        check("transcript-only" in out["summary"].lower(),
              f"summary must admit no_save instead of claiming a registry save: {out['summary']}")
        check(out["registry_path"], f"registry_path still reported: {out}")

        out = mcp_server.whosaid_relabel("rec", {}, auto=True, fold_unknown=True)
        check(out["ok"] is True, f"cached unknown folding must accept auto=true: {out}")
        check(calls[-1] == ["relabel", "rec", "--auto", "--fold-unknown"],
              f"fold_unknown must follow --auto in CLI argv: {calls[-1]}")

        out = mcp_server.whosaid_relabel("rec", {}, auto=True, forget=["Jane", "Bob_2"])
        check(out["ok"] is True, f"forget with auto=true must be accepted: {out}")
        check(calls[-1] == ["relabel", "rec", "--auto", "--forget", "Jane", "--forget", "Bob_2"],
              f"each forget name must become its own --forget flag: {calls[-1]}")

        before = len(calls)
        out = mcp_server.whosaid_relabel("rec", {"SPEAKER_00": "Jane"}, forget=["Jane"])
        check(out["ok"] is False and len(calls) == before and "requires auto=true" in out["error"],
              f"forget without auto must be rejected before shelling: {out}")
        out = mcp_server.whosaid_relabel("rec", {}, auto=True, forget=["bad name; rm"])
        check(out["ok"] is False and len(calls) == before and "forget" in out["error"],
              f"a malformed forget name must be rejected before shelling: {out}")

        before = len(calls)
        out = mcp_server.whosaid_relabel("rec", {"SPEAKER_00": "Jane"}, fold_unknown=True)
        check(out["ok"] is False and len(calls) == before and "requires auto=true" in out["error"],
              f"fold_unknown without auto must be rejected before shelling: {out}")

        before = len(calls)
        out = mcp_server.whosaid_relabel("rec", {"BAD": "Jane"})
        check(out["ok"] is False and len(calls) == before,
              f"validation must reject bad keys before shelling: {out}")

        mcp_server._run_cli = lambda args, timeout=None: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="cluster similarity to the registry print is below the match threshold")
        out = mcp_server.whosaid_relabel("rec", {"SPEAKER_00": "Jane"}, force=True)
        check(out["ok"] is False and "whosaid_doctor" in out["fix"],
              f"CLI failure still surfaces as an error dict: {out}")
    finally:
        mcp_server._run_cli = saved


def test_fold_metadata(tmp: Path) -> None:
    """MCP exposes core fold metadata and normalizes old sidecars safely."""
    outdir = tmp / "fold-metadata"
    outdir.mkdir()
    audio = tmp / "sample.m4a"
    audio.write_bytes(b"fake")
    calls: list[list] = []

    def fake_cli(args, timeout=None):
        calls.append([str(a) for a in args])
        (outdir / "sample.txt").write_text("hello\\n")
        (outdir / "sample.speakers.txt").write_text("[00:00:00] SPEAKER_00: hello\\n")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    saved = mcp_server._run_cli
    mcp_server._run_cli = fake_cli
    try:
        # Old sidecar: no fold fields. The observable rendered count is both ends.
        (outdir / "sample.diarization.json").write_text(json.dumps({
            "num_speakers": 2,
            "names": {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01"},
            "segments": [{"speaker": "SPEAKER_00"}, {"speaker": "SPEAKER_01"}],
        }))
        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir))
        check(out["ok"] is True, f"legacy sidecar transcribe result: {out}")
        check((out["count_before_fold"], out["count_after_fold"], out["fold_note"]) == (2, 2, None),
              f"legacy sidecar defaults must preserve observed count: {out}")
        legacy_relabel = mcp_server.whosaid_relabel(
            str(outdir / "sample.diarization.json"), {}, auto=True,
        )
        check((legacy_relabel["count_before_fold"], legacy_relabel["count_after_fold"],
               legacy_relabel["fold_note"]) == (2, 2, None),
              f"legacy sidecar relabel defaults must preserve observed count: {legacy_relabel}")
        check(calls[-1][-1:] == ["--auto"] and "--fold-unknown" not in calls[-1],
              f"auto without fold_unknown must preserve no-fold CLI behavior: {calls[-1]}")

        # New core sidecar: preserve its explicitly recorded before/after/note values.
        sidecar = outdir / "sample.diarization.json"
        sidecar.write_text(json.dumps({
            "base": "sample", "num_speakers": 2,
            "count_before_fold": 20, "count_after_fold": 2,
            "fold_note": "Folded 18 anonymous phantom clusters into 2 rendered speakers.",
            "count_warning": "auto estimate saturated at cap",
            "count_estimate": {"k": 20, "saturated": True},
            "names": {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01"},
            "segments": [{"speaker": "SPEAKER_00"}, {"speaker": "SPEAKER_01"}],
        }))
        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir))
        check((out["count_before_fold"], out["count_after_fold"], out["fold_note"]) == (
            20, 2, "Folded 18 anonymous phantom clusters into 2 rendered speakers."),
            f"transcribe must preserve current fold metadata: {out}")

        relabeled = mcp_server.whosaid_relabel(str(sidecar), {}, auto=True, fold_unknown=True)
        check(relabeled["ok"] is True, f"relabel sidecar result: {relabeled}")
        check((relabeled["num_speakers"], relabeled["count_before_fold"],
               relabeled["count_after_fold"], relabeled["fold_note"]) == (
                   2, 20, 2, "Folded 18 anonymous phantom clusters into 2 rendered speakers."),
              f"relabel must return the same current fold metadata: {relabeled}")
        check(relabeled["count_warning"] == "auto estimate saturated at cap"
              and relabeled["count_estimate"] == {"k": 20, "saturated": True},
              f"relabel must retain existing count metadata: {relabeled}")
        check(calls[-1][-2:] == ["--auto", "--fold-unknown"],
              f"relabel metadata test must use the real fold CLI contract: {calls[-1]}")
        check(out["suggested_max_speakers"] is None and relabeled["suggested_max_speakers"] is None,
              f"a sidecar without the #59 hint must surface None: {out} {relabeled}")

        # Issue #59 proposal 4: a saturated, unrecovered estimate carries a
        # structured --max-speakers suggestion that both MCP result paths surface.
        hint = "Auto count remains UNRELIABLE: primary found 44 clusters at its bound; " \
               "20 is not a verified speaker count. Try `--max-speakers 8`."
        sidecar.write_text(json.dumps({
            "base": "sample", "num_speakers": 2,
            "count_warning": hint, "suggested_max_speakers": 8,
            "count_estimate": {"k": 20, "raw_k": 44, "saturated": True, "fallback": None,
                               "suggested_max": 8},
            "names": {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01"},
            "segments": [{"speaker": "SPEAKER_00"}, {"speaker": "SPEAKER_01"}],
        }))
        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir))
        check(out["suggested_max_speakers"] == 8 and "--max-speakers 8" in out["summary"],
              f"transcribe must surface the #59 suggestion: {out}")
        relabeled = mcp_server.whosaid_relabel(str(sidecar), {}, auto=True)
        check(relabeled["suggested_max_speakers"] == 8 and relabeled["count_warning"] == hint,
              f"relabel must surface the #59 suggestion: {relabeled}")
        # Only count_estimate carries it (e.g. a hand-edited sidecar): still surfaced.
        data = json.loads(sidecar.read_text())
        del data["suggested_max_speakers"]
        sidecar.write_text(json.dumps(data))
        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir))
        check(out["suggested_max_speakers"] == 8, f"count_estimate.suggested_max fallback: {out}")

        # Workspace [diarize] hints (issue #59): an outdir inside a workspace gets them
        # when no speaker arg is passed; any explicit speaker arg replaces them.
        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir))
        check("--max-speakers" not in calls[-1] and out["speaker_hints_from"] is None,
              f"no workspace -> no hints: {calls[-1]}")
        (tmp / "whosaid.toml").write_text("[diarize]\nmax_speakers = 8\n")
        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir))
        check(calls[-1][-2:] == ["--max-speakers", "8"] and "[diarize]" in (out["speaker_hints_from"] or ""),
              f"outdir inside a workspace takes [diarize]: {calls[-1]} {out.get('speaker_hints_from')}")
        mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir), speakers=2)
        check("--max-speakers" not in calls[-1] and "--speakers" in calls[-1],
              f"explicit speakers replaces [diarize]: {calls[-1]}")
        n = len(calls)
        (tmp / "whosaid.toml").write_text("[diarize]\nmax_speakers = 0\n")
        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir))
        check(out["ok"] is False and "[diarize] max_speakers" in out["error"] and len(calls) == n,
              f"bad [diarize] refuses before running: {out}")
        (tmp / "whosaid.toml").unlink()
    finally:
        mcp_server._run_cli = saved


def test_diarization_failure(tmp: Path) -> None:
    """GitHub issue #56: a diarization crash must not come back as ok/"All speakers named"."""
    outdir = tmp / "diarize-fail"
    outdir.mkdir()
    audio = tmp / "long.m4a"
    audio.write_bytes(b"fake")
    rc = {"code": 3}
    stderr = ("[E:onnxruntime] Non-zero status code returned while running Where node.\n"
              "whosaid: WARN diarization failed; plain transcript is unaffected.\n"
              "whosaid: plain transcript saved at x.txt; diarization failed (see warnings above)\n")

    def fake_cli(args, timeout=None):
        (outdir / "long.txt").write_text("hello\n")
        return subprocess.CompletedProcess(args, rc["code"], stdout="", stderr=stderr)

    saved = mcp_server._run_cli
    mcp_server._run_cli = fake_cli
    try:
        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir))
        check(out["ok"] is False and out.get("partial") is True,
              f"exit 3 must be ok=false/partial: {out}")
        check("onnxruntime" in (out.get("diarization_error") or ""),
              f"diarization_error carries the CLI's reason: {out}")
        check("All speakers named" not in out["next_step"] and out["transcript_txt"].endswith("long.txt"),
              f"next_step must not claim success; transcript path still returned: {out}")

        # exit 0 but only an earlier run's sidecar on disk: still a failure, files flagged stale
        rc["code"] = 0
        old = outdir / "long.diarization.json"
        old.write_text(json.dumps({"num_speakers": 2, "names": {"SPEAKER_00": "Alice_Example"}, "segments": []}))
        os.utime(old, (1_000_000, 1_000_000))
        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir))
        check(out["ok"] is False and str(old) in out.get("stale_speaker_files", []),
              f"stale sidecar must not pass as this run's result: {out}")

        out = mcp_server.whosaid_transcribe(str(audio), outdir=str(outdir), diarize=False)
        check(out["ok"] is True and "diarization_error" not in out,
              f"diarize=false never reports a diarization failure: {out}")
    finally:
        mcp_server._run_cli = saved


def test_resources(tmp: Path) -> None:
    ws = make_workspace(tmp / "resources")
    outside = tmp / "resources" / "outside"
    outside.mkdir()
    (outside / "a.speakers.txt").write_text("[00:00:01] Eve_Example: SECRET outside the workspace\n")
    (outside / "action-items.md").write_text("SECRET action items\n")

    clear_env()
    for fn in (mcp_server.workspace_wiki, mcp_server.workspace_action_items, mcp_server.workspace_index):
        text = fn()
        check("WHOSAID_WORKSPACE" in text, f"{fn.__name__} without env must explain: {text}")
    text = mcp_server.meeting_transcript("2026-01-05-0900")
    check("WHOSAID_WORKSPACE" in text, f"template without env must explain: {text}")

    os.environ["WHOSAID_WORKSPACE"] = str(ws)
    check(mcp_server.workspace_wiki().startswith("# Wiki"), "wiki resource returns _WIKI.md")
    text = mcp_server.workspace_action_items()
    check("_ACTION-ITEMS.md" in text and "roll-up" in text, f"missing corpus explains what writes it: {text}")
    text = mcp_server.workspace_index()
    check("_INDEX.md" in text and "roll-up" in text, f"missing index explains what writes it: {text}")
    (ws / "_INDEX.md").write_text("| Meeting |\n")
    check(mcp_server.workspace_index().startswith("| Meeting |"), "index resource returns _INDEX.md")

    text = mcp_server.meeting_transcript("2026-01-05-0900")
    check("# a.speakers.txt" in text and "# b.speakers.txt" in text, f"several transcripts are joined with headers: {text}")
    check("Bob_Example" in text and "Second recording" in text, "both transcripts' text is present")
    check(text.index("a.speakers.txt") < text.index("b.speakers.txt"), "transcripts are joined in sorted order")

    single = ws / "2026-01-06-1000"
    single.mkdir()
    (single / "only.speakers.txt").write_text("[00:00:01] Alice_Example: Just one file.\n")
    text = mcp_server.meeting_transcript("2026-01-06-1000")
    check(text.startswith("[00:00:01] Alice_Example"), f"a single transcript is returned verbatim: {text}")

    check(mcp_server.meeting_action_items("2026-01-05-0900").startswith("# Action items"), "meeting action items")
    text = mcp_server.meeting_action_items("2026-01-06-1000")
    check("action-items.md" in text and "ingest" in text, f"missing per-meeting file explains: {text}")

    empty = ws / "2026-01-07-1100"
    empty.mkdir()
    text = mcp_server.meeting_transcript("2026-01-07-1100")
    check("speakers.txt" in text and "ingest" in text, f"folder without transcripts explains: {text}")
    text = mcp_server.meeting_transcript("2099-12-31-2359")
    check("no meeting folder" in text and "whosaid_meetings" in text, f"unknown folder explains: {text}")

    for bad in ("../outside", "outside/../outside", "a/b", "..", "", ".", ".hidden", "x\\y"):
        for fn in (mcp_server.meeting_transcript, mcp_server.meeting_action_items):
            text = fn(bad)
            check("SECRET" not in text, f"{fn.__name__}({bad!r}) must never read outside the workspace")
            check("invalid meeting folder" in text, f"{fn.__name__}({bad!r}) must reject: {text}")
    text = mcp_server.meeting_transcript("outside")
    check("SECRET" not in text and "no meeting folder" in text,
          f"a plain name resolves inside ws only, never to the sibling folder: {text}")

    check(mcp_server._safe_folder("2026-01-05-0900") and mcp_server._safe_folder("standup notes"), "normal names pass")
    check(not mcp_server._safe_folder(None), "None is not a folder")


# ---------------------------------------------------------------------------
# whosaid_reprocess: argument -> CLI mapping via a stubbed _run_cli (nothing is executed)
# ---------------------------------------------------------------------------
def test_reprocess(tmp: Path) -> None:
    calls: list = []

    def fake_cli(args, timeout=None):
        calls.append(([str(a) for a in args], timeout))
        return subprocess.CompletedProcess(args, 0, stdout="dry run: 1 meeting(s)\n", stderr="")

    ws = make_workspace(tmp / "reprocess")
    saved = mcp_server._run_cli
    mcp_server._run_cli = fake_cli
    try:
        out = mcp_server.whosaid_reprocess(workspace=str(ws))
        argv, to = calls[-1]
        check(argv == ["reprocess", str(ws.resolve()), "--dry-run"], f"default must be a dry run: {argv}")
        check(to == mcp_server.REPROCESS_TIMEOUT >= 3600, f"generous timeout passed: {to}")
        check(out["ok"] is True and out["dry_run"] is True and out["returncode"] == 0
              and "dry run" in out["stdout"] and out["stderr"] == "", f"dry-run result shape: {out}")

        out = mcp_server.whosaid_reprocess(
            workspace=str(ws), meetings=["m1", "m2"], dry_run=False, all_meetings=True, no_items=True,
            speakers=3, min_speakers=2, max_speakers=4, engine="sherpa", hook="echo hi")
        argv, _ = calls[-1]
        check(argv == ["reprocess", str(ws.resolve()), "m1", "m2", "--all", "--no-items",
                       "--speakers", "3", "--min-speakers", "2", "--max-speakers", "4",
                       "--engine", "sherpa", "--hook", "echo hi"], f"apply argv: {argv}")
        check(out["dry_run"] is False and "--dry-run" not in argv, f"dry_run=false must drop --dry-run: {out}")

        out = mcp_server.whosaid_reprocess(
            workspace=str(ws), dry_run=False, max_speakers=4, engine="sherpa",
            expected_speakers=[" Alice ", "Bob"], match_threshold=0.6)
        argv, _ = calls[-1]
        check(argv == ["reprocess", str(ws.resolve()), "--max-speakers", "4",
                       "--expected-speakers", "Alice,Bob", "--match-threshold", "0.6",
                       "--engine", "sherpa"], f"roster/threshold argv: {argv}")
        check(out["ok"] is True, f"roster/threshold call ok: {out}")
        mcp_server.whosaid_reprocess(workspace=str(ws), expected_speakers=[], match_threshold=1)
        argv, _ = calls[-1]
        check("--expected-speakers" not in argv and argv[-2:] == ["--match-threshold", "1.0"],
              f"empty roster omitted, threshold 1 ok: {argv}")

        before = len(calls)
        for bad in (dict(meetings=["../x"]), dict(meetings=[".hid"]), dict(meetings=["a/b"]),
                    dict(speakers=0), dict(max_speakers="x"),
                    dict(expected_speakers=["a,b"]), dict(expected_speakers=["Al", " "]), dict(match_threshold=0),
                    dict(match_threshold=1.5), dict(match_threshold="x")):
            out = mcp_server.whosaid_reprocess(workspace=str(ws), **bad)
            check(out["ok"] is False and "hint" in out, f"{bad} must be rejected: {out}")
        out = mcp_server.whosaid_reprocess(workspace=str(tmp / "nope"))
        check(out["ok"] is False, f"bad workspace rejected: {out}")
        check(len(calls) == before, "validation failures must not shell")

        mcp_server._run_cli = lambda args, timeout=None: subprocess.CompletedProcess(
            args, 1, stdout="partial\n", stderr="\n".join(f"e{i}" for i in range(30)))
        out = mcp_server.whosaid_reprocess(workspace=str(ws), dry_run=False)
        check(out["ok"] is False and out["returncode"] == 1 and out["stderr"].splitlines()[-1] == "e29"
              and len(out["stderr"].splitlines()) == 20, f"failure keeps rc and stderr tail: {out}")

        def slow(args, timeout=None):
            raise subprocess.TimeoutExpired(args, timeout)
        mcp_server._run_cli = slow
        out = mcp_server.whosaid_reprocess(workspace=str(ws), dry_run=False)
        check(out["ok"] is False and "timed out" in out["error"], f"timeout surfaces as error: {out}")
    finally:
        mcp_server._run_cli = saved


# Registration (works against the stub's dicts and the real SDK's managers)
# ---------------------------------------------------------------------------
def test_registration() -> None:
    new_tools = {
        "whosaid_search", "whosaid_context", "whosaid_items", "whosaid_item", "whosaid_person",
        "whosaid_meetings", "whosaid_prs", "whosaid_speakers", "whosaid_workspace_status",
        "whosaid_worklist", "whosaid_priorities", "whosaid_tracking_suggestions",
    }
    if SDK == "stub":
        registered = set(mcp_server.mcp.tools)
        check(new_tools <= registered, f"all workspace tools registered: {sorted(registered)}")
        for name in new_tools:
            _, desc, ann = mcp_server.mcp.tools[name]
            check(ann.read_only_hint is True and ann.destructive_hint is False, f"{name} annotations")
            check(ann.idempotent_hint is True and ann.open_world_hint is False, f"{name} annotations")
            check(bool(desc) and len(desc) < 2048 and "\u2014" not in desc, f"{name} description")
        uris = set(mcp_server.mcp.resources)
    else:
        import asyncio
        tools = asyncio.run(mcp_server.mcp.list_tools())
        check(new_tools <= {t.name for t in tools}, "all workspace tools registered (real SDK)")
        uris = {str(r.uri) for r in asyncio.run(mcp_server.mcp.list_resources())}
        uris |= {t.uri_template for t in asyncio.run(mcp_server.mcp.list_resource_templates())}
    check({
        "whosaid://workspace/wiki",
        "whosaid://workspace/action-items",
        "whosaid://workspace/index",
        "whosaid://workspace/meeting/{folder}/transcript",
        "whosaid://workspace/meeting/{folder}/action-items",
    } <= uris, f"workspace resources registered: {sorted(uris)}")
    check("Meeting workspace" in mcp_server.SERVER_INSTRUCTIONS, "instructions gained the workspace paragraph")
    check("WHOSAID_WORKSPACE" in mcp_server.SERVER_INSTRUCTIONS, "instructions name WHOSAID_WORKSPACE")
    check("whosaid index" in mcp_server.SERVER_INSTRUCTIONS, "instructions say who builds the index")
    check("Meeting workspace" in mcp_server._GUIDE, "guide gained the workspace section")


def main() -> None:
    saved_env = dict(os.environ)
    real_runner = mcp_server._run_ws
    with tempfile.TemporaryDirectory(prefix="whosaid-mcp-ws-") as d:
        tmp = Path(d)
        try:
            fake = FakeRunner()
            mcp_server._run_ws = fake
            test_workspace_resolution(tmp, fake)

            ws = make_workspace(tmp / "tools")
            clear_env()
            os.environ["WHOSAID_WORKSPACE"] = str(ws)
            fake.calls.clear()
            test_search(ws, fake)
            test_context(ws, fake)
            test_graph_tools(ws, fake)
            test_workspace_status(ws, fake)
            test_worklist(ws, fake)
            test_worklist_paging(ws, fake)
            test_list_paging(ws, fake)
            test_items_paging(ws, fake)
            test_tracking_filters(ws, fake)
            test_tracking_reads_and_cache(tmp, fake)
            test_tracking_suggestions(tmp, fake)

            mcp_server._run_ws = real_runner
            clear_env()
            test_runner(tmp)
            test_relabel_flags()
            test_reprocess(tmp)
            test_fold_metadata(tmp)
            test_diarization_failure(tmp)
            test_worklist_diagnostics(tmp)
            test_resources(tmp)
            test_registration()
        finally:
            mcp_server._run_ws = real_runner
            os.environ.clear()
            os.environ.update(saved_env)
    print(f"PASS: {CHECKS} assertions (mcp SDK: {SDK})")


if __name__ == "__main__":
    main()
