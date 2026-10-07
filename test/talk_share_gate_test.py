#!/usr/bin/env python3
"""
Unit test for the seconds-weighted talk-share gate in name_clusters (GitHub issue #77).

A still-unnamed cluster is absorbed into a placed voice when >= 60% of its talk time is in
turns whose best known voice is that voice and that score >= 0.40 against it. Synthetic
one-hot embeddings; no models.

Run:
    uv run --with numpy --with mcp python test/talk_share_gate_test.py
"""

import sys
from pathlib import Path

import numpy as np

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import diarize_sherpa as d  # noqa: E402

E = np.eye(64, dtype=np.float32)
THRESH = 0.70


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


C02 = unit(0.75 * E[0] + 0.66 * E[9])


def sibling_at(target):
    """A centroid for P's own cluster (e0 + x*e9, still P's best registry match) whose
    cosine to cluster 02 is `target`, found by bisection on x."""
    lo, hi = 0.0, 0.85
    for _ in range(60):
        x = (lo + hi) / 2
        if float(np.dot(unit(E[0] + x * E[9]), C02)) < target:
            lo = x
        else:
            hi = x
    return unit(E[0] + hi * E[9])


def scene(c1=None):
    """P (e0) and Q (e1) placed via clusters 00/01; cluster 02 is the one under test.
    c1 overrides P's own cluster centroid (to move the sibling away)."""
    # c1 shares the e9 component with cluster 02, so 02 sits close to P's own cluster
    # (sibling 0.98) as a real split does; the sibling-margin gate passes.
    c1_override = c1
    c1, c3 = unit(E[0] + 0.1 * E[5] + 0.6 * E[9]), unit(E[1] + 0.1 * E[6])
    # fixed centroid in the placed-absorb band (0.75 to P, under the strict 0.85 bar),
    # independent of the turns: the gate under test reads the turns, not the centroid
    emb = {"SPEAKER_00": c1 if c1_override is None else c1_override, "SPEAKER_01": c3,
           "SPEAKER_02": C02}
    ents = [{"name": "P", "embedding": E[0].tolist()}, {"name": "Q", "embedding": E[1].tolist()}]
    return emb, ents


def run(turns, durs=None, c1=None):
    """durs: per-turn seconds (spans laid end to end); None => no turn_spans."""
    emb, ents = scene(c1)
    rep = []
    kw = {"turn_emb": {"SPEAKER_02": turns}}
    if durs is not None:
        t, spans = 0.0, []
        for dur in durs:
            spans.append((t, t + dur))
            t += dur + 1.0
        kw["turn_spans"] = {"SPEAKER_02": spans}
    out = d.name_clusters(emb, 0.5, 0.85, ents, [], report=rep,
                          placed_absorb_threshold=THRESH, **kw)
    hit = [r for r in rep if r["pass"] == "absorb" and r["cluster"] == "SPEAKER_02"]
    assert len(hit) == 1, rep
    return out, hit[0]


def turn(p, q=0.0):
    """A unit turn with cosine p to P and q to Q."""
    rest = np.sqrt(max(0.0, 1 - p * p - q * q))
    return unit(p * E[0] + q * E[1] + rest * E[9])


QTURN = turn(0.1, 0.99)


def test_low_scoring_split_is_absorbed():
    # the #77 failure case: every turn scores 0.45-0.65 vs P (all < 0.70), so the old
    # purity was 0.0 and the split was refused
    turns = [turn(s) for s in (0.45, 0.5, 0.55, 0.6, 0.65, 0.48)]
    out, rec = run(turns, durs=[2.0] * 6)
    assert rec["purity"] == 0.0, rec
    assert rec["talk_share"] == 1.0 and rec["matched"], rec
    assert out["SPEAKER_02"] == "P", out


def test_seconds_weighting_absorbs_long_p_turns():
    turns = [turn(0.7), turn(0.7)] + [QTURN] * 4
    out, rec = run(turns, durs=[10.0, 10.0, 1.0, 1.0, 1.0, 1.0])
    assert 2 / 6 < 0.6  # equal-weight count share would refuse
    assert abs(rec["talk_share"] - 20 / 24) < 1e-3 and rec["matched"], rec
    assert out["SPEAKER_02"] == "P", out


def test_long_q_turns_are_refused():
    turns = [turn(0.7), turn(0.7)] + [QTURN] * 4
    out, rec = run(turns, durs=[1.0, 1.0, 10.0, 10.0, 10.0, 10.0])
    assert abs(rec["talk_share"] - 2 / 42) < 1e-3 and not rec["matched"], rec
    assert out["SPEAKER_02"] == "SPEAKER_02", out


def test_turns_below_floor_do_not_count():
    # best known voice is P (nothing else is close) but the score is under 0.40
    turns = [turn(0.35), turn(0.3), turn(0.2), turn(0.7)]
    out, rec = run(turns, durs=[5.0, 5.0, 5.0, 5.0])
    assert [r["best"] for r in rec["turns"]] == ["P"] * 4, rec
    assert abs(rec["talk_share"] - 0.25) < 1e-3 and not rec["matched"], rec
    assert out["SPEAKER_02"] == "SPEAKER_02", out


def test_without_spans_count_weighting():
    turns = [turn(0.7), turn(0.7)] + [QTURN] * 4
    out, rec = run(turns, durs=None)
    assert abs(rec["talk_share"] - 2 / 6) < 1e-3 and not rec["matched"], rec
    turns = [turn(0.7)] * 4 + [QTURN] * 2
    out, rec = run(turns, durs=None)
    assert abs(rec["talk_share"] - 4 / 6) < 1e-3 and rec["matched"], rec
    assert out["SPEAKER_02"] == "P", out


def test_record_carries_purity_and_talk_share():
    turns = [turn(0.8), turn(0.5), turn(0.5), QTURN]
    _, rec = run(turns, durs=[3.0, 3.0, 3.0, 3.0])
    assert rec["purity"] == 0.25 and rec["talk_share"] == 0.75, rec
    assert rec["matched"] and len(rec["turns"]) == 4, rec


def test_unenrolled_lookalike_is_refused():
    # v1.13.1: a stranger who sounds like P. Every turn's best known voice is P, so
    # talk share is 1.0, but the cluster is no closer to P's own cluster here (0.747)
    # than to P's voiceprint (0.751): no sibling margin, stays anonymous.
    turns = [turn(0.72)] * 6
    out, rec = run(turns, durs=[5.0] * 6, c1=unit(E[0] + 0.1 * E[5]))
    assert rec["talk_share"] == 1.0, rec
    assert out["SPEAKER_02"] == "SPEAKER_02", out
    assert not rec["matched"], rec
    assert abs(rec["sibling_sim"] - 0.747) < 1e-3, rec


def test_sibling_margin_boundary():
    sim = float(np.dot(C02, E[0]))
    turns = [turn(0.6)] * 4
    for gap, want in ((0.030, False), (0.040, True)):
        out, rec = run(turns, durs=[3.0] * 4, c1=sibling_at(sim + gap))
        assert rec["talk_share"] == 1.0, rec
        assert (out["SPEAKER_02"] == "P") is want, (gap, out)
        assert rec["matched"] is want, (gap, rec)
        assert abs(rec["sibling_sim"] - (sim + gap)) < 1e-3, rec


def test_matched_record_carries_sibling_sim():
    _, rec = run([turn(0.6)] * 3, durs=[2.0] * 3)
    assert rec["matched"] and rec["sibling_sim"] > rec["similarity"] + 0.035, rec


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("talk_share_gate_test: all passed")
