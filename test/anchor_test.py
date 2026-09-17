#!/usr/bin/env python3
"""
Unit test for registry-anchored diarization, `--expected-speakers` (GitHub issue #1, part 4).

The bug this guards: on a roster whose attendance varies, an exact `--num-speakers N`
merges two distinct people the moment fewer than N show up, and bare auto-detect
over-segments and then leaves a major speaker's averaged cluster unmatched. Anchoring
pins every turn that is close enough to an ENROLLED voiceprint straight to that person
and clusters only the residual, so the count only has to be guessed for the strangers.

Synthetic embeddings only — no models, no audio. Fixtures reproduce the cosine regime
MEASURED on real TitaNet-small per-turn embeddings from this pipeline (two 17.8-minute
6-voice recordings, 65 and 72 embedded turns, 192-dim): intra-speaker ~0.90 (p10
0.72-0.82), inter-speaker ~0.25 (p90 ~0.45). Each speaker is a shared "channel"
direction plus a private direction, jittered per turn; an all-random basis would put
inter-speaker similarity near 0.0, which is a strictly easier problem than the real one,
and it is the inter-speaker FLOOR that decides where the anchor gate has to sit.

Run:
    uv run --with numpy python test/anchor_test.py
"""

import os
import sys
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


def voices(n_speakers: int, per_speaker: int, jitter: float = 0.024,
           common: float = 0.40, seed: int = 0) -> tuple:
    """(enrollment voiceprints, per-turn embeddings, true speaker index per turn).

    The enrollment voiceprint is the speaker's clean base direction — what an
    enrolled clip or a registry entry holds — and every turn is that base plus
    per-turn jitter, which is what pulls intra-speaker cosine down off 1.0.
    `common` is the shared channel component that lifts inter-speaker cosine off 0.0.
    """
    rng = np.random.default_rng(seed)
    channel = rng.normal(size=DIM)
    channel /= np.linalg.norm(channel)
    bases, rows, truth = [], [], []
    for i in range(n_speakers):
        private = rng.normal(size=DIM)
        private /= np.linalg.norm(private)
        base = common * channel + (1.0 - common) * private
        base /= np.linalg.norm(base)
        bases.append(base.astype(np.float32))
        for _ in range(per_speaker):
            v = base + rng.normal(scale=jitter, size=DIM)
            rows.append(v / np.linalg.norm(v))
            truth.append(i)
    return (np.array(bases, dtype=np.float32),
            np.array(rows, dtype=np.float32),
            np.array(truth))


def segments_from(X: np.ndarray, truth: np.ndarray) -> list:
    """Turn a fixture into the segment dicts cluster_segments() consumes.

    Start times are spaced so no two segments collide and so the true speaker is
    recoverable from `start` after cluster_segments sorts its output by time.
    """
    return [{"start": float(3 * i), "end": float(3 * i) + 2.0, "emb": X[i].tolist()}
            for i in range(len(X))]


def true_of(seg: dict, truth: np.ndarray) -> int:
    return int(truth[int(round(seg["start"] / 3.0))])


def cosine_stats(X: np.ndarray, truth: np.ndarray) -> tuple:
    S = X @ X.T
    same = (truth[:, None] == truth[None, :]) & ~np.eye(len(X), dtype=bool)
    return float(S[same].mean()), float(S[truth[:, None] != truth[None, :]].mean())


def run(X, truth, anchors, num_speakers=-1, thresh=None, **kw):
    """cluster_segments over a fixture; returns its full 5-tuple."""
    return d.cluster_segments(segments_from(X, truth), num_speakers,
                              anchors=anchors, anchor_threshold=thresh, **kw)


# ---------------------------------------------------------------------------


