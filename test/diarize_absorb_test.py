#!/usr/bin/env python3
"""
Unit test for the phantom-speaker fixes in lib/diarize_sherpa.py.

Exercises, with synthetic embeddings (no models, no audio):
  * name_clusters absorb pass — phantom splits of one voice fold into that voice.
  * name_clusters --ref skip — a ref whose name the registry already used is skipped
    (no duplicate speaker for one person).
  * estimate_k cap guard — a saturating auto-count is re-estimated below the cap.
  * render_outputs card merge — one card per NAME with combined turns/talk time.

Run:
    uv run --with numpy python test/diarize_absorb_test.py
"""

import sys
import tempfile
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


def test_absorb_pass():
    rng = np.random.default_rng(1)
    matt = voice(rng)
    mike = voice(rng)
    stranger = voice(rng)
    # Two Matt clusters (a phantom split), one Mike cluster, one genuine unknown.
    c00 = near(rng, matt, 0.023)  # Matt main  (~0.95)
    c01 = near(rng, matt, 0.035)  # Matt split (~0.90, still >= absorb 0.85)
    c02 = near(rng, mike, 0.023)  # Mike       (~0.95)
    c03 = stranger                # unknown    (~0.0 to everyone)
    cluster_emb = {"SPEAKER_00": c00, "SPEAKER_01": c01, "SPEAKER_02": c02, "SPEAKER_03": c03}
    registry = [{"name": "Matt", "embedding": matt.tolist()},
                {"name": "Mike", "embedding": mike.tolist()}]

    names = {sp: sp for sp in cluster_emb}
    d.name_clusters(cluster_emb, 0.40, 0.85, registry, [], names)

    check(names["SPEAKER_00"] == "Matt", f"00 should be Matt, got {names['SPEAKER_00']}")
    check(names["SPEAKER_01"] == "Matt", f"01 (phantom split) should absorb to Matt, got {names['SPEAKER_01']}")
    check(names["SPEAKER_02"] == "Mike", f"02 should be Mike, got {names['SPEAKER_02']}")
    check(names["SPEAKER_03"] == "SPEAKER_03", f"03 (stranger) must stay UNIDENTIFIED, got {names['SPEAKER_03']}")
    # sanity: the split's similarity really is in the absorb band, not below it
    check(float(np.dot(unit(c01), unit(matt))) >= 0.85, "test setup: c01 must be >= 0.85 to Matt")
    check(float(np.dot(unit(c03), unit(matt))) < 0.85 and float(np.dot(unit(c03), unit(mike))) < 0.85,
          "test setup: stranger must be < 0.85 to all known voices")


def test_ref_skip_no_double_naming():
    rng = np.random.default_rng(2)
    matt = voice(rng)
    c00 = near(rng, matt, 0.023)  # Matt main   (registry's best match)
    c01 = near(rng, matt, 0.05)   # a second Matt-ish cluster
    cluster_emb = {"SPEAKER_00": c00, "SPEAKER_01": c01}
    registry = [{"name": "Matt", "embedding": matt.tolist()}]
    # A --ref 'Matt' clip whose CLOSEST cluster is 01, not 00 (distinct from both
    # cluster vectors, so it is the pass-2 skip -- not the absorb pass -- under test).
    # Pre-fix, the unrestricted --ref pass would name 01 'Matt' too -> a duplicate.
    ref_clip = unit(c01 + 0.03 * rng.standard_normal(len(c01)))
    ref_voices = [("Matt", ref_clip)]
    check(float(np.dot(ref_clip, unit(c01))) > float(np.dot(ref_clip, unit(c00))),
          "test setup: ref clip must be closest to cluster 01")

    names = {sp: sp for sp in cluster_emb}
    # absorb disabled (0.999) so this isolates the pass-2 registry-skip.
    d.name_clusters(cluster_emb, 0.40, 0.999, registry, ref_voices, names)

    check(names["SPEAKER_00"] == "Matt", f"00 should be Matt (registry), got {names['SPEAKER_00']}")
    check(names["SPEAKER_01"] == "SPEAKER_01",
          f"01 must stay unnamed: ref Matt already named by registry, so it is skipped "
          f"instead of naming a second cluster Matt, got {names['SPEAKER_01']}")


