#!/usr/bin/env python3
"""
Tests for lib/backup.py: workspace backup, restore and team share (GitHub issue #180).

Every fixture is synthetic (Alice_Example, Bob_Example, made-up text). Nothing
leaves the machine:
  - local-path targets live in a temp dir;
  - the S3 round trip runs only when WHOSAID_TEST_S3_ENDPOINT points at a local
    S3-compatible server (MinIO, or `uv run --with 'moto[server]' moto_server
    -p 19180`), with throwaway AWS_* credentials; SigV4 itself is checked
    offline against AWS's published example signature;
  - rclone is never run: its argv is recorded by a fake runner;
  - age tests run when the `age` binary is installed (brew install age).

Run:
    python3 test/backup_test.py
    WHOSAID_TEST_S3_ENDPOINT=http://127.0.0.1:19180 python3 test/backup_test.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import backup  # noqa: E402

AGE = shutil.which("age")
S3_ENDPOINT = os.environ.get("WHOSAID_TEST_S3_ENDPOINT", "")
VOICEPRINT = [0.0123, -0.4567, 0.891, 0.2222]


def sidecar(kind: str | None = None) -> dict:
    d = {"base": "transcript", "emb_model": "fixture-emb", "num_speakers": 2,
         "names": {"SPEAKER_00": "Alice_Example", "SPEAKER_01": "Bob_Example"},
         "registry_matches": [{"cluster": "SPEAKER_00", "name": "Alice_Example", "similarity": 0.91}],
         "segments": [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00"}],
         "cluster_emb": {"SPEAKER_00": VOICEPRINT, "SPEAKER_01": VOICEPRINT[::-1]},
         "source": {"path": "/fixture/meeting.m4a", "duration_seconds": 60.0}}
    if kind:
        d["source"]["kind"] = kind
    return d


def speakers_txt(lines: list[tuple[str, str, str]]) -> str:
    head = f"# Speakers ({len({s for _, s, _ in lines})}): " + ", ".join(sorted({s for _, s, _ in lines})) + "\n\n"
    return head + "".join(f"[{t}] {s}: {x}\n" for t, s, x in lines)


def make_ws(root: Path) -> Path:
    """A small synthetic workspace with every tier and every share exclusion."""
    ws = root / "ws"
    m1 = ws / "2026-09-14-0900"
    m2 = ws / "2026-10-02-1430"
    chat = ws / "2026-10-03-0815"
    secret = ws / "2026-10-04-1000"
    retired = ws / "2026-08-01-1100"
    for d in (m1, m2, chat, secret, retired):
        d.mkdir(parents=True)
    (m1 / "transcript.speakers.txt").write_text(speakers_txt([
        ("00:00:01", "Alice_Example", "Let's move the quarterly zebra review to Thursday."),
        ("00:00:09", "Bob_Example", "I'll draft the zebra agenda tonight.")]))
    (m1 / "transcript.txt").write_text("Let's move the quarterly zebra review to Thursday.\n")
    (m1 / "transcript.diarization.json").write_text(json.dumps(sidecar(), indent=2))
    (m1 / "action-items.md").write_text("- [ ] Bob_Example: draft the zebra agenda\n")
    (m1 / "meeting.m4a").write_bytes(b"\x00\x00\x00\x20ftypM4A " + os.urandom(4000))
    (m1 / "transcript.samples").mkdir()
    (m1 / "transcript.samples" / "SPEAKER_00-Alice_Example.wav").write_bytes(b"RIFF" + os.urandom(800))
    (m2 / "transcript.speakers.txt").write_text(speakers_txt([
        ("00:01:00", "Alice_Example", "The heron migration dashboard ships next sprint.")]))
    (m2 / "transcript.diarization.json").write_text(json.dumps(sidecar(), indent=2))
    (m2 / "meeting.wav").write_bytes(b"RIFF" + os.urandom(3000))
    (chat / "teams.speakers.txt").write_text(speakers_txt([
        ("08:15:00", "Bob_Example", "Posting the walrus build link here.")]))
    (chat / "teams.diarization.json").write_text(json.dumps(
        {"source": {"kind": "teams-chat", "creation_time": "2026-10-03T15:15:00Z"}, "segments": []}))
    (secret / "transcript.speakers.txt").write_text(speakers_txt([
        ("00:00:05", "Alice_Example", "Confidential: the narwhal reorg is not public yet.")]))
    (secret / "_noshare").write_text("")
    (retired / "transcript.speakers.txt").write_text(speakers_txt([
        ("00:00:05", "Bob_Example", "Old stale ocelot plan.")]))
    (retired / "_retired").write_text("")
    (ws / "_ACTION-ITEMS.md").write_text("# Action items\n- AI-001 draft the zebra agenda (2026-09-14-0900)\n"
                                         "- AI-002 narwhal reorg memo (2026-10-04-1000)\n")
    (ws / "_COMMITMENTS.md").write_text("# Commitments\n")
    (ws / "_WIKI.md").write_text("# Wiki\n")
    (ws / "_workspace.json").write_text(json.dumps({"meetings": {}}))
    (ws / "_voice-groups.json").write_text(json.dumps({"result": {"groups": [{"centroid": VOICEPRINT}]}}))
    (ws / "_reprocess-backups").mkdir()
    (ws / "_reprocess-backups" / "old.diarization.json").write_text(json.dumps(sidecar()))
    (ws / "_search.db").write_bytes(b"SQLite format 3\x00" + os.urandom(500))
    (ws / "whosaid.toml").write_text('[workspace]\nowner = "Alice_Example"\n')
    (ws / ".watch_state.json").write_text("{}")
    (ws / ".watch.log").write_text("log\n")
    return ws


def text_tier_files(ws: Path) -> dict[str, str]:
    return {rel: backup.sha256_file(p) for rel, p, tier in backup.walk_workspace(ws) if tier == "text"}


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = backup.main(argv)
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="whosaid-backup-test-"))
        self.ws = make_ws(self.tmp)
        self.reg = self.tmp / "config" / "speakers.json"
        self.reg.parent.mkdir()
        self.reg.write_text(json.dumps({"speakers": [{"name": "Alice_Example", "model": "fixture-emb",
                                                      "embedding": VOICEPRINT}]}))
        self.env = mock.patch.dict(os.environ, {"WHOSAID_SPEAKER_DB": str(self.reg)})
        self.env.start()
        for k in ("WHOSAID_BACKUP_AGE_RECIPIENT", "WHOSAID_BACKUP_AGE_IDENTITY", "WHOSAID_WORKSPACE"):
            os.environ.pop(k, None)
        self.ack = mock.patch.object(backup, "NOTICE_ACK", self.tmp / "config" / "share-ack")
        self.ack.start()

    def tearDown(self):
        self.ack.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TierTest(Base):
    def test_classify(self):
        cases = {
            "2026-09-14-0900/transcript.speakers.txt": "text",
            "2026-09-14-0900/transcript.diarization.json": "text",
            "_ACTION-ITEMS.md": "text",
            "whosaid.toml": "text",
            "_search.db": "index",
            "2026-09-14-0900/meeting.m4a": "audio",
            "2026-09-14-0900/meeting.WAV": "audio",
            "2026-09-14-0900/transcript.samples/SPEAKER_00.wav": "audio",
            ".watch_state.json": None,
            ".watch_staging/x.m4a": None,
            ".backup/hashes.json": None,
            "_tracking.lock": None,
        }
        for rel, want in cases.items():
            self.assertEqual(backup.classify(PurePosixPath(rel)), want, rel)

    def test_parse_tiers(self):
        self.assertEqual(backup.parse_tiers(None, backup.TIERS), ["text"])
        self.assertEqual(backup.parse_tiers("text,audio", backup.TIERS), ["text", "audio"])
        with self.assertRaisesRegex(backup.BackupError, "never goes to a shared target"):
            backup.parse_tiers("text,biometric", backup.SHARE_TIERS)
        with self.assertRaisesRegex(backup.BackupError, "unknown tier"):
            backup.parse_tiers("video", backup.TIERS)


class LocalBackupTest(Base):
    def test_plaintext_round_trip_dry_run_and_incremental(self):
        tgt = self.tmp / "nas" / "whosaid"
        want = text_tier_files(self.ws)
        rc, out, _ = run(["backup", str(self.ws), "--to", str(tgt), "--plaintext", "--dry-run"])
        self.assertEqual(rc, 0)
        listed = {line.split()[2] for line in out.splitlines() if line.startswith("would upload")}
        self.assertEqual(listed, set(want), "dry-run lists exactly the text tier")
        self.assertFalse(tgt.exists(), "dry-run writes nothing")

        rc, _, _ = run(["backup", str(self.ws), "--to", str(tgt), "--plaintext"])
        self.assertEqual(rc, 0)
        self.assertFalse(list(tgt.rglob("*.m4a")) or list(tgt.rglob("*.wav")), "no audio by default")
        self.assertFalse((tgt / "_search.db").exists(), "index skipped by default")

        rc, out, _ = run(["backup", str(self.ws), "--to", str(tgt), "--plaintext", "--dry-run"])
        self.assertEqual([line for line in out.splitlines() if line.startswith("would upload")], [],
                         "second pass uploads nothing")
        (self.ws / "2026-10-02-1430" / "action-items.md").write_text("- [ ] new heron item\n")
        rc, out, _ = run(["backup", str(self.ws), "--to", str(tgt), "--plaintext", "--dry-run"])
        self.assertEqual([line.split()[2] for line in out.splitlines() if line.startswith("would upload")],
                         ["2026-10-02-1430/action-items.md"], "only the changed file uploads")
        run(["backup", str(self.ws), "--to", str(tgt), "--plaintext"])

        dest = self.tmp / "restored"
        rc, _, err = run(["backup", "restore", str(tgt), str(dest)])
        self.assertEqual(rc, 0, err)
        got = {rel: backup.sha256_file(p) for rel, p, tier in backup.walk_workspace(dest) if tier == "text"}
        self.assertEqual(got, text_tier_files(self.ws), "text tier byte-identical after restore")

    def test_audio_tier_opt_in(self):
        tgt = self.tmp / "t"
        run(["backup", str(self.ws), "--to", str(tgt), "--plaintext", "--tier", "text,audio,index"])
        self.assertTrue((tgt / "2026-09-14-0900" / "meeting.m4a").is_file())
        self.assertTrue((tgt / "_search.db").is_file())

    def test_restore_conflict_needs_force(self):
        tgt = self.tmp / "t"
        run(["backup", str(self.ws), "--to", str(tgt), "--plaintext"])
        dest = self.tmp / "r"
        run(["backup", "restore", str(tgt), str(dest)])
        f = dest / "2026-09-14-0900" / "transcript.txt"
        f.write_text("hand edit\n")
        rc, _, err = run(["backup", "restore", str(tgt), str(dest)])
        self.assertEqual(rc, 1)
        self.assertIn("pass --force", err)
        self.assertEqual(f.read_text(), "hand edit\n")
        rc, _, _ = run(["backup", "restore", str(tgt), str(dest), "--force"])
        self.assertEqual(rc, 0)
        self.assertEqual(f.read_bytes(), (self.ws / "2026-09-14-0900" / "transcript.txt").read_bytes())

    def test_backup_is_additive_and_prunes(self):
        tgt = self.tmp / "t"
        run(["backup", str(self.ws), "--to", str(tgt), "--plaintext"])
        shutil.rmtree(self.ws / "2026-10-02-1430")
        run(["backup", str(self.ws), "--to", str(tgt), "--plaintext"])
        m = json.loads((tgt / backup.BACKUP_MANIFEST).read_text())
        self.assertIn("2026-10-02-1430/transcript.speakers.txt", m["files"], "local delete is not propagated")
        days = (dt.date.today() - dt.date(2026, 9, 20)).days
        run(["backup", str(self.ws), "--to", str(tgt), "--plaintext", "--prune-older-than", f"{days}d"])
        m = json.loads((tgt / backup.BACKUP_MANIFEST).read_text())
        self.assertFalse(any(k.startswith("2026-09-14-0900/") for k in m["files"]))
        self.assertFalse((tgt / "2026-09-14-0900").exists(), "pruned files deleted from the target")
        self.assertIn("2026-10-02-1430/transcript.speakers.txt", m["files"])

    def test_encryption_is_the_default(self):
        rc, _, err = run(["backup", str(self.ws), "--to", str(self.tmp / "t")])
        self.assertEqual(rc, 1)
        self.assertIn("encrypts with age by default", err)
        rc, _, err = run(["backup", str(self.ws), "--to", str(self.tmp / "t"), "--plaintext",
                          "--tier", "text,biometric"])
        self.assertEqual(rc, 1)
        self.assertIn("only backed up encrypted", err)

    def test_target_inside_workspace_refused(self):
        rc, _, err = run(["backup", str(self.ws), "--to", str(self.ws / "bk"), "--plaintext"])
        self.assertEqual(rc, 1)
        self.assertIn("inside the workspace", err)


@unittest.skipUnless(AGE, "age binary not installed (brew install age)")
class AgeBackupTest(Base):
    def setUp(self):
        super().setUp()
        self.key = self.tmp / "backup.key"
        subprocess.run([shutil.which("age-keygen") or "age-keygen", "-o", str(self.key)],
                       check=True, capture_output=True)
        self.pub = [ln.split(": ", 1)[1] for ln in self.key.read_text().splitlines()
                    if ln.startswith("# public key: ")][0]

    def test_encrypted_round_trip_with_registry(self):
        tgt = self.tmp / "enc"
        rc, _, err = run(["backup", str(self.ws), "--to", str(tgt), "--recipient", self.pub,
                          "--tier", "text,biometric"])
        self.assertEqual(rc, 0, err)
        names = {p.relative_to(tgt).as_posix() for p in tgt.rglob("*") if p.is_file()}
        self.assertIn(backup.BACKUP_MANIFEST + ".age", names)
        self.assertTrue(all(n in (backup.BACKUP_MANIFEST + ".age", backup.RECIPIENTS_KEY)
                            or n.startswith("objects/") for n in names),
                        "no plaintext file names on the target")
        blob = b"".join(p.read_bytes() for p in tgt.rglob("*") if p.is_file())
        for needle in (b"zebra", b"Alice_Example", b"cluster_emb", b"0.4567"):
            self.assertNotIn(needle, blob, f"plaintext {needle!r} leaked into the encrypted target")

        rc, _, err = run(["backup", "restore", str(tgt), str(self.tmp / "r")])
        self.assertEqual(rc, 1)
        self.assertIn("--identity", err)

        reg_out = self.tmp / "restored-speakers.json"
        rc, _, err = run(["backup", "restore", str(tgt), str(self.tmp / "r"), "--identity", str(self.key),
                          "--registry-out", str(reg_out)])
        self.assertEqual(rc, 0, err)
        got = {rel: backup.sha256_file(p) for rel, p, tier in backup.walk_workspace(self.tmp / "r")}
        self.assertEqual(got, text_tier_files(self.ws))
        self.assertEqual(reg_out.read_bytes(), self.reg.read_bytes(), "registry round-trips byte-identical")

        # incremental: unchanged content is not re-encrypted or re-uploaded
        rc, out, _ = run(["backup", str(self.ws), "--to", str(tgt), "--recipient", self.pub,
                          "--tier", "text,biometric", "--dry-run"])
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("would upload")], [])

    def test_new_recipient_reencrypts_instead_of_reusing_objects(self):
        # content-addressed objects encrypted to key A must not count as "unchanged" for key B
        tgt = self.tmp / "enc-rotate"
        rc, _, err = run(["backup", str(self.ws), "--to", str(tgt), "--recipient", self.pub])
        self.assertEqual(rc, 0, err)
        key_b = self.tmp / "b.key"
        subprocess.run([shutil.which("age-keygen") or "age-keygen", "-o", str(key_b)],
                       check=True, capture_output=True)
        pub_b = [ln.split(": ", 1)[1] for ln in key_b.read_text().splitlines()
                 if ln.startswith("# public key: ")][0]
        shutil.rmtree(self.ws / backup.STATE_DIR)   # e.g. a new machine: no local record
        rc, out, err = run(["backup", str(self.ws), "--to", str(tgt), "--recipient", pub_b])
        self.assertEqual(rc, 0, err)
        self.assertIn("re-encrypting every object", err)
        self.assertEqual(sum(ln.startswith("upload ") for ln in out.splitlines()), len(text_tier_files(self.ws)))
        rc, _, err = run(["backup", "restore", str(tgt), str(self.tmp / "rb"), "--identity", str(key_b)])
        self.assertEqual(rc, 0, err)
        got = {rel: backup.sha256_file(p) for rel, p, tier in backup.walk_workspace(self.tmp / "rb")}
        self.assertEqual(got, text_tier_files(self.ws))
        # same recipients again: nothing to re-upload
        rc, out, _ = run(["backup", str(self.ws), "--to", str(tgt), "--recipient", pub_b, "--dry-run"])
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("would upload")], [])


def target_blob(tgt: Path) -> bytes:
    return b"".join(p.read_bytes() for p in tgt.rglob("*") if p.is_file())


class PersonalBackupVoiceprintTest(Base):
    """#234 item 1: a personal backup without the biometric tier must not carry voiceprint vectors.
    The plaintext variant runs without `age` (CI); the age variants are in AgeVoiceprintTest."""

    def test_text_audio_plaintext_backup_strips_cluster_emb(self):
        tgt = self.tmp / "t"
        rc, _, err = run(["backup", str(self.ws), "--to", str(tgt), "--plaintext", "--tier", "text,audio"])
        self.assertEqual(rc, 0, err)
        blob = target_blob(tgt)
        self.assertNotIn(b"cluster_emb", blob, "voiceprint key reached the target without the biometric tier")
        self.assertNotIn(b"0.4567", blob, "voiceprint value reached the target")
        self.assertNotIn(b"centroid", blob, "_voice-groups.json centroid reached the target")
        dest = self.tmp / "r"
        rc, _, err = run(["backup", "restore", str(tgt), str(dest)])
        self.assertEqual(rc, 0, err)
        side = json.loads((dest / "2026-09-14-0900" / "transcript.diarization.json").read_text())
        self.assertNotIn("cluster_emb", side)
        self.assertEqual(side["names"]["SPEAKER_00"], "Alice_Example", "the rest of the sidecar survives")
        self.assertTrue((dest / "2026-09-14-0900" / "meeting.m4a").is_file(), "audio still restored")
        # restoring over the intact original: the stripped copy is not a conflict, and --force
        # never destroys the local vectors
        for extra in ([], ["--force"]):
            rc, _, err = run(["backup", "restore", str(tgt), str(self.ws), *extra])
            self.assertEqual(rc, 0, err)
            self.assertIn("cluster_emb", (self.ws / "2026-09-14-0900" / "transcript.diarization.json").read_text())
        # incremental: the stripped sidecar is unchanged on the next pass
        rc, out, _ = run(["backup", str(self.ws), "--to", str(tgt), "--plaintext", "--tier", "text,audio",
                          "--dry-run"])
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("would upload")], [])


@unittest.skipUnless(AGE, "age binary not installed (brew install age)")
class AgeVoiceprintTest(Base):
    def setUp(self):
        super().setUp()
        self.key = self.tmp / "backup.key"
        subprocess.run(["age-keygen", "-o", str(self.key)], check=True, capture_output=True)
        self.pub = [ln.split(": ", 1)[1] for ln in self.key.read_text().splitlines()
                    if ln.startswith("# public key: ")][0]

    def restored_sidecar(self, tiers: str) -> dict:
        tgt = self.tmp / f"enc-{tiers.replace(',', '-')}"
        rc, _, err = run(["backup", str(self.ws), "--to", str(tgt), "--recipient", self.pub, "--tier", tiers])
        self.assertEqual(rc, 0, err)
        dest = self.tmp / f"r-{tiers.replace(',', '-')}"
        rc, _, err = run(["backup", "restore", str(tgt), str(dest), "--identity", str(self.key),
                          "--registry-out", str(self.tmp / f"reg-{tiers.replace(',', '-')}.json")])
        self.assertEqual(rc, 0, err)
        return json.loads((dest / "2026-09-14-0900" / "transcript.diarization.json").read_text())

    def test_without_biometric_restores_without_cluster_emb(self):
        self.assertNotIn("cluster_emb", self.restored_sidecar("text,audio"))

    def test_with_biometric_restores_intact(self):
        side = self.restored_sidecar("text,audio,biometric")
        self.assertEqual(side["cluster_emb"]["SPEAKER_00"], VOICEPRINT)
        self.assertEqual(side, json.loads((self.ws / "2026-09-14-0900" / "transcript.diarization.json").read_text()))


class ShareTest(Base):
    def share(self, *extra, yes=True):
        tgt = self.tmp / "team-share"
        argv = ["share", str(self.ws), "--to", str(tgt), *extra] + (["--yes"] if yes else [])
        return (*run(argv), tgt)

    def assert_no_voiceprints_or_audio(self, tgt: Path, audio_ok: bool = False):
        for p in tgt.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(tgt).as_posix()
            data = p.read_bytes()
            self.assertNotIn(b"cluster_emb", data, rel)
            self.assertNotIn(b"0.4567", data, f"voiceprint value in {rel}")
            self.assertNotIn(b"centroid", data, rel)
            self.assertNotEqual(p.name, "speakers.json", rel)
            if not audio_ok:
                self.assertNotIn(p.suffix.lower(), backup.AUDIO_EXT, rel)
                self.assertNotIn(".samples/", rel)

    def test_first_share_needs_yes_and_writes_nothing(self):
        rc, _, err, tgt = self.share(yes=False)
        self.assertEqual(rc, 3)
        self.assertIn("consent", err)
        self.assertIn("--yes", err)
        self.assertFalse(tgt.exists())

    def test_share_never_contains_voiceprints_or_audio(self):
        rc, _, err, tgt = self.share()
        self.assertEqual(rc, 0, err)
        self.assert_no_voiceprints_or_audio(tgt)
        side = json.loads((tgt / "2026-09-14-0900" / "transcript.diarization.json").read_text())
        self.assertNotIn("cluster_emb", side)
        self.assertEqual(side["names"]["SPEAKER_00"], "Alice_Example", "the rest of the sidecar survives")
        self.assertTrue((tgt / "2026-09-14-0900" / "transcript.speakers.txt").is_file())
        self.assertTrue((tgt / "2026-10-03-0815" / "teams.speakers.txt").is_file(), "chat days included")
        for kept in ("2026-10-04-1000", "2026-08-01-1100", "_reprocess-backups"):
            self.assertFalse((tgt / kept).exists(), f"{kept} must stay local")
        for f in ("_voice-groups.json", "whosaid.toml", "_workspace.json", "_search.db"):
            self.assertFalse((tgt / f).exists(), f)
        self.assertFalse((tgt / "_ACTION-ITEMS.md").exists(),
                         "roll-ups mention the _noshare meeting, so they stay local")
        m = json.loads((tgt / backup.SHARE_MANIFEST).read_text())
        self.assertEqual(m["meetings"], ["2026-09-14-0900", "2026-10-02-1430", "2026-10-03-0815"])
        self.assertEqual(m["excluded_counts"], {"noshare": 1, "retired": 1})

    def test_audio_only_when_asked_biometric_never(self):
        rc, _, err, tgt = self.share("--tier", "text,audio")
        self.assertEqual(rc, 0, err)
        self.assertTrue((tgt / "2026-09-14-0900" / "meeting.m4a").is_file())
        self.assert_no_voiceprints_or_audio(tgt, audio_ok=True)
        rc, _, err, _ = self.share("--tier", "text,biometric")
        self.assertEqual(rc, 1)
        self.assertIn("never goes to a shared target", err)

    def test_filters(self):
        rc, _, _, tgt = self.share("--exclude-chat")
        self.assertFalse((tgt / "2026-10-03-0815").exists())
        shutil.rmtree(tgt)
        rc, _, _, tgt = self.share("--since", "2026-10-01")
        self.assertEqual(sorted(p.name for p in tgt.iterdir() if p.is_dir()),
                         ["2026-10-02-1430", "2026-10-03-0815"])
        shutil.rmtree(tgt)
        rc, _, _, tgt = self.share("--meetings", "2026-10-02-1430")
        self.assertEqual([p.name for p in tgt.iterdir() if p.is_dir()], ["2026-10-02-1430"])
        rc, _, err, _ = self.share("--meetings", "2026-10-04-1000")
        self.assertEqual(rc, 0)
        self.assertFalse((tgt / "2026-10-04-1000").exists(), "_noshare beats --meetings")

    def test_config_never_and_complete_share_rollups(self):
        (self.ws / "2026-10-04-1000" / "_noshare").unlink()
        (self.ws / "2026-08-01-1100" / "_retired").unlink()
        rc, _, _, tgt = self.share()
        self.assertTrue((tgt / "_ACTION-ITEMS.md").is_file(), "complete share carries roll-ups")
        self.assertFalse((tgt / "_voice-groups.json").exists())
        (self.ws / "whosaid.toml").write_text('[share]\nnever = ["2026-10-04-1000"]\n')
        rc, out, _, tgt = self.share()
        self.assertFalse((tgt / "2026-10-04-1000").exists(), "revoked meeting deleted from the target")
        self.assertFalse((tgt / "_ACTION-ITEMS.md").exists(), "roll-ups withdrawn once a meeting is held back")
        self.assertIn("delete 2026-10-04-1000/transcript.speakers.txt", out)

    def test_share_into_backup_refused(self):
        tgt = self.tmp / "team-share"
        run(["backup", str(self.ws), "--to", str(tgt), "--plaintext"])
        rc, _, err, _ = self.share()
        self.assertEqual(rc, 1)
        self.assertIn("personal backup", err)


class PulledShareSearchTest(Base):
    """A teammate pulls a share, rebuilds the index, and whosaid_search answers."""

    def test_pull_index_search(self):
        tgt = self.tmp / "team-share"
        self.assertEqual(run(["share", str(self.ws), "--to", str(tgt), "--yes"])[0], 0)
        pulled = self.tmp / "teammate"
        rc, _, err = run(["share", "pull", str(tgt), str(pulled)])
        self.assertEqual(rc, 0, err)
        env = {**os.environ, "HOME": str(self.tmp), "WHOSAID_OLLAMA": "http://127.0.0.1:9"}
        proc = subprocess.run([str(REPO / "whosaid"), "index", str(pulled), "--no-embed"],
                              capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sys.path.insert(0, str(REPO / "test"))
        import mcp_workspace_tools_test as mt  # installs the MCP SDK stub when needed
        if "mcp" not in sys.modules:
            mt._install_sdk_stub()
        import mcp_server
        res = mcp_server.whosaid_search("zebra agenda", mode="exact", workspace=str(pulled))
        self.assertTrue(res.get("ok"), res)
        self.assertGreaterEqual(res["count"], 1, res)
        self.assertEqual(res["hits"][0]["meeting"], "2026-09-14-0900")
        res = mcp_server.whosaid_search("narwhal", mode="exact", workspace=str(pulled))
        self.assertEqual(res.get("count"), 0, "_noshare meeting is not searchable on the teammate side")


class S3SigningTest(unittest.TestCase):
    def test_sigv4_matches_aws_example(self):
        # "GET Object" example from the AWS SigV4 for S3 documentation (header-based auth).
        t = backup.S3Target("examplebucket", "", env={
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWS_REGION": "us-east-1"})
        t.host = "examplebucket.s3.amazonaws.com"
        h = t.sign("GET", "/test.txt", {}, hashlib.sha256(b"").hexdigest(),
                   now=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc), extra={"Range": "bytes=0-9"})
        self.assertTrue(h["authorization"].endswith(
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"), h["authorization"])
        self.assertIn("SignedHeaders=host;range;x-amz-content-sha256;x-amz-date", h["authorization"])

    def test_endpoint_and_missing_credentials(self):
        t = backup.S3Target("bk", "team/ws", env={"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b",
                                                  "WHOSAID_S3_ENDPOINT": "http://127.0.0.1:9000"})
        self.assertEqual((t.scheme, t.host, t.base_path), ("http", "127.0.0.1:9000", "/bk"))
        self.assertEqual(t.describe(), "s3://bk/team/ws")
        with self.assertRaisesRegex(backup.BackupError, "AWS_ACCESS_KEY_ID"):
            backup.S3Target("bk", "", env={})


@unittest.skipUnless(S3_ENDPOINT, "set WHOSAID_TEST_S3_ENDPOINT to a local MinIO/moto server")
class S3RoundTripTest(Base):
    def setUp(self):
        super().setUp()
        self.bucket = f"whosaid-test-{uuid.uuid4().hex[:8]}"
        self.s3env = mock.patch.dict(os.environ, {
            "WHOSAID_S3_ENDPOINT": S3_ENDPOINT, "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", "testing"),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", "testing"),
            "AWS_REGION": "us-east-1"})
        self.s3env.start()
        backup.open_target(f"s3://{self.bucket}")._request("PUT", None)  # create the bucket

    def tearDown(self):
        self.s3env.stop()
        super().tearDown()

    def round_trip(self, extra: list[str], restore_extra: list[str]):
        spec = f"s3://{self.bucket}/whosaid/ws"
        want = text_tier_files(self.ws)
        rc, out, err = run(["backup", str(self.ws), "--to", spec, *extra, "--dry-run"])
        self.assertEqual(rc, 0, err)
        self.assertEqual({ln.split()[2] for ln in out.splitlines() if ln.startswith("would upload")}, set(want))
        rc, _, err = run(["backup", str(self.ws), "--to", spec, *extra])
        self.assertEqual(rc, 0, err)
        rc, out, _ = run(["backup", str(self.ws), "--to", spec, *extra, "--dry-run"])
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("would upload")], [])
        dest = self.tmp / "from-s3"
        rc, _, err = run(["backup", "restore", spec, str(dest), *restore_extra])
        self.assertEqual(rc, 0, err)
        got = {rel: backup.sha256_file(p) for rel, p, tier in backup.walk_workspace(dest) if tier == "text"}
        self.assertEqual(got, want, "text tier byte-identical through S3")
        return spec

    def test_plaintext_round_trip(self):
        spec = self.round_trip(["--plaintext"], [])
        keys = backup.open_target(spec).list_keys()
        self.assertIn("2026-09-14-0900/transcript.speakers.txt", keys)

    @unittest.skipUnless(AGE, "age binary not installed")
    def test_encrypted_round_trip(self):
        key = self.tmp / "k"
        subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
        pub = [ln.split(": ", 1)[1] for ln in key.read_text().splitlines() if ln.startswith("# public key: ")][0]
        spec = self.round_trip(["--recipient", pub], ["--identity", str(key)])
        keys = backup.open_target(spec).list_keys()
        self.assertTrue(all(k.startswith("objects/") or k in (backup.BACKUP_MANIFEST + ".age", backup.RECIPIENTS_KEY)
                            for k in keys))

    def test_share_via_s3(self):
        spec = f"s3://{self.bucket}/share"
        rc, _, err = run(["share", str(self.ws), "--to", spec, "--yes"])
        self.assertEqual(rc, 0, err)
        keys = backup.open_target(spec).list_keys()
        self.assertFalse(any(k.endswith((".m4a", ".wav")) for k in keys))
        body = backup.open_target(spec).get_bytes("2026-09-14-0900/transcript.diarization.json")
        self.assertIn(b"Alice_Example", body, "the sidecar arrived with its content")
        self.assertNotIn(b"cluster_emb", body)
        pulled = self.tmp / "pulled-from-s3"
        rc, _, err = run(["share", "pull", spec, str(pulled)])
        self.assertEqual(rc, 0, err)
        self.assertIn("zebra agenda", (pulled / "2026-09-14-0900" / "transcript.speakers.txt").read_text())


class RcloneCommandTest(unittest.TestCase):
    def test_command_construction(self):
        calls = []

        def runner(argv, stdin=None):
            calls.append((argv, stdin))
            out = b"a/b.txt\n_whosaid-share.json\n" if argv[1] == "lsf" else b"payload"
            return subprocess.CompletedProcess(argv, 0, out, b"")

        t = backup.RcloneTarget("onedrive:Work/whosaid", runner=runner, binary="rclone")
        self.assertEqual(t.describe(), "rclone:onedrive:Work/whosaid")
        self.assertEqual(t.list_keys(), {"a/b.txt", "_whosaid-share.json"})
        t.put_file("2026-09-14-0900/transcript.txt", Path("/tmp/x.txt"))
        t.put_bytes("_whosaid-share.json", b"{}")
        self.assertEqual(t.get_bytes("a/b.txt"), b"payload")
        t.get_to_file("a/b.txt", Path("/tmp/y"))
        t.delete("a/b.txt")
        self.assertEqual([c[0] for c in calls], [
            ["rclone", "lsf", "-R", "--files-only", "onedrive:Work/whosaid"],
            ["rclone", "copyto", "/tmp/x.txt", "onedrive:Work/whosaid/2026-09-14-0900/transcript.txt"],
            ["rclone", "rcat", "onedrive:Work/whosaid/_whosaid-share.json"],
            ["rclone", "cat", "onedrive:Work/whosaid/a/b.txt"],
            ["rclone", "copyto", "onedrive:Work/whosaid/a/b.txt", "/tmp/y"],
            ["rclone", "deletefile", "onedrive:Work/whosaid/a/b.txt"],
        ])
        self.assertEqual(calls[2][1], b"{}", "rcat streams the bytes on stdin")
        root = backup.RcloneTarget("dropbox:", runner=runner, binary="rclone")
        self.assertEqual(root.path("x/y.txt"), "dropbox:x/y.txt")
        with self.assertRaises(backup.BackupError):
            root.path("../escape")

    def test_open_target_dispatch(self):
        self.assertIsInstance(backup.open_target("rclone:gdrive:whosaid"), backup.RcloneTarget)
        self.assertIsInstance(backup.open_target("/tmp/x"), backup.LocalTarget)
        self.assertIsInstance(backup.open_target("file:///tmp/x"), backup.LocalTarget)
        with self.assertRaisesRegex(backup.BackupError, "unsupported target"):
            backup.open_target("ftp://host/x")


class ScheduledPassTest(Base):
    def test_interval(self):
        self.assertIsNone(backup.interval_seconds(None))
        self.assertEqual(backup.interval_seconds("daily"), 86400)
        self.assertEqual(backup.interval_seconds("12h"), 43200)

    def test_watcher_runs_backup_when_due(self):
        tgt = self.tmp / "sched"
        cfg = {"backup": {"interval": "daily", "to": str(tgt), "plaintext": True}}
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            msg = backup.scheduled_pass(self.ws, cfg)
        self.assertIn("ran", msg)
        self.assertTrue((tgt / backup.BACKUP_MANIFEST).is_file())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(backup.scheduled_pass(self.ws, cfg), "", "not due again within a day")
            msg = backup.scheduled_pass(self.ws, cfg, now=dt.datetime.now().timestamp() + 2 * 86400)
        self.assertIn("ran", msg)
        self.assertEqual(backup.scheduled_pass(self.ws, {"backup": {}}), "", "off by default")
        import watch
        with mock.patch.object(watch, "tlog") as tl, redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            watch.backup_pass(self.ws, {"backup": {"interval": "daily"}}, False)
        self.assertIn("[backup] to is not", tl.call_args[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
