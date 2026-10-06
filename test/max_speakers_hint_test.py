#!/usr/bin/env python3
"""Issue #59 proposal 4: a saturated auto count whose recovery abstains suggests
`--max-speakers N`, without changing the estimate itself.

Synthetic embeddings only (no models, no audio). The saturating fixture is the
#38 recovery fixture's recipe scaled to 8 voices: substantive 15 s turns per
voice, plus short 0.6 s noisy turns that each open a singleton cluster (raw_k
past the cap of 20), plus ONE brief distinct guest so the short-turn guard in
estimate_speakers abstains (#59 proposal 3 keeps that guard as is).

The short turns sit at cosine 0.30 to their voice by default: below the brief
fold's gate (BRIEF_VOICE_GATE 0.40), so the fold keeps them as distinct and the
count still saturates. At the #38 recipe's 0.55 the fold explains them and there
is nothing left to hint about (test_brief_fold_resolves_saturation).

Run:
    uv run --with numpy python test/max_speakers_hint_test.py
"""

import copy
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import diarize_sherpa as d  # noqa: E402
from estimate_k_test import turns  # noqa: E402

CHECKS = 0
TRUE_VOICES = 8


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def unit(v):
    return v / (np.linalg.norm(v) + 1e-9)


def saturating_fixture(voices: int = TRUE_VOICES, guest: bool = True, cohesion: float = 0.30):
    """(X, durations) for `voices` substantive voices + short noise (+ a brief guest).

    `cohesion` is each short turn's cosine to its own voice."""
    per = 20
    X = turns(voices, per, seed=59)
    bases = [unit(X[i * per:(i + 1) * per].sum(axis=0)) for i in range(voices)]
    rng = np.random.default_rng(59)
    rows, durs = list(X), [15.0] * len(X)
    for i in range(90):
        b = bases[i % voices]
        noise = rng.normal(size=192)
        noise = unit(noise - float(noise @ b) * b)
        rows.append(cohesion * b + np.sqrt(1 - cohesion ** 2) * noise)
        durs.append(0.6)
    if guest:
        rows.append(unit(np.random.default_rng(12).normal(size=192)))
        durs.append(0.6)
    return np.array(rows, dtype=np.float32), np.array(durs)


def test_saturated_abstain_suggests():
    X, dur = saturating_fixture()
    est = d.estimate_speakers(X, durations=dur)
    check(est["saturated"] is True, f"fixture must saturate the cap: raw_k {est['raw_k']}")
    check(est["fallback"] is None, f"the brief guest must make recovery abstain: {est['fallback']}")
    n = est.get("suggested_max")
    check(isinstance(n, int) and abs(n - TRUE_VOICES) <= 2,
          f"suggested_max must be within 2 of {TRUE_VOICES}, got {n}")
    # Reporting only: the count and labels equal a run that never asks for a
    # suggestion (no durations => no ladder => no suggestion path at all).
    plain = d.estimate_speakers(X)
    check(est["k"] == plain["k"] == 20, f"k must stay the capped 20: {est['k']} / {plain['k']}")
    # With durations the labels are the primary cut after the brief fold; the
    # suggestion itself must not move them.
    folded, _ = d.fold_brief_clusters(X, plain["labels"], dur)
    check(np.array_equal(est["labels"], folded), "labels must be the folded primary cut")
    check(est["raw_k"] == plain["raw_k"], "raw_k must be unchanged")
    warning = d.count_recovery_warning(est, 20, 20)
    check(f"--max-speakers {n}" in warning, f"warning must carry the hint: {warning}")
    check(warning.startswith("Auto count remains UNRELIABLE"), warning)
    # Deterministic: the same input gives the same hint.
    check(d.estimate_speakers(X, durations=dur)["suggested_max"] == n, "suggestion must be stable")


def test_user_max_gives_no_suggestion():
    X, dur = saturating_fixture()
    est = d.estimate_speakers(X, durations=dur, max_speakers=12)
    check(est["saturated"] is True and est["fallback"] is None, f"control must still saturate: {est}")
    check(est["suggested_max"] is None, f"a user cap must never produce a suggestion: {est}")
    check("--max-speakers" not in (d.count_recovery_warning(est, 12, 12) or ""), "no hint text")