def test_fixture_matches_the_measured_regime():
    """Guard the guard: a fixture outside the measured band is not testing the real
    problem, and would let a wrong anchor threshold pass."""
    bases, X, truth = voices(5, 30, seed=1)
    intra, inter = cosine_stats(X, truth)
    check(0.87 <= intra <= 0.93,
          f"fixture intra-speaker cosine must match the measured ~0.90, got {intra:.3f}")
    check(0.20 <= inter <= 0.32,
          f"fixture inter-speaker cosine must match the measured ~0.25, got {inter:.3f}")
    # The gate must sit inside the empty band: above every stranger, below the
    # bulk of genuine same-speaker turns. Check it against the ENROLLMENT
    # voiceprints, which is the comparison anchoring actually makes.
    S = X @ bases.T
    own = S[np.arange(len(X)), truth]
    other = S[truth[:, None] != np.arange(len(bases))[None, :]]
    t = d.DEFAULT_ANCHOR_THRESHOLD
    check(t > float(np.percentile(other, 90)),
          f"anchor threshold {t} must sit above the stranger p90 "
          f"{float(np.percentile(other, 90)):.3f}")
    check(t <= float(np.percentile(own, 10)) + 0.02,
          f"anchor threshold {t} must sit at or below the same-speaker p10 "
          f"{float(np.percentile(own, 10)):.3f}")
    check(float(other.max()) < t,
          f"no stranger may reach the gate: max stranger cosine {float(other.max()):.3f} >= {t}")


def test_anchored_turns_land_on_the_right_anchor():
    """3 enrolled speakers + 2 unknowns: every anchored turn must carry the name of
    the person who actually spoke it, and the two unknowns must form their own clusters."""
    bases, X, truth = voices(5, 30, seed=1)
    anchors = [("Alice", bases[0]), ("Bob", bases[1]), ("Carol", bases[2])]
    segs, cluster_emb, speakers, estimate, info = run(X, truth, anchors)

    check(info is not None, "anchored run must return an anchor_info record")
    name_of = info["anchored"]                       # {SPEAKER_NN: name}
    by_name = {n: sp for sp, n in name_of.items()}
    for n in ("Alice", "Bob", "Carol"):
        check(n in by_name, f"{n} spoke 30 turns and must have an anchored cluster: {name_of}")

    truth_name = {0: "Alice", 1: "Bob", 2: "Carol"}
    misassigned = 0
    unknown_clusters = set()
    for s in segs:
        t = true_of(s, truth)
        sp = s["speaker"]
        if t in truth_name:
            if name_of.get(sp) != truth_name[t]:
                misassigned += 1
        else:
            if sp in name_of:
                misassigned += 1      # a stranger stole an enrolled person's name
            else:
                unknown_clusters.add(sp)
    check(misassigned == 0,
          f"{misassigned} turn(s) landed on the wrong speaker under anchoring")
    check(len(unknown_clusters) == 2,
          f"the 2 unenrolled speakers must form exactly 2 residual clusters, "
          f"got {len(unknown_clusters)}: {sorted(unknown_clusters)}")
    check(len(speakers) == 5, f"3 anchors + 2 residual = 5 clusters, got {speakers}")
    check(set(speakers) == set(cluster_emb),
          "every returned speaker must have a centroid in cluster_emb")
    check(sorted(speakers) == [f"SPEAKER_{i:02d}" for i in range(5)],
          f"clusters must be labelled SPEAKER_00..NN with no gaps: {speakers}")


def test_anchor_stats_and_estimate_are_reported():
    bases, X, truth = voices(5, 30, seed=1)
    anchors = [("Alice", bases[0]), ("Bob", bases[1]), ("Carol", bases[2])]
    segs, cluster_emb, speakers, estimate, info = run(X, truth, anchors)

    check(set(info["names"]) == {"Alice", "Bob", "Carol"},
          f"anchors report must name exactly the attendees: {info['names']}")
    for n, stat in info["names"].items():
        check(stat["turns"] == 30, f"{n} spoke 30 turns, report says {stat['turns']}")
        check(stat["mean_cosine"] >= d.DEFAULT_ANCHOR_THRESHOLD,
              f"{n}'s mean anchor cosine {stat['mean_cosine']} must clear the gate")
    check(info["threshold"] == d.DEFAULT_ANCHOR_THRESHOLD,
          f"anchor_info must record the gate it used: {info['threshold']}")
    # The count estimate now describes the RESIDUAL only — that is the whole point:
    # bounds and the cap apply to the unknowns, not to people we already identified.
    check(estimate is not None, "an auto-count anchored run must still report an estimate")
    check(estimate["anchored"] is True, f"estimate must be flagged anchored: {estimate}")
    check(estimate["anchors_active"] == 3, f"3 anchors attended: {estimate}")
    check(estimate["residual_turns"] == 60,
          f"2 unenrolled speakers x 30 turns = 60 residual turns: {estimate}")
    check("labels" not in estimate, "the label array must not leak into the sidecar record")


