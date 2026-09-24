# Action-item eval harness

`test/eval/` measures how well the built-in action-item summarizer (`lib/action_items.py`) does
its job. It runs the real pipeline over a set of synthetic meetings whose correct action items
have been labeled by hand, scores each draft against those labels, and turns the result into a
number you can track: precision, recall, F1, and how many drafted items carry a ⚠ flag.

The eval has two jobs:

1. **A baseline for the local model.** The summarizer runs a local Ollama model (default
   `qwen2.5:14b`). The eval says how good that model is on these meetings, and whether a prompt
   or pipeline change made it better or worse. The committed numbers are under "Committed runs" below.
2. **A reference point.** The same fixtures can be drafted by a cloud model (Claude, through the
   Claude Code CLI) to show how much of the gap is the local model and how much is the pipeline.
   This is a measuring stick only. whosaid never uses a cloud model.

## What leaves the machine

**whosaid itself sends nothing anywhere, and nothing in this harness changes that.** `lib/` holds
no cloud code and no cloud fallback. The summarizer talks only to Ollama on `127.0.0.1`.

The `claude-cli` backend is different, so here is exactly what it does:

- It is developer tooling that lives only in `test/eval/run_eval.py`. whosaid never imports or
  calls it, and it runs only when a developer passes `--backend claude-cli`.
- It sends the **committed synthetic fixtures** in `test/eval/fixtures/` to Anthropic through the
  Claude Code CLI, and nothing else. Every person, project and meeting in those fixtures is made
  up, and every speaker label ends in `_Example`.
- It refuses to read any other fixtures directory. `--allow-external-fixtures` lifts that
  refusal, and exists only for other synthetic fixtures, such as the harness's own tiny test
  fixture. **Never point it at a real meeting.**
- Everything else is fully offline: the `ollama` backend (local), `--replay`, `--report`, and
  `test/eval_test.py`.

## Layout

```
test/eval/fixtures/<slug>/     one synthetic meeting per directory (see "Fixtures")
test/eval/validate_fixtures.py checks the fixtures themselves
test/eval/score.py             the scorer: pure functions, stdlib only
test/eval/run_eval.py          the runner: backends, record/replay, report
test/eval/cassettes/<run>/     recorded model replies, one JSON file per fixture
test/eval/results/<run>.json   recorded scores for one run
test/eval/results/<run>/       the drafts that run produced, one <fixture>.md each
test/eval_test.py              offline self-test (fake models only)
```

`<run>` is the run slug: `<backend>-<model>`, lowercased, with every character outside
`[a-z0-9.-]` replaced by `-`. For example, `ollama-qwen2.5-14b` and `claude-cli-opus`.

## Fixtures

Each `test/eval/fixtures/<slug>/` holds three files:

- `transcript.speakers.txt` is the meeting in whosaid's transcript format. Line 1 is
  `# Speakers (N): A, B, ...`, then one turn per line as `[HH:MM:SS] Label: text`.
- `whosaid.toml` is the workspace config the summarizer reads: `[workspace]` owner and aliases,
  and `[groups]`. It is loaded with `wsconfig.load_config`.
- `gold.json` holds the hand labels:

```json
{"schema": 1, "fixture": "<slug>", "description": "...", "owner": "Alice_Example",
 "items": [{"id": "G1", "t": "00:00:05", "speaker": "Bob_Example", "kind": "ask",
            "keywords": ["vendor report"], "desc": "...", "optional": false}],
 "distractors": [{"t": "00:01:20", "why": "..."}]}
```

Each item is one action item the owner should get. `kind` is `ask` (someone asks the owner),
`commit` (the owner commits; only for the owner's own turns) or `directive` (a `[groups]`
leadership speaker sets something for the whole team, with or without naming the owner).
`t` is the turn it comes from. `keywords` are
short phrases, and a drafted bullet counts as that item when its text contains any of them.
`optional: true` marks an item the model may reasonably list or skip. A distractor is a turn that
looks like an ask but is not one for the owner, such as a request aimed at someone else.

