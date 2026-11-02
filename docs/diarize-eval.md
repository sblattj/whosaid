# Synthetic-meeting diarization eval

This harness answers two questions about whosaid's diarization: how many people spoke, and
who said each turn. It builds fake meetings from text-to-speech voices, so the ground truth is
exact to the sample. It then runs whosaid on each meeting in several modes and scores the
result.

The voices are **TTS voices, not real people.** A result here shows how whosaid behaves on
clean, studio-quality synthetic speech with known turn boundaries. It does not predict accuracy
on a real call, and you should not quote it as if it did. For headcounts from your own
recordings, see [count-eval.md](count-eval.md).
For the October 2026 survey of newer transcription and embedder models run through this
harness, see [model-survey-2026-10.md](model-survey-2026-10.md).

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
   uv run --with numpy --with sherpa-onnx==1.13.8 python test/diarize_eval/voice_sim.py
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
- **WER**: word error rate of the transcript against the script. Realistic meetings report no WER (`wer` is null): their turns voice fragments of rendered lines but carry the whole line's text, so there is no exact reference.

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

## Calibrated realistic meetings (#80)

`realistic.py` builds meetings whose **diarizer output** matches the stats of 44 real
recordings (`real_stats.json`). `calibrate.py` compares the two side by side.

### Roster similarity

