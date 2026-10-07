# Synthetic-meeting diarization eval

This harness answers two questions about whosaid's diarization: how many people spoke, and
who said each turn. It builds fake meetings from text-to-speech voices, so the ground truth is
exact to the sample. It then runs whosaid on each meeting in several modes and scores the
result.

The voices are **TTS voices, not real people.** A result here shows how whosaid behaves on
clean, studio-quality synthetic speech with known turn boundaries. It does not predict accuracy
on a real call, and you should not quote it as if it did. For headcounts from your own
recordings, see [count-eval.md](count-eval.md).

## What leaves the machine

Only the scripted lines in `test/diarize_eval/lines.json` (121 generic meeting lines and 4
read-aloud passages) are sent, to ElevenLabs and OpenAI text-to-speech. These lines contain
no names, companies or products. No recording, transcript or voiceprint is uploaded. Everything
after rendering (building meetings, running whosaid, scoring) runs locally.

## Pipeline

The rendered pool, the built meetings and the run outputs live in the cache
(`$WHOSAID_DIARIZE_EVAL_CACHE`, default `~/.cache/whosaid/diarize-eval`), outside the repo.

1. **Render the voice pool** (paid API calls; a cached clip is never re-rendered):

   ```sh
   ELEVENLABS_API_KEY=... OPENAI_API_KEY=... python3 test/diarize_eval/render.py \
       [--voices el-roger,oa-coral|all] [--per-voice short=3,medium=6,long=2] \
       [--budget-chars 45000] [--dry-run]
   ```

   `voices.json` lists 34 voices: 21 from ElevenLabs and 13 from OpenAI. For each voice, the
   script renders some dialogue lines plus one 30 to 40 s enrollment passage, as 16 kHz mono
   WAV. It then writes `pool.json`. The keys are read from the environment only.
   `--dry-run` prints the characters each vendor would bill without calling the APIs. A
   run that would spend more ElevenLabs characters than `--budget-chars` refuses to start.

2. **Measure voice separability** (local):

   ```sh
   uv run --with numpy --with sherpa-onnx python test/diarize_eval/voice_sim.py
   ```

   This step embeds each enrollment clip with whosaid's own TitaNet-small extractor and
   writes `voice_sim.json` next to `pool.json`. `run.py` uses that file to split its report
   (see "Similar voices" below).

3. **Build meetings** (stdlib only, deterministic):

   ```sh
   python3 test/diarize_eval/build.py --pool ~/.cache/whosaid/diarize-eval/pool.json \
       --out /tmp/meetings --seed 31 --count 14 [--profile mixed|easy|hard|long] \
       [--pattern balanced|dominant|rare-speaker|cameo] [--min-speakers 2 --max-speakers 6]
   ```

   A `(seed, index)` pair always rebuilds the same meeting byte for byte. Profiles set how
   hard the meeting is. `hard` adds overlap, background noise, gain differences,
   backchannels ("yeah", "right") and rapid exchanges. `long` builds meetings of 960 to
   1300 s, which crosses the 900 s parallel-chunking threshold. Patterns set how talk time is
   shared. For example, `cameo` gives one person a single brief appearance.

4. **Run and score**:

   ```sh
   python3 test/diarize_eval/run.py --meetings /tmp/meetings \
       [--modes blind,hint,refs,refs-subset] [--whosaid ./whosaid] [--run-name NAME] \
       [--extra "--some-flag"] [--force]
   python3 test/diarize_eval/run.py --report test/diarize_eval/results/NAME.json
   ```

   | mode | what whosaid is given |
   |---|---|
   | `blind` | nothing: it must count and separate the speakers |
   | `hint` | `--speakers N` with the true count |
   | `refs` | an enrollment clip for every speaker (`WHOSAID_VOICE_REFS`) |
   | `refs-subset` | enrollment clips for half the speakers |

   Each run's results are cached per meeting and mode, so an interrupted run resumes where
   it stopped. Pass `--whosaid` a frozen copy of the code (`git archive <sha>`) when comparing
   two versions, so an edit in the working tree cannot leak into a run.

**Isolation.** Every whosaid call gets its own `WHOSAID_SPEAKER_DB`, a private
`WHOSAID_VOICE_REFS` directory and an unused `WHOSAID_WORKSPACE`, with `WHOSAID_OWNER` unset.
`run.py` fingerprints the real registry (`~/.config/whosaid/speakers.json`) and the repo's
`voices/` directory before and after each call. If either changes, the run aborts.

## Meeting format

