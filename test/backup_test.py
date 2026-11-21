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
import time
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


def stripped_text_tier(ws: Path) -> dict[str, str]:
    """What a backup without the biometric tier restores: JSON with voiceprint keys stripped (#234)."""
    out = {}
    for rel, p, tier in backup.walk_workspace(ws):
        if tier != "text":
            continue
        body = backup.stripped_body(p)[0] if p.suffix == ".json" else None
        out[rel] = hashlib.sha256(body).hexdigest() if body is not None else backup.sha256_file(p)
    return out


def encrypted_names_ok(name: str) -> bool:
    return (name in (backup.BACKUP_MANIFEST + ".age", backup.RECIPIENTS_KEY) or name.startswith("objects/")
            or (name.startswith(backup.MANIFESTS_DIR + "/") and name.endswith(".age")))


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
        self.env = mock.patch.dict(os.environ, {
            "WHOSAID_SPEAKER_DB": str(self.reg), "WHOSAID_NO_NOTIFY": "1",
            "WHOSAID_DRAFTS_DIR": str(self.tmp / "drafts"),
            "WHOSAID_LAUNCH_AGENTS_DIR": str(self.tmp / "LaunchAgents")})
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
        self.assertEqual(got, stripped_text_tier(self.ws),
                         "text tier byte-identical after restore, voiceprints stripped (no biometric tier)")

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
        self.assertTrue(all(encrypted_names_ok(n) for n in names),
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
        self.assertEqual(got, stripped_text_tier(self.ws))
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
                              capture_output=True, text=True, env=env, timeout=300, check=False)
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
        want = stripped_text_tier(self.ws)
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
        self.assertTrue(all(encrypted_names_ok(k) for k in keys))

    @unittest.skipUnless(AGE, "age binary not installed")
    def test_verify_full_gc_and_at_via_s3(self):
        key = self.tmp / "k"
        subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
        pub = [ln.split(": ", 1)[1] for ln in key.read_text().splitlines() if ln.startswith("# public key: ")][0]
        spec = f"s3://{self.bucket}/whosaid/ws"
        # the endpoint comes from [backup] in whosaid.toml only: verify --full must keep it
        os.environ.pop("WHOSAID_S3_ENDPOINT")
        (self.ws / "whosaid.toml").write_text(f'[backup]\nto = "{spec}"\ns3_endpoint = "{S3_ENDPOINT}"\n'
                                              f'recipients = ["{pub}"]\n')
        rc, _, err = run(["backup", str(self.ws), "--to", spec])
        self.assertEqual(rc, 0, err)
        first = [k for k in backup.dated_manifests(
            backup.open_target(spec, {"s3_endpoint": S3_ENDPOINT}).list_keys())]
        note = self.ws / "2026-09-14-0900" / "transcript.speakers.txt"
        note.write_text(note.read_text() + "[00:09:00] Alice: a later line\n")
        rc, _, err = run(["backup", str(self.ws), "--to", spec, "--keep", "1"])
        self.assertEqual(rc, 0, err)
        rc, out, err = run(["backup", "verify", str(self.ws), "--identity", str(key), "--full",
                            "--query", "zebra agenda", "--json"])
        self.assertEqual(rc, 0, err + out)
        self.assertTrue(json.loads(out)["full"]["ok"])
        rc, out, err = run(["backup", "gc", str(self.ws), "--identity", str(key), "--json"])
        self.assertEqual(rc, 0, err)
        self.assertGreaterEqual(len(json.loads(out)["delete"]), 1, "the old speakers.txt object is gone")
        left = backup.open_target(spec, {"s3_endpoint": S3_ENDPOINT}).list_keys()
        self.assertFalse(set(first) & set(left), "the first dated manifest was dropped (keep 1)")
        dest = self.tmp / "at"
        os.environ["WHOSAID_S3_ENDPOINT"] = S3_ENDPOINT
        stamp = __import__("pathlib").PurePosixPath(backup.dated_manifests(left)[-1]).name[:8]
        rc, _, err = run(["backup", "restore", spec, str(dest), "--identity", str(key), "--at", stamp])
        self.assertEqual(rc, 0, err)
        self.assertIn("a later line", (dest / "2026-09-14-0900" / "transcript.speakers.txt").read_text())

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


