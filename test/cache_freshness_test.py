#!/usr/bin/env python3
"""Synthetic source-dependency regressions for worklist caches and snapshots.

Real CLI reads use a 15-second deadline and disable model-dependent work.
All mutable fixtures live in temporary directories, outside the repository.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
# Reuse the existing test-only MCP SDK stub when the real SDK is absent.
from mcp_workspace_tools_test import mcp_server
import workspace


class CacheFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cache-source-test-")
        self.ws = Path(self.tmp.name)
        self.meeting = "2020-01-01-0900"
        folder = self.ws / self.meeting
        folder.mkdir()
        self.transcript = folder / "meeting.speakers.txt"
        self.transcript.write_text("[00:00:01] Alpha_Example: I will send the setup guide.\n")
        self.sidecar = folder / "meeting.diarization.json"
        self.config = self.ws / "whosaid.toml"
        self.config.write_text('[workspace]\nowner="Alpha_Example"\n[search]\nembed=false\n')
        self.item = {"id": "CM-001", "text": "I will send the setup guide.", "speaker": "Alpha_Example",
                     "status": "open", "first_seen": self.meeting, "last_seen": self.meeting,
                     "occurrences": [{"source": "transcript", "meeting": self.meeting, "line": 1,
                                      "t_sec": 1, "owner": "Alpha_Example", "text": "I will send the setup guide."}]}
        (self.ws / "_commitments.json").write_text(json.dumps({"items": [self.item], "folded_meetings": [self.meeting]}))
        self.cache = self.ws / workspace.worklist_cache_filename("Alpha_Example")
        self.payload = {"owner": "Alpha_Example", "items": [{"id": "CM-001", "status": "open",
                        "kind": "explicit_commitment", "accepted_owner": "Alpha_Example"}]}
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "WHOSAID_TODAY": "2020-01-10",
                    "WHOSAID_OLLAMA": "http://127.0.0.1:9", "WHOSAID_EMBED_FAKE": "",
                    "WHOSAID_OWNER": "", "WHOSAID_SPEAKER_DB": str(self.ws / "missing-speakers.json")}

    def tearDown(self):
        self.tmp.cleanup()

    def save_cache(self, payload=None):
        return workspace.write_worklist_cache(self.cache, payload or self.payload, workspace.corpora_digests(self.ws))

    def read_cache(self):
        result = mcp_server.whosaid_worklist(workspace=str(self.ws), owner="Alpha_Example", use_cache=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual("cache", result["source"])
        return result

    def live(self):
        result = subprocess.run([sys.executable, str(REPO / "lib/workspace.py"), "worklist", str(self.ws),
                                 "--owner", "Alpha_Example", "--json"], capture_output=True, text=True,
                                env=self.env, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def rewrite_same_mtime(self, path, text):
        stat = path.stat()
        path.write_text(text)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def assert_change_invalidates(self, change):
        self.save_cache()
        self.assertFalse(self.read_cache()["stale"])
        snapshot = mcp_server._snapshot_token(self.ws)
        cached_snapshot = self.read_cache()["snapshot_token"]
        change()
        self.assertTrue(self.read_cache()["stale"])
        self.assertNotEqual(snapshot, mcp_server._snapshot_token(self.ws))
        self.assertNotEqual(cached_snapshot, self.read_cache()["snapshot_token"])

    def contents(self):
        return {str(p.relative_to(self.ws)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.ws.rglob("*") if p.is_file()}

    def test_live_quote_correction_invalidates_previously_accepted_cache(self):
        before = self.live()
        self.assertEqual("Alpha_Example", before["items"][0]["accepted_owner"])
        self.save_cache(before)
        self.assertFalse(self.read_cache()["stale"])
        snapshot = mcp_server._snapshot_token(self.ws)
        self.rewrite_same_mtime(self.transcript, "[00:00:01] Alpha_Example: > I will send the setup guide.\n")
        files = self.contents()
        current = self.live()
        self.assertFalse(current["items"][0]["accepted_owner"])
        cached = self.read_cache()
        self.assertEqual("Alpha_Example", cached["items"][0]["accepted_owner"])
        self.assertTrue(cached["stale"])
        self.assertNotEqual(snapshot, mcp_server._snapshot_token(self.ws))
        self.assertEqual(files, self.contents(), "live and cached queries must stay read-only")

    def test_sidecar_correction_with_preserved_mtime_changes_acceptance_and_freshness(self):
        sidecar = {"source": {}, "segments": [{"speaker": "Alpha_Example", "start": 1,
                    "text": "I will send the setup guide.", "is_quote": False}]}
        self.sidecar.write_text(json.dumps(sidecar))
        self.assertTrue(self.live()["items"][0]["accepted_owner"])
        sidecar["segments"][0]["is_quote"] = True
        self.assert_change_invalidates(lambda: self.rewrite_same_mtime(self.sidecar, json.dumps(sidecar)))
        self.assertFalse(self.live()["items"][0]["accepted_owner"])

    def test_source_additions_removals_and_renames_invalidate(self):
        second = self.transcript.with_name("second.speakers.txt")
        renamed = self.transcript.with_name("renamed.speakers.txt")
        for change in (lambda: self.sidecar.write_text("{}"), self.sidecar.unlink,
                       lambda: second.write_text(self.transcript.read_text()), second.unlink,
                       lambda: self.transcript.rename(renamed), renamed.unlink):
            with self.subTest(change=change):
                self.assert_change_invalidates(change)

    def test_new_meeting_folder_invalidation_and_ignored_files(self):
        self.save_cache()
        snapshot = mcp_server._snapshot_token(self.ws)
        (self.ws / "orphan.diarization.json").write_text("{}")
        (self.transcript.parent / "orphan.diarization.json").write_text("{}")
        (self.ws / "top.speakers.txt").write_text("not consumed")
        hidden = self.ws / "_ignored"
        hidden.mkdir()
        (hidden / "hidden.speakers.txt").write_text("not consumed")
        self.assertFalse(self.read_cache()["stale"])
        self.assertEqual(snapshot, mcp_server._snapshot_token(self.ws))
        visible = self.ws / "notes"
        self.assert_change_invalidates(lambda: (visible.mkdir(), (visible / "turn.speakers.txt").write_text("a new source")))

    def test_legacy_caches_without_source_coverage_are_readable_but_stale(self):
        self.save_cache()
        original = json.loads(self.cache.read_text())
        for digests in (None, {key: value for key, value in original["corpora"].items()
                              if key in workspace.WORKLIST_CORPORA}):
            with self.subTest(digests=digests):
                old = {key: value for key, value in original.items() if key != "corpora"}
                if digests is not None:
                    old["corpora"] = digests
                self.cache.write_text(json.dumps(old))
                future = datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()
                os.utime(self.cache, (future, future))
                self.assertTrue(self.read_cache()["stale"])

    def test_empty_source_manifest_is_explicit_and_stable(self):
        self.transcript.unlink()
        self.save_cache()
        cached = self.read_cache()
        self.assertFalse(cached["stale"])
        self.assertEqual(workspace.corpora_digests(self.ws), workspace.corpora_digests(self.ws))
        old = json.loads(self.cache.read_text())
        old["corpora"] = {key: value for key, value in old["corpora"].items() if key in workspace.WORKLIST_CORPORA}
        self.cache.write_text(json.dumps(old))
        self.assertTrue(self.read_cache()["stale"])

    def test_unreadable_inputs_never_yield_a_stable_snapshot(self):
        stable = mcp_server._snapshot_token(self.ws)
        with patch.object(workspace, "corpora_digests", side_effect=OSError("file replaced mid-read")):
            first, second = mcp_server._snapshot_token(self.ws), mcp_server._snapshot_token(self.ws)
        for token in (first, second):
            self.assertIsInstance(token, str)
            self.assertNotEqual(token, stable)
        self.assertNotEqual(first, second)

    def test_identical_bytes_do_not_invalidate_or_rewrite_cache(self):
        self.save_cache()
        cached_bytes, cached_mtime = self.cache.read_bytes(), self.cache.stat().st_mtime_ns
        snapshot = mcp_server._snapshot_token(self.ws)
        self.transcript.touch()
        self.assertFalse(self.read_cache()["stale"])
        self.assertEqual(snapshot, mcp_server._snapshot_token(self.ws))
        self.assertFalse(self.save_cache())
        self.assertEqual((cached_bytes, cached_mtime), (self.cache.read_bytes(), self.cache.stat().st_mtime_ns))

    def test_timeout_still_attaches_readable_stale_cache_with_phase(self):
        self.save_cache()
        self.transcript.write_text("[00:00:01] Alpha_Example: > I will send the setup guide.\n")
        failure = {"ok": False, "category": "timeout", "phase": "match Alpha_Example", "elapsed_s": 60,
                   "error": "timed out", "hint": ""}
        with patch.object(mcp_server, "_run_ws", return_value=(None, failure)):
            result = mcp_server.whosaid_worklist(workspace=str(self.ws), accepted_only=True)
        self.assertEqual("timeout", result["category"])
        self.assertEqual("match Alpha_Example", result["phase"])
        self.assertTrue(result["cached"]["stale"])
        self.assertEqual("CM-001", result["cached"]["items"][0]["id"])


if __name__ == "__main__":
    unittest.main()
