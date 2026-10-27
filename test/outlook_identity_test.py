#!/usr/bin/env python3
"""Offline Outlook scraper ID coverage through import and exact search (#110)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import message_identity
import search
import teams_chat


class OutlookIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="outlook-identity-test-")
        self.ws = Path(self.temp.name)
        (self.ws / "whosaid.toml").write_text("[search]\nembed = false\n")
        self.registry = patch.dict(os.environ, {"WHOSAID_SPEAKER_DB": str(self.ws / "no-registry.json")})
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.addCleanup(self.temp.cleanup)

    def export(self, records):
        path = self.ws / "export.json"
        path.write_text(json.dumps({"Sample thread": {"chat_id": "sample-conversation", "messages": records}}))
        return path

    def record(self, msg_id="message-one", **fields):
        return {"author": "Speaker A", "epoch": 1893456000000,
                "text": "I will send the sample guide.", "msg_id": msg_id, **fields}

    def ingest(self, records, **kwargs):
        return teams_chat.ingest(self.export(records), self.ws, tz_flag="UTC", src=teams_chat.OUTLOOK, **kwargs)

    def sidecar(self):
        return json.loads(next(self.ws.glob("*/outlook.diarization.json")).read_text())

    def test_scraper_ids_preserve_identical_simultaneous_messages_through_search(self):
        records = [self.record("message-one"), self.record("message-two")]
        first = self.ingest(records)
        again = self.ingest(records)
        sidecar = self.sidecar()
        ids = [r["message_id"] for r in teams_chat.load_export(self.ws / "export.json")]
        with self.subTest(stage="load"):
            self.assertEqual(ids, ["message-one", "message-two"])
        with self.subTest(stage="ingest"):
            self.assertEqual(first[0]["total"], 2)
            self.assertEqual(again[0]["total"], 2)
            self.assertEqual(again[0]["new"], 0)
        with self.subTest(stage="sidecar"):
            self.assertEqual({s.get("message_id") for s in sidecar["segments"]}, set(ids))
            self.assertEqual(sidecar["source"]["chat_id"], "sample-conversation")
        with self.subTest(stage="identity"):
            self.assertEqual(len(message_identity.MessageIndex(self.ws).by_id), 2)
        search.build(self.ws, embed=False)
        hits = search.query(self.ws, "guide", mode="exact", k=10)[1]
        with self.subTest(stage="search"):
            self.assertEqual(len(hits), 2)
            self.assertEqual(len({hit.message_id for hit in hits}), 2)
            self.assertTrue(all(len(hit.provenance) == 1 for hit in hits))

    def test_existing_explicit_aliases_win_over_scraper_id(self):
        records = [self.record(message_id="canonical-id", source_message_id="source-id"),
                   self.record(source_message_id="source-id"), self.record("scraper-id", mid="legacy-id"),
                   self.record("scraper-id", mid="1893456000000")]
        loaded = teams_chat.load_export(self.export(records))
        self.assertEqual([r["message_id"] for r in loaded],
                         ["canonical-id", "source-id", "legacy-id", "scraper-id"])

    def test_reimport_upgrades_one_exact_legacy_match_without_adding_a_row(self):
        self.ingest([self.record(msg_id="")])
        path = next(self.ws.glob("*/outlook.diarization.json"))
        before = path.read_bytes()
        preview = self.ingest([self.record()], dry_run=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((preview[0]["total"], preview[0]["new"]), (1, 0))
        upgraded = self.ingest([self.record()])
        self.assertEqual((upgraded[0]["total"], upgraded[0]["new"]), (1, 0))
        self.assertEqual(self.sidecar()["segments"][0].get("message_id"), "message-one")
        self.assertEqual(self.ingest([self.record()])[0]["new"], 0)
        search.build(self.ws, embed=False)
        self.assertEqual(len(search.query(self.ws, "guide", k=10)[1]), 1)

    def test_reimport_keeps_ambiguous_legacy_row_and_both_incoming_ids(self):
        self.ingest([self.record(msg_id="")])
        result = self.ingest([self.record("message-one"), self.record("message-two")])
        self.assertEqual((result[0]["total"], result[0]["new"]), (3, 2))
        self.assertEqual({s.get("message_id") for s in self.sidecar()["segments"]},
                         {None, "message-one", "message-two"})

    def test_existing_ids_also_make_a_legacy_upgrade_ambiguous(self):
        # A partial export must not choose one candidate by ignoring stored IDs.
        self.ingest([self.record(msg_id=""), self.record("message-two")])
        result = self.ingest([self.record("message-one")])
        self.assertEqual((result[0]["total"], result[0]["new"]), (3, 1))
        self.assertEqual({s.get("message_id") for s in self.sidecar()["segments"]},
                         {None, "message-one", "message-two"})

    def test_legacy_upgrade_preserves_images_and_uncertain_attribution(self):
        text = "I will send the sample guide."
        self.ingest([self.record(msg_id="", text=text + " [image: outlook-images/sample.png]",
                                 quoted=True, attribution_uncertain=True, assertion_text="")])
        result = self.ingest([self.record()])
        self.assertEqual((result[0]["total"], result[0]["new"]), (1, 0))
        segment = self.sidecar()["segments"][0]
        self.assertEqual(segment["message_id"], "message-one")
        self.assertIn("[image: outlook-images/sample.png]", segment["text"])
        self.assertTrue(segment["quoted"])
        self.assertTrue(segment["attribution_uncertain"])
        self.assertEqual(segment["assertion_text"], "")
        again = self.ingest([self.record()])
        self.assertEqual((again[0]["total"], again[0]["new"]), (1, 0))
        segment = self.sidecar()["segments"][0]
        self.assertTrue(segment["attribution_uncertain"])
        self.assertEqual(segment["assertion_text"], "")
        self.assertIn("[image: outlook-images/sample.png]", segment["text"])

    def test_legacy_upgrade_preserves_images_across_normalized_body_formatting(self):
        self.ingest([self.record(msg_id="", text="I will send the guide, tomorrow. [image: outlook-images/sample.png]")])
        result = self.ingest([self.record(text="I will send the guide,tomorrow.")])
        self.assertEqual((result[0]["total"], result[0]["new"]), (1, 0))
        self.assertIn("[image: outlook-images/sample.png]", self.sidecar()["segments"][0]["text"])
        # A real body edit must not carry an unrelated attachment or old quote state.
        self.ingest([self.record(text="Their old promise.", is_quote=True)])
        self.ingest([self.record(text="I will send a different guide.")])
        segment = self.sidecar()["segments"][0]
        self.assertEqual(segment["text"], "I will send a different guide.")
        self.assertFalse(segment.get("attribution_uncertain", False))
        self.assertFalse(segment.get("quoted", False))

    def test_legacy_upgrade_requires_exact_clock_speaker_and_body(self):
        self.ingest([self.record(msg_id="")])
        result = self.ingest([self.record("other-clock", epoch=1893456001000),
                              self.record("other-author", author="Speaker B"),
                              self.record("other-body", text="I will send a different guide.")])
        self.assertEqual((result[0]["total"], result[0]["new"]), (4, 3))

    def test_precise_quote_boundary_survives_upgrade_and_repeated_imports(self):
        quote = "Another person spoke."
        assertion = "I will send the guide."
        record = self.record(text=quote + " " + assertion, quoted_text=quote)
        self.ingest([{**record, "msg_id": ""}])
        for incoming in (record, record, {k: v for k, v in record.items() if k != "quoted_text"}):
            result = self.ingest([incoming])
            self.assertEqual((result[0]["total"], result[0]["new"]), (1, 0))
            segment = self.sidecar()["segments"][0]
            self.assertTrue(segment["quoted"])
            self.assertFalse(segment["attribution_uncertain"])
            self.assertEqual(segment["assertion_text"], assertion)

    def test_conflicting_precise_quote_boundaries_remain_uncertain(self):
        quote = "Another person spoke."
        assertion = "I will send the guide."
        record = self.record(text=quote + " " + assertion, quoted_text=quote)
        self.ingest([record])
        self.ingest([{**record, "quoted_text": assertion}])
        segment = self.sidecar()["segments"][0]
        self.assertTrue(segment["quoted"])
        self.assertTrue(segment["attribution_uncertain"])
        self.assertEqual(segment["assertion_text"], "")

    def test_legacy_row_does_not_transfer_attribution_to_an_edited_identified_row(self):
        self.ingest([self.record(msg_id="", text="Their prior promise.", is_quote=True),
                     self.record(text="Their prior promise.", is_quote=True)])
        result = self.ingest([self.record(text="I will send the new guide.")])
        self.assertEqual((result[0]["total"], result[0]["new"]), (2, 0))
        segments = self.sidecar()["segments"]
        identified = next(s for s in segments if s.get("message_id"))
        self.assertEqual(identified["text"], "I will send the new guide.")
        self.assertFalse(identified.get("quoted", False))
        self.assertFalse(identified.get("attribution_uncertain", False))
        legacy = next(s for s in segments if not s.get("message_id"))
        self.assertEqual(legacy["text"], "Their prior promise.")
        self.assertTrue(legacy["quoted"])

    def test_source_id_remains_scoped_to_conversation(self):
        first = {"kind": "outlook-mail", "chat_id": "sample-one"}
        second = {"kind": "outlook-mail", "chat_id": "sample-two"}
        segment = {"message_id": "message-one", "epoch_ms": 1893456000000,
                   "speaker": "Speaker A", "text": "Original text."}
        self.assertNotEqual(message_identity.source_message_id(first, segment),
                            message_identity.source_message_id(second, segment))
        self.assertEqual(message_identity.source_message_id(first, segment),
                         message_identity.source_message_id(first, {**segment, "text": "Edited text."}))


if __name__ == "__main__":
    unittest.main()