def test_an_absent_expected_speaker_is_dropped():
    """The flag that makes a varying roster work: Dave is on the list and never speaks,
    so he must produce NO cluster — not an empty one, and not a stolen one."""
    bases, X, truth = voices(5, 30, seed=1)
    absent = bases[4].copy()            # speaker 4's turns are removed below
    keep = truth < 4
    X4, truth4 = X[keep], truth[keep]
    anchors = [("Alice", bases[0]), ("Bob", bases[1]),
               ("Carol", bases[2]), ("Dave", absent)]
    segs, cluster_emb, speakers, estimate, info = run(X4, truth4, anchors)

    check("Dave" not in info["names"],
          f"an expected speaker who never spoke must be dropped: {info['names']}")
    check("Dave" not in set(info["anchored"].values()),
          f"a dropped anchor must own no SPEAKER_NN label: {info['anchored']}")
    check(set(info["names"]) == {"Alice", "Bob", "Carol"},
          f"the three who attended must all be anchored: {info['names']}")
    check(len(speakers) == 4,
          f"3 attendees + 1 unenrolled speaker = 4 clusters, got {speakers}: "
          f"a dropped anchor must not leave an empty cluster behind")
    check(sorted(speakers) == [f"SPEAKER_{i:02d}" for i in range(4)],
          f"dropping an anchor must not leave a gap in the labels: {speakers}")


def test_names_map_is_preseedable():
    """main() pre-seeds `names` from anchor_info before name_clusters() runs, and
    name_clusters only fills STILL-UNNAMED clusters — so the anchor must win."""
    bases, X, truth = voices(5, 30, seed=1)
    anchors = [("Alice", bases[0]), ("Bob", bases[1]), ("Carol", bases[2])]
    segs, cluster_emb, speakers, estimate, info = run(X, truth, anchors)

    names = {sp: sp for sp in speakers}
    for sp, n in info["anchored"].items():
        check(sp in names, f"anchored label {sp} must be a real cluster: {speakers}")
        names[sp] = n

    # A registry holding a DIFFERENT, wrong name for Alice's voice must not be able
    # to overwrite the anchor — that is the ordering guarantee this seeding buys.
    entries = [{"name": "Mallory", "model": "x", "embedding": bases[0].tolist()}]
    out = d.name_clusters(cluster_emb, 0.10, 0.85, entries, [], names)
    alice_cluster = {n: sp for sp, n in info["anchored"].items()}["Alice"]
    check(out[alice_cluster] == "Alice",
          f"the anchored name must survive naming, got {out[alice_cluster]!r}")
    check("Mallory" not in {out[sp] for sp in info["anchored"]},
          f"no anchored cluster may be renamed by a later pass: {out}")
    # The residual clusters are still free for the ordinary passes to name.
    residual = [sp for sp in speakers if sp not in info["anchored"]]
    check(len(residual) == 2, f"2 residual clusters expected, got {residual}")


def test_threshold_is_clamped_and_honoured():
    bases, X, truth = voices(5, 30, seed=1)
    anchors = [("Alice", bases[0]), ("Bob", bases[1]), ("Carol", bases[2])]

    # Above 1.0: no cosine can ever reach it, so nothing is pinned and EVERY turn
    # falls through to ordinary clustering. It must clamp, not crash or pin all.
    _s, _c, speakers, _e, info = run(X, truth, anchors, thresh=5.0)
    check(info["threshold"] == 1.0, f"a threshold above 1.0 must clamp to 1.0: {info}")
    check(info["names"] == {}, f"nothing may be anchored at a cosine of 1.0: {info['names']}")
    check(info["anchored"] == {}, f"no cluster may be anchored at 1.0: {info['anchored']}")
    check(len(speakers) >= 1, "the residual path must still produce clusters")

    # Below -1.0: every cosine clears it, so every turn is pinned to its nearest
    # anchor and there is no residual at all.
    _s, _c, speakers, estimate, info = run(X, truth, anchors, thresh=-5.0)
    check(info["threshold"] == -1.0, f"a threshold below -1.0 must clamp to -1.0: {info}")
    check(len(speakers) == 3,
          f"at -1.0 every turn is pinned to one of the 3 anchors, got {speakers}")
    check(sum(s["turns"] for s in info["names"].values()) == len(X),
          f"every turn must be accounted for: {info['names']}")
    check(estimate is None, "with no residual turns there is no count to estimate")

    # A gate in the real band pins the enrolled speakers and only them.
    _s, _c, speakers, _e, info = run(X, truth, anchors, thresh=0.70)
    check(set(info["names"]) == {"Alice", "Bob", "Carol"},
          f"0.70 must anchor exactly the 3 enrolled voices: {info['names']}")


