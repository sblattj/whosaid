#!/usr/bin/env python3
"""
Unit test for the naming gates in lib/diarize_sherpa.py name_clusters (GitHub issue #77).

Exercises, with synthetic embeddings (no models, no audio):
  * pass-1 margin gate: an enrolled voice that is NOT in the meeting cannot claim a
    leftover cluster that is closer to a voice already placed (the "sink voice").
  * a genuine two-voice meeting is still named in full.
  * absorb split: a cluster absorbs into a voice ALREADY PLACED in this meeting at
    placed_absorb_threshold (0.70) only when most of its turns individually agree
    (talk-share gate); a blend of two speakers is refused.
  * absorb into a voice NOT placed in this meeting keeps the strict 0.85 bar.
  * without per-turn embeddings the low bar never applies (falls back to 0.85).

Run:
    uv run --with numpy --with mcp python test/naming_gates_test.py
"""

import inspect
import sys
from pathlib import Path

import numpy as np

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))

import diarize_sherpa as d  # noqa: E402

DIM = 64
E = np.eye(DIM, dtype=np.float32)  # orthonormal basis: exact, controllable cosines


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def mix(**parts):
    """mix(e0=.8, e1=.6) -> the vector 0.8*E[0] + 0.6*E[1] (keys are e<index>)."""
    v = np.zeros(DIM, dtype=np.float32)
    for k, w in parts.items():
        v += w * E[int(k[1:])]
    return v


def around(base_unit_vec, rng, jitter=0.02, dims=range(40, 60)):
    """A turn embedding: base plus tiny noise in dims no voice uses."""
    v = base_unit_vec.copy()
    for k in dims:
        v[k] += jitter * rng.standard_normal()
    return unit(v)


def call(cluster_emb, entries, *, ref_threshold=0.5, absorb=0.85, placed=0.70,
         turn_emb=None, names=None, ref_voices=None):
    """Call name_clusters, passing only the kwargs the current signature has, so a
    RED run on the old code fails on behaviour, not on a TypeError."""
    report = []
    kw = {"report": report, "names": names}
    sig = inspect.signature(d.name_clusters).parameters
    if "placed_absorb_threshold" in sig:
        kw["placed_absorb_threshold"] = placed
    if "turn_emb" in sig:
        kw["turn_emb"] = turn_emb
    out = d.name_clusters(cluster_emb, ref_threshold, absorb, entries, ref_voices, **kw)
    return out, report


def entries(**voices):
    return [{"name": n, "embedding": v.tolist()} for n, v in voices.items()]


def test_sink_voice_blocked():
    rng = np.random.default_rng(1)
    P, S = E[0], E[1]
    c1 = unit(mix(e0=0.95, e2=np.sqrt(1 - 0.95 ** 2)))
    c2 = unit(mix(e0=0.80, e1=0.60))           # P-ish: 0.80 to P, 0.60 to the absent S
    emb = {"SPEAKER_00": c1, "SPEAKER_01": c2}
    turns = {"SPEAKER_01": [around(c2, rng) for _ in range(6)]}
    out, rep = call(emb, entries(P=P, S=S), turn_emb=turns)
    assert out["SPEAKER_00"] == "P", out
    assert out["SPEAKER_01"] != "S", f"absent voice S claimed P's cluster: {out}"
    blocked = [r for r in rep if r.get("blocked_by") and r["pass"] == "registry"]
    assert any(r["name"] == "S" and r["cluster"] == "SPEAKER_01" and r["matched"] is False
               and r["blocked_by"] == "P" for r in blocked), rep
    # With per-turn evidence the leftover cluster folds into P (placed absorb at 0.70).
    assert out["SPEAKER_01"] == "P", out
    # Without per-turn evidence it stays anonymous: never named S.
    out2, _ = call(emb, entries(P=P, S=S))
    assert out2["SPEAKER_01"] == "SPEAKER_01", out2


def test_genuine_two_voice_meeting_named():
    P, S = E[0], E[1]
    c1 = unit(mix(e0=0.90, e1=0.40, e2=np.sqrt(1 - 0.90 ** 2 - 0.40 ** 2)))
    c2 = unit(mix(e1=0.90, e0=0.40, e3=np.sqrt(1 - 0.90 ** 2 - 0.40 ** 2)))
    out, rep = call({"SPEAKER_00": c1, "SPEAKER_01": c2}, entries(P=P, S=S))
    assert out == {"SPEAKER_00": "P", "SPEAKER_01": "S"}, out
    assert not [r for r in rep if r.get("blocked_by")], rep


