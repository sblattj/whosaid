#!/usr/bin/env python3
"""
Offline test for GitHub issue #69: an anonymous cluster (still SPEAKER_NN) with
under UNCOUNTED_MAX_TALK_SECONDS (1.0 s) of total talk is UNCOUNTED. It keeps its
label, words and RTTM time, but is left out of the headcount (`num_speakers`),
the speaker cards, the transcript's `# Speakers (N)` header and the workspace
attendee list, and the sidecar lists it in `uncounted`. Named clusters are never
uncounted; a cluster of exactly 1.0 s is counted.

Both paths that settle the final speaker set are covered:
  * transcribe: the in-process `uncounted_clusters` + `render_outputs` calls
    exactly as `main()` makes them (the models cannot run offline; the real
    transcribe path is exercised by the eval harness in the PR notes);
  * relabel: the real CLI (`--relabel SIDECAR --auto`) over a synthetic sidecar.
Synthetic 8-dim one-hot embeddings stand in for voiceprints. The registry
(WHOSAID_SPEAKER_DB) and voice refs (WHOSAID_VOICE_REFS) point at temp dirs.

Run:
    uv run --with numpy --with mcp python test/uncounted_clusters_test.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DIARIZE = REPO_DIR / "lib" / "diarize_sherpa.py"
EMB_MODEL = "nemo_en_titanet_small.onnx"
DIM = 8


def onehot(i):
    v = [0.0] * DIM
    v[i] = 1.0
    return v


CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def env_for(tmp: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("WHOSAID_") and k != "DIARIZE_EMB_NAME"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["WHOSAID_SPEAKER_DB"] = str(tmp / "speakers.json")
    env["WHOSAID_VOICE_REFS"] = str(tmp / "voices")
    return env


def build(tmp: Path, tiny: tuple, registry: list | None = None) -> Path:
    """Two substantive anonymous clusters plus SPEAKER_02 with `tiny` = (start, end)."""
    segs = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 20.0},
            {"speaker": "SPEAKER_01", "start": 21.0, "end": 36.0},
            {"speaker": "SPEAKER_02", "start": tiny[0], "end": tiny[1]}]
    wj = tmp / "m.json"
    wj.write_text(json.dumps({"segments": [
        {"start": 0.0, "end": 20.0, "text": "alpha alpha alpha long talk"},
        {"start": 21.0, "end": 36.0, "text": "bravo bravo bravo long talk"},
        {"start": tiny[0], "end": tiny[1], "text": "tinyword yeah"}]}))
    sidecar = tmp / "m.diarization.json"
    sidecar.write_text(json.dumps({
        "base": "m", "whosaid_version": "1.12.0", "emb_model": EMB_MODEL,
        "num_speakers": 3,
        "names": {f"SPEAKER_0{i}": f"SPEAKER_0{i}" for i in range(3)},
        "local_labels": {}, "registry_matches": [],
        "source": {"path": "/x/m.m4a", "duration_seconds": 40.0},
        "detect_mode": "3 speakers", "count_warning": None, "count_estimate": None,
        "segments": segs,
        "cluster_emb": {f"SPEAKER_0{i}": onehot(i) for i in range(3)},
        "whisper_json": str(wj)}, indent=2))
    (tmp / "speakers.json").write_text(json.dumps({"speakers": [
        {"name": n, "model": EMB_MODEL, "embedding": v, "added": "seed"}
        for n, v in (registry or [])]}))
    return sidecar


def relabel_auto(sidecar: Path, tmp: Path, *extra: str) -> dict:
    r = subprocess.run([sys.executable, str(DIARIZE), "--relabel", str(sidecar),
                        "--outdir", str(tmp / "out"), "--auto", *extra],
                       capture_output=True, text=True, env=env_for(tmp))
    check(r.returncode == 0, f"relabel must exit 0, got {r.returncode}: {r.stderr[-400:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def read_out(tmp: Path) -> tuple:
    return ((tmp / "out" / "m.speakers.txt").read_text(),
            (tmp / "out" / "m.speaker-cards.txt").read_text(),
            (tmp / "out" / "m.rttm").read_text())


def test_tiny_anonymous_is_uncounted():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = build(tmp, (40.0, 40.6))
        out = relabel_auto(sidecar, tmp)
        check(out["num_speakers"] == 2, f"0.6 s anonymous cluster must not count, got {out}")
        check(out["uncounted"] == ["SPEAKER_02"], f"must be listed as uncounted, got {out}")
        data = json.loads(sidecar.read_text())
        check(data["uncounted"] == ["SPEAKER_02"] and data["num_speakers"] == 2,
              f"the sidecar must record uncounted and the counted headcount, got {data['uncounted']}")
        check("SPEAKER_02" in data["names"] and
              any(s["speaker"] == "SPEAKER_02" for s in data["segments"]),
              "the cluster and its segment must stay in the sidecar")
        txt, cards, rttm = read_out(tmp)
        check("# Speakers (2): SPEAKER_00, SPEAKER_01" in txt, f"header must count 2: {txt[:300]}")
        check("SPEAKER_02" not in cards, f"no card for an uncounted cluster: {cards}")
        check("# 2 speaker(s)" in cards, f"cards must announce 2 speakers: {cards[:200]}")
        check("] SPEAKER_02: tinyword yeah" in txt,
              f"its words must stay in the transcript, labeled SPEAKER_02: {txt}")
        check("SPEAKER_02" in rttm, "its time must stay in the RTTM")
        check("SPEAKER_02" in txt.split("\n\n")[0] and "Uncounted" in txt.split("\n\n")[0],
              "the transcript header must say which cluster is uncounted")
        sys.path.insert(0, str(REPO_DIR / "lib"))
        import workspace  # noqa: E402
        check(workspace.parse_speakers(txt) == ["SPEAKER_00", "SPEAKER_01"],
              f"an uncounted cluster is not an attendee, got {workspace.parse_speakers(txt)}")
        # a hand relabel still reaches it and then it counts
        r = subprocess.run([sys.executable, str(DIARIZE), "--relabel", str(sidecar),
                            "--outdir", str(tmp / "out"), "--no-save",
                            "--save-speaker", "SPEAKER_02=Guest"],
                           capture_output=True, text=True, env=env_for(tmp))
        check(r.returncode == 0, f"hand relabel failed: {r.stderr[-300:]}")
        got = json.loads(r.stdout.strip().splitlines()[-1])
        check(got["num_speakers"] == 3 and got["uncounted"] == [],
              f"a hand-named tiny cluster must count, got {got}")


def test_tiny_named_is_counted():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = build(tmp, (40.0, 40.6), registry=[("Zed", onehot(2))])
        out = relabel_auto(sidecar, tmp)
        check(out["clusters"]["SPEAKER_02"] == "Zed", f"registry must name SPEAKER_02: {out}")
        check(out["num_speakers"] == 3 and out["uncounted"] == [],
              f"a named cluster is never uncounted, got {out}")
        txt, cards, _ = read_out(tmp)
        check("Zed" in cards and "Uncounted" not in txt, "named tiny cluster keeps its card")


def test_exactly_one_second_is_counted():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = build(tmp, (40.3, 41.3))   # 41.3 - 40.3 = 0.9999999999999929 in floats
        out = relabel_auto(sidecar, tmp)
        check(out["num_speakers"] == 3 and out["uncounted"] == [],
              f"exactly 1.0 s total is counted, got {out}")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = build(tmp, (40.0, 40.999))
        out = relabel_auto(sidecar, tmp)
        check(out["uncounted"] == ["SPEAKER_02"], f"0.999 s is under the cut, got {out}")


def test_transcribe_and_relabel_agree():
    sys.path.insert(0, str(REPO_DIR / "lib"))
    os.environ["WHOSAID_SPEAKER_DB"] = "/nonexistent/never-read.json"
    import diarize_sherpa as d  # noqa: E402
    d.log = lambda *a, **k: None
    check(d.UNCOUNTED_MAX_TALK_SECONDS == 1.0, "the cut is one module constant, 1.0 s")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sidecar = build(tmp, (40.0, 40.6))
        data = json.loads(sidecar.read_text())
        segs, names = data["segments"], data["names"]
        speakers = sorted({s["speaker"] for s in segs})
        # exactly what main() does after naming/folding
        unc = d.uncounted_clusters(segs, names, speakers)
        num = len(speakers) - len(unc)
        tdir = tmp / "transcribe"
        tdir.mkdir()
        turns = d.build_turns(segs, data["whisper_json"])
        d.render_outputs(tdir, "m", segs, speakers, names, turns, 3, "t", uncounted=unc)
        out = relabel_auto(sidecar, tmp)
        check((num, unc) == (out["num_speakers"], out["uncounted"]),
              f"transcribe decided {(num, unc)}, relabel --auto decided "
              f"{(out['num_speakers'], out['uncounted'])}")
        t_txt = (tdir / "m.speakers.txt").read_text()
        r_txt, r_cards, _ = read_out(tmp)
        body = lambda s: [ln for ln in s.splitlines() if not ln.startswith("# Diarization")]  # noqa: E731
        check(body(t_txt) == body(r_txt), "the two paths must render the same transcript")
        t_cards = (tdir / "m.speaker-cards.txt").read_text()
        check("SPEAKER_02" not in t_cards and "# 2 speaker(s)" in t_cards,
              f"the transcribe-path cards must omit the uncounted cluster: {t_cards[:200]}")
        # nothing uncounted -> same transcript shape as before, no Uncounted line
        check(d.uncounted_clusters(segs, {**names, "SPEAKER_02": "Zed"}, speakers) == [],
              "a named cluster is never uncounted")
        # all-brief clip: never a zero headcount
        one = [{"speaker": "SPEAKER_00", "start": 0.0, "end": 0.5}]
        check(d.uncounted_clusters(one, {"SPEAKER_00": "SPEAKER_00"}) == [],
              "a clip with only brief clusters keeps them all counted")


def main() -> None:
    test_tiny_anonymous_is_uncounted()
    test_tiny_named_is_counted()
    test_exactly_one_second_is_counted()
    test_transcribe_and_relabel_agree()
    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
