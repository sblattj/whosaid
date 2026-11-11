#!/usr/bin/env python3
"""teams_cache.py: decoding, naming and sync, on synthetic records only (no real cache read).

Set WHOSAID_TEAMS_CACHE_IT=1 to also smoke-read this Mac's real Teams cache (prints counts only).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import teams_cache as tc  # noqa: E402
import watch  # noqa: E402

SELF = "8:orgid:aaaaaaaa-0000-0000-0000-000000000001"
BOB = "8:orgid:bbbbbbbb-0000-0000-0000-000000000002"
CAT = "8:orgid:cccccccc-0000-0000-0000-000000000003"
DM = "19:aaaaaaaa-0000-0000-0000-000000000001_bbbbbbbb-0000-0000-0000-000000000002@unq.gbl.spaces"
GROUP = "19:0123456789abcdef@thread.v2"
TOPIC = "19:fedcba9876543210@thread.v2"
NOW = int(time.time() * 1000)


def msg(cid, mid, creator, content, mtype="RichText/Html", **kw):
    return {"conversationId": cid, "id": str(mid), "originalArrivalTime": mid, "creator": creator,
            "content": content, "messageType": mtype, **kw}


def raw_fixture():
    t = NOW - 3_600_000
    chains = [
        {"conversationId": DM, "messageMap": {
            "a": msg(DM, t + 1, BOB, "<p>hi <span itemtype=\"http://schema.skype.com/Mention\">Ann</span></p>"),
            "b": msg(DM, t + 2, SELF, "on it, see <a href=\"https://example.test/pr/7\">the PR</a>"),
            "c": msg(DM, "-1", SELF, "pending copy"),
            "d": msg(DM, t + 3, BOB, "", "ThreadActivity/AddMember"),
            "e": msg(DM, t + 4, BOB, "gone", deletionInfo={"deleteTime": 1}),
            "f": msg(DM, t + 5, BOB, "Text body &amp; more", "Text"),
        }},
        {"conversationId": GROUP, "messageMap": {
            "a": msg(GROUP, t + 10, BOB, "<div>line one</div><div>line two<br>three</div>"),
            "b": msg(GROUP, t + 11, CAT, "<img alt=\"smile\" src=\"x\"> ok"),
            "c": msg(GROUP, t + 12, SELF, "me too"),
        }},
        {"conversationId": TOPIC, "messageMap": {
            "a": msg(TOPIC, t + 20, CAT, "release is out"),
        }},
        {"conversationId": "48:notes", "messageMap": {
            "a": msg("48:notes", t + 30, SELF, "note to self"),
        }},
        {"conversationId": "19:chan@thread.tacv2", "messageMap": {
            "a": msg("19:chan@thread.tacv2", t + 40, CAT, "channel post"),
        }},
    ]
    convs = [{"id": DM, "type": "Chat"}, {"id": GROUP, "type": "Chat"},
             {"id": TOPIC, "type": "Chat", "threadProperties": {"topic": "Release Room"}},
             {"id": "48:notes", "type": "StreamOfNotes"}, {"id": "19:chan@thread.tacv2", "type": "Space"}]
    profiles = [{"mri": SELF, "displayName": "Ann Example"}, {"mri": BOB, "displayName": "Bob Builder"},
                {"mri": CAT, "displayName": "Cat Coder"}]
    return {"conversations": convs, "replychains": chains, "profiles": profiles}


class HtmlToText(unittest.TestCase):
    def test_blocks_mentions_links_images(self):
        self.assertEqual(tc.html_to_text("<p>a</p><p>b<br>c</p>"), "a\n\nb\nc")
        self.assertEqual(tc.html_to_text('<span itemtype="http://schema.skype.com/Mention">Bob</span> hi'), "@Bob hi")
        self.assertEqual(tc.html_to_text('<a href="https://x.test/1">PR</a>'), "PR (https://x.test/1)")
        self.assertEqual(tc.html_to_text('<a href="https://x.test/1">https://x.test/1</a>'), "https://x.test/1")
        self.assertEqual(tc.html_to_text('<img alt="party">'), "[image: party]")
        self.assertEqual(tc.html_to_text("a&nbsp;b\r\n\r\n\r\nc​"), "a b\n\nc")
        self.assertEqual(tc.html_to_text("x &amp; y", "Text"), "x & y")

    def test_reply_quote(self):
        out = tc.html_to_text('<blockquote itemtype="http://schema.skype.com/Reply">old</blockquote><p>new</p>')
        self.assertIn("> old", out)
        self.assertTrue(out.endswith("new"))


class Decode(unittest.TestCase):
    def setUp(self):
        self.d = tc.decode(raw_fixture())

    def test_self_from_notes(self):
        self.assertEqual(self.d["self"], {"mri": SELF, "name": "Ann Example"})

    def test_filters(self):
        texts = [m["text"] for m in self.d["messages"]]
        self.assertNotIn("pending copy", texts)
        self.assertNotIn("gone", texts)
        self.assertNotIn("note to self", texts)
        self.assertNotIn("channel post", texts)
        self.assertIn("Text body & more", texts)
        self.assertEqual(len(texts), 7)
        self.assertEqual([m["epoch_ms"] for m in self.d["messages"]], sorted(m["epoch_ms"] for m in self.d["messages"]))

    def test_channels_opt_in(self):
        d = tc.decode(raw_fixture(), channels=True)
        self.assertIn("channel post", [m["text"] for m in d["messages"]])

    def test_names(self):
        c = self.d["chats"]
        self.assertEqual(c[DM]["name"], "Bob Builder")
        self.assertEqual(c[GROUP]["name"], "Bob, Cat")
        self.assertEqual(c[TOPIC]["name"], "Release Room")
        me = [m for m in self.d["messages"] if m["self"]]
        self.assertEqual({m["author"] for m in me}, {"Ann Example"})

    def test_self_without_notes_is_most_present(self):
        raw = raw_fixture()
        raw["replychains"] = [c for c in raw["replychains"] if c["conversationId"] != "48:notes"]
        dm2 = "19:aaaaaaaa-0000-0000-0000-000000000001_cccccccc-0000-0000-0000-000000000003@unq.gbl.spaces"
        raw["conversations"].append({"id": dm2, "type": "Chat"})
        raw["replychains"].append({"conversationId": dm2, "messageMap": {"a": msg(dm2, NOW - 10, CAT, "ping")}})
        self.assertEqual(tc.decode(raw)["self"]["mri"], SELF)

    def test_newer_version_wins(self):
        raw = raw_fixture()
        m = dict(raw["replychains"][2]["messageMap"]["a"], content="edited", version="2")
        raw["replychains"].append({"conversationId": TOPIC, "messageMap": {"a": m}})
        self.assertIn("edited", [x["text"] for x in tc.decode(raw)["messages"]])


class WithWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.ws.mkdir()
        self.data = tc.decode(raw_fixture())
        self.p = patch.object(tc, "load", lambda *a, **k: json.loads(json.dumps(self.data)))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def sidecar(self, folder, chat_name, mids, chat_id=None):
        d = self.ws / folder
        d.mkdir()
        segs = [{"start": 0, "end": 0, "speaker": "Bob_Builder", "epoch_ms": int(m), "mid": str(m), "text": "x"}
                for m in mids]
        (d / "teams.diarization.json").write_text(json.dumps({
            "source": {"kind": "teams-chat", "chat_name": chat_name, "chat_id": chat_id},
            "segments": segs, "roles": {}}))

    def test_name_vote_and_override(self):
        g = next(m for m in self.data["messages"] if m["chat_id"] == GROUP)
        self.sidecar("2026-01-01-0900", "Bob, Cat, +1", [g["message_id"]])
        (self.ws / "whosaid.toml").write_text(f'[teams.chat_names]\n"{TOPIC}" = "Releases"\n')
        names = tc.name_chats(self.data, self.ws)
        self.assertEqual(names[GROUP], "Bob, Cat, +1")
        self.assertEqual(names[TOPIC], "Releases")
        self.assertEqual(names[DM], "Bob Builder")

    def test_messages_filter(self):
        r = tc.messages("bob", None, 50, None)
        self.assertEqual(r["chats"], ["Bob Builder", "Bob, Cat"])
        r = tc.messages("Bob Builder", None, 2, None, exact=True)
        self.assertEqual((r["total"], r["count"]), (3, 2))
        self.assertEqual(tc.messages(DM, None, 50, None)["total"], 3)

    def test_chats(self):
        r = tc.chats(None, None)
        self.assertEqual(r["chats"][0]["chat"], "Release Room")
        dm = next(c for c in r["chats"] if c["chat_id"] == DM)
        self.assertEqual((dm["messages"], dm["last_from"]), (3, "Bob Builder"))

    def test_sync_only_new_and_idempotent(self):
        with patch.dict(os.environ, {"TZ": "UTC"}):
            first = tc.sync(self.ws, None, tz="UTC")
            self.assertEqual(first["new_messages"], 7)
            folders = sorted(p.name for p in self.ws.iterdir() if p.is_dir())
            self.assertEqual(len(folders), 3)
            sc = json.loads(next(self.ws.glob("*/teams.diarization.json")).read_text())
            self.assertEqual(sc["source"]["scraper"], tc.SCRAPER)
            self.assertEqual(tc.sync(self.ws, None, tz="UTC")["new_messages"], 0)

    def test_same_name_chats_do_not_share_a_folder(self):
        # a second 1:1 with someone else also called "Bob Builder", same day as DM
        dm2 = "19:aaaaaaaa-0000-0000-0000-000000000001_dddddddd-0000-0000-0000-000000000004@unq.gbl.spaces"
        raw = raw_fixture()
        raw["profiles"].append({"mri": "8:orgid:dddddddd-0000-0000-0000-000000000004", "displayName": "Bob Builder"})
        raw["conversations"].append({"id": dm2, "type": "Chat"})
        dm_first = next(m for m in raw["replychains"][0]["messageMap"].values() if m["creator"] == BOB)
        raw["replychains"].append({"conversationId": dm2, "messageMap": {
            "a": msg(dm2, dm_first["originalArrivalTime"] + 500, "8:orgid:dddddddd-0000-0000-0000-000000000004",
                     "other bob here")}})
        self.data = tc.decode(raw)
        # a scraper folder (no chat id) already holds one DM message under the shared name
        self.sidecar("2020-01-01-0000", "Bob Builder", [dm_first["id"]])
        sc = self.ws / "2020-01-01-0000" / "teams.diarization.json"
        d = json.loads(sc.read_text())
        day = tc.datetime.fromtimestamp(dm_first["originalArrivalTime"] / 1000, tc.timezone.utc).strftime("%Y-%m-%d")
        d["source"]["day"] = day
        sc.write_text(json.dumps(d))
        names = tc.name_chats(self.data, self.ws)
        self.assertEqual(names[DM], "Bob Builder")
        self.assertTrue(names[dm2].startswith("Bob Builder (chat "), names[dm2])
        tc.sync(self.ws, None, tz="UTC")
        merged = json.loads(sc.read_text())
        self.assertNotIn("other bob here", [x["text"] for x in merged["segments"]])
        other = [json.loads(p.read_text()) for p in self.ws.glob("*/teams.diarization.json")
                 if json.loads(p.read_text())["source"].get("chat_id") == dm2]
        self.assertEqual(len(other), 1)

    def test_drafts_history_merges_unseen_cache_messages(self):
        import drafts
        dm_msgs = [m for m in self.data["messages"] if m["chat_id"] == DM]
        self.sidecar("2099-01-01-0000", "Bob Builder", [dm_msgs[0]["message_id"]])
        sc = self.ws / "2099-01-01-0000" / "teams.diarization.json"
        d = json.loads(sc.read_text())
        d["segments"][0]["epoch_ms"] = dm_msgs[0]["epoch_ms"]
        sc.write_text(json.dumps(d))
        with patch.dict(os.environ, {"WHOSAID_TEAMS_CACHE": ""}), \
                patch.object(tc, "_leveldb", lambda d: Path(self.tmp.name)):
            got = drafts.chat_messages(self.ws, "Bob Builder", since_ms=0)
        self.assertEqual([g["mid"] for g in got], [m["message_id"] for m in dm_msgs])
        self.assertEqual(got[0]["folder"], "2099-01-01-0000")
        self.assertEqual({g["folder"] for g in got[1:]}, {"(teams cache)"})
        self.assertTrue(next(g for g in got if g["text"].startswith("on it"))["self"])
        with patch.dict(os.environ, {"WHOSAID_TEAMS_CACHE": "off"}):
            self.assertEqual(len(drafts.chat_messages(self.ws, "Bob Builder", since_ms=0)), 1)

    def test_sync_reingests_a_message_edited_since_and_keeps_its_edit_time(self):
        """#209: a known message id comes in again once Teams edited it; the folder holds the new text."""
        with patch.dict(os.environ, {"TZ": "UTC"}):
            tc.sync(self.ws, None, tz="UTC")
            mine = next(m for m in self.data["messages"] if m["chat_id"] == DM and m["self"])
            edited = int(time.time() * 1000) + 60_000  # after the folder was written
            raw = raw_fixture()
            m = raw["replychains"][0]["messageMap"]["b"]
            m.update(content="on it, see <a href=\"https://example.test/pr/8\">the fixed PR</a>",
                     version=str(edited), properties={"edittime": str(edited)})
            self.data = tc.decode(raw)
            self.assertEqual(next(x for x in self.data["messages"] if x["message_id"] == mine["message_id"])
                             ["edited_ms"], edited)
            r = tc.sync(self.ws, None, tz="UTC")
            self.assertEqual((r["new_messages"], r["edited_messages"]), (1, 1))
            segs = [s for p in self.ws.glob("*/teams.diarization.json")
                    for s in json.loads(p.read_text())["segments"] if s["mid"] == mine["message_id"]]
            self.assertEqual(len(segs), 1, "replaced in place, not added")
            self.assertIn("the fixed PR", segs[0]["text"])
            self.assertEqual(segs[0]["edited_ms"], edited)
            self.assertTrue(segs[0]["edited_at"].endswith("Z"))
            self.assertEqual(tc.sync(self.ws, None, tz="UTC")["new_messages"], 0, "once per edit")
            # an edit older than the folder's capture is already in its text: no re-ingest
            m.update(version=str(edited + 1), properties={"edittime": str(mine["epoch_ms"] + 1000)},
                     content="an older edit the scraper already captured")
            self.data = tc.decode(raw)
            self.assertEqual(tc.sync(self.ws, None, tz="UTC")["new_messages"], 0)

    def test_dry_run_writes_nothing(self):
        r = tc.sync(self.ws, None, dry_run=True, tz="UTC")
        self.assertEqual(r["new_messages"], 7)
        self.assertEqual([p for p in self.ws.iterdir() if p.is_dir()], [])

    def test_watch_hook_off_by_default(self):
        self.assertEqual(watch.teams_live_sync(self.ws, {}, False), 0)
        self.assertEqual([p for p in self.ws.iterdir() if p.is_dir()], [])
        self.assertEqual(watch.teams_live_sync(self.ws, {"teams": {"live_cache": True, "live_cache_since": "3650d"}},
                                               False), 7)


