#!/usr/bin/env python3
"""Offline unit tests for test/diarize_eval/score.py (no audio, stdlib only).

Run:
    python3 test/diarize_eval_test.py
"""

import itertools
import random
import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "test" / "diarize_eval"))

import run  # noqa: E402
import score as sc  # noqa: E402


def T(*turns):
    """truth dict from (speaker, start, end[, text]) tuples."""
    return {"turns": [
        {"speaker": t[0], "start": t[1], "end": t[2], "text": t[3] if len(t) > 3 else ""}
        for t in turns]}


def H(*segs):
    return [{"start": s, "end": e, "speaker": lab} for lab, s, e in segs]


# Three non-overlapping 10 s speakers, 30 s of truth speech.
TRUTH3 = T(("Alice", 0, 10), ("Bob", 10, 20), ("Carol", 20, 30))
PERFECT3 = H(("SPEAKER_00", 0, 10), ("SPEAKER_01", 10, 20), ("SPEAKER_02", 20, 30))


class Hungarian(unittest.TestCase):
    def test_3x4_known_optimum(self):
        cost = [[9, 2, 7, 8], [6, 4, 3, 7], [5, 8, 1, 8]]
        pairs = sc.hungarian(cost)
        # optimum 9: row0->col1 (2) + row1->col0 (6) + row2->col2 (1)
        self.assertEqual(pairs, [(0, 1), (1, 0), (2, 2)])
        self.assertEqual(sum(cost[i][j] for i, j in pairs), 9)

    def test_tall_matrix(self):
        cost = [[4, 1], [2, 0], [3, 5], [1, 9]]
        pairs = sc.hungarian(cost)
        self.assertEqual(len(pairs), 2)
        best = min(cost[a][0] + cost[b][1] for a, b in itertools.permutations(range(4), 2))
        self.assertEqual(sum(cost[i][j] for i, j in pairs), best)

    def test_matches_brute_force(self):
        rng = random.Random(3)
        for _ in range(40):
            n, m = rng.randint(1, 5), rng.randint(1, 5)
            cost = [[rng.randint(0, 20) for _ in range(m)] for _ in range(n)]
            k = min(n, m)
            if n <= m:
                best = min(sum(cost[i][c[i]] for i in range(n))
                           for c in itertools.permutations(range(m), n))
            else:
                best = min(sum(cost[r[j]][j] for j in range(m))
                           for r in itertools.permutations(range(n), m))
            pairs = sc.hungarian(cost)
            self.assertEqual(len(pairs), k)
            self.assertEqual(len({i for i, _ in pairs}), k)
            self.assertEqual(len({j for _, j in pairs}), k)
            self.assertEqual(sum(cost[i][j] for i, j in pairs), best)

    def test_max_weight(self):
        w = [[5, 1, 0], [4, 4, 0]]
        pairs = sc.hungarian_max(w)
        self.assertEqual(sum(w[i][j] for i, j in pairs), 9)  # 5+4, not 4+1


