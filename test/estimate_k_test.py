#!/usr/bin/env python3
"""
Unit test for the agglomerative speaker-count estimator (GitHub issue #5).

The old farthest-first estimator saturated at the hard cap of 20 on every long
recording, because it compared each turn to a SINGLE previous turn and real
same-speaker turns sit at cosine 0.6-0.8. These fixtures reproduce that regime
with synthetic embeddings (no models, no audio) and assert that the
average-linkage estimator recovers the true count instead.

Fixtures are generated to match the cosine statistics MEASURED on real
TitaNet-small per-turn embeddings coming out of this pipeline (two 17.8-minute
6-voice recordings, 65 and 72 embedded turns): intra-speaker ~0.90, inter-speaker
~0.25. That is achieved by giving every speaker a shared "channel" component
plus a private direction, then jittering each turn — an all-random basis would
put inter-speaker similarity at ~0.0, which is an easier problem than the real
one, and it is the inter-speaker FLOOR that sets where the cut has to go.

Note the issue text quotes 0.6-0.8 for same-speaker turns. Measured against this
model that is the low tail (p10 0.72-0.82), not the centre; fixtures built to
0.6-0.8 as the MEAN make the problem harder than reality and pull the
calibrated cut roughly 0.13 too low. See AGGLOM_THRESHOLD in lib/diarize_sherpa.py.

Run:
    uv run --with numpy python test/estimate_k_test.py
"""

import sys
import time
from pathlib import Path

import numpy as np

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import diarize_sherpa as d  # noqa: E402

DIM = 192          # TitaNet-small embedding width
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def turns(n_speakers: int, per_speaker: int, jitter: float = 0.024,
          common: float = 0.40, seed: int = 0) -> np.ndarray:
    """Unit-norm per-turn embeddings for `n_speakers` distinct voices.

    `common` is how much of each speaker's voiceprint is a shared channel
    direction (this is what lifts inter-speaker similarity off 0.0), `jitter`
    is the per-turn spread (this is what pulls intra-speaker similarity down
    off 1.0). See cosine_stats below for the resulting numbers.
    """
    rng = np.random.default_rng(seed)
    channel = rng.normal(size=DIM)
    channel /= np.linalg.norm(channel)
    rows = []
    for _ in range(n_speakers):
        private = rng.normal(size=DIM)
        private /= np.linalg.norm(private)
        base = common * channel + (1.0 - common) * private
        base /= np.linalg.norm(base)
        for _ in range(per_speaker):
            v = base + rng.normal(scale=jitter, size=DIM)
            rows.append(v / np.linalg.norm(v))
    return np.array(rows, dtype=np.float32)


def cosine_stats(X: np.ndarray, n_speakers: int, per_speaker: int) -> tuple:
    """(mean intra-speaker cosine, mean inter-speaker cosine) for a fixture."""
    S = X @ X.T
    lab = np.repeat(np.arange(n_speakers), per_speaker)
    same_mask = (lab[:, None] == lab[None, :]) & ~np.eye(len(X), dtype=bool)
    return float(S[same_mask].mean()), float(S[lab[:, None] != lab[None, :]].mean())


# ---------------------------------------------------------------------------


def test_fixture_matches_the_issues_regime():
    """Guard the guard: if the fixture drifts out of the 0.6-0.8 / 0.2-0.5 band
    it is no longer testing the bug that was reported."""
    X = turns(8, 90, seed=1)
    intra, inter = cosine_stats(X, 8, 90)
    check(0.87 <= intra <= 0.93,
          f"fixture intra-speaker cosine must match the measured real ~0.90, got {intra:.3f}")
    check(0.20 <= inter <= 0.32,
          f"fixture inter-speaker cosine must match the measured real ~0.25, got {inter:.3f}")


def test_eight_speakers_recovered():
    X = turns(8, 90, seed=1)
    est = d.estimate_speakers(X)
    check(abs(est["k"] - 8) <= 1, f"8-speaker fixture must estimate 8 (+-1), got {est['k']}")
    check(est["saturated"] is False, f"8 speakers must not saturate the cap: {est}")
    check(est["method"] == "agglomerative", f"method must be agglomerative: {est['method']}")


def test_eight_speakers_recovered_in_a_noisier_regime():
    """Second design point: same true count, a tighter gap — turns scatter more
    (intra ~0.84) AND the speakers share more channel (inter ~0.33)."""
    X = turns(8, 90, jitter=0.032, common=0.45, seed=5)
    intra, inter = cosine_stats(X, 8, 90)
    check(intra < 0.87, f"harder fixture must have looser turns, got intra {intra:.3f}")
    check(inter > 0.30, f"harder fixture must have a tighter gap, got inter {inter:.3f}")
    est = d.estimate_speakers(X)
    check(abs(est["k"] - 8) <= 1,
          f"noisier 8-speaker fixture must still estimate 8 (+-1), got {est['k']}")


def test_two_speakers():
    X = turns(2, 80, seed=7)
    est = d.estimate_speakers(X)
    check(est["k"] == 2, f"a 1:1 call must estimate exactly 2, got {est['k']}")


def test_six_speaker_meeting():
    X = turns(6, 120, seed=11)
    est = d.estimate_speakers(X)
    check(abs(est["k"] - 6) <= 1, f"6-speaker meeting must estimate 6 (+-1), got {est['k']}")


