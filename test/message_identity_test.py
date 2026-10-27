#!/usr/bin/env python3
"""Offline acceptance cases for message identity, provenance and attribution."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import message_identity as identity
import search
import teams_chat


class MessageIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="message-identity-test-")
        self.ws = Path(self.temp.name)
        (self.ws / "whosaid.toml").write_text("[search]\nembed = false\n")

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, name, *, mid="sample-message", epoch=1893456000000, chat_id="sample-chat",
                 chat_name="Sample discussion", text="I will send the sample guide."):
        folder = self.ws / name
        folder.mkdir()
        stamp = epoch // 1000 % 86400
        h, rem = divmod(stamp, 3600)
        m, sec = divmod(rem, 60)
        (folder / "teams.speakers.txt").write_text(f"[{h:02}:{m:02}:{sec:02}] Speaker A: {text}\n")
        segment = {"start": stamp, "end": stamp, "speaker": "Speaker A", "text": text,
                   "epoch_ms": epoch, "mid": mid or str(epoch)}
        if mid:
            segment["message_id"] = mid
        (folder / "teams.diarization.json").write_text(json.dumps({
            "source": {"kind": "teams-chat", "chat_id": chat_id, "chat_name": chat_name, "day": "2030-01-01"},
            "segments": [segment],
        }))

    def test_overlapping_imports_count_once_and_keep_provenance(self):
        for n in range(3):
            self.snapshot(f"2030-01-01-0000-{n}")
        index = identity.MessageIndex(self.ws)
        occurrences = [{"meeting": m, "t_sec": 0} for m in index.by_meeting]
        self.assertEqual(len(index.occurrence_keys(occurrences)), 1)
        search.build(self.ws, embed=False)
        hits = search.query(self.ws, "guide", mode="exact", k=10)[1]
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(hits[0].provenance), 3)
        original = hits[0].as_json()
        search.build(self.ws, embed=False)
        self.assertEqual(search.query(self.ws, "guide", k=10)[1][0].as_json(), original)
        # A requested source folder remains reachable even when it is a duplicate.
        selected = search.query(self.ws, "guide", meeting="2030-01-01-0000-2")[1]
        self.assertEqual(selected[0].meeting, "2030-01-01-0000-2")

    def test_new_message_id_and_later_date_are_distinct(self):
        self.snapshot("2030-01-01-0000")
        self.snapshot("2030-01-02-0000", mid="later-message", epoch=1893542400000)
        self.snapshot("2030-01-01-0000-2", mid="different-message")
        index = identity.MessageIndex(self.ws)
        self.assertEqual(len(index.by_id), 3)
        search.build(self.ws, embed=False)
        self.assertEqual(len(search.query(self.ws, "guide", k=10)[1]), 3)

    def test_duplicate_candidates_do_not_starve_search_result_limit(self):
        for n in range(5):
            self.snapshot(f"2030-01-01-0000-{n}")
        self.snapshot("2030-01-02-0000", mid="later-message", epoch=1893542400000)
        search.build(self.ws, embed=False)
        self.assertEqual(len(search.query(self.ws, "guide", k=2)[1]), 2)
        cfg = search.load_config(self.ws)
        conn = search.open_index(self.ws)
        try:
            for _, meeting, _, speaker, _, stamp, _, body in conn.execute(f"SELECT {search.SEG_COLS} FROM seg").fetchall():
                vector = [0.9, 0.1] if meeting.startswith("2030-01-02") else [1.0, 0.0]
                conn.execute("INSERT INTO emb VALUES (?,?,?,?)",
                             (search.seg_key(meeting, stamp, speaker, body),
                              cfg["search"]["embed_model"], 2, search.pack_vec(vector)))
        finally:
            conn.close()
        with patch.object(search, "ollama_up", return_value=True), \
                patch.object(search, "embed_texts", return_value=[[1.0, 0.0]]):
            for mode in ("meaning", "hybrid"):
                hits = search.query(self.ws, "guide", mode=mode, k=2)[1]
                self.assertEqual(len(hits), 2)
                self.assertEqual(len({h.message_id for h in hits}), 2)

    def test_pre_identity_index_remains_readable(self):
        self.snapshot("2030-01-01-0000")
        search.build(self.ws, embed=False)
        conn = search.open_index(self.ws)
        try:
            conn.execute("DROP TABLE seg_message")
            conn.execute("DROP TABLE message_provenance")
        finally:
            conn.close()
        hits = search.query(self.ws, "guide")[1]
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0].message_id)

    def test_legacy_fallback_keeps_conversation_context_and_alias_is_explicit(self):
        body = "I will prepare a sample guide explaining the example application and send it to the sample group."
        self.snapshot("2030-01-01-0000", mid=None, chat_id=None, chat_name="Discussion", text=body)
        self.snapshot("2030-01-01-0000-2", mid=None, chat_id=None, chat_name="Discussion copy", text=body)
        index = identity.MessageIndex(self.ws)
        self.assertEqual(len(index.by_id), 2)
        self.assertEqual(len(index.possible_duplicates), 1)
        aliases = {"Discussion": "confirmed-group", "Discussion copy": "confirmed-group"}
        self.assertEqual(len(identity.MessageIndex(self.ws, aliases).by_id), 1)
        with (self.ws / "whosaid.toml").open("a") as handle:
            handle.write('\n[messages.conversation_aliases]\n"Discussion"="confirmed-group"\n"Discussion copy"="confirmed-group"\n')
        self.assertEqual(len(identity.MessageIndex(self.ws).by_id), 1)

    def test_fallback_later_identical_text_not_collapsed(self):
        self.snapshot("2030-01-01-0000", mid=None)
        self.snapshot("2030-01-02-0000", mid=None, epoch=1893542400000)
        self.assertEqual(len(identity.MessageIndex(self.ws).by_id), 2)

    def test_ambiguous_timestamp_is_not_guessed(self):
        self.snapshot("2030-01-01-0000")
        p = self.ws / "2030-01-01-0000" / "teams.speakers.txt"
        p.write_text(p.read_text() + "[00:00:00] Speaker B: Another promise\n")
        index = identity.MessageIndex(self.ws)
        self.assertIsNone(index.resolve("2030-01-01-0000", t_sec=0))
        self.assertIsNotNone(index.resolve("2030-01-01-0000", t_sec=0, line=1))

    def test_commitment_ordinal_is_not_a_transcript_line_or_quote_attribution(self):
        folder = self.ws / "2030-01-01-0000"
        folder.mkdir()
        folder.joinpath("audio.speakers.txt").write_text(
            "[00:00:00] Speaker A: I will send the sample guide.\n"
            "[00:00:00] Speaker B: > I will send the sample guide.\n")
        index = identity.MessageIndex(self.ws)
        generic = {"meeting": folder.name, "source": "transcript", "line": 1, "t_sec": 0}
        self.assertIsNone(index.resolve_occurrence(generic), "an entry ordinal cannot choose among simultaneous turns")
        explicit = index.resolve_occurrence({**generic, "source": "audio.speakers.txt"})
        self.assertEqual(explicit["speaker"], "Speaker A")
        quote = index.resolve_occurrence({**generic, "owner": "Speaker_B", "text": "> I will send the sample guide."})
        self.assertEqual(quote["speaker"], "Speaker B")
        self.assertTrue(quote["quoted"])
        self.assertEqual(quote["assertion_text"], "")
        self.assertIsNone(index.resolve_occurrence({**generic, "owner": "Speaker C"}))

    def test_summary_brackets_resolve_source_without_using_document_line(self):
        for n in range(3):
            self.snapshot(f"2030-01-01-0000-{n}")
        index = identity.MessageIndex(self.ws)
        # A generated action-item line is unrelated to its source transcript line.
        occurrences = [{"meeting": name, "source": "action-items", "line": 99}
                       for name in index.by_meeting]
        summary = "[Speaker A ~00:00:00] Prepare and distribute the guide."
        self.assertEqual(len(index.occurrence_keys(occurrences, summary)), 1)
        resolved = index.resolve_occurrence(occurrences[0], summary)
        self.assertEqual(resolved["text"], "I will send the sample guide.")
        self.assertEqual(len(resolved["provenance"]), 3)
        # No nearest-second matching and no inference from a timestamp in prose.
        for uncertain in ("[Speaker A ~00:00:01] Send it.", "Send it by 00:00:00.",
                          "[Speaker A 00:00:00 / 00:00:01] Send it.", "[Speaker A 00:00] Send it."):
            self.assertEqual(len(index.occurrence_keys(occurrences, uncertain)), 3)

    def test_audio_repeat_scoring_counts_meetings_instead_of_sentence_copies(self):
        occurrences = []
        for day in (1, 2):
            meeting = f"2030-01-{day:02}-0000"
            folder = self.ws / meeting
            folder.mkdir()
            (folder / "meeting.speakers.txt").write_text(
                "[00:00:00] Speaker A: I will send the guide.\n"
                "[00:00:01] Speaker A: I will send the guide.\n")
            occurrences += [{"meeting": meeting, "t_sec": second, "source": "transcript"}
                            for second in (0, 1)]
        index = identity.MessageIndex(self.ws)
        self.assertEqual(len(index.by_id), 4)  # all raw turns remain available
        self.assertEqual(len(index.occurrence_keys(occurrences)), 2)

    def test_import_preserves_explicit_ids_quotes_and_simultaneous_messages(self):
        export = self.ws / "input.json"
        export.write_text(json.dumps({"Sample": {"chat_id": "thread-id", "messages": [
            {"author": "Speaker A", "epoch_ms": 1893456000000, "message_id": "one", "text": "> I will send a guide.\nThanks for the update."},
            {"author": "Speaker A", "epoch_ms": 1893456000000, "message_id": "two", "text": "Another message"},
        ]}}))
        with patch.dict(os.environ, {"WHOSAID_SPEAKER_DB": str(self.ws / "no-registry.json")}):
            result = teams_chat.ingest(export, self.ws, tz_flag="UTC")
            again = teams_chat.ingest(export, self.ws, tz_flag="UTC")
        self.assertEqual(result[0]["total"], 2)
        self.assertEqual(again[0]["new"], 0)
        sidecar = json.loads(next(self.ws.glob("*/teams.diarization.json")).read_text())
        self.assertEqual(sidecar["source"]["chat_id"], "thread-id")
        self.assertEqual({s["message_id"] for s in sidecar["segments"]}, {"one", "two"})
        quote = next(s for s in sidecar["segments"] if s["message_id"] == "one")
        self.assertEqual(quote["assertion_text"], "Thanks for the update.")
        index = identity.MessageIndex(self.ws)
        row = next(r for values in index.by_meeting.values() for r in values if r["quoted"])
        self.assertEqual(row["assertion_text"], "Thanks for the update.")

    def test_quote_attribution_abstains_on_flattened_reply(self):
        parts = identity.assertion_parts("Speaker B 1/1/2030 9:30 AM I will send a guide. Thanks!")
        self.assertTrue(parts["attribution_uncertain"])
        self.assertEqual(parts["assertion_text"], "")
        for header in ("Blatt, Stephen 1/1/2030 9:30 am Sounds good.",
                       "Speaker B (External) 12/31/2030, 11:05 PM Done."):
            self.assertTrue(identity.assertion_parts(header)["attribution_uncertain"], header)
        for promise in ("I will send the guide on 1/2/2030 9:30 AM",
                        "Standup 1/15/2030 9:30 AM will move to Room 4"):
            parts = identity.assertion_parts(promise)
            self.assertEqual(parts, {"assertion_text": promise, "quoted": False, "attribution_uncertain": False})
        parts = identity.assertion_parts("Their words. My reply.", {"quoted_text": "Their words."})
        self.assertEqual(parts["assertion_text"], "My reply.")

    def test_legacy_import_does_not_overwrite_simultaneous_distinct_messages(self):
        export = self.ws / "legacy.json"
        export.write_text(json.dumps([
            {"chat": "Sample", "author": "Speaker A", "epoch_ms": 1893456000000,
             "mid": "1893456000000", "text": "First message"},
            {"chat": "Sample", "author": "Speaker B", "epoch_ms": 1893456000000,
             "text": "Second message"},
        ]))
        with patch.dict(os.environ, {"WHOSAID_SPEAKER_DB": str(self.ws / "no-registry.json")}):
            first = teams_chat.ingest(export, self.ws, tz_flag="UTC")
            again = teams_chat.ingest(export, self.ws, tz_flag="UTC")
        self.assertEqual(first[0]["total"], 2)
        self.assertEqual(again[0]["new"], 0)
        index = identity.MessageIndex(self.ws)
        self.assertEqual(len(index.by_id), 2)

    def test_identical_simultaneous_bodies_preserve_each_explicit_source_id(self):
        body = "I will send the sample guide with complete usage examples and instructions for the review group."
        for n in range(2):
            meeting = f"2030-01-01-0000-{n}"
            self.snapshot(meeting, mid="one", text=body)
            transcript = self.ws / meeting / "teams.speakers.txt"
            transcript.write_text(transcript.read_text() * 2)
            path = self.ws / meeting / "teams.diarization.json"
            sidecar = json.loads(path.read_text())
            sidecar["segments"].append({**sidecar["segments"][0], "message_id": "two", "mid": "two"})
            path.write_text(json.dumps(sidecar))
        index = identity.MessageIndex(self.ws)
        self.assertEqual(len(index.by_id), 2)
        self.assertEqual(sorted(map(len, index.by_id.values())), [2, 2])
        self.assertEqual(index.possible_duplicates, [])  # distinct IDs in a known conversation
        self.assertIsNone(index.resolve("2030-01-01-0000-0", t_sec=0))
        search.build(self.ws, embed=False)
        self.assertEqual(len(search.query(self.ws, "guide", k=10)[1]), 2)


if __name__ == "__main__":
    unittest.main()
