#!/usr/bin/env python3
"""
Offline test for the downstream half of the Teams-chat corpus (sblattj/whosaid#49):
an audio-less meeting folder gets its timestamp from the sidecar's
source.creation_time, the manifest and the graph carry a `kind`, and
`graph.py meetings --kind` filters on it.

Drives the real CLIs (lib/workspace.py rollup, lib/graph.py build/meetings) against a
hand-written Teams folder fixture (placeholder names only). stdlib, no network, no ffprobe.

Run:  python3 test/teams_manifest_test.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))
import workspace  # noqa: E402
import wsconfig  # noqa: E402

TEAMS = "2026-09-24-0905"     # teams-chat folder, sidecar timestamp
BARE = "2026-09-25-1000"      # audio-less, no sidecar -> folder-name fallback
TEAMS_SIDECAR_DAY = "2026-09-24"
CREATED = "2026-09-24T13:05:06Z"
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


SPEAKERS = """\
# Speaker-labeled transcript: teams
# Source: ms-teams-chat (Example Chat), scraped 2026-09-30
# Speakers (2): Peer_Example, Self_Example
# Role: Self_Example = self

[09:05:06] Peer_Example: i got the telemetry for the widget

[09:12:39] Self_Example: opened PR 68 against the service
"""

SIDECAR = {
    "base": "teams",
    "source": {
        "kind": "teams-chat", "platform": "microsoft-teams", "surface": "web",
        "chat_name": "Example Chat", "chat_id": None, "url": None,
        "day": TEAMS_SIDECAR_DAY, "message_count": 2,
        "scraped_at": "2026-09-30T22:30:00Z", "scraper": "teams-dom-scraper@1",
        "creation_time": CREATED,
    },
    "names": {"Example, Peer": "Peer_Example"},
    "roles": {"Self_Example": "self"},
    "segments": [
        {"start": 32706, "end": 32706, "speaker": "Peer_Example", "epoch_ms": 1790255106000,
         "mid": "1790255106000", "text": "i got the telemetry for the widget"},
        {"start": 33159, "end": 33159, "speaker": "Self_Example", "epoch_ms": 1790255559000,
         "mid": "1790255559000", "text": "opened PR 68 against the service"},
    ],
}


def run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True, timeout=120)


def make_seg(ws: Path) -> None:
    """FTS5 seg table the way lib/search.py creates it (graph build requires it)."""
    c = sqlite3.connect(wsconfig.search_db(ws))
    c.execute("CREATE VIRTUAL TABLE seg USING fts5(meeting, source, speaker, t_sec, t_str, line, text)")
    for folder, files in wsconfig.iter_meetings(ws):
        for f in files:
            for t in wsconfig.parse_turns(f.read_text()):
                c.execute("INSERT INTO seg VALUES (?,?,?,?,?,?,?)",
                          (folder, f.name, t.speaker, t.t_sec, t.t_str, t.line, t.text))
    c.commit()
    c.close()


def entry(ws: Path, folder: str) -> dict:
    man = json.loads((ws / "_workspace.json").read_text())
    return next(e for e in man["meetings"] if e["folder"] == folder)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="whosaid-teams-manifest-test-"))
    ok = False
    try:
        ws = root / "ws"
        (ws / TEAMS).mkdir(parents=True)
        (ws / TEAMS / "teams.speakers.txt").write_text(SPEAKERS)
        (ws / TEAMS / "teams.diarization.json").write_text(json.dumps(SIDECAR))
        (ws / BARE).mkdir()
        (ws / BARE / "note.speakers.txt").write_text("[00:00:05] Peer_Example: hello there\n")

        # (a) roll-up: created from source.creation_time, kind == teams-chat
        p = run(str(REPO / "lib" / "workspace.py"), "rollup", str(ws))
        check(p.returncode == 0, f"rollup failed: {p.stderr}")
        e = entry(ws, TEAMS)
        check(e["created"] == CREATED, f"created from sidecar, got {e['created']}")
        check(e["kind"] == "teams-chat", f"kind: {e}")
        check("source_sha256" not in e and "source_name" not in e, f"no audio fields: {e}")
        check(e["has_speakers"] is True and e["has_txt"] is False, f"flags: {e}")

        # (b) audio-less, no sidecar: folder-name fallback, no kind
        b = entry(ws, BARE)
        check(b["created"] == BARE, f"folder-name fallback, got {b['created']}")
        check("kind" not in b, f"no kind for a bare folder: {b}")

        # the audit marks a teams-chat folder as expected text-only, not as missing
        idx = (ws / "_INDEX.md").read_text()
        trow = next(ln for ln in idx.splitlines() if ln.startswith(f"| {TEAMS} |"))
        check("n/a" in trow, f"index shows n/a for the plain transcript: {trow}")
        aud = next(ln for ln in idx.splitlines() if ln.startswith(f"- {TEAMS}:"))
        check("teams-chat" in aud and "NO SOURCE AUDIO" not in aud
              and "transcript .txt" not in aud, f"audit line: {aud}")
        check("action-items.md" in aud, f"teams folder still flags missing action-items: {aud}")
        bud = next(ln for ln in idx.splitlines() if ln.startswith(f"- {BARE}:"))
        check("NO SOURCE AUDIO" in bud and "transcript .txt" in bud, f"bare folder still flagged: {bud}")

        # sidecar without a parseable creation_time -> folder-name fallback (kind still set)
        bad = json.loads(json.dumps(SIDECAR))
        bad["source"]["creation_time"] = "not-a-date"
        (ws / TEAMS / "teams.diarization.json").write_text(json.dumps(bad))
        run(str(REPO / "lib" / "workspace.py"), "rollup", str(ws))
        e2 = entry(ws, TEAMS)
        check(e2["created"] == TEAMS and e2["kind"] == "teams-chat", f"bad creation_time: {e2}")
        (ws / TEAMS / "teams.diarization.json").write_text(json.dumps(SIDECAR))
        run(str(REPO / "lib" / "workspace.py"), "rollup", str(ws))
        check(entry(ws, TEAMS)["created"] == CREATED, "restored sidecar timestamp")

        # (c) an old manifest entry without `kind` still loads
        old = {k: v for k, v in entry(ws, TEAMS).items() if k != "kind"}
        loaded = workspace.manifest_to_meetings({"meetings": [old]})
        check(TEAMS in loaded and loaded[TEAMS].kind is None, f"old manifest entry: {loaded}")
        check(workspace.Meeting(folder="x").kind is None, "Meeting.kind defaults to None")

        # (d) graph: kind visible in `meetings --json`, --kind filters
        make_seg(ws)
        p = run(str(REPO / "lib" / "graph.py"), "build", str(ws))
        check(p.returncode == 0, f"graph build: {p.stderr}")
        rows = json.loads(run(str(REPO / "lib" / "graph.py"), "meetings", str(ws), "--json").stdout)
        kinds = {r["folder"]: r["kind"] for r in rows}
        check(kinds == {TEAMS: "teams-chat", BARE: None}, f"graph kinds: {kinds}")
        created = {r["folder"]: r["created"] for r in rows}
        check(created[TEAMS] == CREATED, f"graph created: {created}")
        only = json.loads(run(str(REPO / "lib" / "graph.py"), "meetings", str(ws),
                              "--json", "--kind", "teams-chat").stdout)
        check([r["folder"] for r in only] == [TEAMS], f"--kind filter: {only}")
        none = json.loads(run(str(REPO / "lib" / "graph.py"), "meetings", str(ws),
                              "--json", "--kind", "audio").stdout)
        check(none == [], f"--kind audio on a chat-only workspace: {none}")
        txt = run(str(REPO / "lib" / "graph.py"), "meetings", str(ws), "--kind", "teams-chat").stdout
        check("teams-chat" in txt and BARE not in txt, f"text view: {txt}")

        # old manifest (no kind) + manifest-less folder: graph derives kind from the sidecar
        man = json.loads((ws / "_workspace.json").read_text())
        for me in man["meetings"]:
            me.pop("kind", None)
        (ws / "_workspace.json").write_text(json.dumps(man))
        run(str(REPO / "lib" / "graph.py"), "build", str(ws))
        rows = json.loads(run(str(REPO / "lib" / "graph.py"), "meetings", str(ws), "--json").stdout)
        check({r["folder"]: r["kind"] for r in rows} == {TEAMS: "teams-chat", BARE: None},
              f"old-manifest graph kinds: {rows}")
        (ws / "_workspace.json").unlink()
        run(str(REPO / "lib" / "graph.py"), "build", str(ws))
        rows = json.loads(run(str(REPO / "lib" / "graph.py"), "meetings", str(ws), "--json").stdout)
        check({r["folder"]: r["kind"] for r in rows} == {TEAMS: "teams-chat", BARE: None},
              f"manifest-less graph kinds: {rows}")
        ok = True
    finally:
        if ok:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"FAIL: {CHECKS} check(s) passed before the failure; fixture left at {root}",
                  file=sys.stderr)
    print(f"PASS: teams_manifest_test ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
