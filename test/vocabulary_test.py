#!/usr/bin/env python3
"""Issue #216 (workspace vocabulary half): Whisper initial prompt + corrections post-pass.

Synthetic data only. No models, no audio, no network.

Run:
    python3 test/vocabulary_test.py
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import vocabulary  # noqa: E402  (a missing module is the "before" failure)
import wsconfig  # noqa: E402

SENTENCE = "we should move the blobser live into the shared repo and use SD for the objects"
CORRECTIONS = {"blobser live": "blob store lib", "SD": "S3"}


class CorrectTextTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(vocabulary.parse_corrections(["blobser=blob store", "SD=S3"]),
                         {"blobser": "blob store", "SD": "S3"})
        with self.assertRaises(ValueError):
            vocabulary.parse_corrections(["no-equals"])
        with self.assertRaises(ValueError):
            vocabulary.parse_corrections(["=x"])

    def test_word_boundary(self):
        c = {"blobser": "blob store"}
        self.assertEqual(vocabulary.correct_text("the blobser, ok", c), "the blob store, ok")
        self.assertEqual(vocabulary.correct_text("blobsers and xblobser", c), "blobsers and xblobser")

    def test_case_rule(self):
        self.assertEqual(vocabulary.correct_text("sd and SD", {"SD": "S3"}), "sd and S3")
        self.assertEqual(vocabulary.correct_text("Blobser BLOBSER", {"blobser": "blob store"}),
                         "blob store blob store")

    def test_longest_first(self):
        c = {"blobser": "blob store", "blobser live": "blob store lib"}
        self.assertEqual(vocabulary.correct_text("the blobser live is up", c),
                         "the blob store lib is up")


class ApplyJsonTest(unittest.TestCase):
    def _fixture(self, d: Path):
        segs = [{"id": 0, "start": 0.0, "end": 4.0, "text": " " + SENTENCE},
                {"id": 1, "start": 4.0, "end": 6.0, "text": " nothing to fix here"}]
        j = d / "t.json"
        j.write_text(json.dumps({"text": "".join(s["text"] for s in segs), "segments": segs,
                                 "language": "en"}))
        t = d / "t.txt"
        t.write_text("\n".join(s["text"].strip() for s in segs) + "\n")
        return j, t

    def test_apply_keeps_raw_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            j, t = self._fixture(Path(td))
            n = vocabulary.apply_to_whisper_json(j, CORRECTIONS, txt_path=t)
            self.assertEqual(n, 1)
            data = json.loads(j.read_text())
            s0 = data["segments"][0]
            self.assertIn("blob store lib", s0["text"])
            self.assertIn("S3", s0["text"])
            self.assertNotIn("blobser", s0["text"])
            self.assertEqual(s0["raw_text"], " " + SENTENCE)
            self.assertNotIn("raw_text", data["segments"][1])
            self.assertIn("blob store lib", data["text"])
            self.assertIn("blob store lib", t.read_text().splitlines()[0])
            first = j.read_text()
            n2 = vocabulary.apply_to_whisper_json(j, CORRECTIONS, txt_path=t)
            self.assertEqual(n2, 1)
            self.assertEqual(j.read_text(), first)
            # a changed rule set recomputes from raw_text; no rules restores the raw text
            self.assertEqual(vocabulary.apply_to_whisper_json(j, {}, txt_path=t), 0)
            data = json.loads(j.read_text())
            self.assertEqual(data["segments"][0]["text"], " " + SENTENCE)
            self.assertNotIn("raw_text", data["segments"][0])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as td:
            j, t = self._fixture(Path(td))
            r = subprocess.run([sys.executable, str(ROOT / "lib" / "vocabulary.py"), "apply", str(j),
                                "--txt", str(t), "--correct", "blobser live=blob store lib",
                                "--correct", "SD=S3"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("vocabulary: corrected 1 segment(s)", r.stdout)
            self.assertIn("blob store lib", json.loads(j.read_text())["segments"][0]["text"])


class WsconfigTest(unittest.TestCase):
    def test_good(self):
        args, err = wsconfig.transcribe_args(
            {"vocabulary": ["blob store lib", "S3"],
             "corrections": {"blobser": "blob store", "SD": "S3"}})
        self.assertIsNone(err)
        self.assertEqual(args, ["--vocabulary", "blob store lib", "--vocabulary", "S3",
                                "--correct", "blobser=blob store", "--correct", "SD=S3"])
        self.assertEqual(wsconfig.transcribe_args({}), ([], None))

    def test_bad(self):
        for bad in ({"vocabulary": "S3"}, {"vocabulary": ["ok", ""]}, {"vocabulary": [3]},
                    {"corrections": ["a"]}, {"corrections": {"a": ""}}, {"corrections": {"a": 1}},
                    {"corrections": {"": "x"}}):
            args, err = wsconfig.transcribe_args(bad)
            self.assertEqual(args, [], bad)
            self.assertTrue(err and "[transcribe" in err, (bad, err))

    def _ws(self, td: str, body: str) -> Path:
        ws = Path(td)
        (ws / "whosaid.toml").write_text(body)
        return ws

    def test_workspace_and_cli(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, '[transcribe]\nvocabulary = ["blob store lib", "S3"]\n'
                              '[transcribe.corrections]\nblobser = "blob store"\nSD = "S3"\n')
            args, err = wsconfig.workspace_transcribe_args(ws)
            self.assertIsNone(err)
            self.assertEqual(len(args), 8)
            r = subprocess.run([sys.executable, str(ROOT / "lib" / "wsconfig.py"),
                                "transcribe-args", str(ws)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.splitlines(), args)

    def test_cli_bad_value_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, '[transcribe]\nvocabulary = "S3"\n')
            r = subprocess.run([sys.executable, str(ROOT / "lib" / "wsconfig.py"),
                                "transcribe-args", str(ws)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("vocabulary", r.stderr)

    def test_no_section_prints_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td, '[workspace]\nowner = "Alice_Example"\n')
            r = subprocess.run([sys.executable, str(ROOT / "lib" / "wsconfig.py"),
                                "transcribe-args", str(ws)], capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))


class TranscribeMlxTest(unittest.TestCase):
    def _run(self, extra):
        import transcribe_mlx
        calls = []

        def fake_transcribe(audio, **kw):
            calls.append(kw)
            return {"text": " hi", "language": "en", "segments": []}

        writers = types.ModuleType("mlx_whisper.writers")
        writers.get_writer = lambda fmt, outdir: (lambda result, base, opts: None)
        mod = types.ModuleType("mlx_whisper")
        mod.transcribe = fake_transcribe
        mod.writers = writers
        with tempfile.TemporaryDirectory() as td:
            audio = Path(td) / "a.wav"
            audio.write_bytes(b"x")
            argv = ["transcribe_mlx.py", str(audio), "--outdir", td, *extra]
            with mock.patch.dict(sys.modules, {"mlx_whisper": mod, "mlx_whisper.writers": writers}), \
                    mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(transcribe_mlx.main(), 0)
        return calls[0]

    def test_initial_prompt_passed(self):
        kw = self._run(["--initial-prompt", "blob store lib, S3"])
        self.assertEqual(kw["initial_prompt"], "Hello, and welcome to the meeting. Terms we use: blob store lib, S3.")
        self.assertFalse(kw["condition_on_previous_text"])

    def test_no_prompt_is_none(self):
        self.assertIsNone(self._run([])["initial_prompt"])
        self.assertIsNone(self._run(["--initial-prompt", ""])["initial_prompt"])


class ShellSyntaxTest(unittest.TestCase):
    def test_bash_n(self):
        r = subprocess.run(["bash", "-n", str(ROOT / "whosaid")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