def age_key(path: Path) -> str:
    subprocess.run(["age-keygen", "-o", str(path)], check=True, capture_output=True)
    return [ln.split(": ", 1)[1] for ln in path.read_text().splitlines() if ln.startswith("# public key: ")][0]


def quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


def mode(p: Path) -> int:
    return p.stat().st_mode & 0o777


def write_watch_plist(agents: Path, ws: Path) -> None:
    import plistlib
    agents.mkdir(parents=True, exist_ok=True)
    with open(agents / "com.whosaid.watch.ws.plist", "wb") as fh:
        plistlib.dump({"Label": "com.whosaid.watch.ws",
                       "ProgramArguments": ["/usr/bin/python3", "/opt/whosaid/lib/watch.py", "run",
                                            "--source", "/fixture/Recordings", "--into", str(ws)],
                       "EnvironmentVariables": {"WHOSAID_ENGINE": "fixture", "WHOSAID_WS": str(ws)},
                       "WatchPaths": ["/fixture/Recordings"], "RunAtLoad": True}, fh)
    with open(agents / "com.whosaid.watch.other.plist", "wb") as fh:
        plistlib.dump({"Label": "com.whosaid.watch.other",
                       "ProgramArguments": ["/usr/bin/python3", "watch.py", "--into", "/elsewhere"]}, fh)


@unittest.skipUnless(AGE, "age binary not installed")
class StateTierTest(Base):
    """#234 item 2 and 3: drafts outbox, watcher state and watcher settings come back; private
    files are 0600; the watcher snapshot is re-rooted to the new workspace and never loaded."""

    def setUp(self):
        super().setUp()
        self.key = self.tmp / "k"
        self.pub = age_key(self.key)
        drafts = Path(os.environ["WHOSAID_DRAFTS_DIR"])
        (drafts / "d-001").mkdir(parents=True)
        (drafts / "d-001" / "message.md").write_text("draft about the zebra agenda\n")
        (drafts / ".outbox.lock").write_text("")
        (drafts / "d-001" / "page.html.part").write_text("half written")
        (self.ws / ".watch_state.json").write_text(json.dumps(
            {"processed": {"Old Memo.m4a:4012": {"name": "Old Memo.m4a", "at": "2026-09-14T09:00:00Z"}}}))
        write_watch_plist(Path(os.environ["WHOSAID_LAUNCH_AGENTS_DIR"]), self.ws)

    def test_state_round_trip(self):
        tgt = self.tmp / "enc-state"
        rc, out, err = run(["backup", str(self.ws), "--to", str(tgt), "--recipient", self.pub,
                            "--tier", "text,audio,state"])
        self.assertEqual(rc, 0, err)
        self.assertNotIn(b"zebra agenda", target_blob(tgt))
        new_drafts = self.tmp / "new-drafts"
        dest = self.tmp / "newhome" / "transcripts"
        with mock.patch.dict(os.environ, {"WHOSAID_DRAFTS_DIR": str(new_drafts)}):
            rc, out, err = run(["backup", "restore", str(tgt), str(dest), "--identity", str(self.key)])
        self.assertEqual(rc, 0, err)
        self.assertEqual((new_drafts / "d-001" / "message.md").read_text(), "draft about the zebra agenda\n")
        self.assertFalse((new_drafts / ".outbox.lock").exists(), "lock files are not backed up")
        self.assertFalse((new_drafts / "d-001" / "page.html.part").exists(), "partial writes are not backed up")
        st = json.loads((dest / ".watch_state.json").read_text())
        self.assertIn("Old Memo.m4a:4012", st["processed"], "watcher state restored")
        self.assertEqual(mode(dest / ".watch_state.json"), 0o600)
        self.assertEqual(mode(new_drafts / "d-001" / "message.md"), 0o600)
        self.assertEqual(mode(dest / "2026-09-14-0900" / "transcript.diarization.json"), 0o600)
        self.assertEqual(mode(dest / "_voice-groups.json"), 0o600)
        self.assertEqual(mode(dest / "whosaid.toml"), 0o600)
        snap = json.loads((dest / backup.STATE_DIR / "restored-watcher.json").read_text())
        self.assertEqual(mode(dest / backup.STATE_DIR / "restored-watcher.json"), 0o600)
        self.assertEqual([a["Label"] for a in snap["agents"]], ["com.whosaid.watch.ws"],
                         "only the agent naming this workspace")
        a = snap["agents"][0]
        self.assertIn(str(dest.resolve()), a["ProgramArguments"], "workspace path mapped to the new root")
        self.assertEqual(a["EnvironmentVariables"]["WHOSAID_WS"], str(dest.resolve()))
        self.assertNotIn(str(self.ws), json.dumps(snap), "old workspace path gone")
        self.assertNotIn(str(self.ws.resolve()), json.dumps(snap))
        self.assertEqual(a["EnvironmentVariables"]["WHOSAID_ENGINE"], "fixture")
        self.assertIn("watch install", err)

    def test_state_refused_plaintext(self):
        rc, _, err = run(["backup", str(self.ws), "--to", str(self.tmp / "p"), "--plaintext", "--tier", "text,state"])
        self.assertEqual(rc, 1)
        self.assertIn("state tier", err)


