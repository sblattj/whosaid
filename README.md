# whosaid

Local, speaker-attributed transcription for Apple Silicon — who said what, on your Mac, nothing
leaves the machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform: macOS Apple Silicon](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)

<!-- demo -->

**Example output** (`meeting.speakers.txt`):

```text
[00:00:04] Alice: Thanks for jumping on, I know it's late for you.
[00:00:11] SPEAKER_01: No problem at all, happy to make it work.
[00:00:19] Alice: Let's start with the roadmap for next quarter.
[00:00:27] SPEAKER_01: Sounds good, I've got a few updates on that.
```

## Why whosaid

- **Meetings, interviews, calls** — get a transcript where turns are attributed to a person, not
  just a wall of text.
- **Privacy by construction** — audio, text, and voice embeddings never leave your Mac. There's no
  cloud step to opt out of, because there isn't one.
- **Your name on your own lines** — a one-time ~45s voice enrollment teaches whosaid your voice, so
  your turns read as your name instead of `SPEAKER_00`.
- **No accounts, no API keys, no Hugging Face token** — every model comes from an open, ungated
  source.

## How it compares

| | whosaid | whisperX | plain mlx-whisper | cloud transcription APIs |
|---|---|---|---|---|
| Speaker labels | Yes | Yes | No | Varies by provider |
| Names speakers by voice | Yes (enrollment) | No | No | No |
| Runs fully offline | Yes | Partial — needs a gated model download | Yes | No |
| Needs an account / token | No | Yes — Hugging Face token for gated pyannote models | No | Yes — API key |
| Install weight | `ffmpeg` + `uv`, ephemeral environments | `torch` + `pyannote` + the full HF stack | `mlx-whisper` only | None (network client only) |

## Quickstart

```bash
git clone https://github.com/sblattj/whosaid && cd whosaid   # get the code
./bootstrap.sh                                                # check deps, download models, install ~/.local/bin/whosaid
whosaid enroll                                                # ~45s reading a printed passage — teaches whosaid your voice
whosaid path/to/meeting.m4a                                   # transcribe + diarize + label -> meeting.speakers.txt (and friends)
```

If `~/.local/bin` is not on your shell's `PATH`, add it or invoke the installed command by its
absolute path. `WHOSAID_INSTALL_DIR=/another/bin ./whosaid install` selects another install
directory. The installed command is a symlink to the checkout, so updating the checkout updates the
command without copying or duplicating the implementation.

## Commands

| Command | What it does |
|---|---|
| `./bootstrap.sh [--yes]` (also `whosaid setup`) | Capability check, dependency install, model pre-download, and command installation. Idempotent — safe to re-run. |
| `whosaid install` | Install/update the command symlink in `~/.local/bin` (or `WHOSAID_INSTALL_DIR`). Refuses to replace an unrelated command. |
| `whosaid enroll [Name]` | Records ~45s from the mic reading a printed passage, saves `voices/<Name>.wav`. |
| `whosaid record [--label L]` | Foreground mic capture to `recordings/<timestamp>[-label].m4a`, then transcribes automatically. |
| `whosaid <audio>… [flags]` | The default command: transcribe + diarize + label one or more audio files. |
| `whosaid doctor` | Read-only environment report. |

### Key flags (on `whosaid <audio>…`)

| Flag | Meaning |
|---|---|
| `-o, --outdir DIR` | Output directory (default: alongside the input file). |
| `-m, --model NAME` | Whisper model to use. |
| `--accurate` | Use the full `large-v3` model instead of the default `large-v3-turbo`. |
| `-l, --lang LANG` | Force the transcription language. |
| `-f, --format FMT` | Output format: `txt`, `srt`, `vtt`, `tsv`, `json`, or `all`. |
| `-n, --name NAME` | Override the output base name (single input only; default: derived from the input filename). |
| `--speakers N` | Hint the expected number of speakers to the diarization clustering step. |
| `--no-diarize` | Skip diarization; write the plain transcript only. |

### Environment variables

| Variable | Purpose |
|---|---|
| `WHOSAID_MODEL` | Default Whisper model, overridden by `-m`. |
| `WHOSAID_LANG` | Default transcription language, overridden by `-l`. |
| `WHOSAID_VOICE_REFS` | Override the directory of enrollment voice clips (default: `voices/`). |
| `WHOSAID_REC_DEVICE` | avfoundation input device used by `record` and `enroll`. |
| `WHOSAID_INSTALL_DIR` | Command install directory used by `whosaid install` (default: `~/.local/bin`). |
| `HF_HOME` | Hugging Face cache location (where the Whisper model lands). |
| `SHERPA_DIARIZE_CACHE` | Diarization model cache location (default: `~/.cache/sherpa-diarization`). |

