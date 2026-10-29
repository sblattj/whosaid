#!/usr/bin/env python3
"""Offline evidence-ledger acceptance tests. All fixtures are synthetic."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import tracking


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.ws.joinpath("whosaid.toml").write_text('[groups]\nleadership = ["Speaker B"]\n')

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, kind="status", ident="AI-001", day="2020-01-02", **kwargs):
        event = {"type": kind, "item_id": ident, "occurred_at": day,
                 "evidence": {"kind": "manual", "ref": "user confirmation"}}
        if kind == "status":
            event["status"] = "delivered"
        return {**event, **kwargs}

    def item(self, **kwargs):
        return {"id": "AI-001", "text": "Send the setup guide", "status": "open",
                "owner": "Speaker A", "first_seen": "2020-01-01-0900", **kwargs}

    def view(self, **kwargs):
        return tracking.reconcile_items(self.ws, [self.item()], **kwargs)[0]

    def test_missing_ledger_does_not_write_or_claim_not_started(self):
        before = list(self.ws.iterdir())
        item = self.view()
        self.assertEqual("open", item["status"])
        self.assertEqual([], item["evidence"])
        self.assertEqual(before, list(self.ws.iterdir()))

    def test_evidence_status_history_and_idempotent_import(self):
        a = self.event(day="2020-01-02", status="in_progress")
        b = self.event(day="2020-01-03", status="delivered")
        self.assertEqual(2, tracking.record_events(self.ws, [a, b])["count"])
        self.assertEqual([], tracking.record_events(self.ws, [a, b])["added"])
        current = self.view()
        self.assertEqual("delivered", current["status"])
        self.assertFalse(current["open"])
        self.assertTrue(current["evidence_available"])
        self.assertEqual(2, len(current["tracking_history"]))
        self.assertEqual("in_progress", self.view(as_of="2020-01-02")["status"])

    def test_fractional_timestamps_keep_chronological_order(self):
        old = tracking.record_event(self.ws, self.event(day="2020-01-02T12:00:00Z", status="open"))
        tracking.record_event(self.ws, self.event(day="2020-01-02T12:00:00.100000Z", supersedes=[old["id"]]))
        self.assertEqual("delivered", self.view()["status"])
        self.assertEqual("open", self.view(as_of="2020-01-02T12:00:00.050000Z")["status"])
        for timestamp, rank in (("2020-01-02T12:00:00Z", 1), ("2020-01-02T12:00:00.100000Z", 2)):
            tracking.record_event(self.ws, self.event("priority", ident="project-x", day=timestamp,
                speaker="Speaker B", scope="delivery", text="Project X", rank=rank))
        self.assertEqual(2, tracking.priority_view(self.ws)["items"][0]["rank"])

    def test_date_as_of_includes_whole_day_and_no_future_state(self):
        tracking.record_event(self.ws, self.event(day="2020-01-02T20:00:00Z"))
        self.assertEqual("delivered", self.view(as_of="2020-01-02")["status"])
        self.assertEqual("unknown", self.view(as_of="2020-01-01")["status"])
        self.assertEqual([], tracking.reconcile_items(self.ws, [self.item(first_seen="2020-02-01")], "2020-01-01"))
        past = tracking.reconcile_items(self.ws, [self.item(status="done")], "2020-01-01")[0]
        self.assertEqual("unknown", past["status"])
        self.assertEqual([], tracking.reconcile_items(self.ws,
            [self.item(first_seen="2020-01-01T13:00:00Z")], "2020-01-01T12:00:00Z"))

    def test_equivalent_ids_overlay_the_same_evidence(self):
        tracking.record_event(self.ws, self.event())
        for alias in ({"also": ["AI-001"]}, {"ai_refs": "AI-001"}, {"ai_refs": ["AI-001"]}):
            result = tracking.reconcile_items(self.ws, [self.item(id="CM-001", **alias)])[0]
            self.assertEqual("delivered", result["status"])

    def test_aliases_are_symmetric_and_transitive_across_input_rows(self):
        tracking.record_event(self.ws, self.event(ident="CM-001"))
        rows = [self.item(), self.item(id="CM-001", ai_refs=["AI-001"]),
                self.item(id="CM-002", also=["CM-001"])]
        self.assertEqual(["delivered"] * 3, [r["status"] for r in tracking.reconcile_items(self.ws, rows)])

    def test_historical_alias_does_not_read_later_status_or_owner(self):
        tracking.record_event(self.ws, self.event(status="in_progress"))
        tracking.record_event(self.ws, self.event("owner", owner="Speaker C", day="2020-01-03"))
        tracking.record_event(self.ws, self.event(day="2020-01-03"))
        row = self.item(id="CM-001", also=["AI-001"], accepted_owner="Speaker A")
        old = tracking.reconcile_items(self.ws, [row], "2020-01-02")[0]
        self.assertEqual("in_progress", old["status"])
        self.assertEqual("Speaker A", old["accepted_owner"])
        self.assertEqual(1, len(old["tracking_history"]))
        current = tracking.reconcile_items(self.ws, [row])[0]
        self.assertEqual("delivered", current["status"])
        self.assertEqual("Speaker C", current["owner"])
        self.assertEqual("", current["accepted_owner"])

    def test_reindex_does_not_replace_curation(self):
        tracking.record_event(self.ws, self.event())
        original = self.ws.joinpath("_tracking.json").read_bytes()
        self.ws.joinpath("_action-items.json").write_text(json.dumps({"items": [self.item(status="open")]}))
        self.assertEqual("delivered", self.view()["status"])
        self.assertEqual(original, self.ws.joinpath("_tracking.json").read_bytes())

    def test_conflict_visible_and_explicit_correction(self):
        a = tracking.record_event(self.ws, self.event(status="blocked"))
        b = tracking.record_event(self.ws, self.event(status="delivered"))
        self.assertEqual("unknown", self.view()["status"])
        self.assertEqual("status", self.view()["conflicts"][0]["type"])
        tracking.record_event(self.ws, self.event(status="accepted", supersedes=[a["id"], b["id"]]))
        self.assertEqual("accepted", self.view()["status"])
        self.assertEqual([], self.view()["conflicts"])

    def test_automatic_extraction_cannot_override_curation(self):
        tracking.record_event(self.ws, self.event(status="blocked"))
        tracking.record_event(self.ws, self.event(day="2020-01-03", source="automatic"))
        self.assertEqual("blocked", self.view()["status"])

    def split(self):
        return tracking.add_milestones(self.ws, "AI-001", [
            {"id": "AI-001:open", "text": "Open the change request", "acceptance": "request URL exists"},
            {"id": "AI-001:deploy", "text": "Deploy the release", "acceptance": "environment deployment receipt"},
        ], {"kind": "manual", "ref": "confirmed split"}, "2020-01-01")

    def test_milestones_require_independent_evidence(self):
        self.split()
        tracking.record_event(self.ws, self.event(ident="AI-001:open"))
        result = self.view()
        self.assertEqual("partial", result["status"])
        self.assertEqual(["AI-001:deploy"], result["remaining_milestones"])
        tracking.record_event(self.ws, self.event(ident="AI-001:deploy", day="2020-01-03"))
        self.assertEqual("delivered", self.view()["status"])
        self.assertEqual("partial", self.view(as_of="2020-01-02")["status"])

    def test_parent_completion_cannot_hide_unfinished_milestones(self):
        self.split()
        tracking.record_event(self.ws, self.event())
        result = self.view()
        self.assertEqual("open", result["status"])
        self.assertEqual("parent_completion", result["conflicts"][0]["type"])

    def test_milestone_revision_keeps_child_history(self):
        old = self.split()
        tracking.record_event(self.ws, self.event(ident="AI-001:open"))
        revision = self.event("milestones", day="2020-01-04", supersedes=[old["id"]], milestones=[
            {"id": "AI-001:open", "text": "Submit the change request", "acceptance": "request URL exists"}])
        tracking.record_event(self.ws, revision)
        self.assertEqual("delivered", self.view()["status"])
        self.assertEqual(1, len(self.view()["milestones"][0]["tracking_history"]))

    def test_conflicting_milestone_definitions_cannot_erase_remaining_work(self):
        first = self.split()
        second = tracking.record_event(self.ws, self.event("milestones", day="2020-01-01", milestones=[
            {"id": "AI-001:open", "text": "Open the change request", "acceptance": "request URL exists"}]))
        tracking.record_event(self.ws, self.event(ident="AI-001:open"))
        result = self.view()
        self.assertEqual("unknown", result["status"])
        self.assertIn("AI-001:deploy", result["remaining_milestones"])
        self.assertEqual("milestones", result["conflicts"][0]["type"])
        self.assertEqual(2, len(result["milestone_definitions"]))
        tracking.record_event(self.ws, self.event("milestones", day="2020-01-03", supersedes=[first["id"], second["id"]],
            milestones=second["milestones"]))
        self.assertEqual("delivered", self.view()["status"])

    def test_requested_handoff_does_not_change_owner(self):
        request = tracking.record_event(self.ws, self.event("handoff_requested", owner="Speaker C"))
        self.assertEqual("Speaker A", self.view()["next_action_owner"])
        self.assertEqual(1, len(self.view()["pending_handoffs"]))
        tracking.record_event(self.ws, self.event("handoff_accepted", owner="Speaker C", day="2020-01-03", request_event_id=request["id"]))
        self.assertEqual("Speaker C", self.view()["next_action_owner"])
        self.assertEqual("Speaker C", self.view()["accepted_owner"])
        self.assertEqual("manual", self.view()["acceptance_evidence"][0]["kind"])
        self.assertEqual([], self.view()["pending_handoffs"])
        self.assertEqual("Speaker A", self.view(as_of="2020-01-02")["next_action_owner"])

    def test_conflicting_ownership_is_not_chosen_by_event_hash(self):
        tracking.record_event(self.ws, self.event("owner", owner="Speaker B"))
        tracking.record_event(self.ws, self.event("owner", owner="Speaker C"))
        result = self.view()
        self.assertEqual("", result["next_action_owner"])
        self.assertEqual("", result["accepted_owner"])
        self.assertEqual("owner", result["conflicts"][0]["type"])
        self.assertFalse(result["actionable"])
        self.assertFalse(result["overdue_eligible"])

    def test_handoff_acceptance_updates_obligation_but_assignment_does_not(self):
        row = self.item(kind="request", origin_kind="request", actionable=False,
                        obligation_status="proposed", accepted_owner=None, overdue_eligible=False)
        tracking.record_event(self.ws, self.event("owner", owner="Speaker C"))
        assigned = tracking.reconcile_items(self.ws, [row])[0]
        self.assertEqual("request", assigned["kind"])
        self.assertFalse(assigned["accepted_owner"])
        self.assertFalse(assigned["actionable"])
        request = tracking.record_event(self.ws, self.event("handoff_requested", owner="Speaker C"))
        tracking.record_event(self.ws, self.event("handoff_accepted", owner="Speaker C", day="2020-01-03", request_event_id=request["id"]))
        accepted = tracking.reconcile_items(self.ws, [row])[0]
        self.assertEqual("explicit_commitment", accepted["kind"])
        self.assertEqual("accepted", accepted["obligation_status"])
        self.assertTrue(accepted["actionable"])
        self.assertTrue(accepted["overdue_eligible"])
        tracking.record_event(self.ws, self.event(day="2020-01-04"))
        self.assertFalse(tracking.reconcile_items(self.ws, [row])[0]["overdue_eligible"])

    def test_accepted_directive_remains_standing_without_overdue_eligibility(self):
        request = tracking.record_event(self.ws, self.event("handoff_requested", owner="Speaker C"))
        tracking.record_event(self.ws, self.event("handoff_accepted", owner="Speaker C", day="2020-01-03", request_event_id=request["id"]))
        row = self.item(kind="standing_directive", origin_kind="standing_directive", actionable=False)
        result = tracking.reconcile_items(self.ws, [row])[0]
        self.assertEqual("standing_directive", result["kind"])
        self.assertEqual("standing", result["obligation_status"])
        self.assertFalse(result["accepted_owner"])
        self.assertFalse(result["actionable"])
        self.assertFalse(result["overdue_eligible"])

    def test_simultaneous_writers_preserve_both_events(self):
        script = Path(tracking.__file__)
        processes = []
        for n in range(4):
            path = self.ws / f"events-{n}.json"
            path.write_text(json.dumps([self.event(ident=f"AI-{n:03}")]))
            processes.append(subprocess.Popen([sys.executable, str(script), "import", str(self.ws), str(path)],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(0, process.returncode, stderr)
        self.assertEqual(4, len(tracking.load_ledger(self.ws)["events"]))

    def test_invalid_handoff_cannot_write(self):
        with self.assertRaises(tracking.TrackingError):
            tracking.record_event(self.ws, self.event("handoff_accepted", owner="Speaker C", request_event_id="missing"))
        self.assertFalse(self.ws.joinpath("_tracking.json").exists())

    def test_priority_changes_as_of_and_group_disclosure(self):
        a = self.event("priority", ident="project-x", day="2020-01-01", speaker="Speaker B", scope="delivery", text="Finish project X", rank=1)
        tracking.record_event(self.ws, a)
        tracking.record_event(self.ws, self.event("priority", ident="project-x", speaker="Speaker B", scope="delivery", text="Defer project X", rank=2, state="deferred"))
        tracking.record_event(self.ws, self.event("priority", ident="project-y", speaker="Speaker B", scope="delivery", text="Finish project Y", rank=1))
        current = tracking.priority_view(self.ws, "leadership")
        self.assertEqual(["Speaker B"], current["members"])
        self.assertEqual("project-y", current["items"][0]["item_id"])
        old = tracking.priority_view(self.ws, "leadership", "2020-01-01")
        self.assertEqual(["project-x"], [e["item_id"] for e in old["items"]])
        self.assertEqual("active", old["items"][0]["state"])

    def test_conflicting_priority_assertions_not_silent_consensus(self):
        for speaker, rank in (("Speaker B", 1), ("Speaker C", 3)):
            tracking.record_event(self.ws, self.event("priority", ident="project-x", speaker=speaker, scope="delivery", text="Project X", rank=rank))
        self.assertEqual(1, len(tracking.priority_view(self.ws)["conflicts"]))
        self.assertEqual([], tracking.priority_view(self.ws, "leadership")["conflicts"])
        with self.assertRaises(tracking.TrackingError):
            tracking.priority_view(self.ws, "missing")

    def test_invalid_input_and_atomic_batch(self):
        invalid = [self.event(day="yesterday"), self.event(day="2020-01-01T10:00:00"), self.event(status="not_started"),
                   self.event(evidence={"kind": "transcript", "ref": "meeting", "meeting": "m1"}),
                   self.event(evidence={"kind": "transcript", "ref": "meeting", "meeting": "m1", "line": True}),
                   self.event(evidence={"kind": "transcript", "ref": "meeting", "meeting": "m1", "t_sec": float("inf")}),
                   self.event(evidence={"kind": "manual", "ref": "test", "confidence": 2}),
                   self.event(supersedes=["missing"])]
        for event in invalid:
            with self.subTest(event=event):
                with self.assertRaises(tracking.TrackingError):
                    tracking.record_events(self.ws, [self.event(), event])
                self.assertFalse(self.ws.joinpath("_tracking.json").exists())

    def test_unmerged_pr_is_not_delivery(self):
        event = self.event(evidence={"kind": "external", "ref": "https://example.test/pull/1", "artifact_type": "pull_request", "state": "closed"})
        with self.assertRaises(tracking.TrackingError):
            tracking.record_event(self.ws, event)
        for stamp in ("false", "2020-01-03T10:00:00Z"):
            event["evidence"]["merged_at"] = stamp
            with self.assertRaises(tracking.TrackingError):
                tracking.record_event(self.ws, event)
        event["evidence"]["merged_at"] = "2020-01-01T10:00:00Z"
        tracking.record_event(self.ws, event)
        self.assertEqual("delivered", self.view()["status"])

    def test_corrupt_ledger_never_overwritten(self):
        path = self.ws / "_tracking.json"
        path.write_text("broken")
        with self.assertRaises(tracking.TrackingError):
            tracking.record_event(self.ws, self.event())
        self.assertEqual("broken", path.read_text())

    def test_cycle_rejected(self):
        events = [self.event(id="EV-a", supersedes=["EV-b"]), self.event(id="EV-b", supersedes=["EV-a"])]
        with self.assertRaises(tracking.TrackingError):
            tracking.record_events(self.ws, events)

    def turn(self, text="I sent the setup guide.", **kwargs):
        return {"text": text, "speaker": "Speaker A", "meeting": "2020-01-02-0900",
                "line": 3, "t_sec": 5, "file": "transcript.speakers.txt", **kwargs}

    def test_suggestions_are_review_required_and_deduplicated(self):
        candidates = tracking.suggest_evidence([self.item()], [self.turn(), self.turn()])
        self.assertEqual(1, len(candidates))
        self.assertEqual("delivered", candidates[0]["status"])
        self.assertTrue(candidates[0]["review_required"])
        with self.assertRaises(tracking.TrackingError):
            tracking.record_events(self.ws, candidates)
        candidates[0]["approved"] = True
        tracking.record_events(self.ws, candidates)
        self.assertEqual("delivered", self.view()["status"])

    def test_suggestions_reject_wrong_owner_early_negated_future_and_quoted(self):
        bad = [self.turn(speaker="Speaker B"), self.turn(meeting="2019-12-01"),
               self.turn("I have not sent the setup guide."), self.turn("I will have sent the setup guide."),
               self.turn(quoted=True), self.turn("> I sent the setup guide.")]
        self.assertEqual([], tracking.suggest_evidence([self.item()], bad))

    def test_compound_suggestion_does_not_complete_whole_promise(self):
        item = self.item(text="Open the setup guide change request and deploy the setup guide")
        candidates = tracking.suggest_evidence([item], [self.turn("I opened the setup guide change request.")])
        self.assertEqual("in_progress", candidates[0]["status"])
        self.assertIn("milestones", candidates[0]["reason"])

    def test_action_nouns_stay_in_subject_without_becoming_extra_promises(self):
        for promise, report in (("Send release checklist", "I sent release checklist."),
                                ("Send checklist and release plan", "I sent checklist and release plan."),
                                ("Release setup package", "I released setup package.")):
            with self.subTest(promise=promise):
                result = tracking.suggest_evidence([self.item(text=promise)], [self.turn(report)])
                self.assertEqual(1, len(result))
                self.assertEqual("delivered", result[0]["status"])
        self.assertEqual([], tracking.suggest_evidence([self.item(text="Deploy release package")],
                                                       [self.turn("I sent release package.")]))
        result = tracking.suggest_evidence([self.item(text="Send release checklist and deploy release package")],
                                            [self.turn("I sent release checklist.")])
        self.assertEqual("in_progress", result[0]["status"])

    def test_suggestion_deadline_is_checked_inside_scan(self):
        with self.assertRaises(TimeoutError):
            tracking.suggest_evidence([], [], deadline=0)
        with patch("tracking.monotonic", side_effect=[0, 0, 2]):
            with self.assertRaises(TimeoutError):
                tracking.suggest_evidence([self.item()], [self.turn(), self.turn()], deadline=1)

    def test_cli_import_and_read(self):
        path = self.ws / "events.json"
        path.write_text(json.dumps([self.event()]))
        script = Path(tracking.__file__)
        proc = subprocess.run([sys.executable, str(script), "import", str(self.ws), str(path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(1, json.loads(proc.stdout)["count"])

    def test_set_status_marks_done_as_delivered(self):
        self.ws.joinpath("_action-items.json").write_text(json.dumps({"items": [self.item()]}))
        event = tracking.set_status(self.ws, "AI-001", "done", "marked done in the whosaid app")
        self.assertEqual(("status", "delivered", "curated"), (event["type"], event["status"], event["source"]))
        self.assertEqual({"kind": "manual", "ref": "marked done in the whosaid app"}, event["evidence"])
        self.assertEqual("delivered", self.view()["status"])
        self.assertEqual(1, len(tracking.load_ledger(self.ws)["events"]))

    def test_set_status_refuses_unknown_ids_and_statuses(self):
        self.ws.joinpath("_commitments.json").write_text(json.dumps({"items": [{"id": "CM-001", "text": "x"}]}))
        with self.assertRaisesRegex(tracking.TrackingError, "no item"):
            tracking.set_status(self.ws, "AI-404", "done", "note")
        with self.assertRaisesRegex(tracking.TrackingError, "status must be"):
            tracking.set_status(self.ws, "CM-001", "finished", "note")
        with self.assertRaises(tracking.TrackingError):
            tracking.set_status(self.ws, "CM-001", "done", "  ")
        self.assertFalse(self.ws.joinpath("_tracking.json").exists())

    def test_cli_set_status(self):
        self.ws.joinpath("_commitments.json").write_text(json.dumps({"items": [{"id": "CM-001", "text": "x"}]}))
        script = Path(tracking.__file__)
        proc = subprocess.run([sys.executable, str(script), "set-status", str(self.ws), "CM-001", "in_progress",
                               "--note", "started"], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("in_progress", json.loads(proc.stdout)["status"])


if __name__ == "__main__":
    unittest.main()