class ReseedTest(Base):
    """#234 item 3: after a restore the watcher knows the recordings by content, so a source file
    with the same bytes under another name is not transcribed again."""

    def test_restored_audio_is_not_reingested(self):
        import watch
        tgt = self.tmp / "plain"
        self.assertEqual(run(["backup", str(self.ws), "--to", str(tgt), "--plaintext", "--tier", "text,audio"])[0], 0)
        dest = self.tmp / "restored"
        rc, _, err = run(["backup", "restore", str(tgt), str(dest)])
        self.assertEqual(rc, 0, err)
        self.assertIn("content hash", err)
        state = json.loads((dest / ".watch_state.json").read_text())
        self.assertEqual(len(state["hashes"]), 2, "both meeting recordings recorded by hash")
        self.assertEqual(mode(dest / ".watch_state.json"), 0o600)
        src = self.tmp / "Recordings"
        src.mkdir()
        shutil.copy(self.ws / "2026-09-14-0900" / "meeting.m4a", src / "20260914 090000-ABCD.m4a")
        shutil.copy(self.ws / "2026-10-02-1430" / "meeting.wav", src / "renamed later.wav")
        (src / "brand new.m4a").write_bytes(b"\x00\x00\x00\x20ftypM4A " + os.urandom(2000))
        old = dt.datetime.now().timestamp() - 3600
        for f in src.iterdir():
            os.utime(f, (old, old))
        st = watch.load_state(dest)
        new, pending = watch.scan(src, st, 60)
        self.assertEqual([n for _, _, n in new], ["brand new.m4a"], "only the unseen recording is new")
        self.assertEqual(pending, [])
        self.assertEqual(sum(1 for v in st["processed"].values() if v.get("by_hash")), 2)
        # control: without the restored hashes both old recordings would be re-transcribed
        new2, _ = watch.scan(src, {"processed": {}}, 60)
        self.assertEqual(len(new2), 3)


