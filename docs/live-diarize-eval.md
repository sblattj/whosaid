# Live diarization eval

`test/live_diarize_eval.py` scores the speaker labels `whosaid live` shows during a call
against exact ground truth, and next to the retroactive pass (`whosaid transcribe`, diarize,
name) on the same audio (#218). It answers two questions: how often does the live view put
the right name on what was said, and where do the live view and the after-the-call transcript
disagree?

```bash
python3 test/live_diarize_eval.py --fast                       # CI: no network, ~70 s
python3 test/live_diarize_eval.py --fast --only handoff,near_far
python3 test/live_diarize_eval.py --fast --gate --max-gap 5 --baseline-ref last-tag
python3 test/live_diarize_eval.py --list-devices               # which outputs --realtime would allow
python3 test/live_diarize_eval_test.py                         # the eval's own offline test
```

Plain `python3` works: the eval re-runs itself under `uv` with numpy and the pinned
sherpa-onnx when they are missing (`uv run --offline` for `--fast`). It needs the
sherpa-onnx models `whosaid setup` downloads (`diarize_sherpa.py --ensure-models-only`).
`--fast` never downloads anything; `--realtime` lets `uv` fetch `sounddevice` the first time.

## The calls

Every call is built at run time from a small clip bank (`test/fixtures/live_diarize/`,
4.6 MB with the cassettes), so the 42-minute call costs no disk. The clips are ElevenLabs
voices `test/diarize_eval` had already rendered; `test/live_diarize_build.py` trimmed and
copied them (dev only, no new renders, no network). All people are fake:

| voice | who | enrolled |
|---|---|---|
| abel, bea, cyd, dora | four colleagues | yes, in the throwaway `registry.json` |
| bea-far | Bea dialed in by phone: the same clips through diarize_eval's phone codec, speaker EQ and room | (same person as Bea) |
| gus | a guest | no |

`calls.json` lists the calls. Each one reproduces a failure seen on a real call:

| call | the failure | check (live pass) |
|---|---|---|
| `handoff` | one segment held the end of one person's sentence and the start of the next person's reply, and the live view gave all of it to one person | `handoff_acc >= 80%` |
| `backchannel` | a short reply ("exactly", "right") at a turn boundary went on the wrong person | `backchannel_acc >= 75%` |
| `near_far` | one known person showed as a named voice plus an unnamed `them` (near mic, then far mic) | `split_voice = 0`, `unnamed <= 10%` |
| `agree` | the same turn was labeled one way live and another way after the call | `disagree_live_off <= 5%` |
| `long` | a 42-minute call that comes back to topics and people after long stretches (seeded, 9 topics) | none: scored only |

The real-call failures are caught by the checks: with the same eval and cassettes, `lib/` at
v1.41.4 fails all five checks, and `lib/` after #218 passes all five.

## How each pass runs

- **Live (`--fast`).** The call's two legs (mic and app) are fed into `lib/live.py`'s
  `follow()` as growing WAVs, two seconds of audio per poll (a 1x clock, without the
  waiting), with the naming code `whosaid live run` builds. Whisper is replaced by a
  **cassette**: what the local `whisper-large-v3-turbo` returned for exactly those chunks,
  keyed by a hash of the chunk's samples. A miss stops the run with exit 2 and is never
  guessed. It raises a `BaseException`, so `live.py`'s own error handling can't skip past it.
- **Retroactive.** `lib/diarize_sherpa.py` runs on the whole call with the cassette's
  whole-file Whisper JSON (`whisper-large-v3`, the `whosaid transcribe` default) and the same
  registry, then `build_turns`.
- **Scoring.** Both passes are scored per 20 ms frame against the ground truth and printed
  side by side, per call and for the suite.

S-T: a cassette is a recording of a slow or networked call's answers, replayed in tests.
Example: the first run asks Whisper about chunk `3fa1...` and saves the segments. Every later
run reads them back, so CI needs no model and gets the same text.

## Metrics

| metric | meaning |
|---|---|
| `name_acc` | share of enrolled talk time under that person's name |
| `unnamed` | share of enrolled talk time under no name (`them`, `SPEAKER_NN`) |
| `missing` | share of enrolled talk time with no line at all |
| `guest_named` | share of the guest's talk time given an enrolled name |
| `split_voice` | share of enrolled people shown both named and unnamed (5% or more each) |
| `handoff_acc` | handoffs (a reply under 0.3 s after another person) where the last 1.5 s before it and the first 1.5 s of the reply both carry the right name |
| `backchannel_acc` | short replies at a turn boundary under the right name |
| `mixed_p` / `mixed_r` | `mixed` flag precision and recall. Recall is over lines that really hold two people (the second holds 0.3 s or more and 20% of the line). A flag is precise on such a line, or on a line whose label is wrong, since the flag tells the reader not to trust the label |
| `disagree` | talk time where live and retroactive show different labels, split into who was wrong |
| `disagree_live_off` | turns live got wrong while retroactive got them right |
| `ttn` | live only: seconds from a person's first word to their name showing |
| `flips` | live only: label changes of a line that already had a name |

Suite on this branch (`--fast`, macOS arm64):

| metric | live | retro |
|---|---|---|
| correct name (talk time) | 92.0% | 66.7% |
| enrolled but unnamed | 0.5% | 15.6% |
| guest given a name | 1.0% | 4.4% |
| people split named + unnamed | 0.0% | 16.7% |
| handoffs right | 97.3% | 35.4% |
| backchannels right | 68.4% | 11.6% |
| mixed flag precision / recall | 75.0% / 62.5% | - / 0.0% |
| turns live got wrong, retro right | 0.3% | |

The calls switch speakers often and keep turns short, which is the live pass's job and the
hard case for clustering a whole file, so read the retro column as "the same audio after the
call", not as how good the retroactive pass is in general: `test/diarize_eval` measures that.

## The gate

`--gate` turns the numbers into an exit code for CI and release checks:

- live `name_acc` more than `--max-gap` points (default 5) under the retroactive pass fails;
- with a baseline, any gated live metric more than `--tolerance` points (default 0.5) worse
  than the baseline fails. Gated: `name_acc`, `handoff_acc`, `backchannel_acc` (higher is
  better); `unnamed`, `split_voice`, `guest_named`, `disagree_live_off` (lower is better);
- a baseline scored over other calls fails, so a changed `calls.json` can't hide a drop.

The baseline is either `--baseline scores.json` (a saved `--out` file) or
`--baseline-ref REF`. `--baseline-ref last-tag` resolves the newest release tag
(`git describe --tags --abbrev=0`), checks it out detached in a throwaway worktree, copies
this branch's `test/` in, runs the same eval on that tag's `lib/` and removes the worktree.
Each output line says which baseline it compared against, e.g.
`baseline lib/ at v1.41.4: live name_acc 76.7%, handoffs 69.4%, backchannels 3.2%`.

Exit codes: 0 pass, 1 a check or the gate failed, 2 setup (missing models, a cassette miss,
a `--realtime` capture that didn't start), 3 `--realtime` refused the device.

`.github/workflows/live-eval.yml` runs the eval's unit test and
`--fast --gate --max-gap 5 --baseline-ref last-tag` on pull requests that touch `lib/` or
the eval, with the sherpa models cached.

## Re-recording the cassette

Changing what `live.py` sends to Whisper (chunk sizes, the handoff re-transcription) changes
the chunk hashes, and `--fast` exits 2 on the first miss. Re-record on a Mac with the local
models:

```bash
python3 test/live_diarize_eval.py --record [--only handoff]
```

`--record` runs `whisper-large-v3-turbo` (both with and without word times, so flipping
`--split-words` replays too) for the live chunks it's missing, and
`whisper-large-v3` for the whole-file retroactive JSON. It prunes keys no chunk used, then
writes `test/fixtures/live_diarize/cassette/<call>.json`. Nothing leaves the Mac.

## `--realtime`: through the capture path

```bash
python3 test/live_diarize_eval.py --realtime --device "BlackHole 2ch" --only agree [--out rt.json]
```

This plays one call at 1x into a loopback output device and captures the same device's
input side into the two growing legs `whosaid record --app` writes (`app.wav` with the call,
`mic.wav` silent, 16 kHz mono). `whosaid live run --once --capture <that folder>` follows it
with real Whisper and the live naming code, against the throwaway registry. Then it scores
the live lines next to the retroactive pass on the same call and prints them side by side,
like `--fast`.

- **The capture.** PortAudio (`sounddevice`, via `uv`) reads the loopback's input at the
  device's clock. ffmpeg's avfoundation input was tried first and dropped about 12% of the
  samples (8 s of wall time gave 6.8 s of audio), which shrank the live timeline.
- **Only its own capture.** `live run` is always pinned with `--capture` to the folder this run
  made, so a real call that autorecord is capturing at the same time is never followed. With
  `--app <bundle id>` the capture is `whosaid record --app <app> --no-mic` instead (the app
  whose audio ScreenCaptureKit takes; it needs an app bundle, so it doesn't work from a
  process with none, like a background agent). The eval takes the one capture folder that
  recorder made, and stops with exit 2 when it isn't exactly one.
- **The call's clock.** Live lines carry the seconds since the capture started. The call's
  start inside the capture is found by cross-correlating the call's first 10 s with the
  captured audio (a loopback gives back nearly the same samples). If that lands more than 3 s
  from the wall clock, the wall clock is used and it says so.
- **Line spans.** A live line has a start, not an end, so each `them` line is scored from its
  start to the next line's start (the last one gets about 65 ms per character, at most 30 s).
  Gaps between turns count against the line before, so `--realtime` scores read a few points
  under `--fast`, which knows each chunk's end (`agree`: 90.6% live name accuracy against 94.0%).

On `agree` through BlackHole 2ch (macOS arm64, real `whisper-large-v3-turbo`), the call
started 3.19 s into the capture and live showed 25 lines:

| metric | live | retro |
|---|---|---|
| correct name (talk time) | 90.6% | 88.2% |
| enrolled but unnamed | 0.4% | 0.0% |
| guest given a name | 0.0% | 0.3% |
| handoffs right | 100.0% (n=4) | 75.0% |
| backchannels right | 66.7% (n=3) | 33.3% |
| turns live got wrong, retro right | 0.0% | |

It only plays to a device that passes every one of these, read straight from CoreAudio
(read-only, nothing is opened):

- the name matches exactly one device, which has output streams;
- the name starts with a loopback driver's name (`BlackHole`, `Loopback Audio`,
  `Soundflower`, `VB-Cable`) and does not mention speakers, headphones, AirPods, `+`,
  multi-output or aggregate;
- CoreAudio reports it as a virtual device (transport `virt`).

So it never plays to speakers, headphones, a meeting app's virtual device (that would feed a
live meeting), or a multi-output device that also goes to speakers. The check runs again
right before ffmpeg starts, and ffmpeg gets the device index CoreAudio has for that name at
that moment. `--list-devices` prints each device with ALLOW or REFUSE and why. A refused
device exits 3 before anything loads.

S-T: a loopback device is a fake sound card. Audio played into it comes back out as an
input, never out of a speaker. Example: BlackHole 2ch.

## Rebuilding the clip bank

```bash
uv run --with numpy --with sherpa-onnx==1.13.8 python test/live_diarize_build.py
```

This reads `~/.cache/whosaid/diarize-eval/pool.json` (the `test/diarize_eval` voice pool)
and rewrites `clips/`, `bank.json` and `registry.json`. The calls, the cassettes and the
eval read only what it wrote.
