#!/usr/bin/env python3
"""
Offline test for lib/graph.py (GitHub issue #14: entity graph + generated wiki;
issue #13: the per-owner commitments view; issue #23: the commitment table is
the CM corpus flattened, one row per occurrence).

Builds a synthetic workspace in a temp dir: one dated meeting folder (in the
manifest) and one hand-named folder (manifest-less, with a diarization sidecar),
speaker-labeled transcripts with placeholder names, a small action-item corpus
(open / merged / contingent), a new-shape CM commitment corpus (_commitments.json
with source-carrying occurrences), and per-meeting action-items.md files. The
FTS5 `seg` table is created here directly (same columns lib/search.py writes),
so this test does not depend on the search module. Then it runs the real CLI
(`python3 lib/graph.py ...`) and asserts every table, every view's JSON, the
`commitments` subcommand and its filters, the wiki markdown, the exit-1 paths,
old-shape/missing corpora degrading cleanly, build idempotence, and that build
waits on a busy database.

Run:  python3 test/graph_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
GRAPH = LIB / "graph.py"
sys.path.insert(0, str(LIB))

import graph  # noqa: E402
import workspace  # noqa: E402
import wsconfig  # noqa: E402

M1 = "2026-09-01-0700"      # dated, in the manifest
M2 = "planning-notes"       # hand-named, sidecar only
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


# ---- fixture ----------------------------------------------------------------------------

M1_TRANSCRIPT = """# Speaker-labeled transcript: meeting
# Speakers (3): Alice_Example, Bob_Example, SPEAKER_02

[00:00:05] Bob_Example: morning everyone, quick standup.
[00:12:01] Bob_Example: Alice, can you ship the schema fix today? we need that fix.
[00:12:20] Alice_Example: yes, I will ship it after lunch.
[00:14:30] Bob_Example: also write the design note for the registry.
[00:15:10] Bob_Example: and share it with the team by Friday.
[00:16:00] SPEAKER_02: I can review it.
[00:20:00] Alice_Example: I will review the open pull requests, starting with 42 pr.
[00:21:00] Alice_Example: pr 42 is the schema one.
"""

M2_TRANSCRIPT = """# Speaker-labeled transcript: notes
# Speakers (2): Alice_Example, Carol_Example

[00:01:00] Carol_Example: let's plan the onboarding.
[00:02:30] Alice_Example: I will get onboarded to the repos first.
continuation of the same turn on a second line.
[00:41:00] Alice_Example: let's move standup to 7am.
"""

# Line numbers matter: the corpus occurrences below point at lines 5 and 6.
M1_ACTION_ITEMS = f"""# Action items - {M1}

## Leadership asks

- **Alice_Example** [Bob_Example 00:12:01] Ship the schema fix (AI-001). "we need that fix" PR #42
- **Alice_Example** [Bob_Example 00:14:30 / 00:15:10] Write the design note for the registry

## Own commitments

- **Alice_Example** [00:20:00] Review the open pull requests
- **Alice_Example** [inferred] Read the onboarding docs
- Plain bullet that is not a commitment
"""

M2_ACTION_ITEMS = """# Action Items - Carol_Example
## Meeting: planning notes

## 1. Leadership asks
- **[Carol_Example 1:04:21] Get onboarded to the repos.** "reach out and ask for access"
- **[Carol_Example 15:41 / 18:49] Apply the merge test** before merging anything.
- **[Carol_Example ~46:36–47:35] Draft the pillars design** with the identified owner.
- **[Carol_Example, implicit] Bring the design doc** to the next grooming.
- _None from anyone else._

## 3. Own commitments
- **[0:41:00] Move standup to 7am** so there is time for a workout.
- **[SPEAKER_02 13:46] "I have a PR open to fix that"** likely the owner, unconfirmed. See #101.
"""

CORPUS_MD = f"""# Action items - workspace

## Alice_Example

### open