The runner loads each fixture's config with the `WHOSAID_OWNER`, `WHOSAID_OLLAMA` and
`WHOSAID_SUMMARIZER_MODEL` environment overrides removed, so your shell cannot change a fixture.
It needs Python 3.11+ for `tomllib`. On an older Python, `load_config` would silently ignore every
`whosaid.toml`, so the runner refuses to start.

## Scoring

`test/eval/score.py` is the definition. In prose:

1. **Parse the draft.** A scored bullet is a line shaped the way `render_bullet` writes it:
   `- **<label>** [<Requester> HH:MM:SS] <body>`. `[inferred]` bullets are counted, but never
   scored. A bullet is *flagged* when its body contains ⚠, which means the quote was missing or
   not verbatim. The evidence block at the end of a draft is not a bullet.
2. **Match each bullet, in document order.** Its candidates are the gold items with the same `t`
   that have at least one keyword that is a substring of the bullet's normalized text.
   Normalizing lowercases, collapses whitespace, and keeps only `[a-z0-9 ]`, the same as lib's
   `norm`. Both sides' times are normalized, so `0:00:05` equals `00:00:05`. The bullet goes to
   the first unmatched candidate, trying non-optional items before optional ones.
   - A match to a non-optional item is a **true positive**.
   - A match to an optional item counts neither way.
   - A bullet with no match is a **false positive**. It gets the tag of the first rule below that
     applies:

| tag | when |
|---|---|
| `distractor` | its `t` is a distractor turn |
| `duplicate` | its `t` has gold items, and either a keyword matched an item that is already taken, or every gold item at that `t` is already taken |
| `wrong-item` | its `t` has gold items, but none matches by keyword |
| `unlabeled` | anything else (a turn with no labels at all) |

3. **Count misses.** An unmatched non-optional gold item is a **false negative**. An unmatched
   optional item is ignored.
4. **Ratios.**
   - precision = TP / (TP + FP). When there are no TP and no FP, precision is 1.0 if the fixture
     has no scorable gold (TP + FN = 0) and 0.0 otherwise.
   - recall = TP / (TP + FN), or 1.0 when TP + FN = 0.
   - F1 is the harmonic mean of the two, or 0.0 when both are 0.
   - flag rate = flagged / bullets, or 0.0 when there are no bullets.
   - Stored ratios are rounded to 4 decimal places.
5. **Totals are micro averages.** The counts (TP, FP, FN, bullets, flagged, inferred, model
   calls, FP tags) are summed across fixtures first, and the ratios are computed from the sums.
   A long fixture therefore weighs more than a short one.

Keyword matching is an approximation. A correct bullet that paraphrases around every keyword
scores as a false positive (usually `wrong-item`) plus a false negative. When a number moves,
read the drafts in `test/eval/results/<run>/` and run with `-v`, which prints every bullet's
verdict and every missed item.

## Running

```sh
# local model, scores only (writes nothing)
python3 test/eval/run_eval.py --backend ollama --model qwen2.5:14b

# local model, recorded
python3 test/eval/run_eval.py --backend ollama --model qwen2.5:14b --record

# reference model, recorded, four fixtures at a time
python3 test/eval/run_eval.py --backend claude-cli --model opus --record --jobs 4

# replay a recorded run offline and check that the scores still match
python3 test/eval/run_eval.py --replay ollama-qwen2.5-14b

# compare every recorded run
python3 test/eval/run_eval.py --report
```

| flag | meaning |
|---|---|
| `--backend ollama\|claude-cli` | the backend for a live run |
| `--model M` | the model name. Defaults to `qwen2.5:14b` for ollama and `opus` for claude-cli. |
| `--ollama URL` | the Ollama base URL (default `http://127.0.0.1:11434`) |
| `--think on\|off` | ollama backend: force `[summarizer] think` for every fixture. An `on` run records as `<run>-think`. Default: the fixture's config, which leaves thinking off. |
| `--record` | save cassettes, the results JSON and the drafts, then replay them as a self-check |
| `--replay RUN` | re-score a recorded run from its cassettes, offline |
| `--report` | print a markdown comparison of every `results/*.json` |
| `--fixtures a,b` | run only these fixture slugs |
| `--fixtures-dir DIR` | the fixtures root (default `test/eval/fixtures`) |
| `--out-dir DIR` | the root that holds `cassettes/` and `results/` (default `test/eval`) |
| `--jobs N` | draft N fixtures at a time on threads. Useful for claude-cli; one local Ollama gains little. |
| `--allow-external-fixtures` | let claude-cli read a fixtures dir other than the committed one |
| `-v` | print every bullet's verdict and every missed gold item |