def test_reapply_does_not_annex_already_named_voice():
    """relabel --auto over a partly-named sidecar: a voice already placed on a
    cluster must NOT claim a second, low-confidence leftover via the 0.40 registry
    pass. Only the strict (0.85) absorb pass may extend it."""
    rng = np.random.default_rng(5)
    matt = voice(rng)
    stranger = voice(rng)
    c00 = near(rng, matt, 0.023)               # Matt's real cluster (already named)
    c01 = unit(0.6 * matt + 0.8 * stranger)    # ~0.6 to Matt: above 0.40, below 0.85
    cluster_emb = {"SPEAKER_00": c00, "SPEAKER_01": c01}
    registry = [{"name": "Matt", "embedding": matt.tolist()}]
    s01 = float(np.dot(unit(c01), unit(matt)))
    check(0.40 <= s01 < 0.85, f"test setup: c01 sim to Matt must be in (0.40, 0.85): {s01:.3f}")

    names = {"SPEAKER_00": "Matt", "SPEAKER_01": "SPEAKER_01"}  # 00 already named (prior run)
    d.name_clusters(cluster_emb, 0.40, 0.85, registry, [], names)

    check(names["SPEAKER_00"] == "Matt", f"00 must stay Matt, got {names['SPEAKER_00']}")
    check(names["SPEAKER_01"] == "SPEAKER_01",
          f"01 (~0.6 to Matt, who is already placed) must stay UNIDENTIFIED, got {names['SPEAKER_01']}")


def test_cap_guard_resolves_saturation():
    rng = np.random.default_rng(3)
    cap = 5
    # 3 true speakers, but noisy within-speaker sim (~0.4) so a farthest-first pass
    # at thresh 0.5 opens a new center for almost every turn and saturates at `cap`.
    bases = [voice(rng) for _ in range(3)]
    X = []
    for b in bases:
        for _ in range(60):
            X.append(near(rng, b, 0.08))   # within-speaker cosine straddles the ladder
    X = np.array(X, dtype=np.float32)
    rng.shuffle(X)

    sat = d._farthest_first_k(X, 0.5, cap)
    check(sat == cap, f"test setup: farthest-first at 0.5 must saturate at cap {cap}, got {sat}")
    k = d.estimate_k(X, thresh=0.5, cap=cap)
    check(k < cap, f"cap guard must re-estimate below cap {cap}, got {k}")

    # Direction sanity: a LOWER merge threshold yields <= clusters (never more).
    ks = [d._farthest_first_k(X, t, 100) for t in (0.25, 0.35, 0.45, 0.55)]
    check(all(ks[i] <= ks[i + 1] for i in range(len(ks) - 1)),
          f"lower threshold must not yield more clusters: {ks}")


def test_cap_guard_unresolvable_returns_cap():
    rng = np.random.default_rng(4)
    cap = 6
    # Many mutually-distinct turns: no threshold in the escalation ladder can
    # collapse below the cap, so estimate_k must return the cap (and WARN).
    X = np.array([voice(rng) for _ in range(40)], dtype=np.float32)
    k = d.estimate_k(X, thresh=0.5, cap=cap)
    check(k == cap, f"unresolvable saturation must return cap {cap}, got {k}")


def test_render_merges_cards_by_name():
    names = {"SPEAKER_00": "Matt", "SPEAKER_01": "Matt", "SPEAKER_02": "SPEAKER_02"}
    speakers = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]
    segs = [
        {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00"},
        {"start": 10.0, "end": 14.0, "speaker": "SPEAKER_00"},
        {"start": 20.0, "end": 24.0, "speaker": "SPEAKER_01"},
        {"start": 30.0, "end": 33.0, "speaker": "SPEAKER_02"},
    ]
    turns = [
        ("SPEAKER_00", 0.0, ["let us kick the meeting off now"]),
        ("SPEAKER_00", 10.0, ["another substantive point from the same voice"]),
        ("SPEAKER_01", 20.0, ["a phantom split of the very same speaker here"]),
        ("SPEAKER_02", 30.0, ["a genuinely different unknown participant speaking"]),
    ]
    with tempfile.TemporaryDirectory() as td:
        d.render_outputs(Path(td), "meeting", segs, speakers, names, turns, 3, "test")
        cards = (Path(td) / "meeting.speaker-cards.txt").read_text()

    matt_headers = [ln for ln in cards.splitlines() if ln.startswith("Matt")]
    check(len(matt_headers) == 1, f"exactly one Matt card expected, got {matt_headers}")
    check("Matt  (SPEAKER_00, SPEAKER_01)" in cards, f"Matt card must name its merged clusters:\n{cards}")
    # 3 turns folded (2 from 00 + 1 from 01), talk time 4+4+4 = 12s
    check("3 turns" in matt_headers[0], f"Matt card must sum turns to 3: {matt_headers[0]}")
    check("00:00:12 talk time" in matt_headers[0], f"Matt card must sum talk to 12s: {matt_headers[0]}")
    check("SPEAKER_02  (UNIDENTIFIED)" in cards, f"unknown cluster must stay one UNIDENTIFIED card:\n{cards}")
    check("# 2 speaker(s)" in cards, f"header must count 2 merged speakers:\n{cards.splitlines()[1]}")


def main():
    test_absorb_pass()
    test_ref_skip_no_double_naming()
    test_reapply_does_not_annex_already_named_voice()
    test_cap_guard_resolves_saturation()
    test_cap_guard_unresolvable_returns_cap()
    test_render_merges_cards_by_name()
    print(f"PASS: {CHECKS} assertions")
    sys.exit(0)


if __name__ == "__main__":
    main()
