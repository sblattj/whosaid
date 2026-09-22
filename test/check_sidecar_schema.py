#!/usr/bin/env python3
"""Assert a <base>.diarization.json carries the issue-#1 keys with the right shape.

Used by test/e2e.sh on the sidecar a real run just produced (the e2e deletes its
temp dir on PASS, so the check has to happen inside the run). Stdlib only.

Usage: python3 test/check_sidecar_schema.py <sidecar.diarization.json>
"""

import json
import sys

ENTRY_KEYS = {"cluster", "name", "similarity", "threshold", "matched", "pass"}
# The anchor pass (--expected-speakers) carries one extra field: how many turns
# the anchor claimed. Folding may add original_cluster to retain match provenance.
EXTRA_KEYS = {"anchor": {"turns"}}
PASSES = {"registry", "ref", "absorb", "anchor"}


def fail(msg: str) -> None:
    print(f"sidecar schema FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: check_sidecar_schema.py <sidecar.diarization.json>")
    with open(sys.argv[1]) as f:
        data = json.load(f)

    matches = data.get("registry_matches")
    if not isinstance(matches, list):
        fail(f"registry_matches must be a list, got {type(matches).__name__}")
    for entry in matches:
        if not isinstance(entry, dict) or "pass" not in entry:
            fail(f"registry_matches entry must be an object with a 'pass' key, got {entry}")
        if entry["pass"] not in PASSES:
            fail(f"registry_matches entry 'pass' must be one of {sorted(PASSES)}, got {entry}")
        allowed = ENTRY_KEYS | EXTRA_KEYS.get(entry["pass"], set())
        if not allowed <= set(entry) or set(entry) - allowed - {"original_cluster"}:
            fail(f"registry_matches '{entry['pass']}' entry must have "
                 f"{sorted(allowed)}, optionally original_cluster, got {entry}")
        if "original_cluster" in entry and (not isinstance(entry["original_cluster"], str)
                                             or not entry["original_cluster"]):
            fail(f"registry_matches 'original_cluster' must be a non-empty string, got {entry}")
        if entry["pass"] == "anchor" and not isinstance(entry["turns"], int):
            fail(f"registry_matches anchor 'turns' must be an int, got {entry}")
        if not isinstance(entry["similarity"], (int, float)):
            fail(f"registry_matches 'similarity' must be numeric, got {entry}")
        if not isinstance(entry["matched"], bool):
            fail(f"registry_matches 'matched' must be a bool, got {entry}")

    source = data.get("source")
    if not isinstance(source, dict):
        fail(f"source must be an object, got {type(source).__name__}")
    if not source.get("path"):
        fail(f"source.path missing/empty: {source}")
    dur = source.get("duration_seconds")
    if not isinstance(dur, (int, float)) or dur <= 0:
        fail(f"source.duration_seconds must be a positive number: {source}")
    if "creation_time" not in source:
        fail(f"source.creation_time key missing (null is fine, absent is not): {source}")

    labels = data.get("local_labels", {})
    if not isinstance(labels, dict):
        fail(f"local_labels must be an object keyed by cluster id, got {type(labels).__name__}")
    for cluster, entry in labels.items():
        if not isinstance(entry, dict) or set(entry) != {"name", "note"}:
            fail(f"local_labels['{cluster}'] must have exactly ['name', 'note'], got {entry}")
        if not isinstance(entry["name"], str) or not entry["name"]:
            fail(f"local_labels['{cluster}'].name must be a non-empty string: {entry}")
        if entry["note"] is not None and not isinstance(entry["note"], str):
            fail(f"local_labels['{cluster}'].note must be a string or null: {entry}")

    named = sum(1 for e in matches if e["matched"])
    print(f"sidecar schema OK: {len(matches)} match record(s), {named} matched; "
          f"source duration {dur}s, creation_time={source['creation_time']!r}")
    # Echo every decision so the measured similarities are visible in the e2e log
    # (the run's temp dir is deleted on PASS, so this is the only chance to see them).
    for e in matches:
        verdict = "MATCH" if e["matched"] else "below threshold"
        print(f"  {e['pass']:>8}: {e['cluster']} -> {e['name']} "
              f"sim {e['similarity']:.3f} vs threshold {e['threshold']} ({verdict})")


if __name__ == "__main__":
    main()
