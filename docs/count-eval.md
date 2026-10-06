# Speaker-count eval

`test/count_eval/run_count_eval.py` measures the automatic speaker count
(`estimate_speakers` in `lib/diarize_sherpa.py`) against recordings whose real
headcount you know. It exists so that changes to the estimator (issue #59: the
threshold, the recovery ladder) can be checked on real meetings, including the
2-person case that #38 fixed, before they ship.

The eval runs offline on saved estimator inputs. It does not need the audio or
the models, and it runs in about a second per recording.

## The data stays local

A dump holds one voiceprint per speaker turn. That is biometric data about the
people in the meeting, so **dumps are never committed**. Keep them outside the
repository, or under `test/count_eval/data/`, which is gitignored. The repository
ships only the harness and a synthetic self-test (`test/count_eval_test.py`).

## 1. Dump the estimator inputs

Set `WHOSAID_DUMP_EMBEDDINGS` to a directory when you transcribe:

```sh
WHOSAID_DUMP_EMBEDDINGS=~/whosaid-count-eval whosaid standup.m4a
```

Each run that reaches the auto-count estimator writes `<base>.npz` to that
directory. The file holds `X` (the per-turn embeddings), `durations`, `raw_k`,
`threshold`, `cap`, `min_speakers` and `max_speakers`. With anchoring
(`--expected-speakers`) the estimator sees only the turns no anchor claimed, so
that file is `<base>-residual.npz`, and its label is the number of people who were
NOT named. Nothing is written when the variable is unset, or when `--speakers N`
fixes the count, because then the estimator does not run. Only the chunked
diarization path runs the estimator. That path is taken for recordings over 15
minutes, and also when you pass `--chunk-seconds`, a `--min-speakers`/`--max-speakers`
range, or `--expected-speakers`. The dump changes no other output.

## 2. Label

Write `labels.json` next to the dumps. It maps each base name to the true
headcount:

```json
{"standup-2026-10-01": 7, "1on1-2026-10-02": 2}
```

## 3. Run

```sh
uv run --with numpy python test/count_eval/run_count_eval.py ~/whosaid-count-eval
```

It prints one row per labeled recording, then the aggregate:

```text
base             true  raw_k  k   saturated  fallback_k  suggested_max  error
1on1-2026-10-02  2     21     2   True       2           -              0
standup-...      7     44     20  True       -           8              13

recordings: 2  MAE: 6.50  saturation rate: 100%  suggested_max MAE: 1.00
```

- `k` is the count the pipeline would use. `error` is `k - true`. MAE is the mean absolute error.
- `fallback_k` is the duration-filtered recovery count (#38), or `-` when recovery abstained.
- `suggested_max` is the `--max-speakers N` hint (#59) that a saturated, unrecovered
  run puts in `count_warning`. Its MAE is over the rows that have one.

Options:

- `--threshold T` re-runs at another cosine cut. Compare a candidate value
  against the default on the same files.
- `--use-bounds` replays the `--min-speakers`/`--max-speakers` that each dump
  recorded. By default these are ignored, so the eval measures the auto count.
- `--labels PATH` reads the labels from somewhere else. `--json` prints machine-readable output.