`voice_sim_realistic.json` holds the pairwise cosines of the 12 `in-m` roster voices
(enrollment clips, whosaid's TitaNet-small embedder):

| pairs | n | mean cosine | max | pairs at 0.60 or more |
|---|---|---|---|---|
| ElevenLabs ↔ ElevenLabs | 15 | 0.243 | 0.456 | 0 |
| OpenAI ↔ OpenAI | 15 | 0.558 | 0.814 | 6 |
| ElevenLabs ↔ OpenAI | 36 | 0.087 | 0.353 | 0 |
| all | 66 | 0.229 | 0.814 | 6 |
| real people (for reference) | | 0.25 (p90 0.45) | | |

The ElevenLabs half sits close to real colleagues. The OpenAI half is still bunched: 6 of
its 15 pairs score above what most real pairs do. Across vendors the voices are further apart
than real people.

### Turn taking

The diarizer does not see the truth turns exactly. It trims each turn to its voiced span,
misses part of every overlap, and splits long turns at pauses. On the roster batches it
widened the gap median by 0.16 to 0.26 s, halved the share of gaps under 0.3 s, saw 66 to 75%
of the overlap and cut the >8 s talk share by about 9%. So the planner aims the **truth** a
little tighter than the real numbers. The knobs are named constants at the top of
`realistic_plan.py` (`GAP_MEDIAN`, `OVERLAP_MEDIAN`, `OV_SWITCH_P`, `OV_SECONDS`,
`OV_MIN_PREV`, `BC_P`, `LONG_TAIL_MEAN`, `BIN_SHARE`).

Overlap comes mostly from a few long talk-overs of turns of 3 s or more, not from many
backchannels. Every overlap is also a 0 s gap, so many short ones would pull the gap median
far below the real 0.74 s.

### Device switches

Parameter drift (bitrate, EQ, level, room) moves whosaid's embedding by only about 0.01
cosine, too little to split a cluster. A change of device does: with 8 s chunks against a
60 s enrollment, the same codec family scores 0.90, opus to g722 0.83, opus to a near-mic
path 0.80 and opus to a phone line 0.67. Real over-split second clusters score 0.70 to 0.83.

- `--device-switch P` (default 0.25): each later epoch of a remote speaker moves to another
  codec family with probability P (`channel.device_switch`).
- `--enroll-switch P` (default 0.3): a remote speaker's enrollment clip comes from another
  device, as when a voiceprint was taken on an earlier call.

`truth.json` records both per speaker (`device_switch_epochs`, `enroll_device_switch`). Pass
`--device-switch 0 --enroll-switch 0` to get the earlier channel behavior.

### Calibration

Batch: `--seed 406 --count 4 --minutes 30-120 --roster in-m` (3 h 33 min of audio, 6 to 8
speakers each), whosaid v1.13.4 with `--max-speakers 8`, refs mode. The seed was picked from
12 candidates (401-412) by planning them without audio. Seed 406 was the one where the
4-meeting median length lands near the real 44 minutes (with 4 meetings, a random draw from
30-120 minutes misses that about half the time) and every meeting has at most 8 true
speakers, so `--max-speakers 8` never forces two people into one cluster. That second choice
affects the speakers-per-meeting row and the switch and gap stats.

```sh
uv run --with numpy python test/diarize_eval/realistic.py \
    --pool ~/.cache/whosaid/diarize-eval-real/pool.json --out /tmp/m80c/final \
    --seed 406 --count 4 --minutes 30-120 --roster in-m
python3 test/diarize_eval/run.py --meetings /tmp/m80c/final --modes refs \
    --whosaid FROZEN/whosaid --run-name m80c-final --extra "--max-speakers 8"
python3 test/diarize_eval/calibrate.py --meetings /tmp/m80c/final \
    --run ~/.cache/whosaid/diarize-eval/runs/m80c-final --mode refs
```

Before is the 6-meeting roster batch of 20 to 45 minutes (seed 91), on the earlier planner
and channel model. The meeting-length row changes because the batch is longer, not because of
the planner.

| metric | before | after | real |
|---|---|---|---|
| meeting minutes | 30.2 | 44.6 | 44 |
| switches / min | 5.84 | 5.74 | 5.7 |
| gap median (s) | 0.89 | 0.76 | 0.74 |
| gaps under 0.3 s | 21.8% | 28.9% | 26% |
| overlap share | 2.7% | 4.7% | 4% |
| 1-2 s turns, share of turns | 17.9% | 19.1% | 18% |
| 2-4 s turns, share of talk | 18.4% | 16.0% | 14% |
| >8 s turns, share of talk | 38.5% | 44.4% | 47% |
| cosine, correct pass-1 | 0.894 | 0.889 | 0.72-0.89 |
| cosine, over-split second cluster | 0.68 (n=11) | 0.65 (n=12) | 0.70-0.83 |
| cosine, blend best candidate | 0.65 (n=3) | none | 0.75-0.84 |
| **outside ±20%** | **7 of 21** | **1 of 20** | |

The one miss is the over-split cosine. Its 12 clusters run from 0.32 to 0.81. The three that
come from a device-switched stretch score 0.64 to 0.81. Most of the rest are fragments of 1 to
40 s at 0.32 to 0.68, and they pull the median down. This batch had no blend cluster, so the blend row is not judged; add
`--blends K` to test that case.

### The #77 failure, reproduced

On the same batch, v1.12.1 (per-turn purity gate) and v1.13.4 (talk-share gate) produce identical
segments and pass-1 scores on all 4 meetings; they differ only in the placed-voice absorb. Truth confirms
two second clusters as true splits, and the old gate refuses both:

| meeting | cluster | truth (share of its time) | cosine to enrollment | per-turn purity | v1.12.1 | v1.13.4 |
|---|---|---|---|---|---|---|
| m406-003 | SPEAKER_04, 218 s | Sanjay (97%) | 0.723 | 0.10 | refused | named Sanjay |
| m406-003 | SPEAKER_05, 189 s | Chandu (92%) | 0.720 | 0.05 | refused | named Chandu |
| m406-002 | SPEAKER_05, 115 s | Echo (84%) | 0.770 | 0.15 | refused | named Echo |

Sanjay's first cluster comes from a device-switched stretch (98% of its time), scores 0.814
and passes both gates at purity 0.53. DER on the two meetings is 25.2 → 21.7% (m406-002) and
21.7 → 14.4% (m406-003) from v1.12.1 to v1.13.4. This is the first synthetic run that shows
#77's shape: a known speaker's second cluster at 0.70 to 0.83 that the purity gate refuses.

### Reproducing older batches

The planner constants and the channel defaults both changed, so the batches this page cites by
seed (the seed-91 roster batch, the seed-95 and seed-96 blend batches) no longer rebuild byte
for byte. `--device-switch 0 --enroll-switch 0` restores only the channel half; the plans
differ anyway. The last rebuild of seed 91 is in `/tmp/m80-rebuild`, outside the repo.

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

## Results (v1.13.0 and v1.13.1, placed-voice absorb)

v1.13.0 replaced the placed-voice absorb's per-turn purity gate with a seconds-weighted talk-share
gate (#77), so a known speaker's second cluster is no longer refused when its short turns score
low. Re-scoring the held-out sets showed that gain, and one regression: with half the roster
enrolled, an **unenrolled** speaker who sounds like an enrolled one now took that person's name.
Talk share cannot see this case. When the real speaker is not enrolled, every turn's best known
voice is the lookalike, so the share reads near 1.0. v1.13.1 adds a sibling margin: the cluster
must sit at least 0.035 closer to the candidate's own cluster in this recording than to the
candidate's voiceprint.

To find more lookalike cases than the original subsets held, val-mixed, val-hard, val-cameo
and the two #77 repro batches were re-run with two more enrolled halves
(`run.py --subset-salt a|b`). Over all those runs plus the tuning sets, the placed-voice absorb
fired on 19 distinct clusters. In 5 of them a stranger took a lookalike's name (9–75 s of talk
each), and they scored sibling − voiceprint between −0.017 and +0.028. The other 14 were true
splits; 11 scored +0.042 to +0.114, and the 3 below the margin are cameo fragments of 4–21 s.
The margin was picked on these same 19 clusters. The held-out same-accent roster batch (6
meetings of 20–45 minutes) had no lookalike case. It had 3 true splits: 2 were absorbed, and
Abhinav (86 s, +0.028) was refused, so on this data the two groups overlap and the margin trades
a few unnamed fragments for no wrong names.

Held-out, mean of per-meeting percentages (final = the v1.10.0-era results above; `refs` = all
enrolled, `refs-subset` = half enrolled; blind and `--speakers N` are unaffected by this pass):

| set | mode | DER final → 1.13.0 → 1.13.1 | named wrong final → 1.13.0 → 1.13.1 |
|---|---|---|---|
| val-mixed | refs | 13.4 → 10.3 → 10.3 | 7.6 → 4.3 → 4.3 |
| val-mixed | refs-subset | 10.5 → 15.1 → 10.5 | 3.3 → 7.9 → 3.3 |
| val-hard | refs | 26.0 → 13.1 → 13.1 | 21.0 → 9.0 → 9.0 |
| val-hard | refs-subset | 24.0 → 19.5 → 19.5 | 17.2 → 13.7 → 13.7 |
| val-cameo | refs | 24.8 → 12.7 → 14.1 | 17.0 → 6.2 → 6.1 |
| val-cameo | refs-subset | 21.9 → 13.9 → 15.3 | 12.1 → 5.1 → 5.0 |
| val-long | refs | 35.7 → 7.0 → 7.0 | 31.0 → 2.0 → 2.0 |
| val-long | refs-subset | 7.6 → 7.0 → 7.0 | 1.4 → 1.4 → 1.4 |

The v1.13.1 column comes from re-running every meeting and mode where the v1.13.0 gate made a
placed-voice decision (40 runs); the gate cannot change any other run. Across those 40 runs the
summed named-wrong fraction fell 4.18 → 1.60 and named-right fell 25.70 → 25.10. The cost is
cameo fragments left anonymous (val-cameo DER +1.4 points).

The synthetic voices are harder on this gate than real meetings. The #80 calibration puts
an unenrolled speaker's best cosine to a known voice at 0.17–0.45 on real recordings, below the
0.70 bar where the placed-voice absorb starts. At the time the roster batch's calibration
missed 7 of 21 metrics by more than ±20%; see [Calibrated realistic meetings (#80)](#calibrated-realistic-meetings-80)
for the retuned batch (1 of 20).

## Blends (v1.13.2)

#77's real replay found 2 by-ear blends (two speakers inside one cluster) that v1.13.0 named
after one of them, at talk shares of 0.77 and 0.88. That replay predates v1.13.1's sibling
margin. To reproduce the case, `realistic.py --blends K` inserts K crosstalk episodes per
meeting: two speakers trading 0.5–2.5 s turns for 15–40 s, each hand-off overlapping by
0.1–0.6 s. These are the clusters that reach the placed-voice absorb. Planning uses its own
random stream, so `--blends 0` (the default) plans byte-identical meetings.

```sh
uv run --with numpy python test/diarize_eval/realistic.py --pool POOL.json --out /tmp/b \
    --seed 95 --count 10 --minutes 8-15 --speakers 3-6 --blends 3
```

Two batches (seeds 95 and 96, 22 meetings, `refs` and `refs-subset`) produced 13 placed-voice
decisions on blend clusters (a cluster where no single speaker holds 60% of the talk). The
talk-share and sibling gates refused 9 of them. The other 4 named 3 clusters wrongly:

| cluster | talk | mix | sim − second placed | runner-up share |
|---|---|---|---|---|
| Sage | 107 s | 51% Sage, 49% Coral | +0.314 | 0.17 |
| Bella | 16 s | 59% Bella, 41% Coral | +0.400 | 0.00 |
| Liam (`refs-subset`) | 34 s | 44% Liam, rest unenrolled | none | 0.00 |

Two blend guards were measured against these and against every true split the current gates
name (72 decisions, from the two batches plus a re-run of every earlier meeting with a
placed-voice decision):

- **Centroid margin** (similarity minus the next placed voice's). True splits went as low as
  +0.110 (15 s) and +0.204 (92 s). The two blends above sat higher. The refused blends that
  had a second placed voice were at +0.071 to +0.222, all already caught by the other gates. Any cutoff gives up real names
  and catches none of the 3.
- **Runner-up share** (the other known voice winning the most talk time). Turns inside a blend
  are themselves mixed, so they rarely name the partner. True splits reached 0.14. A 0.15 cutoff
  would catch Sage alone, on a 0.03 gap from one example.

Neither is gated. Both are recorded on every placed-voice `absorb` record (`runner_up`,
`runner_up_share`, `second_placed`, `second_placed_sim`), so a guard can be tuned on real
by-ear labels. The Liam case, where the partner is not enrolled, has no second voice to compare
against.

The #77 reporter's replay of 23 real by-ear clusters on v1.13.1 agrees on the limits. The sibling
margin refused one of the 2 real blends (+0.010). The other blend is partly the candidate's own
voice, so it sits close to the candidate's own cluster (+0.107) and is still named. The margin
also refused a 59 s true split (−0.057), so on real audio it costs more than the cameo
fragments the synthetic sets showed.

## Anonymous runner-up (v1.13.3)

v1.13.3 adds `runner_up_any` / `runner_up_any_share` / `runner_up_any_sim` to the placed-voice
absorb record. These fields cover the reporter's "placed or anonymous" suggestion. Each turn of
the cluster competes against the known voices and against the meeting's other anonymous cluster
centroids. `runner_up_any_sim` is the winning cluster's centroid similarity to the candidate.

The 22 blend meetings and all 24 earlier split runs were re-run on this code (26 runs; real
registry unchanged).

| label | matched | rows | `runner_up_any_share` min / median / max |
|---|---|---|---|
| blend | refused | 9 | 0.00 / 0.12 / 0.71 |
| blend | named | 4 | 0.11 / 0.47 / 0.71 |
| true split | refused | 13 | 0.00 / 0.00 / 0.24 |
| true split | named | 72 | 0.00 / 0.15 / 1.00 |

The raw share does not separate blends from splits. In 58 rows the anonymous winner's best
voice was the candidate, so its recorded similarity is its similarity to the candidate. In 50
of those rows the winner was, by truth, another fragment of the candidate itself (similarity
0.225 to 0.899). A split's turns land on a sibling fragment just as easily as a blend's land on
the partner.

The real partners in the blends sat far from the candidate: Liam/Echo 0.215 (share 0.71),
Nova/Matilda 0.223 (0.45), and Laura/Sage 0.343 (0.12). A rule requiring similarity < 0.40
and share >= 0.30 hits 1 of the 3 wrongly named blends (Liam, refs-subset) and no named split,
for any cutoff from 0.35 to 0.45. That is one catch, from a rule chosen after looking at this
data, so v1.13.3 records the fields and does not gate on them.

The other two named blends cannot be reached this way. In Bella (16 s), the anonymous winner
is another fragment of the Bella voice (0.877). In Sage (107 s, 51/49 with Coral), the
runner-up is the known voice Coral, at a share of 0.17, or 0.11 when the anonymous clusters
also compete.

## Placed runner-up (v1.13.4)

v1.13.4 adds `runner_up_placed` / `runner_up_placed_name` / `runner_up_placed_share` /
`runner_up_placed_sim`, following the reporter's next suggestion. Each turn of the cluster
competes against the known voices and against the in-meeting centroids of the clusters already
named for OTHER voices (the candidate's own clusters never compete). This targets an enrolled
partner whose voiceprint misses the blend turns. `_name` is the voice owning the winning
cluster, and `_sim` is that cluster's centroid similarity to the candidate (null when a known
voice wins outright).

Same 26 runs (real registry unchanged):

| label | matched | rows | `runner_up_placed_share` min / median / max |
|---|---|---|---|
| blend | refused | 9 | 0.00 / 0.07 / 0.71 |
| blend | named | 4 | 0.00 / 0.15 / 0.17 |
| true split | refused | 13 | 0.00 / 0.00 / 0.24 |
| true split | named | 72 | 0.00 / 0.00 / 0.31 |

The field finds the right partner. In 9 of the 13 blend rows the winner is a true speaker of the
blend other than the candidate, in 1 it is a third voice, and in 3 nothing wins a turn. But the
share stays small on the blends that get named (0.00 to 0.17), while 8 of the 72 named true
splits reach 0.10 or more and the highest reaches 0.31: Coral, 15 s, where Sage's cluster sits
0.76 from Coral's voiceprint. So the share alone does not separate them.

The four named blends:

- Bella (16 s, 9/4 with Coral), refs: Coral's cluster wins 0.15 of the time, at centroid
  similarity 0.19. This is the case the field was built for.
- Bella, refs-subset: a Matilda cluster wins 0.09 instead.
- Sage (107 s, 51/49 with Coral): the known voice Coral wins outright, at 0.17.
- Liam (refs-subset): Echo has no placed cluster, so nothing wins. `runner_up_any` already
  shows Echo's anonymous cluster at 0.71.

A rule requiring `_sim < 0.30` and share >= 0.10 would catch 1 named blend and no named split.
That is one catch, from a rule chosen after looking at this data, so v1.13.4 records the fields
and does not gate on them.

## Placed runner-up alone (after v1.13.4)

The #77 reporter's v1.13.4 replay found that `runner_up_placed` is often won by an enrolled voice
that has no cluster in the meeting, which hides how the best placed cluster did. The placed-voice
`absorb` record now also carries `runner_up_placed_alone` / `_name` / `_share` / `_sim`. Each turn
of the cluster competes against ONLY the in-meeting centroids of the clusters already named for
OTHER voices: no voiceprints, and never the candidate's own clusters. The floor (0.40) and the
seconds weighting are the same as `runner_up_placed`. `_sim` is the winning cluster's centroid
similarity to the candidate. All four are null / 0 when no other voice is placed in the meeting.

The 22 blend meetings were rebuilt from the cached clips (seeds 95 and 96, `--blends 3` and
`--blends 4`; every meeting's truth time matched the earlier build) and re-run on this code in
`refs` and `refs-subset` (44 runs). Names, named-correct time and named-wrong time were identical
to v1.13.4 on all 44. The real registry was unchanged.

| label | matched | rows | `runner_up_placed_alone_share` min / median / max | `_sim` min / median / max |
|---|---|---|---|---|
| blend | refused | 9 | 0.00 / 0.83 / 1.00 | 0.28 / 0.42 / 0.63 |
| blend | named | 4 | 0.00 / 0.40 / 0.57 | 0.32 / 0.34 / 0.37 |
| true split | refused | 7 | 0.00 / 0.14 / 0.66 | 0.34 / 0.40 / 0.71 |
| true split | named | 41 | 0.00 / 0.28 / 0.96 | 0.30 / 0.45 / 0.60 |

With no voiceprints competing, any turn scoring 0.40 against some other placed cluster counts, so
shares run much higher than `runner_up_placed` for splits and blends alike. On the three named
blends that have a winner, it is not the true partner: Sage (51/49 with Coral) goes to a third
voice at 0.57, because Coral has no placed cluster there. The two Bella rows go to third voices
at 0.31 and 0.40. In `refs`, Coral does have placed clusters but loses to a third voice's cluster
(0.31). In `refs-subset`, neither partner is placed (0.40).

The reporter's candidate gate (refuse when the placed winner has `_sim` < 0.0 and share >= 0.05,
picked from the one real blend, where `runner_up_placed_sim` was -0.113) fires on no row here, on
either field. No synthetic winner, blend or split, sits below 0.28. The gate would neither catch
a synthetic blend nor cost a synthetic split. So the synthetic set can't test it. The real
replay was not available for this change, so the field is recorded only, and names do not change.
