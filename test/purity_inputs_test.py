#!/usr/bin/env python3
"""
Unit test for the per-turn inputs (and purity) recorded by name_clusters (GitHub issue #77).

Every absorb record whose purity was computed also carries `turns`, the per-turn inputs
behind it, so the gate can be tuned offline. Synthetic one-hot embeddings; no models.

Run:
    uv run --with numpy --with mcp python test/purity_inputs_test.py
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


def scene(turns):
    """P (e0) and Q (e1) placed via clusters 00/01; cluster 02 is the one under test."""
    # c1 shares the e9 component with cluster 02, so 02 sits close to P's own cluster
    # (sibling 0.98) as a real split does; the sibling-margin gate passes.
    c1, c3 = unit(E[0] + 0.1 * E[5] + 0.6 * E[9]), unit(E[1] + 0.1 * E[6])
    # fixed centroid in the placed-absorb band (0.75 to P, under the strict 0.85 bar),
    # independent of the turns: the gate under test reads the turns, not the centroid
    emb = {"SPEAKER_00": c1, "SPEAKER_01": c3, "SPEAKER_02": unit(0.75 * E[0] + 0.66 * E[9])}
    ents = [{"name": "P", "embedding": E[0].tolist()}, {"name": "Q", "embedding": E[1].tolist()}]
    return emb, ents


def run(turns, spans=True, with_turns=True):
    emb, ents = scene(turns)
    rep = []
    kw = {}
    if with_turns:
        kw["turn_emb"] = {"SPEAKER_02": turns}
        if spans:
            # deliberately NOT in time order: the record must come out sorted by start
            kw["turn_spans"] = {"SPEAKER_02": [(10.0 * (len(turns) - i), 10.0 * (len(turns) - i) + 2.5)
                                               for i in range(len(turns))]}
    out = d.name_clusters(emb, 0.5, 0.85, ents, [], report=rep,
                          placed_absorb_threshold=THRESH, **kw)
    hit = [r for r in rep if r["pass"] == "absorb" and r["cluster"] == "SPEAKER_02"]
    assert len(hit) == 1, rep
    return out, hit[0]


def turn(p, q=0.0):
    """A unit turn with cosine p to P and q to Q."""
    rest = np.sqrt(max(0.0, 1 - p * p - q * q))
    return unit(p * E[0] + q * E[1] + rest * E[9])


def test_refused_records_turns():
    turns = [turn(0.8), turn(0.6, 0.7), turn(0.1, 0.99), turn(0.75), turn(0.05, 0.99)]
    out, rec = run(turns)
    assert out["SPEAKER_02"] == "SPEAKER_02" and not rec["matched"], (out, rec)
    t = rec["turns"]
    assert len(t) == 5, t
    assert [r["start"] for r in t] == sorted(r["start"] for r in t), t
    # spans were handed in reversed, so the time-ordered record is the reverse of input
    for r, v in zip(t, reversed(turns)):
        assert set(r) == {"start", "end", "score", "best", "best_score"}, r
        assert r["end"] == r["start"] + 2.5, r
        assert abs(r["score"] - round(float(np.dot(v, E[0])), 4)) < 1e-9, (r, v)
    assert [r["best"] for r in t] == ["Q", "P", "Q", "Q", "P"], t
    assert t[0]["best_score"] > 0.9 and t[1]["score"] == t[1]["best_score"], t


def test_matched_records_turns():
    turns = [turn(0.8), turn(0.75), turn(0.78), turn(0.1, 0.99)]
    out, rec = run(turns)
    assert out["SPEAKER_02"] == "P" and rec["matched"], (out, rec)
    assert len(rec["turns"]) == 4 and rec["purity"] == 0.75, rec


def test_no_spans_gives_null_times():
    out, rec = run([turn(0.8), turn(0.75), turn(0.78)], spans=False)
    assert all(r["start"] is None and r["end"] is None for r in rec["turns"]), rec
    assert len(rec["turns"]) == 3 and rec["matched"], rec


def test_without_turn_emb_no_turns_key():
    turns = [turn(0.8), turn(0.75), turn(0.78)]
    out, rec = run(turns, with_turns=False)
    assert "turns" not in rec and "purity" not in rec and not rec["matched"], rec
    assert out["SPEAKER_02"] == "SPEAKER_02", out
    # decisions identical with and without spans
    o1, r1 = run(turns, spans=True)
    o2, r2 = run(turns, spans=False)
    assert o1 == o2 and r1["purity"] == r2["purity"] and r1["matched"] == r2["matched"]


def test_purity_equals_recomputed_fraction():
    for turns in ([turn(0.8), turn(0.6, 0.7), turn(0.1, 0.99), turn(0.75), turn(0.05, 0.99)],
                  [turn(0.8), turn(0.75), turn(0.78), turn(0.1, 0.99)],
                  [turn(0.72), turn(0.68), turn(0.74), turn(0.9)]):
        _, rec = run(turns)
        t = rec["turns"]
        ok = sum(1 for r in t if r["score"] >= THRESH and r["best"] == rec["name"])
        assert abs(ok / len(t) - rec["purity"]) < 1e-3, (ok, len(t), rec)
        assert rec["matched"] == (ok / len(t) > 0.5), rec


def test_misaligned_spans_give_null_times():
    turns = [turn(0.8), turn(0.75), turn(0.78)]
    emb, ents = scene(turns)
    rep = []
    d.name_clusters(emb, 0.5, 0.85, ents, [], report=rep, placed_absorb_threshold=THRESH,
                    turn_emb={"SPEAKER_02": turns}, turn_spans={"SPEAKER_02": [(1.0, 3.0)]})
    rec = [r for r in rep if r["pass"] == "absorb" and r["cluster"] == "SPEAKER_02"][0]
    assert len(rec["turns"]) == 3, rec
    assert all(r["start"] is None and r["end"] is None for r in rec["turns"]), rec


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("purity_inputs_test: all passed")
