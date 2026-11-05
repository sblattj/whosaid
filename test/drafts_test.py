#!/usr/bin/env python3
"""Offline tests for lib/drafts.py, the drafts outbox (GitHub issues #165 and #166)."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
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
            "WHOSAID_DRAFTS_NO_HISTORY": "1",
            "WHOSAID_TEAMS_CACHE": "off",
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
        other = drafts.new("Test Person", "other", separate=True)
        self.assertIn("open draft(s) to Test Person", other["note"])
        self.assertNotEqual(other["id"], did)
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

    def test_lock_without_owner_file_is_held_while_young(self):
        did = self.ready()
        lk = drafts.root() / ".outbox.lock"
        lk.mkdir()
        with self.assertRaises(drafts.DraftsError):
            drafts.stage([did])
        old = time.time() - 60
        os.utime(lk, (old, old))
        self.assertEqual(len(drafts.stage([did])["staged"]), 1)
        self.assertEqual([p.name for p in drafts.root().iterdir() if p.name.startswith(".outbox")], [])

    def test_release_leaves_someone_elses_lock(self):
        lk = drafts.root() / ".outbox.lock"
        lk.mkdir()
        (lk / "owner.json").write_text(json.dumps({"pid": os.getpid() + 999999, "at": "x", "what": "other"}))
        drafts.release_lock()
        self.assertTrue(lk.exists())

    def test_git_timeout_is_a_drafts_error(self):
        with patch.object(drafts.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)):
            with self.assertRaises(drafts.DraftsError) as e:
                drafts.git_authors(str(self.root), [])
        self.assertIn("over 30s", str(e.exception))

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
            r = drafts.stage([did], dry_run=True)  # unset: the built-in stager (#168)
        self.assertIn("drafts_stager.py", r["would_run"][0]["argv"][6])

    def test_stager_failure_is_recorded_not_staged(self):
        did = self.ready()
        with patch.dict(os.environ, {"WHOSAID_DRAFTS_STAGER": f"{sys.executable} -c 'import sys; sys.exit(4)'"}):
            r = drafts.stage([did])
        self.assertIn("stager exit 4", r["failed"][0]["why"])
        self.assertEqual(drafts.load(did)["status"], "verified")

    def test_detach_page(self):
        did = self.ready()
        drafts.write(did, remove_pages=[str(self.page)])
        self.assertEqual(drafts.load(did)["pages"], [])
        self.assertTrue(drafts.state(drafts.load(did)).startswith("stale"))
        with self.assertRaises(drafts.DraftsError):
            drafts.write(did, remove_pages=[str(self.page)])

    def test_other_live_session_refused_without_force(self):
        did = self.ready()
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-2222"}):
            for fn in (lambda **k: drafts.write(did, text="hijack", **k), lambda **k: drafts.verify(did, **k),
                       lambda **k: drafts.close(did, "dropped", **k)):
                with self.assertRaises(drafts.DraftsError) as e:
                    fn()
                self.assertIn("belongs to live session", str(e.exception))
            drafts.write(did, text="forced", force=True)
        self.assertEqual(drafts.load(did)["owner"]["sessionId"], "sess-1111", "force keeps the live owner")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            drafts.write(did, text="the user at a terminal")

    def test_gone_owner_is_adopted(self):
        did = self.ready()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        m = drafts.load(did)
        m["owner"]["pid"] = dead.pid
        drafts.save(m)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-3333"}):
            drafts.write(did, text="picked up")
        m = drafts.load(did)
        self.assertEqual(m["owner"]["sessionId"], "sess-3333")
        self.assertIn("adopted", [h["event"] for h in m["history"]])

    def test_close_is_soft_and_final(self):
        did = self.ready()
        drafts.close(did, "sent")
        self.assertEqual(drafts.listing(), [])
        self.assertEqual([d["state"] for d in drafts.listing(include_closed=True)], ["sent"])
        for fn in (lambda: drafts.write(did, text="x"), lambda: drafts.close(did, "dropped"),
                   lambda: drafts.close(drafts.new("A B", "t")["id"], "deleted")):
            with self.assertRaises(drafts.DraftsError):
                fn()

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


class ExtrasTest(DraftsBase):
    """#166: revisions, one draft per recipient, mentions, review page."""

    def session(self, sid, name, pid=None):
        (self.root / "sessions" / f"{sid}.json").write_text(json.dumps(
            {"sessionId": sid, "name": name, "pid": pid or os.getpid(), "cwd": f"/repo/{name}"}))

    def test_second_session_joins_the_open_draft_and_writes_a_revision(self):
        did = self.ready()
        self.session("sess-2222", "worker-b")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-2222"}):
            r = drafts.new("Test Person", "dns follow-up")
            self.assertTrue(r["joined"])
            self.assertEqual(r["id"], did)
            drafts.write(did, text="Hi, can you add the two DNS records today?")
        m = drafts.load(did)
        self.assertEqual(m["owner"]["sessionId"], "sess-1111", "the first writer stays the owner")
        self.assertEqual([c["name"] for c in m["contributors"]], ["worker-b"])
        self.assertEqual([r["by"]["name"] for r in m["revisions"]], ["worker-a", "worker-b"])
        self.assertTrue(drafts.state(m).startswith("stale"))
        self.assertEqual(len([d for d in drafts.all_drafts() if d["status"] in drafts.OPEN]), 1)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-3333"}):
            with self.assertRaises(drafts.DraftsError):
                drafts.write(did, text="not joined")

    def test_new_flag_makes_a_second_draft(self):
        did = self.ready()
        r = drafts.new("Test Person", "other", separate=True)
        self.assertFalse(r["joined"])
        self.assertNotEqual(r["id"], did)

    def test_revisions_show_and_diff(self):
        did = drafts.new("Test Person", "t")["id"]
        for t in ("one\n", "one\ntwo\n", "one\ntwo\n", "one\nthree\n"):
            drafts.write(did, text=t)
        m = drafts.load(did)
        self.assertEqual(len(m["revisions"]), 3, "an unchanged write keeps no revision")
        self.assertEqual(drafts.show(did, rev=1)["text"], "one\n")
        d = drafts.diff(did)
        self.assertEqual((d["from"], d["to"]), (2, 3))
        self.assertIn("-two", d["diff"])
        self.assertIn("+three", d["diff"])
        self.assertIn("+two", drafts.diff(did, 1, 2)["diff"])
        with self.assertRaises(drafts.DraftsError):
            drafts.show(did, rev=9)

    def test_draft_from_before_revisions_gets_rev_1(self):
        did = drafts.new("Test Person", "t")["id"]
        (drafts.ddir(did) / "draft.md").write_text("old text\n")
        m = drafts.load(did)
        m.pop("revisions")
        drafts.save(m)
        self.assertEqual(drafts.summary(drafts.load(did))["revisions"], 1)
        self.assertEqual(drafts.show(did, rev=1)["text"], "old text\n")
        drafts.write(did, text="new text\n")
        m = drafts.load(did)
        self.assertEqual(len(m["revisions"]), 2)
        self.assertIn("-old text", drafts.diff(did)["diff"])

    def test_hash_without_mentions_matches_the_prototype(self):
        did = self.ready()
        m = drafts.load(did)
        h = hashlib.sha256()
        d = drafts.ddir(did)
        for f in [str(d / "draft.md"), str(d / "claims.json"), *m["pages"]]:
            h.update(f.encode() + b"\0" + Path(f).read_bytes() + b"\0")
        self.assertEqual(drafts.content_hash(m), h.hexdigest()[:16])

    def test_mentions_are_staged_and_count_as_content(self):
        did = self.ready()
        drafts.write(did, mentions=["Other Person"])
        self.assertTrue(drafts.state(drafts.load(did)).startswith("stale"), "a mention change needs re-verify")
        drafts.verify(did)
        argv = drafts.stage([did], dry_run=True)["would_run"][0]["argv"]
        self.assertEqual(argv[argv.index("--mention") + 1], "Other Person")

    def test_html_review_page(self):
        did = self.ready()
        drafts.write(did, text="Hi <b>there</b>, records?")
        r = drafts.html_page(str(self.root / "out" / "review.html"))
        page = Path(r["path"]).read_text()
        self.assertEqual(r["drafts"], 1)
        self.assertIn("Test Person", page)
        self.assertIn("&lt;b&gt;there", page, "draft text is escaped")
        self.assertIn("worker-a", page)
        self.assertNotIn("\u2014", page)