- **AI-001** [open] (leadership ask) {M1} (1x): Ship the schema fix, see PR #42
"""

MANIFEST = {
    "meetings": [{
        "folder": M1, "source_name": "meeting.m4a", "source_sha256": "0" * 64,
        "created": "2026-09-01T14:00:00Z", "duration_s": 1800.0, "has_txt": True,
        "has_json": False, "has_speakers": True, "has_action_items": True,
        "ingested_at": "2026-09-01T15:00:00Z",
    }]
}

CORPUS = {
    "next_id": 4, "similarity_threshold": 0.82, "folded_meetings": [M1],
    "items": [
        {"id": "AI-001", "text": "Ship the schema fix", "type": "leadership ask",
         "owner": "Alice_Example", "status": "open", "first_seen": M1, "last_seen": M1,
         "merged_into": "", "occurrences": [{"meeting": M1, "line": 5}]},
        {"id": "AI-002", "text": "Ship the schema fix today", "type": "",
         "owner": "Alice_Example", "status": "merged", "first_seen": M1, "last_seen": M1,
         "merged_into": "AI-001", "occurrences": [{"meeting": M1, "line": 5}]},
        {"id": "AI-003", "text": "Write the design note for the registry", "type": "leadership ask",
         "owner": "Alice_Example", "status": "contingent", "first_seen": M1, "last_seen": M1,
         "merged_into": "", "occurrences": [{"meeting": M1, "line": 6}]},
    ],
}

SIDECAR = {
    "source": {"path": "/tmp/staging/notes.m4a", "duration_seconds": 1500.0,
               "creation_time": "2026-09-03T15:00:00.000000Z"},
    "segments": [],
}

# New-shape CM corpus (issue #23): item-level id/status/ai_refs/merged_into,
# occurrences carrying source + meeting + line + t_sec + owner + requester
# (+role) + text + cue + negative. Every fixture commitment is owned by Alice;
# Bob asked for CM-001/CM-002, Carol for CM-005/CM-006.
CM_CORPUS = {
    "next_id": 7, "folded_meetings": [M1, M2],
    "items": [
        {"id": "CM-001", "text": "Ship the schema fix", "speaker": "Alice_Example",
         "status": "open", "priority": "high", "requested_by": "Bob_Example",
         "merged_into": "", "ai_refs": ["AI-001", "AI-002"],
         "occurrences": [
             {"source": "transcript", "meeting": M1, "line": 6, "t_sec": 740,
              "owner": "Alice_Example", "requester": "Bob_Example", "requester_role": "peer",
              "text": "yes, I will ship it after lunch.", "cue": "will", "negative": False},
             {"source": "action-items", "meeting": M1, "line": 5, "t_sec": 721,
              "owner": "Alice_Example", "requester": "Bob_Example", "requester_role": "",
              "text": "Ship the schema fix", "cue": "", "negative": False},
         ]},
        {"id": "CM-002", "text": "Write the design note for the registry",
         "speaker": "Alice_Example", "status": "open", "requested_by": "Bob_Example",
         "merged_into": "", "ai_refs": ["AI-003"],
         "occurrences": [
             {"source": "action-items", "meeting": M1, "line": 6, "t_sec": 870,
              "owner": "Alice_Example", "requester": "Bob_Example",
              "text": "Write the design note for the registry"},
             {"source": "action-items", "meeting": M1, "line": 6, "t_sec": 910,
              "owner": "Alice_Example", "requester": "Bob_Example",
              "text": "Write the design note for the registry"},
         ]},
        {"id": "CM-003", "text": "Review the open pull requests",
         "speaker": "Alice_Example", "status": "ongoing", "merged_into": "", "ai_refs": [],
         "occurrences": [
             {"source": "action-items", "meeting": M1, "line": 10, "t_sec": 1200,
              "owner": "Alice_Example", "requester": "",
              "text": "Review the open pull requests", "cue": "will"}]},
        {"id": "CM-004", "text": "Read the onboarding docs",
         "speaker": "Alice_Example", "status": "open", "merged_into": "", "ai_refs": [],
         "occurrences": [
             {"source": "action-items", "meeting": M1, "line": 11, "t_sec": None,
              "owner": "Alice_Example", "requester": "",
              "text": "Read the onboarding docs", "cue": "inferred"}]},
        {"id": "CM-005", "text": "Get onboarded to the repos",
         "speaker": "Alice_Example", "status": "done", "merged_into": "", "ai_refs": [],
         "occurrences": [
             {"source": "transcript", "meeting": M2, "line": 5, "t_sec": 150,
              "owner": "Alice_Example", "requester": "Carol_Example", "requester_role": "boss",
              "text": "I will get onboarded to the repos first.", "cue": "will", "negative": False},
             {"source": "action-items", "meeting": M2, "line": 5, "t_sec": 3861,
              "owner": "Alice_Example", "requester": "Carol_Example",
              "text": "Get onboarded to the repos."},
         ]},
        {"id": "CM-006", "text": "Apply the merge test",
         "speaker": "Alice_Example", "status": "merged", "merged_into": "CM-005", "ai_refs": [],
         "occurrences": [
             {"source": "action-items", "meeting": M2, "line": 6, "t_sec": 941,
              "owner": "Alice_Example", "requester": "Carol_Example",
              "text": "Apply the merge test", "negative": False},
             {"source": "action-items", "meeting": M2, "line": 6, "t_sec": 1129,
              "owner": "Alice_Example", "requester": "Carol_Example",
              "text": "Apply the merge test", "cue": "before", "negative": True},
         ]},
    ],
}

# Pre-#23 corpus: occurrences were {meeting, line} only, no source/t_sec/owner.
OLD_CM_CORPUS = {
    "next_id": 2, "folded_meetings": [M1],
    "items": [{"id": "CM-001", "text": "Old-style commitment", "speaker": "Alice_Example",
               "status": "done", "merged_into": "",
               "occurrences": [{"meeting": M1, "line": 5}]}],
}


def make_workspace(root: Path) -> Path:
    ws = root / "ws"
    (ws / M1).mkdir(parents=True)
    (ws / M2).mkdir(parents=True)
    (ws / M1 / "meeting.speakers.txt").write_text(M1_TRANSCRIPT)
    (ws / M1 / "action-items.md").write_text(M1_ACTION_ITEMS)
    (ws / M2 / "notes.speakers.txt").write_text(M2_TRANSCRIPT)
    (ws / M2 / "notes.diarization.json").write_text(json.dumps(SIDECAR))
    (ws / M2 / "action-items.md").write_text(M2_ACTION_ITEMS)
    (ws / "_workspace.json").write_text(json.dumps(MANIFEST, indent=2))
    (ws / "_action-items.json").write_text(json.dumps(CORPUS, indent=2))
    (ws / "_commitments.json").write_text(json.dumps(CM_CORPUS, indent=2))
    (ws / "_ACTION-ITEMS.md").write_text(CORPUS_MD)
    (ws / "_ignored").mkdir()                      # underscore folders are never meetings
    (ws / "_ignored" / "x.speakers.txt").write_text("[00:00:01] Nobody: skip me\n")
    return ws


def make_seg(ws: Path) -> int:
    """Create the FTS5 seg table the way lib/search.py does and fill it from the
    workspace's *.speakers.txt files. Returns the row count."""
    c = sqlite3.connect(wsconfig.search_db(ws))
    c.execute("CREATE VIRTUAL TABLE seg USING fts5(meeting, source, speaker, t_sec, t_str, line, text)")
    n = 0
    for folder, files in wsconfig.iter_meetings(ws):
        for f in files:
            for t in wsconfig.parse_turns(f.read_text()):
                c.execute("INSERT INTO seg VALUES (?,?,?,?,?,?,?)",
                          (folder, f.name, t.speaker, t.t_sec, t.t_str, t.line, t.text))
                n += 1
    c.commit()
    c.close()
    return n


