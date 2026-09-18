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
WHOSAID_WORKSPACE pointed at a temp workspace.

If the `mcp` SDK is not importable, a minimal stub of the names mcp_server uses
(MCPServer.tool/.resource decorators and ToolAnnotations) is installed first, so
this file runs with plain python3 and needs no `uv`.

Run:
    python3 test/mcp_workspace_tools_test.py
    uv run --with "mcp[cli]>=2,<3" python test/mcp_workspace_tools_test.py   # real SDK
"""

import json
import os
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
    check(out["items"] == [{"id": "AI-001"}] and out["count"] == 1, f"items shape: {out}")

    fake.queue.append(([], None))
    mcp_server.whosaid_items(owner="Bob_Example", requester="Alice_Example", status="open", type="task")
    _, args, _ = fake.last()
    check(args == ["items", w, "--owner", "Bob_Example", "--requester", "Alice_Example",
                   "--status", "open", "--type", "task"], f"items filters: {args}")

    fake.queue.append(([], None))
    mcp_server.whosaid_items(owner="  ", status="")
    check(fake.last()[1] == ["items", w], "blank filters are dropped")

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
    check("roll-up" in out["hint"] and "whosaid index" not in out["hint"], f"worklist hint names roll-up, not the index: {out['hint']}")

    before = len(fake.calls)
    out = mcp_server.whosaid_worklist(workspace=str(ws.parent / "nope"))
    check(out["ok"] is False and len(fake.calls) == before, "bad workspace never reaches the runner")


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


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------
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
# Registration (works against the stub's dicts and the real SDK's managers)
# ---------------------------------------------------------------------------
def test_registration() -> None:
    new_tools = {
        "whosaid_search", "whosaid_context", "whosaid_items", "whosaid_item", "whosaid_person",
        "whosaid_meetings", "whosaid_prs", "whosaid_speakers", "whosaid_workspace_status",
        "whosaid_worklist",
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

            mcp_server._run_ws = real_runner
            clear_env()
            test_runner(tmp)
            test_resources(tmp)
            test_registration()
        finally:
            mcp_server._run_ws = real_runner
            os.environ.clear()
            os.environ.update(saved_env)
    print(f"PASS: {CHECKS} assertions (mcp SDK: {SDK})")


if __name__ == "__main__":
    main()
