#!/usr/bin/env python3
"""Offline acceptance and attribution tests for issue #109.

Run: python3 test/obligations_test.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from obligations import KINDS, classify_item, enrich_item, filter_items, kind_counts


class ClassificationTest(unittest.TestCase):
    def test_open_request_is_unassigned_even_with_default_owner(self):
        row = enrich_item({
            "text": "Can someone review this?", "owner": "Speaker_A",
            "type": "Asks from team", "status": "open",
        })
        self.assertEqual(row["kind"], "request")
        self.assertIsNone(row["proposed_owner"])
        self.assertIsNone(row["accepted_owner"])
        self.assertEqual(row["obligation_status"], "unassigned")
        self.assertFalse(row["overdue_eligible"])
        self.assertEqual(row["owner"], "Speaker_A", "preserve extraction provenance")

    def test_direct_promise_accepts_attributed_owner(self):
        row = enrich_item({
            "text": "I will review it.", "speaker": "Speaker_A", "status": "open",
            "source": "transcript", "meeting": "meeting-one", "t_sec": 15,
        })
        self.assertEqual(row["kind"], "explicit_commitment")
        self.assertEqual(row["accepted_owner"], "Speaker_A")
        self.assertTrue(row["overdue_eligible"])
        self.assertEqual(row["acceptance_evidence"][0]["meeting"], "meeting-one")

    def test_acceptance_after_conversational_prefix(self):
        for text in ("Yeah, I'll send it.", "I read the draft and I'll send the review."):
            with self.subTest(text=text):
                self.assertEqual(classify_item({"text": text, "speaker": "Speaker_A"})["accepted_owner"], "Speaker_A")

    def test_standing_directive_is_not_an_overdue_deliverable(self):
        for text in ("Always include a test plan", "Review every pull request before merging"):
            row = classify_item({"text": text, "owner": "Speaker_A", "status": "open"})
            self.assertEqual(row["kind"], "standing_directive")
            self.assertFalse(row["overdue_eligible"])
            self.assertFalse(row["actionable"])

    def test_generated_suggestion_is_inferred(self):
        row = classify_item({
            "text": "[inferred] Schedule a review tomorrow.", "owner": "Speaker_A",
            "type": "Inferred next steps", "status": "open",
        })
        self.assertEqual(row["kind"], "inferred_next_step")
        self.assertIsNone(row["accepted_owner"])
        self.assertFalse(row["overdue_eligible"])

    def test_dynamic_own_commitment_heading_needs_actual_acceptance(self):
        row = classify_item({
            "text": "Build a report", "type": "Speaker A's own commitments",
            "owner": "Speaker_A",
        })
        self.assertEqual(row["kind"], "explicit_commitment")
        self.assertEqual(row["obligation_status"], "uncertain")
        self.assertIsNone(row["accepted_owner"])

    def test_summary_quote_and_explicit_alias(self):
        item = {
            "text": '[Speaker A 00:02:30] Send the report. "I will send the report."',
            "type": "Speaker A's own commitments", "owner": "Speaker_A_Example",
            "first_seen": "meeting-one",
        }
        self.assertIsNone(classify_item(item)["accepted_owner"])
        row = classify_item(item, aliases=["Speaker A"])
        self.assertEqual(row["accepted_owner"], "Speaker_A_Example")
        self.assertEqual(row["acceptance_evidence"][0]["evidence_quality"], "summary_quote")
        self.assertEqual(row["acceptance_evidence"][0]["t_str"], "00:02:30")

    def test_requester_cannot_accept_for_the_proposed_owner(self):
        row = classify_item({
            "text": '[Speaker B 00:00:12] Send the report. "I will need this tomorrow."',
            "type": "Asks from team", "owner": "Speaker_A",
        })
        self.assertEqual(row["kind"], "request")
        self.assertEqual(row["requester"], "Speaker B")
        self.assertIsNone(row["accepted_owner"])

    def test_wrong_speaker_in_own_heading_stays_uncertain(self):
        row = classify_item({
            "text": '[Speaker B 00:00:12] Send the report. "I will send this tomorrow."',
            "type": "Speaker A's own commitments", "owner": "Speaker_A",
        })
        self.assertEqual(row["obligation_status"], "uncertain")
        self.assertIsNone(row["accepted_owner"])

    def test_unresolved_speaker_not_attributed(self):
        row = classify_item({
            "text": "I will review this.", "speaker": "SPEAKER_03", "source": "transcript",
        })
        self.assertEqual(row["obligation_status"], "uncertain")
        self.assertIsNone(row["accepted_owner"])

    def test_nonverbatim_summary_quote_does_not_establish_acceptance(self):
        row = classify_item({
            "text": '[Speaker A 00:00:12] Send the report. "I will send this." (quote not verbatim)',
            "type": "Own commitments", "owner": "Speaker_A",
        })
        self.assertIsNone(row["accepted_owner"])

    def test_negative_and_conditional_language_not_accepted(self):
        for text in (
            "I will not review it.", "I won't review it.", "I could review it.",
            "Maybe I'll review it.", "I'll review it if there is time.",
            "I am not going to review it.", "I might review it.",
            "I will definitely not review it.", "I'll surely never review it.",
        ):
            with self.subTest(text=text):
                row = classify_item({"text": text, "speaker": "Speaker_A", "source": "transcript"})
                self.assertIsNone(row["accepted_owner"])
                self.assertFalse(row["overdue_eligible"])

    def test_reported_quote_not_attributed_to_current_speaker(self):
        row = classify_item({
            "text": 'Speaker B said "I will review it."', "speaker": "Speaker_A", "source": "transcript",
        })
        self.assertIsNone(row["accepted_owner"])

    def test_later_acceptance_promotes_request_and_retains_origin(self):
        row = classify_item({
            "text": "Can someone review this?", "type": "Asks from team", "owner": "Speaker_B",
        }, evidence=[{
            "kind": "acceptance", "speaker": "Speaker_A", "quote": "Yes, I will review it.",
            "meeting": "meeting-two", "t_sec": 25,
        }])
        self.assertEqual(row["kind"], "explicit_commitment")
        self.assertEqual(row["origin_kind"], "request")
        self.assertEqual(row["accepted_owner"], "Speaker_A")

    def test_inferred_text_cannot_self_promote(self):
        row = classify_item({
            "text": '[Speaker A 00:00:12] Review the draft. "I will review it."',
            "type": "Inferred next steps", "owner": "Speaker_A",
        })
        self.assertEqual(row["kind"], "inferred_next_step")
        self.assertIsNone(row["accepted_owner"])

    def test_inferred_task_requires_explicit_confirmation(self):
        item = {"text": "Review the draft", "type": "Inferred next steps", "owner": "Speaker_A"}
        confirmation = {
            "kind": "user_confirmation", "confirmed": True, "speaker": "Speaker_A",
            "at": "2026-01-01T10:00:00Z", "note": "I accepted this task.",
        }
        row = classify_item(item, evidence=[confirmation])
        self.assertEqual(row["kind"], "explicit_commitment")
        self.assertEqual(row["origin_kind"], "inferred_next_step")
        self.assertEqual(row["accepted_owner"], "Speaker_A")
        for broken in ({**confirmation, "confirmed": False}, {**confirmation, "note": ""}, {**confirmation, "at": None}):
            self.assertIsNone(classify_item(item, evidence=[broken])["accepted_owner"])
        self.assertEqual(classify_item(item, evidence=iter([confirmation]))["accepted_owner"], "Speaker_A")

    def test_completion_does_not_remain_overdue(self):
        row = classify_item({"text": "I will review it", "speaker": "Speaker_A", "status": "delivered"})
        self.assertEqual(row["accepted_owner"], "Speaker_A")
        self.assertFalse(row["overdue_eligible"])

    def test_acceptance_survives_reenrichment(self):
        original = {"text": "Please review it", "owner": "Speaker_A", "type": "Asks from team"}
        row = enrich_item(original, evidence=[{
            "kind": "user_confirmation", "confirmed": True, "speaker": "Speaker_A",
            "at": "2026-01-01T10:00:00Z", "note": "Accepted personally",
        }])
        self.assertEqual(enrich_item(row), row)
        self.assertNotIn("kind", original)

    def test_raw_dataclass_uses_occurrence_speaker_and_keeps_provenance(self):
        @dataclass
        class Occurrence:
            source: str = "transcript"
            owner: str = "Speaker_A"
            meeting: str = "meeting-one"
            text: str = "I will send the guide."
            t_sec: int = 25

        @dataclass
        class Commitment:
            text: str = "Send the guide"
            speaker: str = "Speaker_A"
            occurrences: list = field(default_factory=lambda: [Occurrence()])

        item = Commitment()
        row = enrich_item(item)
        self.assertEqual(row["accepted_owner"], "Speaker_A")
        self.assertEqual(row["acceptance_evidence"][0]["t_sec"], 25)
        self.assertEqual(item.text, "Send the guide")

    def test_summary_occurrence_does_not_use_default_owner_as_speaker(self):
        row = classify_item({
            "text": "Send the guide", "speaker": "Speaker_A",
            "occurrences": [{"source": "action-items", "owner": "Speaker_A", "requester": "Speaker_B", "text": "I will send the guide"}],
        })
        self.assertIsNone(row["accepted_owner"])

    def test_counts_and_filters(self):
        rows = [
            enrich_item({"text": "I will review it", "speaker": "Speaker_A"}),
            enrich_item({"text": "Can someone review it?", "owner": "Speaker_A"}),
            enrich_item({"text": "Always add tests", "owner": "Speaker_A"}),
        ]
        counts = kind_counts(rows)
        self.assertEqual(set(counts), set(KINDS))
        self.assertEqual(sum(counts.values()), 3)
        self.assertEqual(counts["request"], 1)
        self.assertEqual(len(filter_items(rows, accepted_only=True)), 1)
        self.assertEqual(len(filter_items(rows, kind="standing_directive")), 1)
        with self.assertRaises(ValueError):
            filter_items(rows, kind="invented_kind")


if __name__ == "__main__":
    unittest.main(verbosity=2)
