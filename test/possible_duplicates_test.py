#!/usr/bin/env python3
"""
Regression test: roll-up's possible-duplicates review must ignore fold-time
near misses whose ids are no longer in the corpus.

A commitments refresh ("commitment sources/roles changed") folds every
meeting under transient ids and then restores the stable ones, so near-miss
pairs recorded during the fold can name an id (e.g. CM-426) the final item
list does not hold. render_commitments_md used to raise KeyError on such a
pair and abort the whole roll-up.

Run:
    python3 test/possible_duplicates_test.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import workspace as w  # noqa: E402


def main() -> None:
    items = [
        w.CommitmentItem(id="CM-001", text="I'll send the report to the team today", speaker="Alice_Example"),
        w.CommitmentItem(id="CM-002", text="I'll review the analytics design doc", speaker="Alice_Example"),
        w.CommitmentItem(id="CM-003", text="I'll merge the old one", speaker="Alice_Example",
                         status="merged", merged_into="CM-001"),
    ]
    near = [
        ("CM-001", "CM-426", 0.81),  # transient id, gone after refresh
        ("CM-777", "CM-002", 0.80),  # transient id on the other side
        ("CM-001", "CM-003", 0.79),  # merged item
        ("CM-001", "CM-002", 0.75),  # both live: kept
    ]
    dupes = w.possible_duplicates(items, near, 0.82)
    ids = {i.id for i in items if i.status != "merged"}
    for a, b, _ in dupes:
        assert a in ids and b in ids, (a, b)
    assert ("CM-001", "CM-002", 0.75) in dupes, dupes

    with tempfile.TemporaryDirectory() as tmp:
        md = w.render_commitments_md(Path(tmp), items, dupes, [])
    assert "Possible duplicates" in md
    assert "CM-426" not in md and "CM-777" not in md
    print("possible_duplicates_test: ok")


if __name__ == "__main__":
    main()