class Plumbing(unittest.TestCase):
    def test_parse_since(self):
        now = time.time() * 1000
        self.assertAlmostEqual(tc.parse_since("2h"), now - 7_200_000, delta=5000)
        self.assertAlmostEqual(tc.parse_since("90m"), now - 5_400_000, delta=5000)
        self.assertEqual(tc.parse_since("2026-01-02T00:00:00Z"), 1767312000000)
        with self.assertRaises(tc.CacheError):
            tc.parse_since("yesterday")

    def test_missing_cache(self):
        with patch.dict(os.environ, {"WHOSAID_TEAMS_CACHE": "/nonexistent/whosaid-test"}):
            self.assertFalse(tc.status()["ok"])
            with self.assertRaises(tc.CacheError):
                tc.load()

    def test_cache_keyed_on_fingerprint(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "IndexedDB"
            ldb = d / f"{tc.ORIGIN}.leveldb"
            ldb.mkdir(parents=True)
            (ldb / "000001.log").write_text("x")
            calls = []

            def fake_read(path=None):
                calls.append(1)
                return raw_fixture()
            with patch.dict(os.environ, {"WHOSAID_CACHE_DIR": str(Path(t) / "c")}), \
                    patch.object(tc, "read_store", fake_read):
                self.assertFalse(tc.load(d)["cached"])
                self.assertTrue(tc.load(d)["cached"])
                (ldb / "000001.log").write_text("xy")
                self.assertFalse(tc.load(d)["cached"])
                self.assertEqual(len(calls), 2)
                self.assertEqual(oct((Path(t) / "c" / "teams-live.json").stat().st_mode & 0o777), "0o600")

    def test_cache_dir_accepts_leveldb_path(self):
        p = f"/x/IndexedDB/{tc.ORIGIN}.leveldb"
        self.assertEqual(str(tc.cache_dir(p)), "/x/IndexedDB")


@unittest.skipUnless(os.environ.get("WHOSAID_TEAMS_CACHE_IT") == "1", "set WHOSAID_TEAMS_CACHE_IT=1 to read the real cache")
class RealCache(unittest.TestCase):
    def test_reads(self):
        d = tc.decode(tc.read_store())
        print(f"\n  real cache: {len(d['messages'])} messages, {len(d['chats'])} chats")
        self.assertTrue(d["self"]["mri"])


if __name__ == "__main__":
    unittest.main()