# ---- helpers ------------------------------------------------------------------------------

def base_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WHOSAID_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run(*argv: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GRAPH), *argv], capture_output=True, text=True,
                          env=env or base_env(), cwd=str(cwd) if cwd else None)


def run_json(*argv: str, env: dict | None = None):
    p = run(*argv, env=env)
    check(p.returncode == 0, f"{argv} exited {p.returncode}: {p.stderr}")
    return json.loads(p.stdout)


def dump(ws: Path) -> dict[str, list]:
    c = sqlite3.connect(f"file:{wsconfig.search_db(ws)}?mode=ro", uri=True)
    out = {}
    for t in graph.TABLES:
        out[t] = c.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall()
    c.close()
    return out


def rows(ws: Path, sql: str, *params) -> list[tuple]:
    c = sqlite3.connect(f"file:{wsconfig.search_db(ws)}?mode=ro", uri=True)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


# ---- sections -----------------------------------------------------------------------------

def test_unit_parsing() -> None:
    check(graph.DATE_DIR_RE.pattern == workspace.DATE_DIR_RE.pattern,
          "graph.DATE_DIR_RE drifted from workspace.DATE_DIR_RE")
    check(graph.fmt_t(721) == "12:01" and graph.fmt_t(3861) == "1:04:21",
          "fmt_t renders MM:SS and H:MM:SS")
    check(graph.fmt_t(None) == "" and graph.fmt_t(0) == "00:00" and graph.fmt_t(3600) == "1:00:00",
          "fmt_t edge cases (no time, zero, exactly one hour)")
    check(graph.iso_utc("2026-09-03T15:00:00.000000Z") == "2026-09-03T15:00:00Z", "iso_utc normalizes")
    check(graph.iso_utc("") is None and graph.iso_utc("garbage") == "garbage", "iso_utc passthrough")
    check(graph.minutes_of(1500.0) == 25 and graph.minutes_of(None) is None, "minutes_of")
    check(graph.name_match("Alice_Example", "alice") and graph.name_match("Alice", "Alice_Example")
          and not graph.name_match("Bob_Example", "Alice"), "name_match")
    deduped = graph.dedupe_commitments([
        {"id": "CM-001", "owner": "A", "requester": "B", "requester_role": "", "text": "t",
         "cue": "", "negative": False, "status": "open", "ai_refs": ["AI-001"], "merged_into": "",
         "source": "transcript", "meeting": "m1", "line": 1, "t_sec": 30},
        {"id": "CM-001", "owner": "A", "requester": "B", "requester_role": "", "text": "t",
         "cue": "", "negative": False, "status": "open", "ai_refs": ["AI-001"], "merged_into": "",
         "source": "action-items", "meeting": "m2", "line": 9, "t_sec": None},
    ])
    check(deduped == [{"id": "CM-001", "owner": "A", "requester": "B", "requester_role": "",
                       "text": "t", "cue": "", "negative": False, "status": "open",
                       "ai_refs": ["AI-001"], "merged_into": "", "occ": 2,
                       "sources": ["action-items", "transcript"], "meetings": ["m1", "m2"],
                       "at": [["m1", 30], ["m2", None]]}],
          f"dedupe_commitments collapses to one entry per id: {deduped}")