@unittest.skipUnless(AGE, "age binary not installed")
class DatedManifestGcTest(Base):
    """#234 item 4: dated manifests, keep N, gc deletes only what no kept manifest references."""

    def setUp(self):
        super().setUp()
        self.key = self.tmp / "k"
        self.pub = age_key(self.key)
        self.tgt = self.tmp / "enc-gc"
        self.item = self.ws / "2026-10-02-1430" / "action-items.md"

    def backup(self, text: str, *extra: str):
        self.item.write_text(text)
        rc, _, err = run(["backup", str(self.ws), "--to", str(self.tgt), "--recipient", self.pub, *extra])
        self.assertEqual(rc, 0, err)
        return "objects/" + hashlib.sha256(text.encode()).hexdigest() + ".age"

    def manifests(self) -> list[str]:
        return backup.dated_manifests(backup.open_target(str(self.tgt)).list_keys())

    def test_keep_gc_and_point_in_time_restore(self):
        o1 = self.backup("- [ ] v1 heron\n", "--keep", "2")
        o2 = self.backup("- [ ] v2 heron\n", "--keep", "2")
        m2 = self.manifests()[-1]
        o3 = self.backup("- [ ] v3 heron\n", "--keep", "2")
        self.assertEqual(len(self.manifests()), 2, "keep=2 dated manifests")
        self.assertTrue(all((self.tgt / o).is_file() for o in (o1, o2, o3)), "backup itself deletes no object")
        rc, out, err = run(["backup", "gc", str(self.ws), "--to", str(self.tgt), "--keep", "2"])
        self.assertEqual(rc, 1)
        self.assertIn("--identity", err)
        rc, out, err = run(["backup", "gc", str(self.ws), "--to", str(self.tgt), "--keep", "2",
                            "--identity", str(self.key), "--dry-run"])
        self.assertEqual(rc, 0, err)
        self.assertIn(f"would delete {o1}", out)
        self.assertTrue((self.tgt / o1).is_file(), "dry run deletes nothing")
        rc, out, err = run(["backup", "gc", str(self.ws), "--to", str(self.tgt), "--keep", "2",
                            "--identity", str(self.key), "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["delete"], [o1], "only the object no kept manifest names")
        self.assertFalse((self.tgt / o1).exists())
        self.assertTrue((self.tgt / o2).is_file() and (self.tgt / o3).is_file())
        # point in time: the second backup still restores
        dest = self.tmp / "at-m2"
        stamp = PurePosixPath(m2).stem
        rc, _, err = run(["backup", "restore", str(self.tgt), str(dest), "--identity", str(self.key),
                          "--at", stamp, "--tier", "text"])
        self.assertEqual(rc, 0, err)
        self.assertEqual((dest / "2026-10-02-1430" / "action-items.md").read_text(), "- [ ] v2 heron\n")
        rc, _, err = run(["backup", "restore", str(self.tgt), str(self.tmp / "nope"), "--identity", str(self.key),
                          "--at", "19990101"])
        self.assertEqual(rc, 1)
        self.assertIn("no manifest matches", err)

    def test_gc_refuses_when_a_kept_manifest_is_unreadable(self):
        o1 = self.backup("- [ ] v1 heron\n")
        self.backup("- [ ] v2 heron\n")
        (self.tgt / self.manifests()[0]).write_bytes(b"not an age file")
        rc, _, err = run(["backup", "gc", str(self.ws), "--to", str(self.tgt), "--keep", "1",
                          "--identity", str(self.key)])
        self.assertEqual(rc, 0, err)   # keep 1: the broken older manifest is out of scope and dropped
        self.assertFalse((self.tgt / o1).exists())
        o3 = self.backup("- [ ] v3 heron\n")
        o4 = self.backup("- [ ] v4 heron\n")
        (self.tgt / self.manifests()[0]).write_bytes(b"not an age file")
        before = sorted(p.name for p in (self.tgt / "objects").iterdir())
        rc, _, err = run(["backup", "gc", str(self.ws), "--to", str(self.tgt), "--identity", str(self.key)])
        self.assertEqual(rc, 1)
        self.assertIn("nothing deleted", err)
        self.assertEqual(sorted(p.name for p in (self.tgt / "objects").iterdir()), before)
        self.assertTrue((self.tgt / o3).is_file() and (self.tgt / o4).is_file())

    def test_encrypted_prune_leaves_objects_for_gc(self):
        self.backup("- [ ] v1 heron\n")
        old = self.ws / "2026-09-14-0900" / "transcript.speakers.txt"
        obj = "objects/" + backup.sha256_file(old) + ".age"
        rc, _, err = run(["backup", str(self.ws), "--to", str(self.tgt), "--recipient", self.pub,
                          "--prune-older-than", "1d"])
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.tgt / obj).is_file(), "an older kept manifest still names it")
        self.assertIn("backup gc", err)
        rc, _, err = run(["backup", "gc", str(self.ws), "--to", str(self.tgt), "--identity", str(self.key),
                            "--keep", "1", "--json"])
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.tgt / obj).exists(), "gc --keep 1 drops it")


