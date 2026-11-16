#!/usr/bin/env python3
"""
Boundary backchannel attribution (GitHub issue #216).

Whisper often glues a short reply from the NEXT speaker ("Exactly, right.") onto
the end of the previous speaker's segment, and segment-level speaker assignment
then hands it to the wrong person. build_turns must split such a 1-3 word
backchannel at a turn boundary into its own turn, then either reassign it (the
diarization covers its words with the other voice, or its voice embedding is
clearly closer to the other speaker) or keep the speaker and flag the split turn
attribution_uncertain. The flag must reach search hits through the sidecar,
survive a relabel, and never touch the long turn it was split from.

All fixtures are synthetic: no audio, no models, fake voices.

Run:
    uv run --with numpy python test/backchannel_test.py
"""

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "lib"))
# never read or write the real voice registry
os.environ["WHOSAID_SPEAKER_DB"] = str(Path(tempfile.mkdtemp(prefix="whosaid-bc-reg-")) / "speakers.json")

import diarize_sherpa as d  # noqa: E402
import search  # noqa: E402

A, B = "SPEAKER_00", "SPEAKER_01"
NAMES = {A: "Alice_Example", B: "Bob_Example"}


def word(text, start, end):
    return {"word": " " + text, "start": start, "end": end, "probability": 0.9}


def whisper_fixture(tail="Exactly, right."):
    """Segment 1: Alice's line with Bob's reply glued on. Segment 2: Bob goes on."""
    body = ["So", "the", "blob", "store", "lib", "goes", "in", "the", "shared", "repo."]
    words, t = [], 0.0
    for w in body:
        words.append(word(w, t, t + 0.3))
        t += 0.35
    tail_words = tail.split()
    tail_start = t + 0.2
    for i, w in enumerate(tail_words):
        words.append(word(w, tail_start + i * 0.35, tail_start + i * 0.35 + 0.3))
    end = words[-1]["end"]
    seg1 = {"id": 0, "start": 0.0, "end": end, "text": " " + " ".join(body + tail_words), "words": words}
    seg2_words = [word(w, end + 0.5 + i * 0.35, end + 0.8 + i * 0.35)
                  for i, w in enumerate(["Then", "we", "ship", "it", "Friday."])]
    seg2 = {"id": 1, "start": seg2_words[0]["start"], "end": seg2_words[-1]["end"],
            "text": " Then we ship it Friday.", "words": seg2_words}
    return {"text": seg1["text"] + seg2["text"], "segments": [seg1, seg2], "language": "en"}, tail_start, end


def write_json(tmp: Path, data: dict) -> str:
    p = tmp / "transcript.json"
    p.write_text(json.dumps(data))
    return str(p)


def texts(turns):
    return [(sp, " ".join(t)) for sp, _start, t in turns]


def build(segs, wj, **kw):
    """build_turns plus its decisions; on a build without #216 the decisions are empty."""
    out: dict = {}
    try:
        turns = d.build_turns(segs, wj, decisions_out=out, **kw)
    except TypeError:  # no #216 parameters: the pre-fix behaviour
        turns = d.build_turns(segs, wj)
    return turns, out.get("backchannel", [])


