#!/usr/bin/env python3
"""
Unit test for make_embed's long-input split (GitHub issue #56): the speaker-embedding
model crashed on a ~145 s turn, killing the whole diarization. make_embed must never
hand the extractor more than EMBED_MAX_SECONDS, and must still return one unit vector.

Uses a fake extractor (no models, no audio).

Run:
    uv run --with numpy python test/long_turn_embed_test.py
"""

import sys
from pathlib import Path

import numpy as np

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import diarize_sherpa as d  # noqa: E402

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


class FakeStream:
    def __init__(self):
        self.n = 0

    def accept_waveform(self, sr, wave):
        self.n += len(wave)

    def input_finished(self):
        pass


class FakeExtractor:
    """Records input lengths; raises like the real model past the limit."""
    def __init__(self):
        self.lengths = []

    def create_stream(self):
        return FakeStream()

    def compute(self, st):
        self.lengths.append(st.n)
        if st.n > d.EMBED_MAX_SECONDS * d.SAMPLE_RATE:
            raise RuntimeError("broadcast an axis by a dimension other than 1")
        v = np.zeros(192, dtype=np.float32)
        v[0], v[1] = 1.0, st.n / 1e7
        return v.tolist()


def run(seconds: float):
    ex = FakeExtractor()
    v = d.make_embed(ex)(np.zeros(int(seconds * d.SAMPLE_RATE), dtype=np.float32))
    return ex, v


def main() -> None:
    sr, lim = d.SAMPLE_RATE, int(d.EMBED_MAX_SECONDS * d.SAMPLE_RATE)
    ex, v = run(10)
    check(ex.lengths == [10 * sr], f"short wave is one call: {ex.lengths}")
    ex, v = run(145)
    check(max(ex.lengths) <= lim, f"no piece above the limit: {ex.lengths}")
    check(sum(ex.lengths) == 145 * sr, f"every sample embedded once: {sum(ex.lengths)}")
    check(abs(np.linalg.norm(v) - 1) < 1e-5, "result is a unit vector")
    ex, v = run(d.EMBED_MAX_SECONDS * 2 + 0.5)  # no sliver piece from a short tail
    check(len(ex.lengths) == 3 and max(ex.lengths) - min(ex.lengths) <= 1, f"equal pieces: {ex.lengths}")
    check(max(ex.lengths) <= lim, f"still under the limit: {ex.lengths}")
    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
