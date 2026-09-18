#!/usr/bin/env python3
"""
Offline test for lib/graph.py (GitHub issue #14: entity graph + generated wiki;
issue #13: the per-owner commitments view).

Builds a synthetic workspace in a temp dir: one dated meeting folder (in the
manifest) and one hand-named folder (manifest-less, with a diarization sidecar),
speaker-labeled transcripts with placeholder names, a small action-item corpus
(open / merged / contingent), and per-meeting action-items.md files covering
every accepted commitment bullet shape. The FTS5 `seg` table is created here
directly (same columns lib/search.py writes), so this test does not depend on
the search module. Then it runs the real CLI (`python3 lib/graph.py ...`) and
asserts every table, every view's JSON, the wiki markdown, the exit-1 paths,
build idempotence, and that build waits on a busy database.

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
    check(graph.parse_bracket("Bob 46:36–47:35", strict=True) == ("Bob", ["46:36", "47:35"]),
          "legacy range bracket takes both ends")
    check(graph.parse_bracket("Bob ~47–48 / 52:xx", strict=True) == ("Bob", [""]),
          "legacy bracket with no parsable time yields one no-time row")
    check(graph.parse_bracket("Bob, implicit", strict=True) == ("Bob", [""]), "name before comma")
    check(graph.parse_bracket("implicit", strict=True) == ("", [""]), "lowercase note is not a name")
    check(graph.parse_bracket("SPEAKER_06 13:46", strict=True) == ("SPEAKER_06", ["13:46"]),
          "SPEAKER_NN counts as a name")
    check(graph.parse_bracket("Bob 18:57 / 20:00 / 20:57", strict=True)[1] == ["18:57", "20:00", "20:57"],
          "slash-separated times")
    check(graph.parse_bracket("inferred") == ("", ["inferred"]), "[inferred]")
    check(graph.parse_bracket("00:20:00") == ("", ["00:20:00"]), "bare time")
    cms = graph.parse_commitments(M2_ACTION_ITEMS, M2, "")
    check([c["owner"] for c in cms] == [""] * 8, "legacy owner is '' without a configured owner")
    cms = graph.parse_commitments(M2_ACTION_ITEMS, M2, "Alice_Example")
    check(len(cms) == 8, f"expected 8 legacy rows, got {len(cms)}")
    check(all(c["owner"] == "Alice_Example" for c in cms), "legacy owner is the configured owner")
    check([c["requester"] for c in cms][:6] == ["Carol_Example"] * 6, "legacy bracket name is the requester")
    check(cms[6]["requester"] == "Alice_Example" and cms[6]["t_str"] == "0:41:00",
          "legacy bare-time bullet: requester = owner")
    check(cms[7]["requester"] == "SPEAKER_02", "legacy SPEAKER_NN requester")
    check(cms[5]["t_str"] == "" and cms[5]["t_sec"] is None and cms[5]["line"] == 8, "no-time legacy row")
    check(cms[0]["text"] == "Get onboarded to the repos." and cms[0]["t_sec"] == 3861, "legacy title + t_sec")
    check(graph.iso_utc("2026-09-03T15:00:00.000000Z") == "2026-09-03T15:00:00Z", "iso_utc normalizes")
    check(graph.iso_utc("") is None and graph.iso_utc("garbage") == "garbage", "iso_utc passthrough")
    check(graph.minutes_of(1500.0) == 25 and graph.minutes_of(None) is None, "minutes_of")
    check(graph.name_match("Alice_Example", "alice") and graph.name_match("Alice", "Alice_Example")
          and not graph.name_match("Bob_Example", "Alice"), "name_match")


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
    for view in (["items"], ["prs"], ["meetings"], ["person"], ["item", "AI-001"], ["wiki", "--stdout"]):
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
    p = run("wiki", str(ws), "--stdout")
    check(p.returncode == 0, f"empty wiki: {p.stderr}")
    for needle in ("# Workspace wiki (generated)", "_No meetings indexed yet._", "_No action items yet._",
                   "_No named speakers yet", "_No PR references yet._", "0 segments · 0 meetings"):
        check(needle in p.stdout, f"empty wiki lacks {needle!r}")
    check(run_json("items", str(ws), "--json") == [], "empty items")
    check(run_json("person", str(ws), "--json") == [], "empty people")
    check(run_json("prs", str(ws), "--json") == [], "empty prs")
    check(run_json("meetings", str(ws), "--json") == [], "empty meetings")


def test_build(ws: Path, n_seg: int) -> None:
    p = run("build", str(ws))
    check(p.returncode == 0, f"build failed: {p.stderr}")
    check(p.stdout.strip() == "built graph: 4 person · 2 meeting · 3 action_item · 3 occurrence "
                              "· 13 commitment · 5 pr_mention", f"summary: {p.stdout!r}")
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

    cms = rows(ws, "SELECT meeting, line, owner, requester, t_sec, t_str, text, ai_refs "
                   "FROM commitment WHERE meeting=? ORDER BY line, t_sec", M1)
    check(cms == [
        (M1, 5, "Alice_Example", "Bob_Example", 721, "00:12:01",
         'Ship the schema fix (AI-001). "we need that fix" PR #42', "AI-001,AI-002"),
        (M1, 6, "Alice_Example", "Bob_Example", 870, "00:14:30", "Write the design note for the registry", "AI-003"),
        (M1, 6, "Alice_Example", "Bob_Example", 910, "00:15:10", "Write the design note for the registry", "AI-003"),
        (M1, 10, "Alice_Example", "Alice_Example", 1200, "00:20:00", "Review the open pull requests", ""),
        (M1, 11, "Alice_Example", "Alice_Example", None, "inferred", "Read the onboarding docs", ""),
    ], f"commitment ({M1}): {cms}")
    cms2 = rows(ws, "SELECT line, owner, requester, t_sec, t_str, text FROM commitment WHERE meeting=? "
                    "ORDER BY line, t_sec", M2)
    check(cms2 == [
        (5, "", "Carol_Example", 3861, "1:04:21", "Get onboarded to the repos."),
        (6, "", "Carol_Example", 941, "15:41", "Apply the merge test"),
        (6, "", "Carol_Example", 1129, "18:49", "Apply the merge test"),
        (7, "", "Carol_Example", 2796, "46:36", "Draft the pillars design"),
        (7, "", "Carol_Example", 2855, "47:35", "Draft the pillars design"),
        (8, "", "Carol_Example", None, "", "Bring the design doc"),
        (12, "", "", 2460, "0:41:00", "Move standup to 7am"),
        (13, "", "SPEAKER_02", 826, "13:46", '"I have a PR open to fix that"'),
    ], f"commitment ({M2}, no configured owner): {cms2}")

    prs = rows(ws, "SELECT pr, source, meeting, detail FROM pr_mention ORDER BY pr, source, meeting, detail")
    check(prs == [
        (42, "doc", "(corpus)", "_ACTION-ITEMS.md"),
        (42, "doc", M1, "action-items.md"),
        (42, "spoken", M1, "@00:20:00: I will review the open pull requests, starting with 42 pr."),
        (42, "spoken", M1, "@00:21:00: pr 42 is the schema one."),
        (101, "doc", M2, "action-items.md"),
    ], f"pr_mention: {prs}")


def test_rebuild_with_owner(ws: Path) -> None:
    (ws / "whosaid.toml").write_text('[workspace]\nowner = "Alice_Example"\n')
    p = run("build", str(ws))
    check(p.returncode == 0, f"rebuild failed: {p.stderr}")
    first = dump(ws)
    check(len(first["commitment"]) == 13, "rebuild keeps 13 commitments (no duplicates)")
    owners = rows(ws, "SELECT DISTINCT owner FROM commitment")
    check(owners == [("Alice_Example",)], f"configured owner applied to legacy rows: {owners}")
    self_row = rows(ws, "SELECT requester FROM commitment WHERE meeting=? AND line=12", M2)
    check(self_row == [("Alice_Example",)], "legacy bare-time bullet: requester = configured owner")
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
    check([(c["t_str"], c["requester"], c["line"]) for c in d["commitments"]]
          == [("00:14:30", "Bob_Example", 6), ("00:15:10", "Bob_Example", 6)], f"item commitments: {d}")
    p = run("item", str(ws), "AI-001")
    check(p.returncode == 0 and f"[{M1} @ 00:12:01] Alice_Example:" in p.stdout
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
    check([(c["t_str"], c["line"]) for c in bob["requested"]] == [("00:12:01", 5), ("00:14:30", 6), ("00:15:10", 6)],
          f"Bob requested: {bob['requested']}")
    check([it["id"] for it in bob["requested_items"]] == ["AI-001", "AI-002", "AI-003"], "Bob requested items")
    check(bob["owned"] == [] and bob["owned_items"] == [], "Bob owns nothing")

    alice = run_json("person", str(ws), "alice_example", "--json")
    check(alice["requested"] == [] and alice["requested_items"] == [], "Alice made no asks of others")
    check(len(alice["owned"]) == 13, f"Alice owns every commitment: {len(alice['owned'])}")
    check([c["meeting"] for c in alice["owned"]][:8] == [M2] * 8 and alice["owned"][8]["meeting"] == M1,
          "owned grouped newest meeting first (sidecar-dated hand-named folder is newer)")
    check([c["t_str"] for c in alice["owned"]][:8]
          == ["13:46", "15:41", "18:49", "0:41:00", "46:36", "47:35", "1:04:21", ""],
          f"owned times ascending, no-time last: {[c['t_str'] for c in alice['owned']][:8]}")
    check([it["id"] for it in alice["owned_items"]] == ["AI-001", "AI-002", "AI-003"], "Alice owned items")
    p = run("person", str(ws), "Alice")
    check(p.returncode == 0 and "Owned by Alice_Example" in p.stdout and "(asked by Bob_Example)" in p.stdout
          and "(asked by Carol_Example)" in p.stdout and "(no time)" in p.stdout and M2 in p.stdout,
          f"person human: {p.stdout}")

    carol = run_json("person", str(ws), "Carol", "--json")
    check(len(carol["requested"]) == 6 and carol["requested_items"] == [] and carol["owned"] == [],
          f"Carol requested 6 legacy commitments: {carol}")
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
        "**Corpus:** 11 segments · 2 meetings · 4 speakers · 3 action items · 13 timestamped commitments · 5 PR refs.",
        f"| `{M1}` | 2026-09-01T14:00:00Z | 30m | yes | 8 | yes |",
        f"| `{M2}` | 2026-09-03T15:00:00Z | 25m | no | 3 | yes |",
        "### Open (1)", "### Contingent (1)", "### Merged (1)",
        "- **AI-001** (leadership ask, owner: Alice_Example, asked by: Bob_Example, " + M1 + ") Ship the schema fix",
        f"    cited: `{M1}@00:12:01`",
        f"    cited: `{M1}@00:14:30`  `{M1}@00:15:10`",
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
        ws = make_workspace(root)
        n_seg = make_seg(ws)
        test_build(ws, n_seg)
        test_rebuild_with_owner(ws)
        test_busy_wait(ws)
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