def test_default_threshold_and_env_override():
    check(abs(d.DEFAULT_ANCHOR_THRESHOLD - 0.70) < 1e-9,
          f"documented default is 0.70, got {d.DEFAULT_ANCHOR_THRESHOLD}")
    check(d.DEFAULT_ANCHOR_THRESHOLD > 0.45,
          "the gate must sit strictly above the measured inter-speaker p90 ~0.45")
    check(d.DEFAULT_ANCHOR_THRESHOLD <= 0.72,
          "the gate must sit at or below the measured intra-speaker p10 ~0.72")
    prev = os.environ.get("WHOSAID_ANCHOR_THRESHOLD")
    try:
        os.environ["WHOSAID_ANCHOR_THRESHOLD"] = "0.55"
        check(abs(d.default_anchor_threshold() - 0.55) < 1e-9,
              "WHOSAID_ANCHOR_THRESHOLD must override the default")
        os.environ["WHOSAID_ANCHOR_THRESHOLD"] = "not-a-number"
        check(abs(d.default_anchor_threshold() - d.DEFAULT_ANCHOR_THRESHOLD) < 1e-9,
              "an unparseable env value must fall back to the default, not crash")
        os.environ["WHOSAID_ANCHOR_THRESHOLD"] = "  "
        check(abs(d.default_anchor_threshold() - d.DEFAULT_ANCHOR_THRESHOLD) < 1e-9,
              "a blank env value must fall back to the default")
    finally:
        os.environ.pop("WHOSAID_ANCHOR_THRESHOLD", None)
        if prev is not None:
            os.environ["WHOSAID_ANCHOR_THRESHOLD"] = prev
    # Omitting the threshold entirely must resolve to the same default.
    bases, X, truth = voices(5, 30, seed=1)
    _s, _c, _sp, _e, info = run(X, truth, [("Alice", bases[0])], thresh=None)
    check(info["threshold"] == d.DEFAULT_ANCHOR_THRESHOLD,
          f"a None threshold must resolve to the default: {info['threshold']}")


def test_exact_count_budgets_the_residual():
    """--num-speakers with anchors still means the TOTAL: the anchors take what they
    take and the remainder is the residual budget."""
    bases, X, truth = voices(5, 30, seed=1)
    anchors = [("Alice", bases[0]), ("Bob", bases[1]), ("Carol", bases[2])]
    _s, _c, speakers, estimate, info = run(X, truth, anchors, num_speakers=5)
    check(len(speakers) == 5, f"--num-speakers 5 with 3 anchors must give 5: {speakers}")
    check(estimate is None, "an explicit count reports no auto estimate")
    check(len(info["anchored"]) == 3, f"3 anchors must still be pinned: {info['anchored']}")


def test_bounds_apply_to_the_residual_only():
    """min/max bound the UNKNOWNS. Anchored people are already identified, so they
    must not consume the budget — the old exact-count trade is exactly what this fixes."""
    bases, X, truth = voices(5, 30, seed=1)
    anchors = [("Alice", bases[0]), ("Bob", bases[1]), ("Carol", bases[2])]
    _s, _c, speakers, estimate, info = run(X, truth, anchors, max_speakers=1)
    check(estimate["max"] == 1, f"the residual estimate must carry the bound: {estimate}")
    check(len(info["anchored"]) == 3,
          f"--max-speakers 1 must NOT squeeze the 3 anchored people: {info['anchored']}")
    check(len(speakers) == 4,
          f"3 anchors + at most 1 residual cluster = 4, got {speakers}")

    _s, _c, speakers, estimate, info = run(X, truth, anchors, min_speakers=4)
    check(estimate["min"] == 4, f"the residual estimate must carry the bound: {estimate}")
    check(len(speakers) == 7,
          f"3 anchors + a floor of 4 residual clusters = 7, got {speakers}")