def test_exit_paths(root: Path) -> None:
    empty = root / "empty"
    empty.mkdir()
    p = run("build", str(empty))
    check(p.returncode == 1 and "whosaid index" in p.stderr, f"build without _search.db -> exit 1 hint: {p.stderr}")
    c = sqlite3.connect(wsconfig.search_db(empty))
    c.execute("CREATE TABLE meta(k TEXT, v TEXT)")
    c.commit()
    c.close()
    p = run("build", str(empty))
    check(p.returncode == 1 and "seg" in p.stderr and "whosaid index" in p.stderr,
          f"build without seg -> exit 1 hint: {p.stderr}")
    for view in (["items"], ["commitments"], ["prs"], ["meetings"], ["person"],
                 ["item", "AI-001"], ["wiki", "--stdout"]):
        p = run(view[0], str(empty), *view[1:])
        check(p.returncode == 1 and "whosaid index" in p.stderr, f"{view} without graph tables -> exit 1")
    p = run("item", str(empty), "bogus")
    check(p.returncode == 2, "malformed item id -> exit 2")
    p = run("build", env={k: v for k, v in base_env().items() if k != "WHOSAID_WORKSPACE"},
            cwd=root)
    check(p.returncode != 0 and "no workspace" in p.stderr, "no ws, no env, cwd not a workspace -> error")


def test_empty_corpus(root: Path) -> None:
    ws = root / "bare"
    ws.mkdir()
    c = sqlite3.connect(wsconfig.search_db(ws))
    c.execute("CREATE VIRTUAL TABLE seg USING fts5(meeting, source, speaker, t_sec, t_str, line, text)")
    c.commit()
    c.close()
    p = run("build", str(ws))
    check(p.returncode == 0, f"empty build: {p.stderr}")
    check(p.stdout.strip() == "built graph: 0 person · 0 meeting · 0 action_item · 0 occurrence "
                              "· 0 commitment · 0 pr_mention", f"empty summary: {p.stdout!r}")
    check("commitment table is empty" in p.stderr, f"missing corpus logs one line: {p.stderr}")
    p = run("wiki", str(ws), "--stdout")
    check(p.returncode == 0, f"empty wiki: {p.stderr}")
    for needle in ("# Workspace wiki (generated)", "_No meetings indexed yet._", "_No action items yet._",
                   "_No named speakers yet", "_No PR references yet._", "0 segments · 0 meetings"):
        check(needle in p.stdout, f"empty wiki lacks {needle!r}")
    check(run_json("items", str(ws), "--json") == [], "empty items")
    check(run_json("commitments", str(ws), "--json") == [], "empty commitments (missing corpus)")
    check(run_json("person", str(ws), "--json") == [], "empty people")
    check(run_json("prs", str(ws), "--json") == [], "empty prs")
    check(run_json("meetings", str(ws), "--json") == [], "empty meetings")