@unittest.skipUnless(AGE, "age binary not installed")
class VerifyTest(Base):
    """#234 item 5: objects read back and hash-checked, counts per tier, a full restore + search."""

    def setUp(self):
        super().setUp()
        self.key = self.tmp / "k"
        self.pub = age_key(self.key)
        self.tgt = self.tmp / "enc-verify"
        rc, _, err = run(["backup", str(self.ws), "--to", str(self.tgt), "--recipient", self.pub,
                          "--tier", "text,audio"])
        self.assertEqual(rc, 0, err)

    def verify(self, *extra):
        rc, out, err = run(["backup", "verify", str(self.ws), "--to", str(self.tgt), "--json", *extra])
        return rc, json.loads(out), err

    def test_clean_backup_verifies(self):
        rc, res, _ = self.verify("--identity", str(self.key), "--sample", "0")
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["checked"], res["objects"])
        self.assertEqual(res["missing"], 0)
        self.assertEqual(res["counts"]["audio"]["pending"], 0)
        self.assertEqual(res["counts"]["text"]["backup"], res["counts"]["text"]["local"])
        rc, res, _ = self.verify("--sample", "0")
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["checked"], res["objects"], "without an identity: checked by ciphertext hash")

    def test_corrupt_and_missing_objects_fail(self):
        objs = sorted((self.tgt / "objects").iterdir())
        objs[0].write_bytes(objs[1].read_bytes())        # valid age, wrong content
        objs[2].unlink()
        for extra in ([], ["--identity", str(self.key)]):
            rc, res, _ = self.verify("--sample", "0", *extra)
            self.assertEqual(rc, 1, res)
            self.assertEqual(res["missing"], 1)
            self.assertTrue(any("hash mismatch" in p for p in res["problems"]), res["problems"])

    def test_pending_files_are_counted(self):
        (self.ws / "2026-10-02-1430" / "new-notes.md").write_text("fresh\n")
        rc, res, _ = self.verify("--identity", str(self.key))
        self.assertEqual(rc, 0, res)
        self.assertEqual(res["counts"]["text"]["pending"], 1)

    def test_full_restore_and_search(self):
        rc, res, _ = self.verify("--identity", str(self.key), "--full", "--query", "zebra")
        self.assertEqual(rc, 0, res)
        self.assertGreaterEqual(res["full"]["hits"], 1)
        rc, res, _ = self.verify("--identity", str(self.key), "--full", "--query", "nosuchwordanywhere")
        self.assertEqual(rc, 1)
        self.assertTrue(any("found nothing" in p for p in res["problems"]))
        st = backup.load_local_state(self.ws, backup.open_target(str(self.tgt)))
        self.assertTrue(st.get("last_full_verify"), "the passing full verify is recorded")
        self.assertFalse(st["last_verify"]["ok"], "the last (failing) verify is recorded")


