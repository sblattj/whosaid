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

<!-- RESULTS -->