def test_brief_fold_resolves_saturation():
    # The #38 recipe (short turns at 0.55 to their voice): the brief fold puts
    # every fragment back on its voice, so the count is right and nothing is hinted.
    X, dur = saturating_fixture(guest=False, cohesion=0.55)
    est = d.estimate_speakers(X, durations=dur)
    check(est["saturated"] is True, "control must saturate the primary cut")
    check(est["k"] == TRUE_VOICES and est["brief_fold"]["folded"] > 0, f"fold must resolve: {est}")
    check(est["fallback"] is None and est["suggested_max"] is None, f"nothing left to hint: {est}")
    check(d.count_recovery_warning(est, 20, est["k"]) is None or
          "--max-speakers" not in d.count_recovery_warning(est, 20, est["k"]), "no hint text")
    # With the guest, the guest is kept as its own speaker.
    X, dur = saturating_fixture(guest=True, cohesion=0.55)
    est = d.estimate_speakers(X, durations=dur)
    check(est["k"] == TRUE_VOICES + 1 and est["brief_fold"]["kept_brief"] == 1,
          f"the distinct guest survives the fold: {est}")
    from diarize_recovery_test import fixture
    _, noisy = fixture()
    seg_est = d.cluster_segments(copy.deepcopy(noisy), -1)[3]
    check(seg_est["k"] == 2 and seg_est["suggested_max"] is None,
          f"#38 fixture through cluster_segments must resolve without a hint: {seg_est}")


def test_not_saturated_gives_no_suggestion():
    X = turns(TRUE_VOICES, 30, seed=9)
    est = d.estimate_speakers(X, durations=np.full(len(X), 15.0))
    check(est["saturated"] is False and est["suggested_max"] is None, f"{est}")
    check(d.count_recovery_warning(est, 8, 8) is None, "no warning when not saturated")


def test_hint_reaches_cluster_segments_and_legacy_estimates():
    X, dur = saturating_fixture()
    segs = []
    t = 0.0
    for v, dd in zip(X, dur):
        segs.append({"start": t, "end": t + float(dd), "emb": v.tolist()})
        t += 16.0
    est = d.cluster_segments(segs, -1)[3]
    check(est["suggested_max"] is not None and "labels" not in est, f"{est}")
    # A pre-#59 sidecar's count_estimate has no suggested_max key.
    legacy = {"raw_k": 92, "k": 20, "saturated": True, "min": None}
    check("--max-speakers" not in d.count_recovery_warning(legacy, 20, 20), "legacy: no hint")
    check(d.max_speakers_hint(None) == "", "None estimate: no hint")


def test_suggestion_clamp():
    X, dur = saturating_fixture()
    R = X[dur >= 1.0]
    merges = d._average_linkage_merges(1.0 - R @ R.T)
    check(d.suggest_max_speakers(R, merges, d.AGGLOM_THRESHOLD, 20) == TRUE_VOICES,
          "8 substantive voices give 8")
    check(d.suggest_max_speakers(R, merges, d.AGGLOM_THRESHOLD, 9) == 8, "count below the cap")
    check(d.suggest_max_speakers(R, merges, d.AGGLOM_THRESHOLD, 8) is None,
          "substantive voices reaching the cap must not be talked down to cap - 1")
    check(d.suggest_max_speakers(R, merges, d.AGGLOM_THRESHOLD, 20, min_speakers=10) == 10,
          "raised to an explicit minimum")
    check(d.suggest_max_speakers(R, merges, d.AGGLOM_THRESHOLD, 20, min_speakers=20) is None,
          "a minimum at the cap gives None")
    # Saturated with more real voices than the cap: no hint, not "19".
    X = turns(25, 30, seed=9)
    est = d.estimate_speakers(X, durations=np.full(len(X), 15.0))
    check(est["saturated"] and est["fallback"] is None and est["suggested_max"] is None,
          f"25 distinct voices must not be told to cap below 20: {est['suggested_max']}")
    est = d.estimate_speakers(turns(6, 30, seed=9), durations=np.full(180, 15.0), cap=6)
    check(est["saturated"] and est["suggested_max"] is None,
          f"6 voices at a cap of 6 must not be told 5: {est['suggested_max']}")


if __name__ == "__main__":
    test_saturated_abstain_suggests()
    test_user_max_gives_no_suggestion()
    test_brief_fold_resolves_saturation()
    test_not_saturated_gives_no_suggestion()
    test_hint_reaches_cluster_segments_and_legacy_estimates()
    test_suggestion_clamp()
    print(f"PASS: {CHECKS} assertions")
