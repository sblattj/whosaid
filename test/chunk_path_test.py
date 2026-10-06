#!/usr/bin/env python3
"""
Unit test for wants_chunked() in lib/diarize_sherpa.py (GitHub issue #59): a speaker
range must take the chunked path, because whole-file FastClustering only takes an exact
count and silently ignored --max-speakers on calls under 15 minutes.

Run:
    uv run --with numpy python test/chunk_path_test.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import diarize_sherpa as d  # noqa: E402

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def args(**kw):
    base = dict(num_speakers=-1, min_speakers=0, max_speakers=0, chunk_seconds=0, no_chunk=False)
    base.update(kw)
    return SimpleNamespace(**base)


def main() -> None:
    short, long_ = 898.0, 2700.0
    check(not d.wants_chunked(args(), short, 300.0, 8), "short, no hints: whole-file (unchanged)")
    check(d.wants_chunked(args(), long_, 340.0, 8), "long audio: chunked (unchanged)")
    check(d.wants_chunked(args(max_speakers=8), short, 300.0, 8), "short + max: chunked so the cap holds")
    check(d.wants_chunked(args(min_speakers=2), short, 300.0, 8), "short + min: chunked")
    check(not d.wants_chunked(args(min_speakers=4, max_speakers=4), short, 300.0, 8),
          "min == max is an exact count the whole-file path honours")
    check(not d.wants_chunked(args(num_speakers=3, max_speakers=8), short, 300.0, 8),
          "exact --speakers stays whole-file")
    check(not d.wants_chunked(args(max_speakers=8, no_chunk=True), short, 300.0, 8), "--no-chunk wins")
    check(not d.wants_chunked(args(max_speakers=8), 200.0, 300.0, 8), "too short to split: whole-file")
    check(not d.wants_chunked(args(max_speakers=8), short, 300.0, 1), "one job: whole-file")
    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