class BoundaryBackchannel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="whosaid-bc-"))
        self.data, self.tail_start, self.seg1_end = whisper_fixture()
        self.wj = write_json(self.tmp, self.data)

    def segs_absorbed(self):
        """Diarization folded Bob's short reply into Alice's turn (the #216 report)."""
        return [{"start": 0.0, "end": self.seg1_end + 0.05, "speaker": A},
                {"start": self.seg1_end + 0.4, "end": self.seg1_end + 3.0, "speaker": B}]

    def segs_caught(self):
        """Diarization did catch Bob for the reply; whisper's segment still spans it."""
        return [{"start": 0.0, "end": self.tail_start - 0.05, "speaker": A},
                {"start": self.tail_start - 0.02, "end": self.seg1_end + 3.0, "speaker": B}]

    def test_reassigned_when_diarization_covers_the_reply(self):
        turns, decisions = build(self.segs_caught(), self.wj)
        self.assertEqual(texts(turns), [
            (A, "So the blob store lib goes in the shared repo."),
            (B, "Exactly, right. Then we ship it Friday."),
        ])
        self.assertEqual([x["action"] for x in decisions], ["reassigned"])
        self.assertEqual(decisions[0]["reason"], "diarization")

    def test_flagged_when_nothing_decides(self):
        turns, decisions = build(self.segs_absorbed(), self.wj)
        self.assertEqual(texts(turns), [
            (A, "So the blob store lib goes in the shared repo."),
            (A, "Exactly, right."),
            (B, "Then we ship it Friday."),
        ])
        self.assertEqual([(x["action"], x["from"], x["to"]) for x in decisions], [("flagged", A, B)])
        keys = d.uncertain_turns(turns, decisions, NAMES)
        self.assertEqual(keys, [{"t_str": d.hms(self.tail_start), "speaker": NAMES[A], "text": "Exactly, right.",
                                 "candidate": NAMES[B]}])

    def test_voice_vote_reassigns_or_keeps(self):
        calls = []

        def vote_other(start, end, current, other):
            calls.append((round(start, 2), round(end, 2), current, other))
            return other

        turns, decisions = build(self.segs_absorbed(), self.wj, voice_vote=vote_other)
        self.assertEqual(texts(turns)[-1], (B, "Exactly, right. Then we ship it Friday."))
        self.assertEqual((decisions[0]["action"], decisions[0]["reason"]), ("reassigned", "voice"))
        self.assertEqual(calls, [(round(self.tail_start, 2), round(self.seg1_end, 2), A, B)])

        turns, decisions = build(self.segs_absorbed(), self.wj, voice_vote=lambda s, e, cur, oth: cur)
        self.assertEqual(texts(turns)[0], (A, "So the blob store lib goes in the shared repo. Exactly, right."))
        self.assertEqual(decisions[0]["action"], "kept")
        self.assertEqual(d.uncertain_turns(turns, decisions, NAMES), [])

        turns, decisions = build(self.segs_absorbed(), self.wj, voice_vote=lambda *a: None)
        self.assertEqual(decisions[0]["action"], "flagged")

    def test_stored_decisions_replay_without_audio(self):
        """relabel re-renders from the sidecar with no audio: the voice decision must persist."""
        _turns, decisions = build(self.segs_absorbed(), self.wj, voice_vote=lambda s, e, cur, oth: oth)
        turns, replayed = build(self.segs_absorbed(), self.wj, stored=decisions)
        self.assertEqual(texts(turns)[-1], (B, "Exactly, right. Then we ship it Friday."))
        self.assertEqual(replayed, decisions)

    def test_not_a_boundary_is_untouched(self):
        """Same speaker on both sides: a trailing 'right' is just part of the line."""
        segs = [{"start": 0.0, "end": self.seg1_end + 3.0, "speaker": A}]
        turns, decisions = build(segs, self.wj)
        self.assertEqual(texts(turns), [(A, " ".join(self.data["segments"][0]["text"].split()
                                                   + self.data["segments"][1]["text"].split()))])
        self.assertEqual(decisions, [])

    def test_long_tail_is_not_a_backchannel(self):
        data, _ts, end = whisper_fixture(tail="Exactly, right, we agree.")
        wj = write_json(self.tmp, data)
        segs = [{"start": 0.0, "end": end + 0.05, "speaker": A}, {"start": end + 0.4, "end": end + 3, "speaker": B}]
        turns, decisions = build(segs, wj)
        self.assertEqual(decisions, [])
        self.assertEqual(len(turns), 2)

    def test_leading_backchannel_from_the_previous_speaker(self):
        """'Yeah.' from Alice glued onto the START of Bob's next line."""
        w1 = [word("I", 0.0, 0.2), word("will", 0.25, 0.5), word("send", 0.55, 0.8), word("it.", 0.85, 1.1)]
        w2 = [word("Yeah.", 1.6, 1.9), word("I", 2.3, 2.5), word("can", 2.55, 2.8), word("review", 2.85, 3.2),
              word("it", 3.25, 3.4), word("today.", 3.45, 3.9)]
        data = {"text": "", "segments": [
            {"id": 0, "start": 0.0, "end": 1.1, "text": " I will send it.", "words": w1},
            {"id": 1, "start": 1.6, "end": 3.9, "text": " Yeah. I can review it today.", "words": w2}]}
        wj = write_json(self.tmp, data)
        segs = [{"start": 0.0, "end": 1.2, "speaker": A}, {"start": 1.5, "end": 4.0, "speaker": B}]
        turns, decisions = build(segs, wj)
        self.assertEqual(texts(turns), [(A, "I will send it."), (B, "Yeah."), (B, "I can review it today.")])
        self.assertEqual([(x["action"], x["side"]) for x in decisions], [("flagged", "leading")])


