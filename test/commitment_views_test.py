#!/usr/bin/env python3
"""Synthetic integration checks for evidence across graph read views."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import commitment_views
import graph
import tracking


class CommitmentViewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.conn = sqlite3.connect(self.ws / "_search.db")
        self.conn.row_factory = sqlite3.Row
        for sql in graph.SCHEMA:
            self.conn.execute(sql)
        self.ai = {"id": "AI-001", "text": "Send the setup guide", "owner": "Alpha_Example",
                   "status": "open", "type": "leadership ask", "first_seen": "2020-01-01",
                   "last_seen": "2020-01-01", "merged_into": "", "occurrences": []}
        self.cm = {"id": "CM-001", "text": "Send the setup guide", "speaker": "Alpha_Example",
                   "status": "open", "first_seen": "2020-01-01", "ai_refs": ["AI-001"],
                   "occurrences": [{"source": "transcript", "meeting": "2020-01-01", "line": 1,
                                    "t_sec": 10, "owner": "Alpha_Example", "requester": "Delta_Example",
                                    "text": "I will send the setup guide."}]}
        self.write_corpus()
        self.conn.execute("INSERT INTO action_item VALUES (?,?,?,?,?,?,?,?,?)",
                          ("AI-001", self.ai["text"], "Alpha_Example", "open", "leadership ask",
                           "2020-01-01", "2020-01-01", "", 1))
        self.conn.execute("INSERT INTO commitment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          ("CM-001", "transcript", "2020-01-01", 1, 10, "Alpha_Example", "Delta_Example", "",
                           "I will send the setup guide.", "will", 0, "open", '["AI-001"]', ""))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def write_corpus(self):
        (self.ws / "_action-items.json").write_text(json.dumps({"items": [self.ai]}))
        (self.ws / "_commitments.json").write_text(json.dumps({"items": [self.cm]}))

    def record(self, kind="status", ident="AI-001", day="2020-01-02", **fields):
        return tracking.record_event(self.ws, {"item_id": ident, "type": kind, "occurred_at": day,
            "evidence": {"kind": "manual", "ref": "synthetic verification"}, **fields})

    def items(self, owner=None, as_of=None):
        return graph.view_items(self.conn, owner, None, None, None, as_of)

    def test_handoff_filters_use_projected_owner_and_acceptance(self):
        request = self.record("handoff_requested", owner="Beta_Example")
        self.assertEqual([], self.items("Beta_Example"))
        self.assertEqual(["AI-001"], [r["id"] for r in self.items("Alpha_Example")])
        self.record("handoff_accepted", day="2020-01-03", owner="Beta_Example", request_event_id=request["id"])
        self.assertEqual([], self.items("Alpha_Example"))
        current = commitment_views.filter_rows(self.items("Beta_Example"), accepted_only=True)
        self.assertEqual(["AI-001"], [r["id"] for r in current])
        self.assertTrue(current[0]["actionable"])
        self.assertEqual("accepted", current[0]["obligation_status"])
        self.assertEqual([], graph.view_person(self.conn, "Alpha_Example")["owned"])
        person = graph.view_person(self.conn, "Beta_Example")
        self.assertEqual(["CM-001"], [r["id"] for r in person["owned"]])
        self.assertEqual(["AI-001"], [r["id"] for r in person["owned_items"]])
        self.assertEqual([], self.items("Beta_Example", as_of="2020-01-02"))
        self.assertEqual(["AI-001"], [r["id"] for r in self.items("Alpha_Example", as_of="2020-01-02")])

    def test_single_item_view_resolves_reverse_alias_from_corpus(self):
        self.record(ident="CM-001", status="delivered")
        item = graph.view_item(self.conn, "AI-001")
        self.assertEqual("delivered", item["status"])
        self.assertEqual("delivered", item["commitments"][0]["status"])
        self.assertTrue(item["evidence_available"])

    def test_milestones_and_parent_contradiction_are_consistent_across_views(self):
        tracking.add_milestones(self.ws, "CM-001", [
            {"id": "CM-001:write", "text": "Write the guide", "acceptance": "guide exists"},
            {"id": "CM-001:send", "text": "Send the guide", "acceptance": "delivery receipt exists"},
        ], {"kind": "manual", "ref": "synthetic split"}, "2020-01-01")
        self.record(ident="CM-001:write", status="delivered")
        self.record(status="accepted")
        ai = graph.view_item(self.conn, "AI-001")
        cm = graph.view_commitments(self.conn, None, None, None)[0]
        for row in (ai, cm):
            self.assertEqual("partial", row["status"])
            self.assertEqual(["CM-001:send"], row["remaining_milestones"])
            self.assertEqual("parent_completion", row["conflicts"][0]["type"])

    def test_conflicting_handoff_is_unassigned_in_every_owner_view(self):
        self.record("owner", owner="Beta_Example")
        self.record("owner", owner="Gamma_Example")
        for owner in ("Alpha_Example", "Beta_Example", "Gamma_Example"):
            self.assertEqual([], self.items(owner))
            self.assertEqual([], graph.view_person(self.conn, owner)["owned"])
        row = self.items()[0]
        self.assertFalse(row["actionable"])
        self.assertEqual([], commitment_views.filter_rows([row], accepted_only=True))

    def test_occurrence_count_instead_of_list_does_not_crash_views(self):
        self.cm["occurrences"] = 3
        self.write_corpus()
        self.assertEqual("open", self.items()[0]["status"])

    def test_missing_completion_evidence_keeps_open_and_historical_unknown(self):
        row = self.items()[0]
        self.assertEqual("open", row["status"])
        self.assertFalse(row["evidence_available"])
        self.assertEqual([], row["tracking_history"])
        self.assertEqual("unknown", self.items(as_of="2020-01-02")[0]["status"])

    def test_future_acceptance_occurrence_is_not_used_for_historical_classification(self):
        self.ai["occurrences"] = [{"source": "transcript", "meeting": "2020-01-03", "line": 1,
                                   "speaker": "Alpha_Example", "text": "I will send the setup guide."}]
        self.write_corpus()
        self.assertEqual("Alpha_Example", self.items()[0]["accepted_owner"])
        old = self.items(as_of="2020-01-02")[0]
        self.assertFalse(old["accepted_owner"])
        self.assertEqual([], old["acceptance_evidence"])

    def test_current_title_cannot_backdate_later_acceptance(self):
        self.cm["text"] = "I will send the setup guide."
        self.cm["occurrences"][0]["text"] = "I might send the setup guide."
        self.cm["occurrences"].append({**self.cm["occurrences"][0], "meeting": "2020-01-03",
                                       "text": "I will send the setup guide."})
        self.write_corpus()
        row = {**self.cm, "owner": "Alpha_Example", "source": "commitments"}
        old = commitment_views.enrich_rows(self.ws, [row], as_of="2020-01-02")[0]
        self.assertFalse(old["accepted_owner"])
        self.assertFalse(old["actionable"])
        self.assertEqual("uncertain", old["obligation_status"])
        self.assertEqual([], old["acceptance_evidence"])
        for cutoff in (None, "2020-01-03"):
            current = commitment_views.enrich_rows(self.ws, [row], as_of=cutoff)[0]
            self.assertEqual("Alpha_Example", current["accepted_owner"])

    def test_no_historical_sources_or_future_confirmation_cannot_establish_acceptance(self):
        (self.ws / "_commitments.json").unlink()
        row = {"id": "CM-001", "text": "I will send the setup guide.", "source": "commitments",
               "owner": "Alpha_Example", "status": "open", "first_seen": "2020-01-01",
               "accepted_owner": "Alpha_Example", "acceptance_evidence": [{
                   "kind": "user_confirmation", "confirmed": True, "speaker": "Alpha_Example",
                   "at": "2020-01-03", "note": "Owner confirmed responsibility"}]}
        old = commitment_views.enrich_rows(self.ws, [row], as_of="2020-01-02")[0]
        self.assertFalse(old["accepted_owner"])
        self.assertEqual("unknown", old["status"])
        self.assertEqual([], old["acceptance_evidence"])
        row["acceptance_evidence"][0]["at"] = "2020-01-01"
        confirmed = commitment_views.enrich_rows(self.ws, [row], as_of="2020-01-02")[0]
        self.assertEqual("Alpha_Example", confirmed["accepted_owner"])

    def test_explicit_occurrence_timestamp_respects_intraday_as_of(self):
        self.ai["occurrences"] = [{"source": "transcript", "meeting": "2020-01-01", "line": 1,
                                   "occurred_at": "2020-01-01T13:00:00Z", "speaker": "Alpha_Example",
                                   "text": "I will send the setup guide."}]
        self.write_corpus()
        old = self.items(as_of="2020-01-01T12:00:00Z")[0]
        self.assertFalse(old["accepted_owner"])

    def test_historical_item_metadata_does_not_expose_future_sightings(self):
        self.ai.update(last_seen="2020-01-03", occurrences=[
            {"meeting": "2020-01-01", "line": 1}, {"meeting": "2020-01-03", "line": 1}])
        self.write_corpus()
        self.conn.execute("UPDATE action_item SET last_seen=?, occurrences=?", ("2020-01-03", 2))
        old = self.items(as_of="2020-01-02")[0]
        self.assertEqual("2020-01-01", old["last_seen"])
        self.assertEqual(1, old["occurrences"])
        self.assertNotIn("2020-01-03", old["span"])

    def test_historical_requesters_exclude_future_requests(self):
        self.conn.execute("INSERT INTO commitment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          ("CM-002", "transcript", "2020-01-03", 1, 10, "Alpha_Example", "Gamma_Example", "",
                           "Please send the setup guide.", "", 0, "open", '["AI-001"]', ""))
        self.assertIn("Gamma_Example", self.items()[0]["requesters"])
        self.assertEqual(["Delta_Example"], self.items(as_of="2020-01-02")[0]["requesters"])

    def pointer_fixture(self):
        self.ai["occurrences"] = [{"meeting": "2020-01-01", "line": 7}]
        self.cm["ai_refs"] = []
        self.cm["occurrences"] = [{"source": "action-items", "meeting": "2020-01-01", "line": 7,
                                   "text": "Send the setup guide", "owner": "Alpha_Example"}]
        self.conn.execute("UPDATE commitment SET source=?, text=?, ai_refs=?, line=?",
                          ("action-items", "Send the setup guide", "[]", 7))

    def test_unique_summary_pointer_restores_inferred_origin_and_tracking_alias(self):
        self.pointer_fixture()
        self.ai["text"] = "[inferred] Send the setup guide"
        self.write_corpus()
        self.record(status="delivered")
        row = graph.view_commitments(self.conn, None, None, None)[0]
        self.assertEqual("inferred_next_step", row["kind"])
        self.assertFalse(row["accepted_owner"])
        self.assertFalse(row["overdue_eligible"])
        self.assertEqual("delivered", row["status"])
        self.assertIn("AI-001", row["tracking_aliases"])

    def test_unique_summary_pointer_restores_directive_origin(self):
        self.pointer_fixture()
        self.ai["type"] = "standing directive"
        self.write_corpus()
        row = graph.view_commitments(self.conn, None, None, None)[0]
        self.assertEqual("standing_directive", row["kind"])
        self.assertEqual("standing", row["obligation_status"])
        self.assertFalse(row["overdue_eligible"])

    def test_summary_title_is_not_spoken_acceptance_without_unique_action_item(self):
        self.pointer_fixture()
        self.cm["text"] = "I will send the setup guide."
        self.cm["occurrences"][0]["text"] = self.cm["text"]
        row = {"id": "CM-001", "source": "commitments", "text": self.cm["text"],
               "owner": "Alpha_Example", "status": "open"}
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                self.write_corpus()
                if ambiguous:
                    second = {**self.cm, "id": "CM-002", "text": "Review the setup guide"}
                    (self.ws / "_commitments.json").write_text(json.dumps({"items": [self.cm, second]}))
                else:
                    (self.ws / "_action-items.json").unlink()
                result = commitment_views.enrich_rows(self.ws, [row])[0]
                self.assertFalse(result["accepted_owner"])
                self.assertFalse(result["actionable"])
                self.assertEqual([], result["acceptance_evidence"])

    def test_source_pointer_join_abstains_for_multiple_clauses_or_action_items(self):
        self.pointer_fixture()
        self.write_corpus()
        raw = commitment_views.corpus_rows(self.ws)
        self.assertEqual({"CM-001": "AI-001"}, commitment_views.source_pointer_pairs(raw))
        second_cm = {**raw["CM-001"], "id": "CM-002", "text": "Review the setup guide"}
        self.assertEqual({}, commitment_views.source_pointer_pairs({**raw, "CM-002": second_cm}))
        second_ai = {**raw["AI-001"], "id": "AI-002", "text": "Review the setup guide"}
        self.assertEqual({}, commitment_views.source_pointer_pairs({**raw, "AI-002": second_ai}))
        raw["CM-001"]["occurrences"][0]["source"] = "transcript"
        self.assertEqual({}, commitment_views.source_pointer_pairs(raw))


if __name__ == "__main__":
    unittest.main()
