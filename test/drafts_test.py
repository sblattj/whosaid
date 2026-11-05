#!/usr/bin/env python3
"""Offline tests for lib/drafts.py, the drafts outbox (GitHub issue #165)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import drafts  # noqa: E402
import search  # noqa: E402

GOOD_CLAIMS = {
    "claims": [
        {"key": "a", "text": "two records are needed", "kind": "code", "verdict": "confirmed",
         "evidence": "infra/dns.tf:12"},
        {"key": "b", "text": "the provider supports wildcards", "kind": "tech", "verdict": "confirmed",
         "evidence": "https://example.com/docs/wildcards"},
        {"key": "c", "text": "an old plan", "kind": "people", "verdict": "refuted", "dropped": True,
         "evidence": "meeting 2026-01-05 00:07"},
    ],
    "stakeholders": [{"name": "Test Person", "speaker": "Test_Person", "why": "owns dns",
                      "searched": ["dns records"]}],
    "docs": [],
}


class DraftsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="whosaid-drafts-")
        self.root = Path(self.tmp.name)
        self.page = self.root / "page.html"
        self.page.write_text("<html>x</html>")
        self.stager_log = self.root / "stager.log"
        stager = self.root / "fake-stager.py"
        stager.write_text(
            "import json, os, sys\n"
            f"open({str(self.stager_log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], "
            "'lock': os.environ.get('TEAMS_OUTBOX_LOCK')}) + '\\n')\n"
            "print('{\"sent\": false}')\n")
        self.env = patch.dict(os.environ, {
            "WHOSAID_DRAFTS_DIR": str(self.root / "drafts"),
            "WHOSAID_SESSIONS_DIR": str(self.root / "sessions"),
            "WHOSAID_DRAFTS_STAGER": f"{sys.executable} {stager}",
            "CLAUDE_CODE_SESSION_ID": "sess-1111",
        })
        self.env.start()
        (self.root / "sessions").mkdir()
        (self.root / "sessions" / "1.json").write_text(json.dumps(
            {"sessionId": "sess-1111", "name": "worker-a", "pid": os.getpid(), "cwd": "/repo/a"}))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def stager_calls(self):
        if not self.stager_log.exists():
            return []
        return [json.loads(x) for x in self.stager_log.read_text().splitlines()]

    def ready(self, to="Test Person", topic="dns records", **kw):
        r = drafts.new(to, topic, [str(self.page)], **kw)
        drafts.write(r["id"], text="Hi, can you add the two DNS records?", **GOOD_CLAIMS)
        drafts.verify(r["id"])
        return r["id"]


class OutboxTest(DraftsBase):
    def test_owner_recorded_from_session_registry(self):
        m = drafts.load(drafts.new("Test Person", "dns")["id"])
        self.assertEqual(m["owner"]["sessionId"], "sess-1111")
        self.assertEqual(m["owner"]["name"], "worker-a")
        self.assertEqual(m["owner"]["cwd"], "/repo/a")
        self.assertIn("claude --resume sess-1111", m["owner"]["resume"])
        self.assertTrue(drafts.summary(m)["owner_live"])
        self.assertEqual([d["id"] for d in drafts.listing(mine=True)], [m["id"]])

    def test_verify_gates(self):
        did = drafts.new("Test Person", "dns", [str(self.page)])["id"]
        with self.assertRaises(drafts.DraftsError) as e:
            drafts.verify(did)
        for want in ("draft.md missing", "no claims", "stakeholders[] empty"):
            self.assertIn(want, str(e.exception))
        bad = json.loads(json.dumps(GOOD_CLAIMS))
        bad["claims"][1]["evidence"] = "I remember it"
        bad["claims"][2]["dropped"] = False
        bad["claims"][0]["verdict"] = "probably"
        bad["stakeholders"][0]["searched"] = []
        drafts.write(did, text="Hi \u2014 there", **bad)
        with self.assertRaises(drafts.DraftsError) as e:
            drafts.verify(did)
        msg = str(e.exception)
        for want in ("em dash", "verdict must be", "refuted but not marked dropped", "needs a doc URL",
                     "no whosaid topics searched"):
            self.assertIn(want, msg)
        drafts.write(did, text="Hi there", **GOOD_CLAIMS)
        self.assertEqual(drafts.verify(did)["confirmed"], 2)
        self.assertEqual(drafts.state(drafts.load(did)), "verified")

    def test_edit_after_verify_is_stale_and_stage_refuses(self):
        did = self.ready()
        with open(drafts.ddir(did) / "draft.md", "a") as f:
            f.write("one more line\n")
        self.assertEqual(drafts.state(drafts.load(did)), "stale, edited after verify")
        r = drafts.stage([did])
        self.assertEqual(r["staged"], [])
        self.assertIn("re-verify", r["failed"][0]["why"])
        self.assertEqual(self.stager_calls(), [])

    def test_page_edit_makes_it_stale(self):
        did = self.ready()
        self.page.write_text("<html>changed</html>")
        self.assertTrue(drafts.state(drafts.load(did)).startswith("stale"))

    def test_rival_draft_blocks_stage_until_dropped(self):
        did = self.ready()
        other = drafts.new("Test Person", "other")
        self.assertIn("open draft(s) to Test Person", other["note"])
        self.assertIn("other open draft", drafts.stage([did], dry_run=True)["failed"][0]["why"])
        drafts.close(other["id"], "dropped", "dup")
        self.assertEqual(len(drafts.stage([did], dry_run=True)["would_run"]), 1)

    def test_stage_runs_stager_with_lock_then_restage_replaces(self):
        did = self.ready()
        r = drafts.stage([did])
        self.assertEqual([s["id"] for s in r["staged"]], [did])
        call = self.stager_calls()[0]
        self.assertEqual(call["argv"][:2], ["--chat", "Test Person"])
        self.assertIn("--attach", call["argv"])
        self.assertNotIn("--replace", call["argv"])
        self.assertTrue(call["lock"].endswith(".outbox.lock"))
        self.assertFalse((drafts.root() / ".outbox.lock").exists(), "lock released")
        self.assertEqual(drafts.state(drafts.load(did)), "staged")
        self.assertEqual(drafts.stage([did])["skipped"][0]["id"], did)
        drafts.write(did, text="Hi, revised.")
        drafts.verify(did)
        self.assertEqual(drafts.state(drafts.load(did)), "verified, changed since staged")
        drafts.stage([did])
        self.assertIn("--replace", self.stager_calls()[-1]["argv"])
        self.assertIn("--only-these-attachments", self.stager_calls()[-1]["argv"])

    def test_supersedes_marks_old_and_replaces(self):
        old = self.ready()
        drafts.stage([old])
        new = self.ready(topic="dns v2", supersedes=old)
        drafts.stage([new])
        self.assertEqual(drafts.load(old)["status"], "superseded")
        self.assertIn("--replace", self.stager_calls()[-1]["argv"])

    def test_live_lock_refuses_and_dead_lock_is_reclaimed(self):
        did = self.ready()
        lk = drafts.root() / ".outbox.lock"
        lk.mkdir()
        (lk / "owner.json").write_text(json.dumps({"pid": os.getpid(), "at": "now", "what": "test"}))
        with self.assertRaises(drafts.DraftsError):
            drafts.stage([did])
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (lk / "owner.json").write_text(json.dumps({"pid": dead.pid, "at": "then", "what": "old"}))
        self.assertEqual(len(drafts.stage([did])["staged"]), 1)

    def test_stage_all_does_one_per_run(self):
        a = self.ready(to="Person One")
        b = self.ready(to="Person Two")
        r = drafts.stage(all_ready=True)
        self.assertEqual(len(r["staged"]), 1)
        self.assertIn("run stage --all again", r["note"])
        r2 = drafts.stage(all_ready=True)
        self.assertEqual({s["id"] for s in r["staged"] + r2["staged"]}, {a, b})

    def test_no_stager_configured(self):
        did = self.ready()
        with patch.dict(os.environ, {"WHOSAID_DRAFTS_STAGER": ""}):
            with self.assertRaises(drafts.DraftsError) as e:
                drafts.stage([did])
        self.assertIn("WHOSAID_DRAFTS_STAGER", str(e.exception))

    def test_stager_failure_is_recorded_not_staged(self):
        did = self.ready()
        with patch.dict(os.environ, {"WHOSAID_DRAFTS_STAGER": f"{sys.executable} -c 'import sys; sys.exit(4)'"}):
            r = drafts.stage([did])
        self.assertIn("stager exit 4", r["failed"][0]["why"])
        self.assertEqual(drafts.load(did)["status"], "verified")

    def test_bad_ids_refused(self):
        for bad in ("../x", ".outbox.lock", ""):
            with self.assertRaises(drafts.DraftsError):
                drafts.load(bad)

    def test_cli_list_and_show(self):
        did = self.ready()
        lib = Path(__file__).resolve().parents[1] / "lib" / "drafts.py"
        out = subprocess.run([sys.executable, str(lib), "list"], capture_output=True, text=True, env=os.environ)
        self.assertIn(did, out.stdout)
        self.assertIn("worker-a", out.stdout)
        out = subprocess.run([sys.executable, str(lib), "show", did, "--json"], capture_output=True, text=True,
                             env=os.environ)
        self.assertEqual(json.loads(out.stdout)["gate"], [])


class StakeholdersTest(DraftsBase):
    def setUp(self):
        super().setUp()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        (self.ws / "whosaid.toml").write_text("[search]\nembed = false\n")
        today = datetime.now()
        self.meet(today - timedelta(days=400), "Old_Voice", "the dns records migration plan is mine")
        self.meet(today - timedelta(days=3), "Test_Person", "dns records need a wildcard for staging")
        self.meet(today - timedelta(days=2), "SPEAKER_03", "dns records again")
        search.build(self.ws, embed=False)

    def meet(self, when, speaker, text):
        folder = self.ws / when.strftime("%Y-%m-%d-%H%M")
        folder.mkdir()
        (folder / "call.speakers.txt").write_text(f"[00:01:00] {speaker}: {text}\n")

    def test_ranks_recent_named_speakers(self):
        r = drafts.stakeholders(self.ws, ["dns records"])
        names = [p["speaker"] for p in r["stakeholders"]]
        self.assertEqual(names[0], "Test_Person")
        self.assertIn("Old_Voice", names)
        self.assertNotIn("SPEAKER_03", names)
        self.assertEqual(r["stakeholders"][0]["newest"]["text"][:8], "dns reco")
        self.assertEqual(drafts.stakeholders(self.ws, ["dns records"], exclude=["Test Person"])
                         ["stakeholders"][0]["speaker"], "Old_Voice")

    def test_git_authors_matched_to_speakers(self):
        repo = self.root / "repo"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "Test Person", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "Test Person", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "dns.tf").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "dns"], check=True, env=env)
        r = drafts.stakeholders(self.ws, ["dns records"], repo=str(repo), paths=["dns.tf"])
        top = r["stakeholders"][0]
        self.assertEqual(top["speaker"], "Test_Person")
        self.assertEqual(top["git_commits"], 1)

    def test_stale_check_flags_newer_statements(self):
        did = self.ready()
        self.assertEqual(drafts.stale_check(self.ws)["maybe_stale"], [])
        m = drafts.load(did)
        m["verified_at"] = (datetime.now().astimezone() - timedelta(days=10)).isoformat()
        drafts.save(m)
        hit = drafts.stale_check(self.ws)["maybe_stale"]
        self.assertEqual(hit[0]["id"], did)
        self.assertEqual(hit[0]["new_statements"][0]["speaker"], "Test_Person")


if __name__ == "__main__":
    unittest.main()
