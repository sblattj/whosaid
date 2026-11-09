#!/usr/bin/env python3
"""teams_calendar.py: decoding, series fill, window filters and recorded-folder matching,
on synthetic rows only (no real cache read).

Set WHOSAID_TEAMS_CACHE_IT=1 to also smoke-read this Mac's real Teams calendar (prints counts only).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import teams_calendar as cal  # noqa: E402


def utc(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def ev(oid, start, end, subject="Sync", **kw):
    return {"objectId": oid, "startTime": start, "endTime": end, "subject": subject,
            "organizerName": "Bob Builder", "location": "Microsoft Teams Meeting", "isOnlineMeeting": True,
            "myResponseType": "Accepted", "showAs": "Busy", "eventTimeZone": "EasternSt",
            "skypeTeamsMeetingUrl": f"https://teams.example.test/join/{oid}", **kw}


ATT = [{"name": "Ann Example", "address": "ann@example.test", "type": "Organizer", "role": "User",
        "status": {"response": "Organizer"}},
       {"name": "Cat Coder", "address": "cat@example.test", "type": "Optional", "role": "User",
        "status": {"response": "Tentative"}}]


def raw_fixture():
    return {"events": [
        ev("M1", "2026-01-05T15:30:00", "2026-01-05T16:00:00", "Standup", eventType="RecurringMaster",
           attendees=ATT, bodyContent="<p>Daily <b>standup</b></p>"),
        ev("O1", "2026-11-09T15:30:00", "2026-11-09T16:00:00", "Standup", eventType="Occurrence",
           seriesMasterId="M1", bodyPreview=""),
        ev("O2", "2026-11-10T15:30:00", "2026-11-10T16:00:00", "Standup", eventType="Occurrence",
           seriesMasterId="M1"),
        # a series whose master is not in the cache: an exception carries the attendees
        ev("X1", "2026-11-09T18:00:00", "2026-11-09T18:30:00", "Design review", eventType="Exception",
           seriesMasterId="M2", attendees=ATT[:1], bodyContent="agenda", bodyContentType="Text"),
        ev("O3", "2026-11-11T18:00:00", "2026-11-11T18:30:00", "Design review", eventType="Occurrence",
           seriesMasterId="M2"),
        ev("S1", "2026-11-09T20:00:00", "2026-11-09T21:00:00", "Planning", attendees=ATT,
           myResponseType="NotResponded", showAs="Tentative", bodyPreview="plan the sprint"),
        ev("C1", "2026-11-09T21:00:00", "2026-11-09T21:30:00", "Old thing", isCancelled=True, attendees=[]),
        ev("F1", "2026-11-09T22:00:00", "2026-11-09T22:30:00", "Focus", showAs="Free", attendees=[]),
        ev("Z1", "2026-12-20T15:00:00", "2026-12-20T16:00:00", "Far away", attendees=[]),
        {"objectId": "bad", "startTime": "not a time"},
        {"no": "id"},
    ], "internal": {"lastSuccesfulSyncTimestamp": {"id": "x", "value": float(utc("2026-11-08T12:00:00"))},
                    "syncState": {"id": "syncState", "value": "USER_CALENDAR_UPTO_DATE"}}}


class Decode(unittest.TestCase):
    def setUp(self):
        self.data = cal.decode(raw_fixture())
        self.by = {e["id"]: e for e in self.data["events"]}

    def test_masters_and_bad_rows_dropped_sorted_utc(self):
        self.assertNotIn("M1", self.by)
        self.assertNotIn("bad", self.by)
        self.assertEqual(len(self.by), 8)
        starts = [e["start_ms"] for e in self.data["events"]]
        self.assertEqual(starts, sorted(starts))
        o1 = self.by["O1"]
        self.assertEqual(o1["start"], "2026-11-09T15:30:00Z")
        self.assertEqual(o1["start_ms"], utc("2026-11-09T15:30:00"))
        self.assertEqual(o1["minutes"], 30)
        self.assertEqual(self.data["synced_at"], "2026-11-08T12:00:00Z")
        self.assertEqual(self.data["sync_state"], "USER_CALENDAR_UPTO_DATE")

    def test_occurrence_inherits_from_master(self):
        o1 = self.by["O1"]
        self.assertEqual(o1["attendees_from"], "series")
        self.assertEqual([a["name"] for a in o1["attendees"]], ["Ann Example", "Cat Coder"])
        self.assertEqual(o1["attendees"][1], {"name": "Cat Coder", "address": "cat@example.test",
                                              "type": "Optional", "response": "Tentative"})
        self.assertEqual(o1["body"], "Daily standup")
        self.assertEqual(o1["kind"], "occurrence")
        self.assertEqual(o1["series_id"], "M1")

    def test_occurrence_inherits_from_exception_sibling(self):
        o3 = self.by["O3"]
        self.assertEqual(o3["attendees_from"], "series")
        self.assertEqual([a["name"] for a in o3["attendees"]], ["Ann Example"])
        self.assertEqual(o3["body"], "agenda")
        self.assertNotIn("attendees_from", self.by["X1"])
        self.assertEqual(self.by["X1"]["kind"], "exception")

    def test_single_fields(self):
        s1 = self.by["S1"]
        self.assertEqual(s1["kind"], "single")
        self.assertEqual(s1["response"], "NotResponded")
        self.assertEqual(s1["body"], "plan the sprint")
        self.assertTrue(s1["online"])
        self.assertEqual(s1["join_url"], "https://teams.example.test/join/S1")
        self.assertTrue(self.by["C1"]["cancelled"])


class Window(unittest.TestCase):
    def setUp(self):
        self.p = patch.object(cal, "load", lambda path=None: cal.decode(raw_fixture()))
        self.p.start()
        self.lo, self.hi = utc("2026-11-09T00:00:00"), utc("2026-11-12T00:00:00")

    def tearDown(self):
        self.p.stop()

    def test_default_hides_cancelled_and_free(self):
        res = cal.events(self.lo, self.hi)
        self.assertEqual([e["id"] for e in res["events"]], ["O1", "X1", "S1", "O2", "O3"])
        self.assertEqual(res["skipped"], {"cancelled": 1, "free": 1})
        self.assertEqual(res["covers"]["events"], 8)
        self.assertEqual(res["covers"]["last_start"], "2026-12-20T15:00:00Z")

    def test_flags_include(self):
        ids = [e["id"] for e in cal.events(self.lo, self.hi, include_cancelled=True, include_free=True)["events"]]
        self.assertIn("C1", ids)
        self.assertIn("F1", ids)

    def test_query_matches_attendee_and_subject(self):
        self.assertEqual([e["id"] for e in cal.events(self.lo, self.hi, "cat coder")["events"]], ["O1", "S1", "O2"])
        self.assertEqual([e["id"] for e in cal.events(self.lo, self.hi, "DESIGN")["events"]], ["X1", "O3"])

    def test_in_progress_event_counts(self):
        res = cal.events(utc("2026-11-09T15:45:00"), utc("2026-11-09T16:00:00"))
        self.assertEqual([e["id"] for e in res["events"]], ["O1"])

    def test_empty_window_hints_not_synced(self):
        res = cal.events(utc("2026-11-16T00:00:00"), utc("2026-11-23T00:00:00"))
        self.assertEqual(res["count"], 0)
        self.assertIn("not synced, not free", res["hint"])
        self.assertIsNone(cal.events(self.lo, self.hi)["hint"])

    def test_limit(self):
        res = cal.events(self.lo, self.hi, limit=2)
        self.assertEqual((res["total"], res["count"]), (5, 2))

    def test_recorded_folders(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            loc = lambda s: datetime.fromtimestamp(utc(s) / 1000).strftime("%Y-%m-%d-%H%M")  # noqa: E731
            (ws / loc("2026-11-09T15:25:00")).mkdir()            # 5 min early: matches O1
            (ws / (loc("2026-11-09T15:31:00") + "-2")).mkdir()   # second recording: matches O1
            (ws / loc("2026-11-09T15:10:00")).mkdir()            # 20 min early: no match
            chat = ws / loc("2026-11-09T20:05:00")
            chat.mkdir()
            (chat / "teams.diarization.json").write_text("{}")   # a chat day, not a recording
            (ws / "notes").mkdir()
            res = cal.events(self.lo, self.hi, ws=ws)
            by = {e["id"]: e for e in res["events"]}
            self.assertEqual(sorted(by["O1"]["recorded"]),
                             sorted([loc("2026-11-09T15:25:00"), loc("2026-11-09T15:31:00") + "-2"]))
            self.assertEqual(by["S1"]["recorded"], [])
            self.assertNotIn("recorded", cal.events(self.lo, self.hi)["events"][0])

    def test_cli_text_and_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cal.main(["--from", "2026-11-09", "--days", "3", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["window"]["from"][:4], "2026")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cal.main(["events", "--from", "2026-11-09", "--to", "2026-11-12"]), 0)
        self.assertIn("Standup  (Bob Builder)", out.getvalue())
        self.assertIn("not answered", out.getvalue())
        self.assertIn("hidden: 1 cancelled, 1 free", out.getvalue())


class ParseWhen(unittest.TestCase):
    def test_forms(self):
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(cal.parse_when("today", 0), int(midnight.timestamp() * 1000))
        self.assertEqual(cal.parse_when("tomorrow", 0) - cal.parse_when("today", 0), 86400000)
        self.assertEqual(cal.parse_when(None, 42), 42)
        self.assertGreater(cal.parse_when("3d", 0, future=True), cal.parse_when("now", 0))
        self.assertLess(cal.parse_when("3d", 0), cal.parse_when("now", 0))
        self.assertEqual(cal.parse_when("2026-11-09T15:30:00Z", 0), utc("2026-11-09T15:30:00"))
        with self.assertRaises(cal.CacheError):
            cal.parse_when("someday", 0)


class Missing(unittest.TestCase):
    def test_no_cache_dir(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(cal.CacheError):
            cal.load(d, use_cache=False)


@unittest.skipUnless(os.environ.get("WHOSAID_TEAMS_CACHE_IT") == "1", "real cache smoke read is opt-in")
class RealCache(unittest.TestCase):
    def test_reads(self):
        st = cal.status()
        print(f"\nreal calendar cache: {st['events']} events, {st['first_start']} .. {st['last_start']}")
        self.assertTrue(st["ok"])


if __name__ == "__main__":
    unittest.main()
