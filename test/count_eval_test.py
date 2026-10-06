#!/usr/bin/env python3
"""Self-test for the issue-#59 embedding dump and offline count-eval harness.

Synthetic fixtures only (no models, no audio, no real voiceprints): the dump is
produced through the real cluster_segments call site with
WHOSAID_DUMP_EMBEDDINGS set, then test/count_eval/run_count_eval.py runs end to
end as a subprocess on those files.

Run:
    uv run --with numpy python test/count_eval_test.py
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent / "lib"))
sys.path.insert(0, str(TEST_DIR))

import diarize_sherpa as d  # noqa: E402
from estimate_k_test import turns  # noqa: E402
from max_speakers_hint_test import saturating_fixture  # noqa: E402

HARNESS = TEST_DIR / "count_eval" / "run_count_eval.py"
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    assert cond, msg
    CHECKS += 1


def segments(X, durations):
    out, t = [], 0.0
    for v, dur in zip(X, durations):
        out.append({"start": t, "end": t + float(dur), "emb": [float(x) for x in v]})
        t += 16.0
    return out


def run_harness(*args):
    return subprocess.run([sys.executable, str(HARNESS), *map(str, args)],
                          capture_output=True, text=True)


def main() -> None:
    six = turns(6, 25, seed=3)
    sat_X, sat_dur = saturating_fixture()
    cases = {
        "six-clean": (six, np.full(len(six), 15.0), 6),
        "eight-saturated": (sat_X, sat_dur, 8),
    }
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "dumps"
        saved = os.environ.pop("WHOSAID_DUMP_EMBEDDINGS", None)
        try:
            # Off by default: no env var, no file, and the result is the control.
            off = d.cluster_segments(segments(*cases["six-clean"][:2]), -1, dump_base="six-clean")
            check(not data.exists(), "no dump without WHOSAID_DUMP_EMBEDDINGS")
            os.environ["WHOSAID_DUMP_EMBEDDINGS"] = str(data)
            for base, (X, dur, _true) in cases.items():
                result = d.cluster_segments(segments(X, dur), -1, dump_base=base)
                if base == "six-clean":
                    check(result[0] == off[0] and result[2] == off[2] and result[3] == off[3],
                          "dumping must not change the clustering or the estimate")
            # The anchored path dumps the RESIDUAL the estimator saw, suffixed.
            anchor = ("Host", np.asarray(six[:25].sum(axis=0)))
            d.cluster_segments(segments(*cases["six-clean"][:2]), -1,
                               anchors=[anchor], dump_base="anchored")
            # An explicit count never runs the estimator, so nothing is dumped.
            d.cluster_segments(segments(*cases["six-clean"][:2]), 6, dump_base="explicit")
        finally:
            if saved is None:
                os.environ.pop("WHOSAID_DUMP_EMBEDDINGS", None)
            else:
                os.environ["WHOSAID_DUMP_EMBEDDINGS"] = saved

        names = sorted(p.name for p in data.glob("*.npz"))
        check(names == ["anchored-residual.npz", "eight-saturated.npz", "six-clean.npz"],
              f"dump files: {names}")
        with np.load(data / "eight-saturated.npz") as z:
            check(set(z.files) >= {"X", "durations", "raw_k", "threshold"}, f"npz keys: {z.files}")
            check(z["X"].shape == sat_X.shape and np.allclose(z["X"], sat_X, atol=1e-6),
                  "X round-trips")
            check(np.allclose(z["durations"], sat_dur), "durations round-trip")
            check(int(z["raw_k"]) == d.estimate_speakers(sat_X)["raw_k"], "raw_k recorded")
            check(abs(float(z["threshold"]) - d.AGGLOM_THRESHOLD) < 1e-9, "threshold recorded")
        with np.load(data / "anchored-residual.npz") as z:
            check(len(z["X"]) == len(six) - 25, f"residual only: {len(z['X'])}")

        labels = {base: true for base, (_X, _d, true) in cases.items()}
        labels["not-dumped"] = 3
        (data / "labels.json").write_text(json.dumps(labels))

        run = run_harness(data)
        check(run.returncode == 0, f"harness exit {run.returncode}: {run.stderr}")
        out = run.stdout
        check(out.splitlines()[0].split() == ["base", "true", "raw_k", "k", "saturated",
                                              "fallback_k", "suggested_max", "error"],
              f"table header: {out.splitlines()[0]}")
        six_row = next(line.split() for line in out.splitlines() if line.startswith("six-clean"))
        check(six_row[1] == "6" and six_row[3] == "6" and six_row[4] == "False"
              and six_row[7] == "0", f"six-clean row: {six_row}")
        sat_row = next(line.split() for line in out.splitlines()
                       if line.startswith("eight-saturated"))
        check(sat_row[3] == "20" and sat_row[4] == "True" and sat_row[5] == "-"
              and sat_row[6] == "8" and sat_row[7] == "12", f"eight-saturated row: {sat_row}")
        check("recordings: 2  MAE: 6.00  saturation rate: 50%  suggested_max MAE: 0.00" in out,
              f"aggregate line: {out}")
        check("labeled but no .npz: not-dumped" in out, "missing labels reported")
        check(".npz without a label (skipped): anchored-residual" in out, "unlabeled reported")

        js = run_harness(data, "--json")
        result = json.loads(js.stdout)
        check(result["mae"] == 6.0 and result["saturation_rate"] == 0.5, f"json: {result}")

        # --threshold is a real second design point: the cut reaches the estimator.
        lo = json.loads(run_harness(data, "--json", "--threshold", "0.30").stdout)
        check(all(r["threshold"] == 0.3 for r in lo["rows"]), f"threshold applied: {lo['rows']}")
        check([r["raw_k"] for r in lo["rows"]] != [r["raw_k"] for r in result["rows"]],
              f"a different cut must change some raw_k: {lo['rows']} vs {result['rows']}")

        # --use-bounds replays a recorded cap; default ignores it.
        np.savez(data / "bounded.npz", X=sat_X, durations=sat_dur, raw_k=np.int64(0),
                 threshold=np.float64(d.AGGLOM_THRESHOLD), cap=np.int64(20),
                 min_speakers=np.int64(0), max_speakers=np.int64(8))
        (data / "labels.json").write_text(json.dumps({"bounded": 8}))
        free = json.loads(run_harness(data, "--json").stdout)["rows"][0]
        bound = json.loads(run_harness(data, "--json", "--use-bounds").stdout)["rows"][0]
        check(free["k"] == 20 and free["suggested_max"] == 8, f"auto run: {free}")
        check(bound["k"] == 8 and bound["suggested_max"] is None, f"bounded run: {bound}")

        (data / "labels.json").unlink()
        check(run_harness(data).returncode == 2, "no labels -> exit 2")
        (data / "labels.json").write_text(json.dumps({"bounded": "eight"}))
        check(run_harness(data).returncode == 2, "non-int label -> exit 2")

    print(f"PASS: {CHECKS} assertions")


if __name__ == "__main__":
    main()