class StatusAlarmTest(Base):
    """#234 items 6 and 7: status, stale-backup and stale-verify alarms, a loud missing `age`."""

    def cfg(self, **kw):
        return {"backup": {"interval": "daily", "to": str(self.tmp / "sched"), "plaintext": True, **kw}}

    def test_status_after_backup(self):
        tgt = self.tmp / "sched"
        cfg = self.cfg()
        (self.ws / "whosaid.toml").write_text(f'[backup]\nto = "{tgt}"\ninterval = "daily"\nplaintext = true\n')
        quiet(backup.scheduled_pass, self.ws, cfg)
        rc, out, err = run(["backup", "status", str(self.ws), "--json"])
        self.assertEqual(rc, 0, err)
        info = json.loads(out)
        self.assertTrue(info["last_backup"])
        self.assertIsNone(info["last_failure"])
        self.assertGreater(info["by_tier"]["text"]["files"], 0)
        self.assertIsNone(info["last_verify"])
        rc, out, err = run(["backup", "status", str(self.ws)])
        self.assertIn("last backup:", out)
        self.assertIn("last full verify: never", out)
        # a second pass uploads nothing: the sizes still describe the whole backup, not the upload
        files = info["by_tier"]["text"]["files"]
        rc, _, err = run(["backup", str(self.ws), "--to", str(tgt), "--plaintext"])
        self.assertEqual(rc, 0, err)
        info2 = json.loads(run(["backup", "status", str(self.ws), "--json"])[1])
        self.assertEqual(info2["by_tier"]["text"]["files"], files)
        self.assertGreater(info2["by_tier"]["text"]["bytes"], 0)

    def test_write_backup_table_keeps_other_tables(self):
        (self.ws / "whosaid.toml").write_text(
            '# top comment\n[share]\nnever = ["x"]\n\n[backup]\nto = "/old"\ninterval = "daily"\n\n'
            '[[hooks]]\nname = "a"\n\n[other]\nk = 1\n')
        backup.write_backup_table(self.ws, {"to": "/new", "keep": 5})
        import tomllib
        cfg = tomllib.loads((self.ws / "whosaid.toml").read_text())
        self.assertEqual(cfg["backup"], {"to": "/new", "keep": 5})
        self.assertEqual(cfg["share"], {"never": ["x"]})
        self.assertEqual(cfg["hooks"], [{"name": "a"}])
        self.assertEqual(cfg["other"], {"k": 1})
        self.assertIn("# top comment", (self.ws / "whosaid.toml").read_text())

    def test_stale_backup_and_verify_alarm_once_then_daily(self):
        tgt = self.tmp / "sched"
        cfg = self.cfg()
        quiet(backup.scheduled_pass, self.ws, cfg)
        t = backup.open_target(str(tgt))
        st = backup.load_local_state(self.ws, t)
        now = time.time()
        st["last_backup"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3 * 86400))
        st["first_backup"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10 * 86400))
        backup.save_local_state(self.ws, t, st)
        keys = {a["key"] for a in backup.compute_alarms(self.ws, cfg, st, now)}
        self.assertEqual(keys, {"backup-stale", "verify-stale"})
        self.assertEqual(backup.compute_alarms(self.ws, self.cfg(alarms=False), st, now), [])
        with mock.patch.object(backup, "notify") as n:
            lines = backup.raise_alarms(self.ws, t, backup.compute_alarms(self.ws, cfg, st, now), now)
            self.assertEqual(n.call_count, 2)
            self.assertTrue(all(ln.startswith("ALARM ") for ln in lines))
            backup.raise_alarms(self.ws, t, backup.compute_alarms(self.ws, cfg, st, now), now + 3600)
            self.assertEqual(n.call_count, 2, "not again within a day")
            backup.raise_alarms(self.ws, t, backup.compute_alarms(self.ws, cfg, st, now), now + 90000)
            self.assertEqual(n.call_count, 4, "again after a day")
        fresh = dict(st, last_backup=backup.now_iso(), last_full_verify=backup.now_iso())
        self.assertEqual(backup.compute_alarms(self.ws, cfg, fresh), [], "control: recent backup and verify")

    def test_missing_age_fails_loudly_once(self):
        tgt = self.tmp / "enc-sched"
        cfg = {"backup": {"interval": "daily", "to": str(tgt), "recipients": ["age1fixture"]}}
        real_which = shutil.which
        with mock.patch.object(backup.shutil, "which", lambda b: None if b == "age" else real_which(b)), \
                mock.patch.dict(os.environ, {"WHOSAID_AGE": ""}), mock.patch.object(backup, "notify") as n:
            m1 = quiet(backup.scheduled_pass, self.ws, cfg)
            m2 = quiet(backup.scheduled_pass, self.ws, cfg, now=time.time() + 7200)
            _, out, _ = run(["backup", "status", str(self.ws), "--to", str(tgt)])
        for m in (m1, m2):
            self.assertIn("`age` is not installed", m)
            self.assertNotIn("will retry", m)
        self.assertEqual(n.call_count, 1, "one notification, not one per pass")
        self.assertIn("NOT INSTALLED", out)
        self.assertIn("ALARM", out)
        self.assertIn("age", backup.load_local_state(self.ws, backup.open_target(str(tgt)))["last_failure"]["error"])

    def test_failure_recorded_and_cleared(self):
        tgt = self.tmp / "sched"
        cfg = self.cfg()
        with mock.patch.object(backup, "cmd_backup", side_effect=backup.BackupError("disk full")):
            msg = quiet(backup.scheduled_pass, self.ws, cfg)
        self.assertIn("disk full", msg)
        t = backup.open_target(str(tgt))
        self.assertEqual(backup.load_local_state(self.ws, t)["last_failure"]["error"], "disk full")
        quiet(backup.scheduled_pass, self.ws, cfg)
        self.assertNotIn("last_failure", backup.load_local_state(self.ws, t))


