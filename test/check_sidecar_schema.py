#!/usr/bin/env python3
"""Assert a <base>.diarization.json carries the issue-#1 keys with the right shape.

Used by test/e2e.sh on the sidecar a real run just produced (the e2e deletes its
temp dir on PASS, so the check has to happen inside the run). Stdlib only.

Usage: python3 test/check_sidecar_schema.py <sidecar.diarization.json>
"""

import json
import sys

ENTRY_KEYS = {"cluster", "name", "similarity", "threshold", "matched", "pass"}
PASSES = {"registry", "ref", "absorb"}


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
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            fail(f"registry_matches entry must have exactly {sorted(ENTRY_KEYS)}, got {entry}")
        if entry["pass"] not in PASSES:
            fail(f"registry_matches entry 'pass' must be one of {sorted(PASSES)}, got {entry}")
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