def test_build(ws: Path, n_seg: int) -> None:
    p = run("build", str(ws))
    check(p.returncode == 0, f"build failed: {p.stderr}")
    check(p.stdout.strip() == "built graph: 4 person · 2 meeting · 3 action_item · 3 occurrence "
                              "· 10 commitment · 5 pr_mention", f"summary: {p.stdout!r}")
    check(n_seg == 11, f"fixture should index 11 turns, got {n_seg}")

    people = rows(ws, "SELECT name, turns, meetings, named FROM person ORDER BY turns DESC, name")
    check(people == [("Alice_Example", 5, 2, 1), ("Bob_Example", 4, 1, 1),
                     ("Carol_Example", 1, 1, 1), ("SPEAKER_02", 1, 1, 0)], f"person: {people}")

    meetings = rows(ws, "SELECT * FROM meeting ORDER BY folder")
    check(meetings == [(M1, "meeting.m4a", "2026-09-01T14:00:00Z", 1800.0, 30, 1, 1, 8),
                       (M2, "notes.m4a", "2026-09-03T15:00:00Z", 1500.0, 25, 0, 1, 3)],
          f"meeting: {meetings}")

    items = rows(ws, "SELECT * FROM action_item ORDER BY id")
    check(items == [("AI-001", "Ship the schema fix", "Alice_Example", "open", "leadership ask", M1, M1, "", 1),
                    ("AI-002", "Ship the schema fix today", "Alice_Example", "merged", "", M1, M1, "AI-001", 1),
                    ("AI-003", "Write the design note for the registry", "Alice_Example", "contingent",
                     "leadership ask", M1, M1, "", 1)], f"action_item: {items}")

    occ = rows(ws, "SELECT * FROM occurrence ORDER BY id, meeting, line")
    check(occ == [("AI-001", M1, 5), ("AI-002", M1, 5), ("AI-003", M1, 6)], f"occurrence: {occ}")

    # the commitment table is the CM corpus flattened: one row per occurrence,
    # item-level status/ai_refs/merged_into stamped onto every sighting
    cms = rows(ws, "SELECT * FROM commitment ORDER BY id, meeting, t_sec IS NULL, t_sec, line")
    ref12 = '["AI-001", "AI-002"]'
    check(cms == [
        ("CM-001", "action-items", M1, 5, 721, "Alice_Example", "Bob_Example", "",
         "Ship the schema fix", "", 0, "open", ref12, ""),
        ("CM-001", "transcript", M1, 6, 740, "Alice_Example", "Bob_Example", "peer",
         "yes, I will ship it after lunch.", "will", 0, "open", ref12, ""),
        ("CM-002", "action-items", M1, 6, 870, "Alice_Example", "Bob_Example", "",
         "Write the design note for the registry", "", 0, "open", '["AI-003"]', ""),
        ("CM-002", "action-items", M1, 6, 910, "Alice_Example", "Bob_Example", "",
         "Write the design note for the registry", "", 0, "open", '["AI-003"]', ""),
        ("CM-003", "action-items", M1, 10, 1200, "Alice_Example", "", "",
         "Review the open pull requests", "will", 0, "ongoing", "[]", ""),
        ("CM-004", "action-items", M1, 11, None, "Alice_Example", "", "",
         "Read the onboarding docs", "inferred", 0, "open", "[]", ""),
        ("CM-005", "transcript", M2, 5, 150, "Alice_Example", "Carol_Example", "boss",
         "I will get onboarded to the repos first.", "will", 0, "done", "[]", ""),
        ("CM-005", "action-items", M2, 5, 3861, "Alice_Example", "Carol_Example", "",
         "Get onboarded to the repos.", "", 0, "done", "[]", ""),
        ("CM-006", "action-items", M2, 6, 941, "Alice_Example", "Carol_Example", "",
         "Apply the merge test", "", 0, "merged", "[]", "CM-005"),
        ("CM-006", "action-items", M2, 6, 1129, "Alice_Example", "Carol_Example", "",
         "Apply the merge test", "before", 1, "merged", "[]", "CM-005"),
    ], f"commitment: {cms}")

    prs = rows(ws, "SELECT pr, source, meeting, detail FROM pr_mention ORDER BY pr, source, meeting, detail")
    check(prs == [
        (42, "doc", "(corpus)", "_ACTION-ITEMS.md"),
        (42, "doc", M1, "action-items.md"),
        (42, "spoken", M1, "@00:20:00: I will review the open pull requests, starting with 42 pr."),
        (42, "spoken", M1, "@00:21:00: pr 42 is the schema one."),
        (101, "doc", M2, "action-items.md"),
    ], f"pr_mention: {prs}")


def test_corpus_shapes(root: Path) -> None:
    """Pre-#23 (meeting+line-only occurrences) and missing/unreadable corpora
    must load or degrade cleanly, never fail the build."""
    ws = make_workspace(root / "shapes")
    make_seg(ws)
    (ws / "_commitments.json").write_text(json.dumps(OLD_CM_CORPUS, indent=2))
    p = run("build", str(ws))
    check(p.returncode == 0 and "1 commitment" in p.stdout, f"old-shape corpus builds: {p.stdout}{p.stderr}")
    cms = rows(ws, "SELECT * FROM commitment")
    check(cms == [("CM-001", "", M1, 5, None, "", "", "", "Old-style commitment",
                   "", 0, "done", "[]", "")],
          f"old-shape row is occurrence defaults with the item's text/status: {cms}")

    (ws / "_commitments.json").write_text("{ not json")
    p = run("build", str(ws))
    check(p.returncode == 0 and "0 commitment" in p.stdout and "unreadable" in p.stderr,
          f"unreadable corpus -> empty table + warn: {p.stdout}{p.stderr}")

    (ws / "_commitments.json").unlink()
    p = run("build", str(ws))
    check(p.returncode == 0 and "0 commitment" in p.stdout
          and "commitment table is empty" in p.stderr,
          f"missing corpus -> empty table + one log line: {p.stdout}{p.stderr}")
    check(run_json("commitments", str(ws), "--json") == [], "commitments view on an empty table")