class Blind(unittest.TestCase):
    def test_perfect(self):
        r = sc.score_meeting(TRUTH3, PERFECT3, "blind")
        self.assertEqual((r["n_true"], r["n_hyp"], r["n_hyp_raw"]), (3, 3, 3))
        self.assertTrue(r["count_ok"])
        self.assertEqual(r["count_delta"], 0)
        self.assertEqual((r["der"], r["miss"], r["false_alarm"], r["confusion"]), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(r["turn_acc"], 1.0)
        self.assertEqual(r["n_turns"], 3)
        self.assertEqual(r["truth_time"], 30.0)
        self.assertEqual(r["mapping"], {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob", "SPEAKER_02": "Carol"})
        self.assertIsNone(r["named_correct_time"])
        self.assertEqual(r["wrong_name_labels"], [])
        self.assertIsNone(r["wer"])

    def test_swapped_anonymous_labels_still_perfect(self):
        hyp = H(("SPEAKER_02", 0, 10), ("SPEAKER_00", 10, 20), ("SPEAKER_01", 20, 30))
        r = sc.score_meeting(TRUTH3, hyp, "blind")
        self.assertEqual(r["der"], 0.0)
        self.assertEqual(r["turn_acc"], 1.0)
        self.assertEqual(r["mapping"]["SPEAKER_02"], "Alice")

    def test_one_speaker_split_in_two(self):
        hyp = H(("SPEAKER_00", 0, 6), ("SPEAKER_03", 6, 10),
                ("SPEAKER_01", 10, 20), ("SPEAKER_02", 20, 30))
        r = sc.score_meeting(TRUTH3, hyp, "blind")
        self.assertEqual(r["n_hyp"], 4)
        self.assertEqual(r["count_delta"], 1)
        self.assertFalse(r["count_ok"])
        self.assertEqual(r["mapping"]["SPEAKER_00"], "Alice")
        self.assertIsNone(r["mapping"]["SPEAKER_03"])
        # 4 s of Alice labelled with the unmapped cluster: 4/30
        self.assertEqual(r["confusion"], 0.1333)
        self.assertEqual(r["der"], 0.1333)
        self.assertEqual(r["miss"], 0.0)
        self.assertEqual(r["turn_acc"], 1.0)  # majority cluster is right

    def test_two_speakers_merged(self):
        hyp = H(("SPEAKER_00", 0, 10), ("SPEAKER_01", 10, 30))
        r = sc.score_meeting(TRUTH3, hyp, "blind")
        self.assertEqual(r["n_hyp"], 2)
        self.assertEqual(r["count_delta"], -1)
        # merged cluster maps to Bob or Carol; the other 10 s is confusion
        self.assertEqual(r["confusion"], 0.3333)
        self.assertEqual(r["der"], 0.3333)
        self.assertEqual(r["turn_acc"], 0.6667)

    def test_stray_cluster_not_counted_as_speaker(self):
        hyp = PERFECT3 + H(("SPEAKER_09", 31.0, 31.2))
        r = sc.score_meeting(TRUTH3, hyp, "blind")
        self.assertEqual(r["n_hyp"], 3)
        self.assertEqual(r["n_hyp_raw"], 4)
        self.assertTrue(r["count_ok"])
        # 0.2 s of hyp speech with no truth speech: 0.2 / 30
        self.assertEqual(r["false_alarm"], 0.0067)
        self.assertEqual(r["der"], 0.0067)
        self.assertIsNone(r["mapping"]["SPEAKER_09"])

    def test_uncounted_label_leaves_headcount_keeps_der(self):
        # #69: a 0.8 s nameless cluster listed in the sidecar's `uncounted`
        hyp = PERFECT3 + H(("SPEAKER_09", 31.0, 31.8))
        r = sc.score_meeting(TRUTH3, hyp, "blind")
        self.assertEqual((r["n_hyp"], r["count_ok"]), (4, False), "without the key: unchanged")
        r2 = sc.score_meeting(TRUTH3, hyp, "blind", uncounted=["SPEAKER_09"])
        self.assertEqual((r2["n_hyp"], r2["n_hyp_raw"], r2["count_ok"]), (3, 4, True))
        self.assertEqual(r2["der"], r["der"], "DER keeps the uncounted label's time")
        self.assertIsNone(r2["mapping"]["SPEAKER_09"])
        self.assertEqual(sc.score_meeting(TRUTH3, hyp, "blind", uncounted=[])["n_hyp"], 4)

    def test_miss_when_hyp_silent(self):
        hyp = H(("SPEAKER_00", 0, 10), ("SPEAKER_01", 10, 20), ("SPEAKER_02", 20, 25))
        r = sc.score_meeting(TRUTH3, hyp, "blind")
        self.assertEqual(r["miss"], 0.1667)  # 5 / 30
        self.assertEqual(r["der"], 0.1667)
        self.assertEqual(r["turn_acc"], 1.0)

    def test_turn_with_no_hyp_overlap_is_wrong(self):
        hyp = H(("SPEAKER_00", 0, 10), ("SPEAKER_01", 10, 20))
        r = sc.score_meeting(TRUTH3, hyp, "blind")
        self.assertEqual(r["turn_acc"], 0.6667)

    def test_overlapped_truth(self):
        truth = T(("Alice", 0, 10), ("Bob", 8, 18))  # 2 s overlap; sum n_ref = 20 s
        r = sc.score_meeting(truth, H(("A", 0, 10), ("B", 8, 18)), "blind")
        self.assertEqual(r["truth_time"], 20.0)
        self.assertEqual(r["der"], 0.0)
        # hyp drops B's first 2 s (the overlap): n_ref 2, n_hyp 1 -> 2 s miss / 20 s
        r = sc.score_meeting(truth, H(("A", 0, 10), ("B", 10, 18)), "blind")
        self.assertEqual(r["miss"], 0.1)
        self.assertEqual(r["der"], 0.1)
        self.assertEqual(r["false_alarm"], 0.0)
        self.assertEqual(r["confusion"], 0.0)
        self.assertEqual(r["turn_acc"], 1.0)

    def test_overlap_confusion_and_false_alarm(self):
        # one hyp speaker over a two-speaker overlap, mapped to Alice
        truth = T(("Alice", 0, 10), ("Bob", 8, 10))
        r = sc.score_meeting(truth, H(("A", 0, 10)), "blind")
        # frames 8-10: n_ref 2, n_hyp 1, correct 1 -> miss 2 s; denominator 12 s
        self.assertEqual(r["miss"], 0.1667)
        # extra hyp speaker over a single-speaker region -> false alarm
        truth = T(("Alice", 0, 10))
        r = sc.score_meeting(truth, H(("A", 0, 10), ("B", 0, 4)), "blind")
        self.assertEqual(r["false_alarm"], 0.4)
        self.assertEqual(r["der"], 0.4)

    def test_short_turns(self):
        truth = T(("Alice", 0, 1.0), ("Bob", 2, 3.0), ("Alice", 4, 8))
        hyp = H(("X", 0, 1.0), ("Y", 2, 3.0), ("X", 4, 8))
        r = sc.score_meeting(truth, hyp, "blind")
        self.assertEqual((r["n_turns"], r["n_short"]), (3, 2))
        self.assertEqual(r["turn_acc_short"], 1.0)
        r = sc.score_meeting(T(("Alice", 0, 5)), H(("X", 0, 5)), "blind")
        self.assertIsNone(r["turn_acc_short"])

    def test_wer_wired_into_score_meeting(self):
        truth = T(("Alice", 0, 5, "The quick brown fox."))
        r = sc.score_meeting(truth, H(("X", 0, 5)), "blind", hyp_text="the quick brown dog jumped")
        self.assertEqual(r["wer"], 0.5)


class Named(unittest.TestCase):
    TRUTH = T(("Alice", 0, 10), ("Bob", 10, 20))

    def test_correct_names(self):
        r = sc.score_meeting(self.TRUTH, H(("Alice", 0, 10), ("Bob", 10, 20)), "named",
                             enrolled=["Alice", "Bob"])
        self.assertEqual(r["der"], 0.0)
        self.assertEqual(r["turn_acc"], 1.0)
        self.assertEqual(r["named_correct_time"], 1.0)
        self.assertEqual(r["named_wrong_time"], 0.0)
        self.assertEqual(r["wrong_name_labels"], [])

    def test_swapped_names_are_wrong(self):
        r = sc.score_meeting(self.TRUTH, H(("Bob", 0, 10), ("Alice", 10, 20)), "named",
                             enrolled=["Alice", "Bob"])
        self.assertEqual(r["der"], 1.0)
        self.assertEqual(r["confusion"], 1.0)
        self.assertEqual(r["turn_acc"], 0.0)
        self.assertEqual(r["named_correct_time"], 0.0)
        self.assertEqual(r["named_wrong_time"], 1.0)
        # both are real speakers and enrolled: not "unknown names"
        self.assertEqual(r["wrong_name_labels"], [])

    def test_unenrolled_name(self):
        r = sc.score_meeting(self.TRUTH, H(("Alice", 0, 10), ("Bob", 10, 20)), "named",
                             enrolled=["Alice"])
        self.assertEqual(r["wrong_name_labels"], ["Bob"])
        self.assertEqual(r["der"], 0.0)
        self.assertEqual(r["named_correct_time"], 1.0)

    def test_name_of_nonspeaker(self):
        r = sc.score_meeting(self.TRUTH, H(("Alice", 0, 10), ("Zed", 10, 20)), "named",
                             enrolled=["Alice"])
        self.assertEqual(r["wrong_name_labels"], ["Zed"])
        self.assertEqual(r["mapping"]["Zed"], "Bob")  # anonymous remainder assignment
        self.assertEqual(r["der"], 0.0)
        self.assertEqual(r["named_correct_time"], 0.5)
        self.assertEqual(r["named_wrong_time"], 0.5)
        self.assertEqual(r["turn_acc"], 1.0)

    def test_mixed_name_and_anonymous(self):
        r = sc.score_meeting(self.TRUTH, H(("Alice", 0, 10), ("SPEAKER_00", 10, 20)), "named")
        self.assertEqual(r["der"], 0.0)
        self.assertEqual(r["mapping"]["SPEAKER_00"], "Bob")
        self.assertEqual(r["named_correct_time"], 0.5)
        self.assertEqual(r["named_wrong_time"], 0.0)
        self.assertEqual(r["wrong_name_labels"], [])


class Wer(unittest.TestCase):
    def test_exact_values(self):
        self.assertEqual(sc.word_error_rate("the quick brown fox", "The quick, brown fox!"), 0.0)
        # 1 substitution + 1 insertion over 4 reference words
        self.assertEqual(sc.word_error_rate("the quick brown fox", "the quick brown dog jumped"), 0.5)
        # 2 deletions over 4
        self.assertEqual(sc.word_error_rate("a b c d", "a d"), 0.5)
        # digits kept, apostrophes dropped on both sides
        self.assertEqual(sc.word_error_rate("call 911 now", "Call 911 now."), 0.0)
        self.assertEqual(sc.word_error_rate("don't stop", "dont stop"), 0.0)
        # all wrong, hyp empty
        self.assertEqual(sc.word_error_rate("one two", ""), 1.0)
        self.assertIsNone(sc.word_error_rate("", "anything"))


class Aggregate(unittest.TestCase):
    def test_micro_average(self):
        # Meeting A: 30 s truth, 3 speakers; hyp gives Bob 10-26, Carol only 26-30.
        #   Carol 20-26 (6 s) is confusion -> der 6/30 = 0.2; turns correct 2/3;
        #   n_hyp 3 -> count_ok, delta 0.
        a = sc.score_meeting(TRUTH3, H(("S0", 0, 10), ("S1", 10, 26), ("S2", 26, 30)), "blind")
        # Meeting B: 10 s truth, 2 speakers; hyp is one merged cluster.
        #   5 s confusion -> der 0.5; turns correct 1/2; n_hyp 1 -> delta -1.
        b = sc.score_meeting(T(("Alice", 0, 5), ("Bob", 5, 10)), H(("S0", 0, 10)), "blind")
        self.assertEqual((a["der"], b["der"]), (0.2, 0.5))
        self.assertEqual((a["turn_acc"], b["turn_acc"]), (0.6667, 0.5))
        a["wer"], b["wer"] = 0.2, 0.4
        g = sc.aggregate([a, b])
        # micro: (6 + 5) / (30 + 10) = 0.275 (macro mean would be 0.35)
        self.assertEqual(g["der"], 0.275)
        self.assertEqual(g["confusion"], 0.275)
        self.assertEqual(g["miss"], 0.0)
        self.assertEqual(g["false_alarm"], 0.0)
        # turns: (2 + 1) / (3 + 2) = 0.6 (macro would be 0.5833)
        self.assertEqual(g["turn_acc"], 0.6)
        self.assertEqual(g["n_turns"], 5)
        self.assertEqual(g["count_accuracy"], 0.5)
        self.assertEqual(g["count_delta_hist"], {"0": 1, "-1": 1})
        self.assertEqual(g["wer"], 0.3)
        self.assertEqual(g["n_meetings"], 2)
        self.assertIsNone(g["named_correct_time"])  # blind meetings only

    def test_wer_mean_skips_missing_and_named_weighting(self):
        truth = T(("Alice", 0, 10), ("Bob", 10, 20))
        a = sc.score_meeting(truth, H(("Alice", 0, 10), ("Bob", 10, 20)), "named")
        b = sc.score_meeting(T(("Alice", 0, 30)), H(("Zed", 0, 30)), "named")
        g = sc.aggregate([a, b])
        # named_correct: (1.0 * 20 + 0.0 * 30) / 50 = 0.4
        self.assertEqual(g["named_correct_time"], 0.4)
        self.assertEqual(g["named_wrong_time"], 0.6)
        self.assertIsNone(g["wer"])


class VoiceSimilarity(unittest.TestCase):
    SIM = {"ids": ["a", "b", "c"], "sim": [[1, .2, .7], [.2, 1, .4], [.7, .4, 1]]}

    def truth(self, *voices):
        return {"speakers": [{"name": v.upper(), "voice": v} for v in voices]}

    def test_max_pair(self):
        self.assertEqual(run.max_voice_sim(self.truth("a", "b"), self.SIM), .2)
        self.assertEqual(run.max_voice_sim(self.truth("a", "b", "c"), self.SIM), .7)
        self.assertIsNone(run.max_voice_sim(self.truth("a", "zz"), self.SIM), "unknown voice")
        self.assertIsNone(run.max_voice_sim(self.truth("a", "b"), None), "no voice_sim.json")

    def test_buckets_reach_summary_and_report(self):
        good = sc.score_meeting(TRUTH3, PERFECT3, "blind")
        bad = sc.score_meeting(TRUTH3, H(("S0", 0, 20), ("S1", 20, 30)), "blind")
        per = {"m1": {"blind": dict(good, tags=[], max_voice_sim=.3, meeting="m1")},
               "m2": {"blind": dict(bad, tags=[], max_voice_sim=.75, meeting="m2")},
               "m3": {"blind": dict(good, tags=[], max_voice_sim=None, meeting="m3")}}
        by_mode, _, by_sim = run.summarize(per)
        self.assertEqual(by_mode["blind"]["n_meetings"], 3)
        self.assertEqual(by_sim["blind"]["distinct"]["count_accuracy"], 1.0)
        self.assertEqual(by_sim["blind"]["similar"]["count_accuracy"], 0.0)
        self.assertEqual(by_sim["blind"]["similar"]["n_meetings"], 1)
        text = run.render({"run": "r", "date": "d", "by_mode": by_mode, "by_sim": by_sim, "per_meeting": per})
        self.assertIn("| blind | similar | 1 | 0.0% |", text)
        self.assertIn("named wrong |", text)
        # A results file from before the split still renders.
        self.assertNotIn("closest voice pair", run.render({"run": "r", "date": "d", "by_mode": by_mode,
                                                          "per_meeting": per}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
