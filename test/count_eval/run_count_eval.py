#!/usr/bin/env python3
"""Offline speaker-count eval (issue #59): re-run estimate_speakers on dumped inputs.

Inputs:
  DATA_DIR/<base>.npz   written by a real run with WHOSAID_DUMP_EMBEDDINGS=DATA_DIR
                        (keys: X, durations, raw_k, threshold, cap, min_speakers,
                        max_speakers; see dump_estimate_inputs in lib/diarize_sherpa.py)
  labels.json           {"<base>": <true headcount>, ...}; default DATA_DIR/labels.json

For every labeled base it prints true, raw_k, k, saturated, the fallback k,
suggested_max and the error (k - true), then the aggregate MAE and saturation
rate. --threshold re-runs at another cosine cut so a tuning pass (proposal 2)
can compare variants on the same data. The recorded min/max bounds are ignored
by default (this measures the AUTO count); --use-bounds replays them.

Voiceprints are private: keep DATA_DIR outside the repo or under the gitignored
test/count_eval/data/. Stdlib + numpy only.

Run:
    uv run --with numpy python test/count_eval/run_count_eval.py DATA_DIR [--threshold 0.52]
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

import diarize_sherpa as d  # noqa: E402


def load_case(path: Path) -> dict:
    with np.load(path) as z:
        case = {k: z[k] for k in z.files}
    for key in ("X", "durations"):
        if key not in case:
            raise ValueError(f"{path}: missing array '{key}'")
    return case


def evaluate(data_dir: Path, labels: dict, threshold=None, use_bounds: bool = False) -> dict:
    """{"rows": [...], "missing": [...], "unlabeled": [...], "mae", "saturation_rate", ...}."""
    rows, missing = [], []
    for base in sorted(labels):
        path = data_dir / f"{base}.npz"
        if not path.exists():
            missing.append(base)
            continue
        case = load_case(path)
        thresh = float(threshold) if threshold is not None else float(
            case.get("threshold", d.AGGLOM_THRESHOLD))
        kwargs = {"thresh": thresh, "durations": case["durations"]}
        if "cap" in case:
            kwargs["cap"] = int(case["cap"])
        if use_bounds:
            kwargs["min_speakers"] = int(case.get("min_speakers", 0))
            kwargs["max_speakers"] = int(case.get("max_speakers", 0))
        est = d.estimate_speakers(np.asarray(case["X"], dtype=np.float32), **kwargs)
        true = int(labels[base])
        rows.append({
            "base": base, "true": true, "turns": int(len(case["X"])),
            "threshold": round(thresh, 4), "raw_k": est["raw_k"], "k": est["k"],
            "saturated": est["saturated"],
            "fallback_k": (est["fallback"] or {}).get("k"),
            "suggested_max": est.get("suggested_max"),
            "error": est["k"] - true,
        })
    labeled = set(labels)
    unlabeled = sorted(p.stem for p in data_dir.glob("*.npz") if p.stem not in labeled)
    n = len(rows)
    suggested = [r for r in rows if r["suggested_max"] is not None]
    return {
        "rows": rows, "missing": missing, "unlabeled": unlabeled,
        "mae": (sum(abs(r["error"]) for r in rows) / n) if n else None,
        "saturation_rate": (sum(r["saturated"] for r in rows) / n) if n else None,
        "suggested_mae": (sum(abs(r["suggested_max"] - r["true"]) for r in suggested)
                          / len(suggested)) if suggested else None,
    }


def render(result: dict) -> str:
    head = ("base", "true", "raw_k", "k", "saturated", "fallback_k", "suggested_max", "error")
    table = [head] + [tuple("-" if r[c] is None else str(r[c]) for c in head)
                      for r in result["rows"]]
    widths = [max(len(row[i]) for row in table) for i in range(len(head))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in table]
    n = len(result["rows"])

    def fmt(x, pct=False):
        if x is None:
            return "-"
        return f"{100 * x:.0f}%" if pct else f"{x:.2f}"

    lines.append("")
    lines.append(f"recordings: {n}  MAE: {fmt(result['mae'])}  "
                 f"saturation rate: {fmt(result['saturation_rate'], pct=True)}  "
                 f"suggested_max MAE: {fmt(result['suggested_mae'])}")
    if result["missing"]:
        lines.append(f"labeled but no .npz: {', '.join(result['missing'])}")
    if result["unlabeled"]:
        lines.append(f".npz without a label (skipped): {', '.join(result['unlabeled'])}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data_dir", type=Path, help="directory of WHOSAID_DUMP_EMBEDDINGS .npz files")
    ap.add_argument("--labels", type=Path, help="labels.json (default DATA_DIR/labels.json)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="cosine cut to evaluate (default: the one each dump recorded)")
    ap.add_argument("--use-bounds", action="store_true",
                    help="replay each dump's recorded --min/--max-speakers")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)
    labels_path = args.labels or args.data_dir / "labels.json"
    if not labels_path.exists():
        print(f"count eval: no labels file at {labels_path}", file=sys.stderr)
        return 2
    labels = json.loads(labels_path.read_text())
    if not isinstance(labels, dict) or not all(
            isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in labels.values()):
        print("count eval: labels.json must map base -> positive int headcount", file=sys.stderr)
        return 2
    result = evaluate(args.data_dir, labels, args.threshold, args.use_bounds)
    if not result["rows"]:
        print(f"count eval: no labeled .npz found in {args.data_dir}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