def test_saturation_is_reported_not_hidden():
    """25 real voices exceed the cap of 20. The count must come back AT the cap
    and flagged — the whole point of #5 is that a capped count is a failed
    estimate, not a result."""
    X = turns(25, 30, seed=9)
    est = d.estimate_speakers(X)
    check(est["raw_k"] >= d.SPEAKER_CAP,
          f"25-voice fixture must exceed the cap before clamping, got raw_k={est['raw_k']}")
    check(est["saturated"] is True, f"exceeding the cap must set saturated: {est}")
    check(est["k"] == d.SPEAKER_CAP,
          f"a saturated estimate must clamp to the cap {d.SPEAKER_CAP}, got {est['k']}")


def test_max_speakers_lowers_the_cap_and_saturates():
    X = turns(8, 90, seed=1)
    est = d.estimate_speakers(X, max_speakers=4)
    check(est["k"] == 4, f"--max-speakers 4 must clamp 8 down to 4, got {est['k']}")
    check(est["saturated"] is True,
          f"clamping to --max-speakers must report the count as untrusted: {est}")
    check(est["max"] == 4, f"estimate record must carry max=4: {est}")


def test_min_speakers_raises_the_estimate():
    X = turns(2, 80, seed=7)
    est = d.estimate_speakers(X, min_speakers=5)
    check(est["k"] == 5, f"--min-speakers 5 must raise 2 up to 5, got {est['k']}")
    check(est["raw_k"] == 2, f"the raw estimate must still be recorded as 2: {est}")
    check(est["min"] == 5, f"estimate record must carry min=5: {est}")


def test_bounds_do_not_bind_when_the_estimate_is_inside_them():
    X = turns(6, 120, seed=11)
    est = d.estimate_speakers(X, min_speakers=2, max_speakers=10)
    check(est["k"] == est["raw_k"],
          f"a non-binding range must leave the estimate alone: {est}")
    check(est["saturated"] is False, f"a non-binding range must not saturate: {est}")


def test_estimate_k_wrapper_agrees():
    X = turns(6, 120, seed=11)
    check(d.estimate_k(X) == d.estimate_speakers(X)["k"],
          "estimate_k must return the same count as estimate_speakers")


def test_threshold_monotonicity():
    """A HIGHER same-speaker cut splits more (never fewer) clusters. The old
    farthest-first pass ran the opposite direction, which is why its cap guard
    retried DOWNWARD; the contract is inverted now and worth pinning."""
    X = turns(8, 90, seed=1)
    ks = [int(d.agglomerative_labels(X, t).max()) + 1 for t in (0.2, 0.35, 0.5, 0.65, 0.8)]
    check(all(ks[i] <= ks[i + 1] for i in range(len(ks) - 1)),
          f"a higher cosine cut must not yield fewer clusters: {ks}")


def test_calibrated_threshold_sits_on_a_plateau():
    """The shipped default must not be perched on a cliff edge: k=8 has to hold
    across a band of cuts on BOTH fixture regimes, and the default must be
    inside that band."""
    for jitter, common, seed in ((0.024, 0.40, 1), (0.032, 0.45, 5)):
        X = turns(8, 90, jitter=jitter, common=common, seed=seed)
        good = [t for t in np.arange(0.35, 0.76, 0.05)
                if abs(int(d.agglomerative_labels(X, float(t)).max()) + 1 - 8) <= 1]
        check(len(good) >= 3,
              f"jitter={jitter}: k=8 must hold over a band of cuts, held only at {good}")
        check(min(good) <= d.AGGLOM_THRESHOLD <= max(good),
              f"jitter={jitter}: default cut {d.AGGLOM_THRESHOLD} must sit inside "
              f"the working band [{min(good):.2f}, {max(good):.2f}]")


def test_degenerate_inputs():
    check(d.estimate_speakers(turns(1, 1, seed=2))["k"] == 1, "a single turn must estimate 1")
    check(d.estimate_speakers(turns(1, 40, seed=3))["k"] == 1,
          "one speaker across many turns must estimate 1")


def test_runtime_for_a_long_meeting():
    """~700 turns is a 52-minute meeting; 1000 is the design target. The naive
    O(n^3) rescan-per-merge loop does not survive this, the NN-chain does."""
    X = turns(10, 100, seed=4)
    check(len(X) == 1000, f"timing fixture must be n=1000, got {len(X)}")
    t0 = time.time()
    est = d.estimate_speakers(X)
    elapsed = time.time() - t0
    check(elapsed < 10.0, f"n=1000 estimate must finish under 10s, took {elapsed:.2f}s")
    check(abs(est["k"] - 10) <= 1, f"n=1000 fixture must estimate 10 (+-1), got {est['k']}")
    print(f"  n=1000 estimate: k={est['k']} in {elapsed:.3f}s")


def main():
    test_fixture_matches_the_issues_regime()
    test_eight_speakers_recovered()
    test_eight_speakers_recovered_in_a_noisier_regime()
    test_two_speakers()
    test_six_speaker_meeting()
    test_saturation_is_reported_not_hidden()
    test_max_speakers_lowers_the_cap_and_saturates()
    test_min_speakers_raises_the_estimate()
    test_bounds_do_not_bind_when_the_estimate_is_inside_them()
    test_estimate_k_wrapper_agrees()
    test_threshold_monotonicity()
    test_calibrated_threshold_sits_on_a_plateau()
    test_degenerate_inputs()
    test_runtime_for_a_long_meeting()
    print(f"PASS: {CHECKS} assertions")
    sys.exit(0)


if __name__ == "__main__":
    main()