def fresh_user_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WHOSAID_")}
    env.update({"HOME": str(home), "WHOSAID_OLLAMA": "http://127.0.0.1:9", "WHOSAID_NO_NOTIFY": "1",
                "WHOSAID_SPEAKER_DB": str(home / ".config" / "whosaid" / "speakers.json"),
                "WHOSAID_DRAFTS_DIR": str(home / ".drafts"),
                "WHOSAID_LAUNCH_AGENTS_DIR": str(home / "Library" / "LaunchAgents")})
    return env


@unittest.skipUnless(AGE, "age binary not installed")
class InitAndRestoreDrillTest(Base):
    """#234 item 8 and the restore drill: init writes both recipients and a 0600 daily key, prints
    the recovery identity once, backs up, reads back, schedules. Then a fresh user with ONLY the
    recovery identity restores, indexes and finds a known phrase."""

    def test_init_then_wiped_mac_drill(self):
        tgt = self.tmp / "nas-backup"
        daily = self.tmp / "config" / "backup.key"
        rc, out, err = run(["backup", "init", str(self.ws), "--to", str(tgt), "--identity", str(daily)])
        self.assertEqual(rc, 0, err)
        self.assertEqual(mode(daily), 0o600)
        secret = [ln for ln in out.splitlines() if ln.startswith("AGE-SECRET-KEY-")]
        self.assertEqual(len(secret), 1, "the recovery identity is printed once")
        on_disk = b"".join(p.read_bytes() for p in self.tmp.rglob("*") if p.is_file())
        self.assertNotIn(secret[0].encode(), on_disk, "the recovery identity is never written to disk")
        import tomllib
        conf = tomllib.loads((self.ws / "whosaid.toml").read_text())
        self.assertEqual(conf["workspace"]["owner"], "Alice_Example", "other tables kept")
        b = conf["backup"]
        self.assertEqual(len(b["recipients"]), 2)
        self.assertEqual(b["interval"], "daily")
        self.assertEqual(b["verify"], "weekly")
        self.assertEqual(b["tier"], "text,audio,state,biometric")
        self.assertIn("read back", err)
        self.assertTrue((tgt / (backup.BACKUP_MANIFEST + ".age")).is_file())
        self.assertNotIn(b"cluster_emb", target_blob(tgt))
        # the wiped Mac: a new HOME, no daily key, only the recovery identity on paper
        home = self.tmp / "fresh-user"
        rkey = home / "recovery.key"
        rkey.parent.mkdir(parents=True)
        rkey.write_text(secret[0] + "\n")
        dest = home / "transcripts"
        env = fresh_user_env(home)
        proc = subprocess.run([str(REPO / "whosaid"), "backup", "restore", str(tgt), str(dest),
                               "--identity", str(rkey)], capture_output=True, text=True, env=env, timeout=300, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        side = json.loads((dest / "2026-09-14-0900" / "transcript.diarization.json").read_text())
        self.assertEqual(side["cluster_emb"]["SPEAKER_00"], VOICEPRINT, "biometric tier chosen: vectors intact")
        self.assertTrue((home / ".config" / "whosaid" / "speakers.json").is_file(), "registry restored")
        self.assertTrue((dest / "2026-09-14-0900" / "meeting.m4a").is_file())
        proc = subprocess.run([str(REPO / "whosaid"), "index", str(dest), "--no-embed"],
                              capture_output=True, text=True, env=env, timeout=300, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run([str(REPO / "whosaid"), "search", str(dest), "zebra agenda", "--mode", "exact",
                               "--json"], capture_output=True, text=True, env=env, timeout=300, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        hits = json.loads(proc.stdout)
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0]["meeting"], "2026-09-14-0900")

    def test_init_requires_age_and_target(self):
        rc, _, err = run(["backup", "init", str(self.ws)])
        self.assertEqual(rc, 1)
        self.assertIn("--to", err)
        with mock.patch.object(backup.shutil, "which", lambda b: None), mock.patch.dict(os.environ, {"WHOSAID_AGE": ""}):
            rc, _, err = run(["backup", "init", str(self.ws), "--to", str(self.tmp / "t")])
        self.assertEqual(rc, 1)
        self.assertIn("age", err)
        self.assertFalse((self.tmp / "t").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