`<meetings>/<id>/audio.wav` is 16 kHz mono 16-bit PCM. `<meetings>/<id>/truth.json` looks like
this:

```json
{"schema": 1, "id": "m31-004", "seed": 31, "tags": ["n4", "backchannel"], "duration": 183.2,
 "speakers": [{"name": "Roger", "voice": "el-roger", "enrolled_clip": "enroll/el-roger.wav"}],
 "turns": [{"speaker": "Roger", "start": 0.50, "end": 4.21, "text": "...", "line_id": "L012"}]}
```

`turns` is sorted by start time. A turn may overlap the one before it. `<meetings>/index.json`
lists the meetings and records the pool root.

## Metrics

`score.py` uses only the standard library and is covered by `test/diarize_eval_test.py`.

- **count acc**: the share of meetings where the number of speakers found equals the true
  number. A speaker found with less than 0.5 s of talk is not counted.
- **DER**: diarization error rate, measured on 10 ms frames: miss, false alarm and confusion,
  over the true speech time. Anonymous labels are mapped to people by the optimal one-to-one
  overlap assignment.
- **turn acc**: the share of true turns whose majority speaker label maps to the right person.
  **short-turn acc** counts only turns under 1.5 s.
- **named right / named wrong** (refs modes): the share of speech time where whosaid printed
  a name and the name was right, or wrong. A wrong name is worse than no name at all.
- **WER**: word error rate of the transcript against the script.

Aggregates are micro-averaged: by speech time for DER and names, and by turn count for turn
accuracy.

## Similar voices

A voiceprint cannot separate two voices that sound the same to the embedder, so a meeting
containing such a pair tests the embedder, not the clustering. `voice_sim.py` measures the
pool. To whosaid's embedder, the OpenAI voices are much closer to each other than real people
are. The ElevenLabs voices behave like different people.

| pairs | mean cosine | max |
|---|---|---|
| OpenAI ↔ OpenAI | 0.55 | 0.85 |
| ElevenLabs ↔ ElevenLabs | 0.19 | 0.60 |
| real people (for reference) | 0.25 (p90 0.45) | |

A real person's own turns reach about 0.72 cosine to each other at the 10th percentile. So the
closest OpenAI pairs are nearer to each other than one person usually is to themself. The
report puts a meeting in the **similar** bucket when two of its voices score 0.60 or more,
and in the **distinct** bucket otherwise. Read the distinct bucket for clustering quality.

## Same-accent roster pool

Real teams often share an accent, a gender and a pitch range, which is the hard case for a
voiceprint. A second pool models that: 12 male, Indian-English-accented voices tagged
`roster: "in-m"` (6 ElevenLabs shared-library voices, 6 OpenAI `gpt-4o-mini-tts` voices whose
`instructions` ask for an Indian-English accent with a slightly different pace each). It has
its own files and its own cache, so the default pool is never touched:

- `voices_realistic.json`: the voices. A voice may carry `instructions` (replaces the generic
  OpenAI style prompt) and `roster` (copied into `pool.json`).
- `lines_realistic.json`: 300 generic meeting lines (stand-ups, planning, incident review,
  vendor call, design review) and 4 enrollment passages of about 60 s. Lines keep the kinds
  `short`/`medium`/`long`; a `sub` field splits `short` into `backchannel` (under 1 s) and
  `phrase` (1 to 2 s). The full file is about 25,600 characters.

```sh
ELEVENLABS_API_KEY=... OPENAI_API_KEY=... python3 test/diarize_eval/render.py \
    --voices-file test/diarize_eval/voices_realistic.json \
    --lines-file test/diarize_eval/lines_realistic.json \
    --max-chars 9000 --budget-chars 54000 \
    --out ~/.cache/whosaid/diarize-eval-real [--dry-run]
```

`--max-chars` caps the characters sent per voice, enrollment passage included. Each voice gets
a different subset, seeded by its id, that keeps the kind mix of the full file; the enrollment
passage is always rendered. At 9,000 characters a voice renders about 96 lines, and the 6
ElevenLabs voices spend about 53,400 characters in total, so raise `--budget-chars` (the
default 45,000 refuses). `--dry-run` prints each voice's lines and character total and makes
no network call. ElevenLabs shared-library voices are synthesized directly by `voice_id`, with no
"add voice" step. A custom voices or lines file refuses to run against the default cache, so
always pass `--out`. Then point `build.py --pool` at `<out>/pool.json`.

## Results (v1.10.0)

