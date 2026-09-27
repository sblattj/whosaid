#!/usr/bin/env python3
"""Regression: a commitment refresh must not leave stale fold-time ids in the
possible-duplicates review (roll-up crashed with KeyError: 'CM-NNN' rendering
_COMMITMENTS.md). Offline; run: python3 test/rollup_dupes_test.py"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR / "lib"))

import workspace as w  # noqa: E402

CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    CHECKS += 1


A = "I will send the revised launch proposal to the team tomorrow morning"
B = "I will send the revised launch plan to the whole team by tomorrow"
ENTRIES = [{"text": A, "speaker": "Alice_Example", "time": "00:00:10"},
           {"text": B, "speaker": "Alice_Example", "time": "00:00:20"}]


def fold(items: list, next_id: list[int], near: list) -> None:
    w.fold_commitments("2026-09-01-0900", ENTRIES, items, next_id,
                       w.SIMILARITY_THRESHOLD, near, None, {**w.WORKLIST_DEFAULTS, "min_words": 0}, [])


def main() -> None:
    ratio = w.similarity(w.normalize_text(A), w.normalize_text(B))
    check(w.SIMILARITY_THRESHOLD - w.NEAR_MISS_BAND <= ratio < w.SIMILARITY_THRESHOLD,
          f"fixture texts are a near miss (ratio {ratio:.2f})")

    # First roll-up: CM-001, CM-002 plus a near miss between them.
    previous, next_id, near = [], [1], []
    fold(previous, next_id, near)
    check([it.id for it in previous] == ["CM-001", "CM-002"], "initial ids")
    check(near and {near[0][0], near[0][1]} == {"CM-001", "CM-002"}, "initial near miss")

    # Refresh (sources/roles changed): the fold mints fresh ids CM-003/CM-004,
    # then restore maps them back to CM-001/CM-002.
    fresh, near = [], []
    fold(fresh, next_id, near)
    check({near[0][0], near[0][1]} == {"CM-003", "CM-004"}, "refresh near miss uses fresh ids")
    renamed = w.restore_commitment_edits(fresh, copy.deepcopy(previous))
    check(renamed == {"CM-003": "CM-001", "CM-004": "CM-002"}, f"renamed map {renamed}")
    check(sorted(it.id for it in fresh) == ["CM-001", "CM-002"], "ids restored")

    # Unmapped (pre-fix) near misses: possible_duplicates drops stale ids, render survives.
    stale = w.possible_duplicates(fresh, near, 0.99)
    check(all(a in {"CM-001", "CM-002"} and b in {"CM-001", "CM-002"} for a, b, _ in stale),
          f"stale ids filtered {stale}")
    w.render_commitments_md(Path("."), fresh, stale, [])

    # Remapped near misses (the fix in cmd_rollup) keep the review pair.
    near = [(renamed.get(a, a), renamed.get(b, b), r) for a, b, r in near]
    dupes = w.possible_duplicates(fresh, near, w.SIMILARITY_THRESHOLD)
    check([(a, b) for a, b, _ in dupes] == [("CM-001", "CM-002")], f"review pair kept {dupes}")
    md = w.render_commitments_md(Path("."), fresh, dupes, [])
    check("CM-001 ↔ CM-002" in md, "review pair rendered")

    # A near miss naming an id the corpus never held is dropped, not a KeyError.
    ghost = w.possible_duplicates(fresh, [("CM-001", "CM-999", 0.8)], 0.99)
    check(all("CM-999" not in (a, b) for a, b, _ in ghost), "unknown id dropped")
    w.render_commitments_md(Path("."), fresh, ghost, [])
    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