class RelabelKeepsTheDecision(unittest.TestCase):
    """`whosaid relabel` re-renders from the sidecar: flags follow the new name, and a
    stored voice decision is replayed rather than lost."""

    def relabel(self, stored):
        tmp = Path(tempfile.mkdtemp(prefix="whosaid-bc-rl-"))
        data, _tail, end = whisper_fixture()
        wj = write_json(tmp, data)
        segs = [{"start": 0.0, "end": end + 0.05, "speaker": A}, {"start": end + 0.4, "end": end + 3, "speaker": B}]
        sidecar = {"base": "transcript", "emb_model": d.EMB_NAME, "names": {A: A, B: B}, "local_labels": {},
                   "segments": segs, "cluster_emb": {A: [1.0, 0.0], B: [0.0, 1.0]}, "whisper_json": wj,
                   "registry_matches": [], "detect_mode": "test"}
        if stored is not None:
            sidecar["backchannel"] = stored
        path = tmp / "transcript.diarization.json"
        path.write_text(json.dumps(sidecar))
        args = argparse.Namespace(
            relabel=str(path), outdir=str(tmp), save_speaker=[f"{A}=Carol_Example"], save_role=[],
            no_save=True, note=None, force=False, blend=False, auto=False, fold_unknown=False,
            no_registry=True, ref_threshold=0.5, absorb_threshold=0.85, placed_absorb_threshold=0.70,
            snippets=0, forget=[], ref=[])
        d.do_relabel(args)
        return json.loads(path.read_text()), (tmp / "transcript.speakers.txt").read_text()

    def test_old_sidecar_gets_flagged_under_the_new_name(self):
        data, text = self.relabel(stored=None)
        self.assertEqual([(u["speaker"], u["text"], u["candidate"]) for u in data["attribution_uncertain_turns"]],
                         [("Carol_Example", "Exactly, right.", B)])
        self.assertIn("] Carol_Example: Exactly, right.\n", text)
        self.assertIn("] Carol_Example: So the blob store lib goes in the shared repo.\n", text)

    def test_stored_voice_decision_is_replayed(self):
        _t, decisions = build([{"start": 0.0, "end": whisper_fixture()[2] + 0.05, "speaker": A},
                               {"start": whisper_fixture()[2] + 0.4, "end": whisper_fixture()[2] + 3, "speaker": B}],
                              write_json(Path(tempfile.mkdtemp()), whisper_fixture()[0]),
                              voice_vote=lambda s, e, cur, oth: oth)
        data, text = self.relabel(stored=decisions)
        self.assertEqual(data["attribution_uncertain_turns"], [])
        self.assertEqual(data["backchannel"][0]["action"], "reassigned")
        self.assertIn(f"] {B}: Exactly, right. Then we ship it Friday.\n", text)


class FlagReachesSearch(unittest.TestCase):
    """The done-when, end to end: transcript -> sidecar -> search index -> hit flag."""

    def test_search_hit_carries_the_flag(self):
        ws = Path(tempfile.mkdtemp(prefix="whosaid-bc-ws-"))
        (ws / "whosaid.toml").write_text('[search]\nembed = false\n')
        meeting = ws / "2026-01-05_sync"
        meeting.mkdir()
        data, _tail, end = whisper_fixture()
        wj = write_json(meeting, data)
        segs = [{"start": 0.0, "end": end + 0.05, "speaker": A}, {"start": end + 0.4, "end": end + 3, "speaker": B}]
        turns, decisions = build(segs, wj)
        d.render_outputs(meeting, "transcript", segs, [A, B], NAMES, turns, 0, "test")
        sidecar = {"base": "transcript", "names": NAMES, "segments": segs, "whisper_json": wj,
                   "source": {"kind": "audio"}}
        if hasattr(d, "uncertain_turns"):
            sidecar["backchannel"] = decisions
            sidecar["attribution_uncertain_turns"] = d.uncertain_turns(turns, decisions, NAMES)
        (meeting / "transcript.diarization.json").write_text(json.dumps(sidecar))

        search.build(ws, embed=False)
        _engine, hits, _fb = search.query(ws, "exactly", mode="exact")
        reply = [h for h in hits if "Exactly" in h.text]
        self.assertEqual([(h.speaker, h.text) for h in reply], [(NAMES[A], "Exactly, right.")])
        self.assertTrue(reply[0].attribution_uncertain, "the split-out reply must be flagged in search")
        _engine, hits, _fb = search.query(ws, "blob store", mode="exact")
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0].attribution_uncertain, "the long turn it was split from stays citable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