Two sets of meetings were used. The 32 **tuning** meetings (`tune`, `tune-hard`, `tune-cameo` and
`pilot`) found the bugs and set the fixes' thresholds. The 33 **held-out** meetings (`val-mixed`,
`val-hard`, `val-cameo` and `val-long`, three of them 16–21 minutes) were generated with fresh
seeds and were not looked at until fixes 1 and 2 below were written. Fix 3 came from them. "Before" is v1.8.2, where the branch started.
v1.9.0 shipped in between, and by its changelog it changed only how the count is reported, not the
count itself. "After" is v1.10.0. Every number is a percentage pooled over the set's meetings.

Held-out, all meetings:

| mode | count acc | DER | turn acc | named right | named wrong |
|---|---|---|---|---|---|
| blind | 21.2 → 36.4 | 24.6 → 10.8 | 65.2 → 84.6 | | |
| `--speakers N` | 30.3 → 100 | 17.5 → 9.2 | 79.4 → 86.8 | | |
| all enrolled | 15.2 → 45.5 | 19.1 → 10.4 | 74.4 → 84.5 | 82.7 → 91.3 | 7.9 → 4.6 |
| half enrolled | 21.2 → 45.5 | 18.4 → 11.3 | 72.0 → 84.4 | 38.9 → 41.0 | 1.9 → 3.8 |

Tuning, all meetings:

| mode | count acc | DER | turn acc | named right | named wrong |
|---|---|---|---|---|---|
| blind | 28.1 → 53.1 | 12.9 → 11.2 | 79.3 → 83.0 | | |
| `--speakers N` | 46.9 → 100 | 19.8 → 9.0 | 76.0 → 85.9 | | |
| all enrolled | 21.9 → 65.6 | 15.6 → 9.4 | 73.9 → 84.4 | 85.8 → 91.8 | 2.4 → 2.7 |
| half enrolled | 18.8 → 65.6 | 14.1 → 10.4 | 76.7 → 83.6 | 39.8 → 42.1 | 2.3 → 2.9 |

"Named right" in half-enrolled mode tops out near 50%, because half the talk belongs to voices
nobody enrolled. Named-wrong in that mode rose 1.9 → 3.8% held out. The likely cause, not traced
meeting by meeting: the per-turn path (fix 2 below) merges clusters more readily, and when an unenrolled voice merges into an enrolled one, its
talk takes the enrolled name.

Split by voice similarity, held-out blind mode reads 13.3 → 40.0% count accuracy and 15.3 → 8.8%
DER on the 15 distinct meetings, against 20.0 → 33.3% and 34.1 → 12.8% on the 15 similar
ones. Blind counts on similar meetings stay poor. Two OpenAI voices at 0.75 cosine merge into one
speaker, and no threshold separates them without also splitting real people. `--speakers N`
gets the count right either way.

What changed, in the order it was found:

1. **Short fragments folded (#59).** Clusters made only of sub-2 s turns ("Yeah.", "Right.")
   were counted as speakers. They now fold into the nearest substantive voice at 0.40 cosine
   or more.
2. **Short audio takes the per-turn path (#68).** Under 15 minutes, whosaid used sherpa's
   whole-file clustering. That path ignored the count estimator and returned fewer than N
   speakers for `--speakers N` on 4 of 12 meetings.
3. **`--ref` voices assigned best pair first.** The held-out run of fixes 1 and 2 showed refs
   mode getting *worse* on similar voices: named-wrong went 7.9 → 19.6%, and to 35.4% on the
   similar bucket. The cause was older than either fix. The `--ref` pass let each clip claim its
   best free cluster in argument order. On m32-005, two similar voices merged into one cluster.
   Marin (0.76) claimed it before Nova (0.93) was considered, and 44% of the meeting went out
   under the wrong name. Fix 2 made such merges more common on short audio, which exposed the
   bug. The registry pass had been fixed the same way in #60. With all three fixes,
   named-wrong is 4.6%.

**Tried and reverted (#69).** Letting an enrolled voice absorb tiny leftover clusters fixed
one tuning meeting but put wrong names on two others. In both, an unenrolled speaker with
under 30 s of talk counted as "tiny" and took an enrolled person's name. #69 stays open with
the numbers.

**Registry round trip.** Enrolling each meeting's voices into an isolated registry and
re-running gave 2 of 2 names correct on the tuning set. On the cameo set, 2
names came out wrong; the same 2 are wrong on v1.9.0, so they are not a regression.

**Long meetings.** Fix 1's 2 s cutoff was set on 2-minute meetings. On the three 16–21-minute
held-out meetings, blind DER went 35.8 → 7.7% and refs named-wrong 11.3 → 2.8%. The cutoff
holds at that length too.
