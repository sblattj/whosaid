#!/usr/bin/env python3
"""Speaker count on REAL per-turn voiceprints from synthetic TTS meetings.
Run: uv run --with numpy python test/diarize_count_fixtures_test.py

The fixtures (test/diarize_eval/fixtures/*.json) are TitaNet-small embeddings that
diarize_window produced for meetings built by test/diarize_eval, each turn tagged
with its true speaker. Unlike estimate_k_test's synthetic vectors they carry the
real noise of short turns, which is what over-split the count.
"""
import json
import sys
from pathlib import Path
import numpy as np
LIB = Path(__file__).resolve().parent.parent / 'lib'
sys.path.insert(0, str(LIB))
import diarize_sherpa as d

FIX = Path(__file__).resolve().parent / 'diarize_eval' / 'fixtures'


def load(mid):
    doc = json.loads((FIX / f'{mid}.json').read_text())
    X = np.array([t['emb'] for t in doc['turns']], dtype=np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    dur = np.array([t['dur'] for t in doc['turns']])
    return doc, X, dur, [t['speaker'] for t in doc['turns']]


def turn_accuracy(labels, truth, dur):
    """Duration-weighted share of turns whose cluster's majority speaker is right."""
    good = 0.0
    for c in set(labels.tolist()):
        rows = np.flatnonzero(labels == c)
        talk = {}
        for i in rows:
            talk[truth[i]] = talk.get(truth[i], 0.0) + dur[i]
        good += max(talk.values())
    return good / dur.sum()


def test_counts():
    for mid in ('m2-007', 'm2-000', 'm12-001', 'm2-008'):
        doc, X, dur, truth = load(mid)
        est = d.estimate_speakers(X, durations=dur)
        assert est['k'] == doc['speakers'], (mid, est['k'], doc['speakers'], est.get('brief_fold'))
        labels = d.cluster_estimated(X, est, dur)
        acc = turn_accuracy(np.asarray(labels), truth, dur)
        # m12-001 is six similar voices in rapid turns: the count is exact but
        # k-means still swaps a few turns between two of them (0.87 measured).
        assert acc >= (0.85 if mid == 'm12-001' else 0.9), (mid, acc)


def test_short_fragments_fold():
    # 4 voices; the 0.58 cut alone returns 7, all extras made of sub-2 s turns.
    doc, X, dur, _ = load('m2-007')
    est = d.estimate_speakers(X, durations=dur)
    assert est['raw_k'] == 7 and est['brief_fold']['folded'] == 3
    assert est['fallback'] is None


def test_brief_distinct_guest_is_kept():
    # 4 voices, one of them a cameo; the fold must keep a short-only cluster
    # that is far from every substantive voice instead of annexing it.
    doc, X, dur, truth = load('m2-000')
    est = d.estimate_speakers(X, durations=dur)
    assert est['k'] == 4 and est['brief_fold']['kept_brief'] >= 1
    labels = est['labels']
    for c in set(labels.tolist()):
        rows = np.flatnonzero(labels == c)
        if np.all(dur[rows] < d.SUBSTANTIVE_TURN_SECONDS):
            assert len({truth[i] for i in rows}) == 1, 'a kept brief cluster is one person'


def test_plateau_needs_excluded_turns():
    # 6 similar voices in 15 turns, all >= 1 s: the plateau "recovery" used to
    # re-cut the same turns at looser thresholds and merge six voices into two.
    _, X, dur, _ = load('m12-001')
    assert np.all(dur >= 1.0)
    est = d.estimate_speakers(X, durations=dur)
    assert est['k'] == 6 and est['fallback'] is None, est['fallback']


def test_control_without_durations():
    # No durations, no fold: the primary agglomerative count is unchanged.
    _, X, _, _ = load('m2-007')
    assert d.estimate_speakers(X)['k'] == 7


if __name__ == '__main__':
    d.log = lambda *a, **k: None
    test_counts()
    test_short_fragments_fold()
    test_brief_distinct_guest_is_kept()
    test_plateau_needs_excluded_turns()
    test_control_without_durations()
    print('PASS: 4 real-embedding meetings counted exactly, fold, brief guest, plateau guard, control')