def test_rebuild_with_owner(root: Path) -> None:
    """The [workspace].owner config fed the old md re-parse; the unified table
    takes owners from the corpus, so a different toml owner must not rewrite
    them, and rebuilds stay idempotent."""
    ws = make_workspace(root / "rebuild")
    make_seg(ws)
    check(run("build", str(ws)).returncode == 0, "first build")
    (ws / "whosaid.toml").write_text('[workspace]\nowner = "Bob_Example"\n')
    p = run("build", str(ws))
    check(p.returncode == 0, f"rebuild failed: {p.stderr}")
    first = dump(ws)
    check(len(first["commitment"]) == 10, "rebuild keeps 10 commitment occurrences (no duplicates)")
    owners = rows(ws, "SELECT DISTINCT owner FROM commitment")
    check(owners == [("Alice_Example",)], f"owners come from the corpus, not the toml: {owners}")
    p = run("build", str(ws))
    check(p.returncode == 0 and dump(ws) == first, "third build is byte-identical (idempotent)")


def test_busy_wait(ws: Path) -> None:
    """A writer holding the database for a moment must not make build fail."""
    held = threading.Event()

    def hold() -> None:
        c = sqlite3.connect(wsconfig.search_db(ws), isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(1.0)
        c.execute("COMMIT")
        c.close()

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    started = time.monotonic()
    p = run("build", str(ws))
    t.join()
    check(p.returncode == 0, f"build under a held write lock: {p.stderr}")
    check(time.monotonic() - started >= 0.5, "build waited for the lock instead of failing fast")


def test_commitments_view(ws: Path) -> None:
    """The new subcommand: the merged table with --owner/--source/--status filters."""
    cms = run_json("commitments", str(ws), "--json")
    check(len(cms) == 10 and [c["id"] for c in cms] == sorted(c["id"] for c in cms),
          "commitments lists every occurrence, id order")
    check(cms[0] == {"id": "CM-001", "source": "action-items", "meeting": M1, "line": 5,
                     "t_sec": 721, "owner": "Alice_Example", "requester": "Bob_Example",
                     "requester_role": "", "text": "Ship the schema fix", "cue": "",
                     "negative": False, "status": "open", "ai_refs": ["AI-001", "AI-002"],
                     "merged_into": ""}, f"commitments[0] shape: {cms[0]}")
    check(cms[1]["source"] == "transcript" and cms[1]["requester_role"] == "peer",
          "transcript occurrence carries its role")
    check(cms[9]["negative"] is True and cms[9]["merged_into"] == "CM-005",
          "negative + merged_into survive the round trip")

    p = run("commitments", str(ws))
    check(p.returncode == 0 and "CM-001" in p.stdout and f"{M1}@12:01" in p.stdout
          and "Alice_Example" in p.stdout and p.stdout.rstrip().endswith("10 commitment occurrence(s)."),
          f"commitments human: {p.stdout}")

    bob = run_json("commitments", str(ws), "--owner", "alice_example", "--json")
    check([c["id"] for c in bob] == ["CM-001"] * 2 + ["CM-002"] * 2 + ["CM-003", "CM-004"]
          + ["CM-005"] * 2 + ["CM-006"] * 2, "--owner is a name_match substring")
    check(run_json("commitments", str(ws), "--owner", "nobody", "--json") == [], "--owner with no hits")

    tr = run_json("commitments", str(ws), "--source", "transcript", "--json")
    check([(c["id"], c["t_sec"]) for c in tr] == [("CM-001", 740), ("CM-005", 150)],
          f"--source transcript: {tr}")
    ai = run_json("commitments", str(ws), "--source", "action-items", "--json")
    check(len(ai) == 8 and all(c["source"] == "action-items" for c in ai), "--source action-items")
    check(len(run_json("commitments", str(ws), "--source", "action_items", "--json")) == 8,
          "--source treats '_' and '-' as the same")
    check(run_json("commitments", str(ws), "--source", "email", "--json") == [], "--source with no hits")

    merged = run_json("commitments", str(ws), "--status", "MERGED", "--json")
    check([c["id"] for c in merged] == ["CM-006", "CM-006"], "--status is a case-insensitive substring")
    check(len(run_json("commitments", str(ws), "--owner", "alice", "--source", "transcript",
                       "--status", "open", "--json")) == 1, "filters stack")


def test_views(ws: Path) -> None:
    items = run_json("items", str(ws), "--json")
    check([it["id"] for it in items] == ["AI-001", "AI-002", "AI-003"], "items order")
    check(items[0] == {"id": "AI-001", "text": "Ship the schema fix", "owner": "Alice_Example",
                       "status": "open", "type": "leadership ask", "first_seen": M1, "last_seen": M1,
                       "span": M1, "merged_into": "", "occurrences": 1, "requesters": ["Bob_Example"]},
          f"items[0] shape: {items[0]}")
    check(items[2]["requesters"] == ["Bob_Example"], "AI-003 linked to Bob through the occurrence line")
    check([it["id"] for it in run_json("items", str(ws), "--status", "MERGED", "--json")] == ["AI-002"],
          "--status is a case-insensitive substring")
    check(len(run_json("items", str(ws), "--owner", "alice", "--json")) == 3, "--owner")
    check([it["id"] for it in run_json("items", str(ws), "--type", "leadership", "--json")]
          == ["AI-001", "AI-003"], "--type")
    check([it["id"] for it in run_json("items", str(ws), "--requester", "bob", "--json")]
          == ["AI-001", "AI-002", "AI-003"], "--requester")
    check(run_json("items", str(ws), "--requester", "carol", "--json") == [], "--requester with no items")
    p = run("items", str(ws), "--status", "open")
    check(p.returncode == 0 and p.stdout.rstrip().endswith("1 item(s).") and "AI-001" in p.stdout,
          f"items human: {p.stdout}")

    d = run_json("item", str(ws), "AI-003", "--json")
    check(d["status"] == "contingent" and d["occurrences"] == [{"meeting": M1, "line": 6}], "item occurrences")
    check([(c["id"], c["t_sec"], c["source"]) for c in d["commitments"]]
          == [("CM-002", 870, "action-items"), ("CM-002", 910, "action-items")],
          f"item commitments: {d}")
    p = run("item", str(ws), "AI-001")
    check(p.returncode == 0 and "CM-001 [open] [" + M1 + " @ 12:01] Alice_Example:" in p.stdout
          and "asked by Bob_Example" in p.stdout and f"{M1}:5" in p.stdout, f"item human: {p.stdout}")
    p = run("item", str(ws), "AI-999")
    check(p.returncode == 1 and "AI-999" in p.stderr, "unknown id -> exit 1")

    people = run_json("person", str(ws), "--json")
    check(people == [{"name": "Alice_Example", "turns": 5, "meetings": 2, "named": True},
                     {"name": "Bob_Example", "turns": 4, "meetings": 1, "named": True},
                     {"name": "Carol_Example", "turns": 1, "meetings": 1, "named": True},
                     {"name": "SPEAKER_02", "turns": 1, "meetings": 1, "named": False}], f"people: {people}")
    check(run_json("speakers", str(ws), "--json") == people, "speakers alias")
    p = run("person", str(ws))
    check("(unlabeled cluster)" in p.stdout and "Alice_Example" in p.stdout, "person human marker")

    bob = run_json("person", str(ws), "Bob", "--json")
    check([p["name"] for p in bob["people"]] == ["Bob_Example"], "person Bob matched")
    check([e["id"] for e in bob["requested"]] == ["CM-001", "CM-002"],
          f"Bob requested, deduped per CM id: {bob['requested']}")
    check(bob["requested"][0]["occ"] == 2 and bob["requested"][0]["sources"] == ["action-items", "transcript"],
          "a repeated commitment keeps its occurrence count and sources")
    check(bob["requested"][0]["at"] == [[M1, 721], [M1, 740]], "deduped entry lists meeting+time pairs")
    check([it["id"] for it in bob["requested_items"]] == ["AI-001", "AI-002", "AI-003"], "Bob requested items")
    check(bob["owned"] == [] and bob["owned_items"] == [], "Bob owns nothing")

    alice = run_json("person", str(ws), "alice_example", "--json")
    check(alice["requested"] == [] and alice["requested_items"] == [], "Alice made no asks of others")
    check([e["id"] for e in alice["owned"]] == ["CM-005", "CM-006", "CM-001", "CM-002", "CM-003", "CM-004"],
          f"Alice owns every CM id, newest meeting first: {[e['id'] for e in alice['owned']]}")
    check(alice["owned"][0]["requester_role"] == "boss" and alice["owned"][0]["status"] == "done",
          "entry fields come from the newest occurrence's row")
    check([it["id"] for it in alice["owned_items"]] == ["AI-001", "AI-002", "AI-003"], "Alice owned items")
    p = run("person", str(ws), "Alice")
    check(p.returncode == 0 and "Owned by Alice_Example (what they signed up for): 6 commitment(s) "
          "across 2 meeting(s)" in p.stdout and "CM-001 [open] (2x)" in p.stdout
          and f"{M1}@12:01 · {M1}@12:20" in p.stdout and "(asked by Bob_Example)" in p.stdout
          and "(asked by Carol_Example)" in p.stdout and "AI-001 [open]" in p.stdout
          and f"{M2}@1:04:21" in p.stdout, f"person human: {p.stdout}")

    carol = run_json("person", str(ws), "Carol", "--json")
    check([e["id"] for e in carol["requested"]] == ["CM-005", "CM-006"] and carol["owned"] == [],
          f"Carol requested the planning commitments: {carol}")
    p = run("person", str(ws), "Nobody_Example", "--json")
    check(p.returncode == 1 and json.loads(p.stdout)["people"] == [], "no match -> exit 1, empty JSON")

    env = dict(base_env(), WHOSAID_WORKSPACE=str(ws))
    check(run_json("person", "Bob", "--json", env=env) == bob, "person NAME with the workspace from env")
    check(run_json("items", "--json", env=env) == items, "items with the workspace from env")

    prs = run_json("prs", str(ws), "--json")
    check(prs == [{"pr": 42, "refs": 4, "spoken": 2, "doc": 2, "meetings": ["(corpus)", M1]},
                  {"pr": 101, "refs": 1, "spoken": 0, "doc": 1, "meetings": [M2]}], f"prs: {prs}")
    p = run("prs", str(ws))
    check("PR #42" in p.stdout and "2 spoken" in p.stdout and "docs only" in p.stdout, f"prs human: {p.stdout}")

    meetings = run_json("meetings", str(ws), "--json")
    check(meetings == [
        {"folder": M1, "source_name": "meeting.m4a", "created": "2026-09-01T14:00:00Z", "duration_s": 1800.0,
         "minutes": 30, "dated": True, "has_action_items": True, "segments": 8},
        {"folder": M2, "source_name": "notes.m4a", "created": "2026-09-03T15:00:00Z", "duration_s": 1500.0,
         "minutes": 25, "dated": False, "has_action_items": True, "segments": 3},
    ], f"meetings: {meetings}")
    p = run("meetings", str(ws))
    check("hand-named" in p.stdout and "dated" in p.stdout and "30m" in p.stdout, f"meetings human: {p.stdout}")


def test_wiki(ws: Path, root: Path) -> None:
    p = run("wiki", str(ws), "--stdout")
    check(p.returncode == 0, f"wiki: {p.stderr}")
    md = p.stdout
    for needle in (
        "# Workspace wiki (generated)",
        "Do not hand-edit",
        "`whosaid index`",
        "**Corpus:** 11 segments · 2 meetings · 4 speakers · 3 action items · 10 commitment occurrences · 5 PR refs.",
        f"| `{M1}` | 2026-09-01T14:00:00Z | 30m | yes | 8 | yes |",
        f"| `{M2}` | 2026-09-03T15:00:00Z | 25m | no | 3 | yes |",
        "### Open (1)", "### Contingent (1)", "### Merged (1)",
        "- **AI-001** (leadership ask, owner: Alice_Example, asked by: Bob_Example, " + M1 + ") Ship the schema fix",
        f"    cited: `CM-001 {M1}@12:01`  `CM-001 {M1}@12:20`",
        f"    cited: `CM-002 {M1}@14:30`  `CM-002 {M1}@15:10`",
        "- **AI-002** (-, owner: Alice_Example, asked by: Bob_Example, merged into AI-001) Ship the schema fix today",
        "| Alice_Example | 5 | 2 | - |",
        "| Bob_Example | 4 | 1 | AI-001, AI-002, AI-003 |",
        "| Carol_Example | 1 | 1 | - |",
        f"| #42 | 4 | 2 | (corpus), {M1} |",
        f"| #101 | 1 | 0 | {M2} |",
    ):
        check(needle in md, f"wiki lacks {needle!r}\n{md}")
    check("SPEAKER_02" not in md.split("## Speakers")[1], "unlabeled clusters stay out of the speakers table")
    check(md.index("### Open") < md.index("### Contingent") < md.index("### Merged"), "status group order")
    check("—" not in md, "wiki has no em dash")

    p = run("wiki", str(ws))
    check(p.returncode == 0 and (ws / "_WIKI.md").is_file() and "_WIKI.md" in p.stderr, "wiki default path")
    body = (ws / "_WIKI.md").read_text()
    check(body.split("\n", 2)[0] == md.split("\n", 2)[0] and "### Open (1)" in body, "written wiki matches")
    out = root / "custom" / "wiki.md"
    p = run("wiki", str(ws), "-o", str(out))
    check(p.returncode == 0 and out.is_file() and "### Merged (1)" in out.read_text(), "wiki -o FILE")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="whosaid-graph-test-"))
    ok = False
    try:
        test_unit_parsing()
        test_exit_paths(root)
        test_empty_corpus(root)
        test_corpus_shapes(root)
        ws = make_workspace(root)
        n_seg = make_seg(ws)
        test_build(ws, n_seg)
        test_rebuild_with_owner(root)
        test_busy_wait(ws)
        test_commitments_view(ws)
        test_views(ws)
        test_wiki(ws, root)
        ok = True
    finally:
        if ok:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"FAIL: {CHECKS} check(s) passed before the failure; fixture left at {root}", file=sys.stderr)
    print(f"PASS: graph_test ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