class ChatHistoryTest(DraftsBase):
    """#166: stage reads the ingested chat first; mark-sent --check finds the sent message."""

    def setUp(self):
        super().setUp()
        self.ws = self.root / "ws"
        self.ws.mkdir()

    def chat(self, messages, chat="Test Person", when=None):
        when = when or datetime.now()
        folder = self.ws / when.strftime("%Y-%m-%d-%H%M")
        folder.mkdir(exist_ok=True)
        segs = [{"start": 0, "end": 0, "speaker": spk, "epoch_ms": int(t.timestamp() * 1000), "mid": f"m{i}",
                 "text": text} for i, (t, spk, text) in enumerate(messages)]
        (folder / "teams.diarization.json").write_text(json.dumps({
            "base": "teams", "source": {"kind": "teams-chat", "chat_name": chat, "day": when.strftime("%Y-%m-%d")},
            "names": {}, "roles": {"Me_Self": "self"}, "segments": segs}))

    def test_stage_reports_the_chat_and_skips_a_draft_already_sent(self):
        did = self.ready()
        later = datetime.now() + timedelta(seconds=5)
        self.chat([(later - timedelta(minutes=3), "Test_Person", "morning, any news on dns?")])
        r = drafts.stage([did], ws=self.ws)
        ctx = r["context"][did]
        self.assertEqual(ctx["ingested"], 1)
        self.assertFalse(ctx["recent"][0]["self"])
        self.assertIsNone(ctx["already_sent"])
        self.assertEqual(len(r["staged"]), 1)
        drafts.write(did, text="Hi, can you add the two DNS records? Thanks")
        drafts.verify(did)
        self.chat([(later, "Me_Self", "Hi, can you add the two **DNS records**? thanks"),
                   (later + timedelta(seconds=30), "Test_Person", "sure")])
        r = drafts.stage([did], ws=self.ws)
        self.assertIn("already in Test Person", r["skipped"][0]["why"])
        self.assertTrue(r["context"][did]["reply_since_ours"])
        self.assertEqual(len(self.stager_calls()), 1, "no second paste of a sent message")

    def test_sent_check_marks_only_what_appears(self):
        did = self.ready()
        other = self.ready(to="Second Person", topic="other")
        drafts.stage([did], ws=self.ws)
        drafts.stage([other], ws=self.ws)
        r = drafts.sent_check(self.ws)
        self.assertEqual(r["sent"], [])
        self.assertEqual(len(r["still_staged"]), 2)
        self.chat([(datetime.now() + timedelta(seconds=5), "Me_Self", "Hi, can you add the two DNS records?")])
        self.chat([(datetime.now() + timedelta(seconds=5), "Me_Self", "an unrelated note")], chat="Second Person",
                  when=datetime.now() + timedelta(minutes=1))
        r = drafts.sent_check(self.ws)
        self.assertEqual([s["id"] for s in r["sent"]], [did])
        self.assertEqual(drafts.load(did)["status"], "sent")
        self.assertEqual(drafts.load(did)["sent_message"]["mid"], "m0")
        self.assertEqual(drafts.load(other)["status"], "staged")

    def test_their_message_is_not_ours(self):
        did = self.ready()
        drafts.stage([did], ws=self.ws)
        self.chat([(datetime.now() + timedelta(seconds=5), "Test_Person", "Hi, can you add the two DNS records?")])
        self.assertEqual(drafts.sent_check(self.ws)["sent"], [])

    def test_cli_mark_sent_check_and_diff(self):
        did = self.ready()
        drafts.stage([did], ws=self.ws)
        self.chat([(datetime.now() + timedelta(seconds=5), "Me_Self", "Hi, can you add the two DNS records?")])
        lib = Path(__file__).resolve().parents[1] / "lib" / "drafts.py"
        out = subprocess.run([sys.executable, str(lib), "mark-sent", "--check", str(self.ws)],
                             capture_output=True, text=True, env=os.environ)
        self.assertIn(f"{did}: sent at", out.stdout, out.stderr)
        out = subprocess.run([sys.executable, str(lib), "diff", did], capture_output=True, text=True, env=os.environ)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