## How it works

```
 audio file
     |
     v
 MLX Whisper (Metal GPU)  ------------->  <base>.txt / .srt / .vtt / .tsv / .json
     |
     v
 sherpa-onnx diarization (CPU)  -------->  <base>.rttm
     |
     v
 cosine-match vs voices/*.wav  --------->  <base>.speakers.txt
```

Transcription and diarization run as two independent local stages that get merged at the end.
Transcription uses MLX Whisper (`mlx-community/whisper-large-v3-turbo` by default, or the full
`large-v3` with `--accurate`) on the Mac's GPU via Metal, through a hallucination-hardened decode
path: the temperature-fallback ladder stays enabled, `condition_on_previous_text` is turned off, and
a hallucination-silence threshold keeps dead air from turning into repeated-token filler. Diarization
runs on the CPU via sherpa-onnx offline diarization: pyannote's `segmentation-3.0` ONNX model finds
who's speaking when, 3D-Speaker's ERes2Net embeds each turn, and clustering (optionally hinted by
`--speakers N`) groups turns into speakers. Both diarization models are small (~30 MB total), ungated
GitHub releases — no Hugging Face token required — cached locally in `~/.cache/sherpa-diarization/`.
Finally, every clip in `voices/` is embedded the same way and matched to a cluster by cosine
similarity (a match at or above 0.40 names the cluster); unmatched clusters keep a `SPEAKER_00`-style
label. Aside from the one-time model downloads, everything runs in ephemeral `uv` environments, so
there's no persistent Python install left behind on your machine.

## Output files

| File | Contents |
|---|---|
| `<base>.txt` | Plain transcript. |
| `<base>.srt` | SubRip subtitles. |
| `<base>.vtt` | WebVTT subtitles. |
| `<base>.tsv` | Tab-separated segments with timestamps. |
| `<base>.json` | Full Whisper segment output. |
| `<base>.rttm` | Raw diarization turns, standard RTTM format. |
| `<base>.speakers.txt` | Speaker-labeled transcript: Whisper text merged with diarization turns and enrollment names. |

## Troubleshooting

- **Enroll/record produces silence.** This is almost always a macOS microphone permission problem:
  go to System Settings → Privacy & Security → Microphone and grant access to your terminal app.
  macOS feeds an unauthorized app *silent zeros* instead of an error, so whosaid detects this by
  checking the captured volume rather than trusting a clean exit code.
- **Doesn't run on my Intel Mac.** MLX is Apple-Silicon-only, so whosaid requires an `arm64` Mac.
- **First run is slow.** The first `./bootstrap.sh` (or first transcribe, if you skip it) downloads
  the Whisper model (~1.5 GB) and the diarization models (~30 MB). Every run after that uses the
  local cache.
- **Long recordings degrade into repeated text.** This is Whisper's well-known
  hallucination/repetition-collapse failure mode, most likely on long or low-signal audio. It's why
  whosaid calls the `mlx-whisper` library directly (`lib/transcribe_mlx.py`) instead of the bare
  CLI: the bare CLI's single-temperature default is exactly the configuration that lets this happen,
  whereas the library call keeps the temperature-fallback ladder, disables conditioning on previous
  text, and applies a hallucination-silence threshold.
- **Speakers show up as `SPEAKER_00` / `SPEAKER_01` instead of a name.** No enrolled voice matched
  closely enough. Enroll the missing person (`whosaid enroll <Name>`), or check that
  `WHOSAID_VOICE_REFS` (or `voices/`) points at the clip you expect — naming uses a cosine-similarity
  threshold (0.40), so a short or noisy enrollment clip can fall just short of it.

## Testing

`./test/e2e.sh` is a fully offline smoke test: it synthesizes a two-speaker dialog with two macOS
`say` voices, builds a one-clip voice enrollment for one of them, and runs the real transcribe +
diarize + name pipeline against it end to end — then asserts the speaker-labeled transcript names
the enrolled speaker and labels the other speaker distinctly. Run `./bootstrap.sh` once first so the
models are cached locally; the test itself makes no network calls.

## License

MIT — see [LICENSE](LICENSE).