Exit codes: 0 means OK. 1 means a replay did not reproduce the recorded scores, or the cassette
had no reply for a prompt. 2 means a usage error, a model error, or a replay that could not be
compared: missing files or changed fixtures.

Each fixture is drafted by calling the pipeline in process:
`action_items.draft(transcript, meeting=<slug>, cfg=<the fixture's config>, model=<model>,
client=<client>)`. `client=` is the pipeline's one injection point. With no client, lib builds
its own Ollama client, exactly as in production. Each fixture gets a fresh client, because
`draft` reports the client's call count as `model_calls`.

### The `ollama` backend

This backend uses lib's own `Ollama` class, the production client. It builds the client with the
fixture's `num_ctx`, `timeout`, `num_predict` and `think`, the same way the summarizer does.
Ollama must be running with the model pulled.

### The `claude-cli` backend

Each model call runs the Claude Code CLI once:

```sh
claude -p --system-prompt "<system>" --model <model> --tools "" --strict-mcp-config \
  --setting-sources "" --disable-slash-commands --no-session-persistence --output-format json
```

The user message goes on stdin. Each call runs in a fresh, empty temporary directory that is
removed afterwards. Those flags give the model no tools and no MCP servers. They also skip user,
project and local settings (so no hooks and no `CLAUDE.md`), skip skills, and save no session. The
model sees only the summarizer's own prompts.