def _placed_scene(c2_turns, rng):
    """P and Q both placed (clusters c1, c3); c2 is the cluster under test."""
    P, Q = E[0], E[1]
    c1 = unit(mix(e0=0.99, e5=0.1))
    c3 = unit(mix(e1=0.99, e6=0.1))
    c2 = unit(np.sum(c2_turns, axis=0))
    emb = {"SPEAKER_00": c1, "SPEAKER_01": c3, "SPEAKER_02": c2}
    return emb, entries(P=P, Q=Q), float(np.dot(c2, P))


def test_absorb_placed_voice_with_purity():
    rng = np.random.default_rng(2)
    base = unit(mix(e0=0.75, e9=0.66))          # 0.75 to P, nothing else known
    turns = [around(base, rng) for _ in range(6)]
    emb, ents, sim = _placed_scene(turns, rng)
    assert 0.72 <= sim <= 0.80, sim
    out, rep = call(emb, ents, turn_emb={"SPEAKER_02": turns})
    assert out["SPEAKER_02"] == "P", (out, sim)
    hit = [r for r in rep if r["pass"] == "absorb" and r["cluster"] == "SPEAKER_02"]
    assert len(hit) == 1 and hit[0]["matched"] and hit[0]["threshold"] == 0.70, hit
    assert hit[0]["purity"] == 1.0 and hit[0]["talk_share"] == 1.0, hit

    # Control: same centroid band, but half the turns are another voice (Q).
    A = [around(E[0], rng) for _ in range(3)]
    B = [around(unit(mix(e0=0.1, e1=0.995)), rng) for _ in range(3)]
    blend = A + B
    emb, ents, sim = _placed_scene(blend, rng)
    assert 0.70 <= sim < 0.85, sim
    out, rep = call(emb, ents, turn_emb={"SPEAKER_02": blend})
    assert out["SPEAKER_02"] == "SPEAKER_02", f"blend was absorbed: {out} ({sim:.3f})"
    hit = [r for r in rep if r["pass"] == "absorb" and r["cluster"] == "SPEAKER_02"]
    assert len(hit) == 1 and not hit[0]["matched"], hit
    assert hit[0].get("purity") is not None and hit[0]["purity"] <= 0.5, hit
    assert hit[0]["talk_share"] < 0.6, hit


def test_unplaced_voice_keeps_strict_absorb():
    rng = np.random.default_rng(3)
    P, U = E[0], E[2]
    c1 = unit(mix(e0=0.99, e5=0.1))
    # ref_threshold 0.95 keeps pass 1 out of the way so this isolates pass 3.
    for sim_u, want in ((0.75, "SPEAKER_01"), (0.90, "U")):
        c2 = unit(mix(e2=sim_u, e9=np.sqrt(1 - sim_u ** 2)))
        turns = [around(c2, rng) for _ in range(6)]
        out, rep = call({"SPEAKER_00": c1, "SPEAKER_01": c2}, entries(P=P, U=U),
                        ref_threshold=0.95, turn_emb={"SPEAKER_01": turns})
        assert out["SPEAKER_00"] == "P", out
        assert out["SPEAKER_01"] == want, f"U at {sim_u}: {out}"


def test_no_turn_emb_falls_back_to_strict():
    rng = np.random.default_rng(4)
    base = unit(mix(e0=0.75, e9=0.66))
    turns = [around(base, rng) for _ in range(6)]
    emb, ents, _ = _placed_scene(turns, rng)
    out, rep = call(emb, ents)                    # no turn_emb
    assert out["SPEAKER_02"] == "SPEAKER_02", out
    hit = [r for r in rep if r["pass"] == "absorb" and r["cluster"] == "SPEAKER_02"]
    assert len(hit) == 1 and hit[0]["threshold"] == 0.85 and not hit[0]["matched"], hit
    # Control that must differ: the same scene WITH turns is absorbed.
    out, _ = call(emb, ents, turn_emb={"SPEAKER_02": turns})
    assert out["SPEAKER_02"] == "P", out


def test_cli_flag_and_default():
    import os
    os.environ.pop("WHOSAID_PLACED_ABSORB_THRESHOLD", None)
    src = (REPO_DIR / "lib" / "diarize_sherpa.py").read_text()
    assert "--placed-absorb-threshold" in src
    assert "WHOSAID_PLACED_ABSORB_THRESHOLD" in src


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"{len(tests) - bad}/{len(tests)} passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
