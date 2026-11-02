#!/usr/bin/env python3
"""sherpa-onnx stays pinned to one version everywhere uv or pip installs it.

An unpinned `uv run --with sherpa-onnx` follows whatever PyPI serves. sherpa-onnx
2.0.1 uploaded its sdist hours before its wheels; under a uv exclude-newer cooldown
that window made uv pick the sdist, whose build needs cmake, and every ingest
quietly lost its speaker labels.
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

REPO = Path(__file__).resolve().parents[1]
SPEC = re.compile(r'SHERPA_SPEC="\$\{WHOSAID_SHERPA_SPEC:-(sherpa-onnx==[0-9.]+)\}"')
SCRIPTS = ["whosaid", "bootstrap.sh", "test/e2e.sh"]
DOCS = ["lib/diarize_sherpa.py", "test/diarize_eval/voice_sim.py", "docs/design.md",
        "docs/diarize-eval.md"]


def pin() -> str:
    m = SPEC.search((REPO / "whosaid").read_text())
    assert m, "whosaid has no SHERPA_SPEC default"
    return m.group(1)


class SherpaPinTest(unittest.TestCase):
    def test_scripts_use_the_same_pin(self):
        want = pin()
        for f in SCRIPTS:
            text = (REPO / f).read_text()
            with self.subTest(f=f):
                self.assertEqual(SPEC.findall(text), [want])
                self.assertNotRegex(text, r"--with sherpa-onnx(?![=\w-])")
                self.assertIn('--with "$SHERPA_SPEC"', text)

    def test_pyproject_matches(self):
        self.assertIn(f'"{pin()}",', (REPO / "pyproject.toml").read_text())

    def test_docs_show_the_pin(self):
        for f in DOCS:
            text = (REPO / f).read_text()
            with self.subTest(f=f):
                self.assertNotRegex(text, r"--with sherpa-onnx(?![=\w-])")
                self.assertIn(pin(), text)


if __name__ == "__main__":
    unittest.main()
