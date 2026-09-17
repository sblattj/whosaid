#!/usr/bin/env python3
"""
Unit test for the machine-readable match confidence + match-threshold default
in lib/diarize_sherpa.py (GitHub issue #1, parts 2-3).

Exercises, with synthetic embeddings (no models, no audio):
  * name_clusters(report=[...]) records a MATCHED registry decision with the
    right pass name, threshold and similarity.
  * a below-threshold best candidate is recorded with matched=False and the
    cluster keeps its SPEAKER_NN label (no low-confidence name).
  * the absorb pass records its own near-miss for a still-unnamed cluster.
  * default_ref_threshold() honours WHOSAID_MATCH_THRESHOLD.

Run:
    uv run --with numpy python test/registry_matches_test.py
"""

import os
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


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def voice(rng, dim=192):
    """A random unit 'voiceprint'. Distinct voices are ~orthogonal in high dim."""
    return unit(rng.standard_normal(dim))


def near(rng, base, jitter, dim=192):
    """A unit vector a controlled distance from `base` (bigger jitter = lower cosine)."""
    return unit(base + jitter * rng.standard_normal(dim))


def find(report, cluster, name, which):
    hits = [r for r in report
            if r["cluster"] == cluster and r["name"] == name and r["pass"] == which]
    check(len(hits) == 1,
          f"expected exactly 1 {which} entry {cluster}->{name}, got {len(hits)}: {report}")
    return hits[0]


def test_matched_entry_recorded():
    rng = np.random.default_rng(11)
    alice = voice(rng)
    stranger = voice(rng)
    c00 = near(rng, alice, 0.023)  # ~0.95 to Alice
    cluster_emb = {"SPEAKER_00": c00, "SPEAKER_01": stranger}
    registry = [{"name": "Alice", "embedding": alice.tolist()}]

    names = {sp: sp for sp in cluster_emb}
    report: list = []
    d.name_clusters(cluster_emb, 0.50, 0.85, registry, [], names, report=report)

    check(names["SPEAKER_00"] == "Alice", f"00 should be Alice, got {names['SPEAKER_00']}")
    hit = find(report, "SPEAKER_00", "Alice", "registry")
    check(hit["matched"] is True, f"matched registry entry must be matched=True: {hit}")
    check(hit["threshold"] == 0.50, f"registry entry threshold must be the ref gate: {hit}")
    true_sim = float(np.dot(unit(c00), unit(alice)))
    check(abs(hit["similarity"] - true_sim) < 1e-4,
          f"recorded similarity {hit['similarity']} != cosine {true_sim}")
    check(hit["similarity"] >= 0.50,
          f"test setup: 00 must clear the gate, got {hit['similarity']}")
    check(set(hit) == {"cluster", "name", "similarity", "threshold", "matched", "pass"},
          f"entry keys must be the documented schema, got {sorted(hit)}")

    # The stranger cluster stays anonymous and still leaves an absorb near-miss,
    # so a reviewer can see how far off the best candidate was.
    check(names["SPEAKER_01"] == "SPEAKER_01",
          f"01 (stranger) must stay SPEAKER_01, got {names['SPEAKER_01']}")
    miss = find(report, "SPEAKER_01", "Alice", "absorb")
    check(miss["matched"] is False, f"stranger absorb entry must be matched=False: {miss}")
    check(miss["threshold"] == 0.85, f"absorb entry threshold must be the absorb gate: {miss}")


def test_near_miss_recorded_and_cluster_stays_anonymous():
    """A registry voice whose best cluster is just UNDER the gate: recorded, not named."""
    rng = np.random.default_rng(12)
    bob = voice(rng)
    # jitter tuned so cosine lands in the 0.40-0.53 band the issue flagged as unsafe
    c00 = near(rng, bob, 0.13)
    cluster_emb = {"SPEAKER_00": c00}
    registry = [{"name": "Bob", "embedding": bob.tolist()}]
    sim = float(np.dot(unit(c00), unit(bob)))
    check(0.40 <= sim < 0.50, f"test setup: sim must sit in the near-miss band, got {sim:.3f}")

    names = {sp: sp for sp in cluster_emb}
    report: list = []
    d.name_clusters(cluster_emb, 0.50, 0.85, registry, [], names, report=report)

    check(names["SPEAKER_00"] == "SPEAKER_00",
          f"below-threshold cluster must stay SPEAKER_NN, got {names['SPEAKER_00']}")
    miss = find(report, "SPEAKER_00", "Bob", "registry")
    check(miss["matched"] is False, f"near-miss must be recorded matched=False: {miss}")
    check(abs(miss["similarity"] - sim) < 1e-4,
          f"near-miss similarity {miss['similarity']} != cosine {sim}")

    # Control: the SAME input at the old 0.40 default WOULD have been named --
    # so the assertion above is measuring the gate, not an inert fixture.
    names_loose = {sp: sp for sp in cluster_emb}
    d.name_clusters(cluster_emb, 0.40, 0.85, registry, [], names_loose)
    check(names_loose["SPEAKER_00"] == "Bob",
          "control: at ref_threshold 0.40 this same cluster IS named (the gate is what changed)")


def test_report_is_optional_for_positional_callers():
    """Existing positional callers must keep working with no report argument."""
    rng = np.random.default_rng(13)
    carl = voice(rng)
    cluster_emb = {"SPEAKER_00": near(rng, carl, 0.023)}
    registry = [{"name": "Carl", "embedding": carl.tolist()}]
    names = {sp: sp for sp in cluster_emb}
    out = d.name_clusters(cluster_emb, 0.50, 0.85, registry, [], names)
    check(out["SPEAKER_00"] == "Carl", f"positional call must still name, got {out}")


def test_env_sets_default_threshold():
    prev = os.environ.get("WHOSAID_MATCH_THRESHOLD")
    try:
        os.environ.pop("WHOSAID_MATCH_THRESHOLD", None)
        check(d.default_ref_threshold() == d.DEFAULT_REF_THRESHOLD,
              f"unset env must give DEFAULT_REF_THRESHOLD, got {d.default_ref_threshold()}")
        check(d.DEFAULT_REF_THRESHOLD == 0.50,
              f"documented default is 0.50, got {d.DEFAULT_REF_THRESHOLD}")
        os.environ["WHOSAID_MATCH_THRESHOLD"] = "0.66"
        check(d.default_ref_threshold() == 0.66,
              f"env must win, got {d.default_ref_threshold()}")
        os.environ["WHOSAID_MATCH_THRESHOLD"] = "not-a-number"
        check(d.default_ref_threshold() == d.DEFAULT_REF_THRESHOLD,
              "a junk env value must fall back to the built-in default, not crash")
        os.environ["WHOSAID_MATCH_THRESHOLD"] = ""
        check(d.default_ref_threshold() == d.DEFAULT_REF_THRESHOLD,
              "an empty env value must fall back to the built-in default")
    finally:
        os.environ.pop("WHOSAID_MATCH_THRESHOLD", None)
        if prev is not None:
            os.environ["WHOSAID_MATCH_THRESHOLD"] = prev


def main() -> None:
    test_matched_entry_recorded()
    test_near_miss_recorded_and_cluster_stays_anonymous()
    test_report_is_optional_for_positional_callers()
    test_env_sets_default_threshold()
    print(f"\nPASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