- **Binary.** The runner uses `claude` from `PATH`, or the path in `WHOSAID_EVAL_CLAUDE_BIN`.
- **Auth is your Claude subscription.** Log in once with `claude`, or mint a long-lived token
  with `claude setup-token` and export it as `CLAUDE_CODE_OAUTH_TOKEN`. The runner removes
  `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the CLI's environment, because either one
  would take precedence over the subscription and bill the API instead.
- **Output parsing.** `--output-format json` prints an array of events. The runner reads the
  `result` event. It checks `is_error`, not `subtype`, because an auth failure arrives as
  `"subtype": "success", "is_error": true`. An `is_error` result raises right away, since the CLI
  has already retried API errors itself. Any other failure (a non-zero exit with no result,
  output that is not JSON, or a 300-second timeout) is retried once, then raised.
- **No temperature.** The CLI has no temperature flag, so the pipeline's temperatures (0.0, and
  0.2 for inferred next steps) are ignored, and two live runs can differ. The recorded cassette
  pins down what one run saw.
- **Resolved model.** A model alias such as `opus` resolves to whatever that alias means on the
  day. The cassette records the resolved model id as `resolved_model`.
- **Draft header.** lib stamps every draft "(local Ollama, offline)". Before saving a claude-cli
  draft, the runner rewrites that to "(claude-cli reference backend, eval only)".

## Record and replay

`--record` wraps each fixture's client in a recorder and writes three things. It writes nothing
unless every fixture succeeds, and it replaces any earlier recording of the same run.

- `test/eval/cassettes/<run>/<fixture>.json` holds the model replies:

  ```json
  {"schema": 1, "backend": "ollama", "model": "qwen2.5:14b", "recorded": "YYYY-MM-DD",
   "calls": {"<sha256 hex>": "<reply>"}}
  ```

  The key is the sha256 of `json.dumps([system, user, float(temperature)], ensure_ascii=False)`.
  If the same prompt comes up twice in one run, the second call is answered from the recording.
  That way a replay sees exactly what the recorded run saw, even from a model that is not
  deterministic.
- `test/eval/results/<run>.json` holds the scores:

  ```json
  {"schema": 1, "run": "<run>", "backend": "...", "model": "...", "recorded": "YYYY-MM-DD",
   "fixtures_sha256": "<hex>",
   "per_fixture": {"<slug>": {"tp": 0, "fp": 0, "fn": 0, "bullets": 0, "flagged": 0,
                              "inferred": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0,
                              "flag_rate": 0.0, "model_calls": 0,
                              "fp_tags": {"distractor": 0, "duplicate": 0,
                                          "wrong-item": 0, "unlabeled": 0}}},
   "total": {"...": "the same keys, micro-averaged"},
   "wall_seconds": {"<slug>": 0.0}}
  ```

- `test/eval/results/<run>/<fixture>.md` holds each draft, for a human to read.

`fixtures_sha256` covers only the fixtures in the run. It hashes each of their files, in order of
relative path, as `<relative path>\0<size in bytes>\0<bytes>`, skipping dotfiles. Adding a new
fixture therefore does not invalidate older runs, but editing one of a run's fixtures does.

`--replay <run>` is strict and fully offline:

1. It refuses with "fixtures changed since this run was recorded; re-record" when
   `fixtures_sha256` no longer matches.
2. Otherwise, it drafts every fixture of the run from its cassette. A prompt with no recorded
   reply raises, rather than falling through to a live call. That happens when the pipeline's
   prompts changed.
3. It compares the new scores with the committed results JSON. Every field except
   `wall_seconds` and `recorded` must be equal, with floats compared after rounding to 4
   decimal places. On a mismatch it prints the differing fields.

Replay writes nothing. After a `--record`, the runner replays the new recording right away and
prints `MATCH` or `MISMATCH`.

A replay only checks that a recording still reproduces its scores under the current pipeline and
scorer. It says nothing about model quality. When you change a prompt in `lib/action_items.py`,
every recording stops matching, and you need a fresh live run.

## Committed runs (recorded 2026-09-24, prompts from #42)

Three runs are committed, over six fixtures. `python3 test/eval/run_eval.py --report` prints this
comparison from them:

| run | model | precision | recall | F1 | flag rate | model calls | wall s |
|---|---|---|---|---|---|---|---|
| claude-cli-opus | opus | 0.891 | 0.961 | 0.924 | 0.018 | 80 | 903.9 |
| ollama-qwen3-14b | qwen3:14b | 0.870 | 0.922 | 0.895 | 0.018 | 86 | 337.7 |
| ollama-qwen2.5-14b | qwen2.5:14b | 0.750 | 0.824 | 0.785 | 0.036 | 81 | 571.8 |

| fixture | claude-cli-opus | ollama-qwen3-14b | ollama-qwen2.5-14b |
|---|---|---|---|
| aliases-no-groups | 1.000 | 0.889 | 0.800 |
| distractor-heavy | 0.857 | 0.769 | 0.714 |
| long-status | 0.952 | 0.870 | 0.833 |
| named-asks | 1.000 | 1.000 | 0.750 |
| team-directives | 0.842 | 0.933 | 0.857 |
| unnamed-asks | 0.875 | 0.889 | 0.737 |

Before #42, over the first five fixtures, the same three models scored F1 0.921 (opus), 0.821
(`qwen3:14b`, thinking off) and 0.717 (`qwen2.5:14b`).

- **Team directives.** `team-directives` is the fixture #42 added: the owner barely speaks and two
  leadership speakers set team-wide rules without naming them. The prompts before #42 excluded
  such turns by design. All three models now find at least 7 of its 8 required items.
- **A bare-bold reply bullet was being dropped.** `qwen3:14b` often writes `**Title.** ...` with
  no leading `- `. Before #42 the parser skipped those lines; on `team-directives` that alone cut
  its recall from 7 of 8 to 1 of 8.
- **The reference is `opus`**, which resolved to `claude-opus-5-5` on the recording date. The
  claude-cli backend has no temperature control, so a new live run of it can differ from this
  one (its `unnamed-asks` SELECT pass picked a turn in one live run and skipped it in the next,
  with the same prompt).
- **Wall time is not comparable.** `wall s` is the sum of per-fixture drafting times. These runs
  were recorded on a 48 GB Apple M5 Pro under heavy unrelated load (load average 20 to 38), with
  the opus run in parallel. Unloaded, `qwen3:14b` took 177 to 194 s for all six fixtures.
- **`num_predict`.** The local runs use the `[summarizer] num_predict` cap (default 2048).
  Without it, one sampled pass (the temperature-0.2 inferred-next-steps step) once fell into a
  repetition loop and ran until the 900-second timeout.

## Thinking models (recorded 2026-09-24, before #42; history)

Qwen3 and later are thinking models: unless the request says otherwise, Ollama lets them reason
before every answer. The summarizer makes one small call per candidate turn, so that reasoning is
paid about 70 times per meeting. `[summarizer] think` (default `false`) now goes out on every
request, for all models or per model (`think = { "qwen3:14b" = true, default = false }`).

These three runs used the prompts before #42 and the first five fixtures. #42 changed the
prompts, which invalidates every cassette, and the thinking and `qwen3.8:27b` runs were not
re-recorded, so their recordings were removed; the numbers stay here as history. All three ran
on a 48 GB Apple M5 Pro with Ollama 0.34.1 and a 32k context, one fixture at a time:

| run | model | think | precision | recall | F1 | model calls | wall s |
|---|---|---|---|---|---|---|---|
| ollama-qwen3-14b-think | qwen3:14b | on | 0.900 | 0.837 | 0.868 | 67 | 1907.8 |
| ollama-qwen3-14b | qwen3:14b | off | 0.750 | 0.907 | 0.821 | 71 | 159.8 |
| ollama-qwen3.8-27b | qwen3.8:27b | off | 0.808 | 0.884 | 0.844 | 66 | 355.6 |

On the same machine, a live rerun of the `qwen2.5:14b` default scored precision 0.609, recall
0.907, F1 0.729 in 177.0 s.

- **Thinking buys precision at about 12 times the wall time.** With thinking on, `qwen3:14b`
  drafts 4 false positives instead of 13, and misses 7 items instead of 4. Measured at the Ollama
  API, it generated 38,429 tokens against 2,635 with thinking off. Two replies hit the
  `num_predict` cap mid-reasoning, and one came back with no answer at all.
- **With thinking off, `qwen3:14b` beats the `qwen2.5:14b` default at the same speed.** Precision
  goes from 0.609 to 0.750 with recall unchanged, and wall time is within 10%.
- **`qwen3.8:27b` with thinking off** is the most precise run without thinking, at about twice the
  wall time of `qwen3:14b`. It is weakest on `unnamed-asks` (4 of 9 found), because its SELECT
  pass adds fewer turns where the owner is asked something without being named. Qwen3.8 has no
  14B size; 27B (about 18 GB loaded) is its smallest open model.
- **`qwen3.8:27b` with thinking on** was run on `named-asks` only, to bound the time: F1 1.000
  (9 of 9, no false positives) in 315.0 s, against 0.947 in 80.9 s with thinking off. It
  generated about 340 tokens per call against about 570 for `qwen3:14b`, and no reply hit
  `num_predict`. One fixture is not a run, so it is not committed.
- **The think-on run was recorded before this flag existed**, with `think` left out of the
  request. Ollama 0.34.1 treats that as on for a thinking model, and all 67 calls returned
  reasoning. `--think on` records the same configuration.
- **Small differences are noise.** A live rerun of the committed `qwen2.5:14b` baseline gave F1
  0.729 against the recorded 0.717 (`distractor-heavy` 0.545 against 0.476). Temperature 0 is
  not bit-exact across runs, and the inferred-next-steps step samples at 0.2.

For a background ingest, `qwen3:14b` with thinking off is the best trade on this set. Turn
thinking on for that model when precision matters more than time.

## Tests

`python3 test/eval_test.py` runs offline, with no network, no Ollama and no `claude`. It covers:

- the scorer on hand-built drafts, and its agreement with lib's `render_bullet`
- the `client=` injection: the real Ollama client is never constructed
- a record-then-replay round trip on a tiny inline fixture in a temporary directory: identical
  scores, a strict cassette miss, a tampered result, a changed fixture, and replay writing nothing
- the claude-cli backend against a fake `claude` executable: API-key variables removed,
  isolation flags present, the prompt on stdin, `is_error`, and one retry
- a replay of every committed run under `test/eval/results/` that has cassettes