def test_unanchored_path_is_unchanged():
    """No anchors -> the pre-existing behaviour, byte for byte, with anchor_info None."""
    bases, X, truth = voices(5, 30, seed=1)
    segs, cluster_emb, speakers, estimate, info = d.cluster_segments(
        segments_from(X, truth), -1)
    check(info is None, f"an unanchored run must report no anchor_info, got {info}")
    check(len(speakers) == 5, f"the plain estimator must still find 5 speakers: {speakers}")
    check(estimate is not None and "anchored" not in estimate,
          f"an unanchored estimate must not be flagged anchored: {estimate}")
    # Anchors explicitly disabled (None / empty list) take the same path.
    for empty in (None, []):
        _s, _c, sp2, _e, i2 = d.cluster_segments(segments_from(X, truth), -1, anchors=empty)
        check(i2 is None, f"anchors={empty!r} must take the unanchored path")
        check(sp2 == speakers, f"anchors={empty!r} must not change the result")


def test_short_turns_inherit_a_label():
    """Segments too short to embed carry no 'emb'; they must still come back with a
    speaker, inherited from the nearest embedded turn in time (unchanged by anchoring)."""
    bases, X, truth = voices(3, 20, seed=2)
    anchors = [("Alice", bases[0])]
    segs_in = segments_from(X, truth)
    segs_in.append({"start": 1.0, "end": 1.2})           # no emb: too short
    segs_in.append({"start": 3 * len(X) + 5.0, "end": 3 * len(X) + 5.2})
    segs, _c, speakers, _e, info = d.cluster_segments(segs_in, -1, anchors=anchors)
    check(len(segs) == len(segs_in),
          f"no segment may be dropped: {len(segs)} out of {len(segs_in)}")
    check(all(s.get("speaker") for s in segs), "every segment must carry a speaker label")
    check(all(s["speaker"] in speakers for s in segs),
          "every segment's speaker must be one of the reported clusters")
    # The unembeddable turn at t=1.0 sits inside Alice's block, so it inherits her.
    first = min(segs, key=lambda s: s["start"])
    check(info["anchored"].get(first["speaker"]) == "Alice",
          f"a short turn inside Alice's block must inherit her cluster: {first}")


def test_degenerate_inputs():
    bases, X, truth = voices(3, 20, seed=3)
    # No embedded turns at all -> the empty 5-tuple, not a crash.
    out = d.cluster_segments([{"start": 0.0, "end": 0.2}], -1,
                             anchors=[("Alice", bases[0])])
    check(out == ([], {}, [], None, None), f"an unembeddable input must return empties: {out}")
    # A single anchor that claims everything leaves no residual to estimate.
    one = X[truth == 0]
    _s, _c, speakers, estimate, info = d.cluster_segments(
        segments_from(one, truth[truth == 0]), -1, anchors=[("Alice", bases[0])])
    check(speakers == ["SPEAKER_00"], f"one speaker, one cluster: {speakers}")
    check(info["anchored"] == {"SPEAKER_00": "Alice"}, f"{info['anchored']}")
    check(estimate is None, "no residual turns means no residual estimate")
    # A duplicate anchor embedding must not crash or double-count: argmax picks one.
    _s, _c, speakers, _e, info = d.cluster_segments(
        segments_from(one, truth[truth == 0]), -1,
        anchors=[("Alice", bases[0]), ("AliceAgain", bases[0])])
    check(len(info["names"]) == 1,
          f"two identical voiceprints must not both claim the turns: {info['names']}")


def main() -> None:
    test_fixture_matches_the_measured_regime()
    test_anchored_turns_land_on_the_right_anchor()
    test_anchor_stats_and_estimate_are_reported()
    test_an_absent_expected_speaker_is_dropped()
    test_names_map_is_preseedable()
    test_threshold_is_clamped_and_honoured()
    test_default_threshold_and_env_override()
    test_exact_count_budgets_the_residual()
    test_bounds_apply_to_the_residual_only()
    test_unanchored_path_is_unchanged()
    test_short_turns_inherit_a_label()
    test_degenerate_inputs()
    print(f"PASS: {CHECKS} assertions")
    sys.exit(0)


if __name__ == "__main__":
    main()
