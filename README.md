# whosaid

Local, speaker-attributed transcription for Apple Silicon — who said what, on your Mac, nothing
leaves the machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform: macOS Apple Silicon](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)

![whosaid demo — speaker-attributed transcription on the terminal](docs/demo.gif)

**whosaid** pairs **Whisper** speech-to-text with **speaker diarization** to turn a meeting,
interview, call, or podcast recording into a transcript that says *who spoke when* — running fully
**offline and on-device** on an Apple Silicon Mac, with no cloud service, no API keys, and no Hugging
Face token. Think of it as `whisper` + speaker labels + voice-based speaker recognition, in one
command.

**Example output** (`meeting.speakers.txt`):

```text
[00:00:04] Alice: Thanks for jumping on, I know it's late for you.
[00:00:11] SPEAKER_01: No problem at all, happy to make it work.
[00:00:19] Alice: Let's start with the roadmap for next quarter.
[00:00:27] SPEAKER_01: Sounds good, I've got a few updates on that.
```

## Why whosaid

- **Meetings, interviews, calls, podcasts** — get a transcript where every turn is attributed to a
  person, not just a wall of text.
- **Privacy by construction** — audio, text, and voice embeddings never leave your Mac. There's no
  cloud step to opt out of, because there isn't one.
- **Your name on your own lines** — a one-time ~45s voice enrollment teaches whosaid your voice, so
  your turns read as your name instead of `SPEAKER_00`.
- **Tells you how many people spoke** — the number of distinct speakers is auto-detected and
  reported up front, with per-speaker turn counts and talk time.
- **Speaker cards to identify who's who** — for every speaker, whosaid writes a card of that voice's
  most representative snippets, so you can read a few lines and know who was talking.
- **Remembers people you name** — identify a speaker once with `whosaid relabel`, and their
  voiceprint is saved to a private local registry so they're auto-named in every future transcript.
- **Fast on long recordings** — recordings over ~15 min are diarized in parallel windows and
  stitched back into consistent speakers by voiceprint, so an 80-minute meeting is minutes, not
  tens of minutes, recovering the same speakers as a single-pass run.
- **No accounts, no API keys, no Hugging Face token** — every model comes from an open, ungated
  source.
- **Usable from an AI agent, too** — `whosaid mcp` exposes transcribe, relabel, and doctor (among
  others) as MCP tools for Claude Code, Claude Desktop, and other MCP clients, with the same
  local-only guarantee as the CLI.

## How it compares

**Looking for a MacWhisper, whisperX, or aTrain alternative?** If you want speaker-attributed
transcription that runs fully offline on Apple Silicon and can put real *names* on voices — not just
`SPEAKER_00` labels — that's the gap whosaid fills. Here's how it stacks up:

| | whosaid | whisperX | plain mlx-whisper | cloud transcription APIs |
|---|---|---|---|---|
| Speaker labels | Yes | Yes | No | Varies by provider |
| Names speakers by voice | Yes (enrollment) | No | No | No |
| Remembers speakers across meetings | Yes — persistent local voiceprint registry | No | No | No |
| Runs fully offline | Yes | Partial — needs a gated model download | Yes | No |
| Parallel diarization on long audio | Yes | No | — (no diarization) | Varies by provider |
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
| `whosaid relabel <base> SPEAKER_02=Jane …` | Put real names on clusters after reading the speaker cards. Rewrites the transcript + cards and saves each named voiceprint to the local registry for future transcripts. No re-transcription. |
| `whosaid relabel <base> --auto` | Re-apply naming to an existing transcript with no assignments: re-runs registry matching + the absorb pass over the cached sidecar and rewrites the transcript + cards. Picks up voices enrolled after the transcript was made, and folds phantom cluster splits of one person into a single speaker. No re-transcription, no re-diarization. In a meeting workspace the base is `transcript`. |
| `whosaid doctor` | Read-only environment report. |

## Meeting workspaces

One-off transcriptions are files; a recurring meeting series is a corpus. The meeting-workspace
layer gives that corpus a home: every recording is transcribed into a dated folder, each meeting
can carry generated action items, and one roll-up produces the index, the audit, and a living
action-item list across all of them — still entirely offline.

### `whosaid ingest` — a batch into dated folders

```bash
whosaid ingest weekly/*.m4a --into ./meetings --folder-by created \
  --tz America/Los_Angeles --action-items
```

Every file is transcribed with the full set of `whosaid <audio>…` flags passed through, into
`meetings/YYYY-MM-DD-HHMM/`. The timestamp comes from the recording's own container
`creation_time` rendered in `--tz` (default UTC), falling back to the file's mtime — so folder
order reflects when meetings actually happened, not when you got around to copying the files.
Ingest is idempotent by source sha256: re-running a batch never re-transcribes or duplicates a
recording.

With `--action-items`, each meeting also gets an `action-items.md`. Generation is pluggable: the
hook command receives the speaker-labeled transcript on stdin, plus `WHOSAID_SPEAKERS` and
`WHOSAID_TRANSCRIPT_PATH` in its environment, and whatever it writes to stdout becomes the
markdown. Pass it per-run with `--hook CMD`, or set `WHOSAID_ACTION_ITEMS_HOOK` once. With no
hook, a skeleton is written instead and everything stays offline. Example hook — illustrative
only; any command that turns stdin into markdown works:

```bash
#!/bin/sh
# Drafts action items with a local Ollama model.
ollama run llama3.2 "List this meeting's action items as markdown bullets (Owner: task):"
```

### `whosaid roll-up` — index, audit, and the action-item corpus

```bash
whosaid roll-up ./meetings --action-items
```

- **Coverage index** — `_INDEX.md` holds one row per meeting (created date, duration, and whether
  it is transcribed, diarized, and has action items), plus a nothing-missing audit that flags
  orphan directories and stale manifest entries, and a recurring-topics section that surfaces
  themes appearing across meetings. Both output paths are overridable with `-o` and
  `--action-items-out`.
- **Action-item corpus** — with `--action-items`, `_ACTION-ITEMS.md` deduplicates items across
  meetings (by text similarity; threshold `--similarity-threshold`, 0.5–1.0, default 0.82) and
  groups them by owner, then status. Ids are stable (`AI-001`… and never renumber), each item
  carries `first_seen`/`last_seen` dates and its occurrence list, and open/ongoing/resolved
  statuses survive re-runs — so the corpus reads as living history across the series, not a
  per-meeting snapshot. Items can carry a free-form type, rendered in parens after the status,
  and pairs scoring just under the threshold are surfaced in a _Possible duplicates (review)_
  section at the end.

Roll-up is incremental and append-only by default: re-running with nothing new writes nothing.
`--rebuild` is the escape hatch — it resets the manifest and corpus and regenerates both from the
folders on disk.

State is two plain JSON files in the workspace directory, `_workspace.json` (the manifest) and
`_action-items.json` (the corpus; it also records the `similarity_threshold` in effect). Both are
safe to read and hand-edit — marking an item `resolved` by hand is the intended way to close one
the extractor phrased wrong. Hand edits made directly in `_ACTION-ITEMS.md` are folded back on
the next roll-up and survive re-runs: statuses, types, retitles, and `(merged AI-NNN)` merge
annotations (the merged item stays at its id, rendered collapsed as `[merged → AI-NNN]`). Only
`--rebuild` discards them.

## Use it from an AI agent (MCP)

whosaid's local, private, GPU transcription and speaker diarization are also exposed as MCP tools,
so any MCP client — Claude Code, Claude Desktop, and others — can call them directly instead of
shelling out to the CLI. Launch is `whosaid mcp`, a stdio server that needs only `uv`, which whosaid
already requires; audio never leaves the machine, exactly as with the CLI.

Add it to your MCP client config:

```json
{
  "mcpServers": {
    "whosaid": { "command": "/ABSOLUTE/PATH/TO/whosaid", "args": ["mcp"] }
  }
}
```

`command` is the path to the `whosaid` script itself — the checkout's `./whosaid`, or the installed
`~/.local/bin/whosaid` symlink.

| Tool | What it does |
|---|---|
| `whosaid_transcribe` | Transcribes + diarizes an audio file and writes the labeled transcript, speaker cards, and a sidecar for relabeling. |
| `whosaid_relabel` | Names `SPEAKER_NN` clusters and remembers them — saved to the local registry and auto-applied to every future transcript. |
| `whosaid_list_speakers` | Read-only: lists enrolled voices and registry names already known. |
| `whosaid_doctor` | Read-only readiness check — models cached, deps present, mic/audio devices — run this first when a transcribe fails. |
| `whosaid_enroll_from_file` | Enrolls a named voice from an existing audio clip, no mic needed. |

`enroll` and `record` (microphone capture) stay CLI-only — they need an interactive terminal and
Microphone permission. The first `whosaid_transcribe` call downloads ~1.5 GB of models; call
`whosaid_doctor` first to check readiness.

### Key flags (on `whosaid <audio>…`)

| Flag | Meaning |
|---|---|
| `-o, --outdir DIR` | Output directory (default: alongside the input file). |
| `-m, --model NAME` | Whisper model to use. |
| `--accurate` | Use the full `large-v3` model instead of the default `large-v3-turbo`. |
| `-l, --lang LANG` | Force the transcription language. |
| `-f, --format FMT` | Output format: `txt`, `srt`, `vtt`, `tsv`, `json`, or `all`. |
| `-n, --name NAME` | Override the output base name (single input only; default: derived from the input filename). |
| `--speakers N` | Hint the expected number of speakers. On long recordings this is recommended — auto-detect can over-segment. |
| `-j, --jobs N` | Parallel diarization workers for long audio (default: auto, ~cores−2, capped at 8). |
| `--chunk-seconds S` | Window length for parallel diarization (default: auto — about `--jobs` windows, min 300s). |
| `--no-chunk` | Diarize the whole file in a single pass (disable parallel chunking). |
| `--match-threshold F` | Cosine similarity a known voice must reach before it may claim a cluster (alias `--ref-threshold`). Default `0.50`; a cluster whose best candidate scores below `F` keeps its anonymous `SPEAKER_NN` label rather than taking a low-confidence name. Raise it (e.g. `0.6`) if you see wrong names, lower it to catch more. |
| `--absorb-threshold F` | Cosine similarity at which a *still-unnamed* cluster is folded into a known voice, merging phantom splits of one person. Default `0.85`. |
| `--no-diarize` | Skip diarization; write the plain transcript only. |

### Environment variables

| Variable | Purpose |
|---|---|
| `WHOSAID_MODEL` | Default Whisper model, overridden by `-m`. |
| `WHOSAID_LANG` | Default transcription language, overridden by `-l`. |
| `WHOSAID_VOICE_REFS` | Override the directory of enrollment voice clips (default: `voices/`). |
| `WHOSAID_SPEAKER_DB` | Local speaker registry of named voiceprints (default: `~/.config/whosaid/speakers.json`). Private, never pushed. |
| `WHOSAID_MATCH_THRESHOLD` | Default registry/reference match threshold, overridden by `--match-threshold` (default: `0.50`). |
| `WHOSAID_ABSORB_THRESHOLD` | Default absorb-pass threshold, overridden by `--absorb-threshold` (default: `0.85`). |
| `DIARIZE_EMB_NAME` | Speaker-embedding model. Default is NeMo `nemo_en_titanet_small.onnx` (English-native, ~2.5× faster than ERes2Net in sherpa's benchmark). Alternatives from the same release: `3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx` (English ERes2Net) or `…_zh-cn_…` for Mandarin. Registry voiceprints are keyed by model, so switching re-enrolls speakers. |
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
who's speaking when, a speaker-embedding model (NeMo TitaNet-small by default, configurable via
`DIARIZE_EMB_NAME`) embeds each turn, and clustering (optionally hinted by
`--speakers N`) groups turns into speakers. Both diarization models are small (~30 MB total), ungated
GitHub releases — no Hugging Face token required — cached locally in `~/.cache/sherpa-diarization/`.
Finally, every clip in `voices/` — plus every voiceprint in your local registry — is embedded the
same way and matched to a cluster by cosine similarity (a match at or above `--match-threshold`,
default 0.50, names the cluster);
unmatched clusters keep a `SPEAKER_00`-style label. Aside from the one-time model downloads,
everything runs in ephemeral `uv` environments, so there's no persistent Python install left behind
on your machine.

**Long recordings run in parallel.** For audio over ~15 minutes, diarization splits into
non-overlapping time windows that are segmented and embedded concurrently across CPU workers, then a
single global clustering pass over every turn's voiceprint recovers speakers that stay consistent
across window boundaries. Because the clustering sees all turns at once, it recovers the same
speakers as a single-pass run while finishing several times faster. Pass `--no-chunk` to force a single pass, or
`--jobs`/`--chunk-seconds` to tune it.

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
| `<base>.speaker-cards.txt` | One card per speaker with turn count, talk time, and representative snippets — read it to identify who each `SPEAKER_NN` is, then name them with `whosaid relabel`. |
| `<base>.diarization.json` | Cached segments + per-cluster voiceprints, so `whosaid relabel` can rename and persist speakers without re-diarizing. Also carries `registry_matches` and `source` (below). |

The sidecar's two machine-readable extras, so a consumer never has to scrape stderr or shell out to
`ffprobe`:

| Sidecar key | Contents |
|---|---|
| `registry_matches` | One record per naming decision, **including near-misses**: `{"cluster": "SPEAKER_03", "name": "Alice", "similarity": 0.919, "threshold": 0.5, "matched": true, "pass": "registry"}`. `pass` is `registry`, `ref`, or `absorb`; `matched: false` means the cluster stayed `SPEAKER_NN` because `similarity < threshold`. Refreshed by `whosaid relabel --auto`, and also printed in the transcribe JSON line. |
| `source` | Recording provenance: `{"path": "/abs/path.m4a", "duration_seconds": 1834.2, "creation_time": "2026-09-14T18:02:11.000000Z"}`. `creation_time` is the container tag, or `null` when the file carries none. |

**Match confidence and the threshold.** Auto-naming only asserts a name when the cluster's cosine
similarity to a known voiceprint reaches `--match-threshold` (default `0.50`); below it the cluster
keeps its `SPEAKER_NN` label — see the `>= ref_threshold` guards in `name_clusters()`
(`lib/diarize_sherpa.py`). The default was raised from `0.40` to `0.50` because on real meeting
audio TitaNet-small produced wrong assertions in the 0.40–0.53 band, while genuine same-speaker
matches score far higher — in the end-to-end test the enrolled reference matches its cluster at
**0.986**, against **0.194** for the nearest stranger, so 0.50 sits in a wide empty gap. Use
`registry_matches` to see exactly how close every near-miss came, then lower the threshold
deliberately if a real speaker is being missed.

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
- **Speakers show up as `SPEAKER_00` / `SPEAKER_01` instead of a name.** No enrolled voice or
  registry entry matched closely enough. Read `<base>.speaker-cards.txt` to tell who each cluster is,
  then run `whosaid relabel <base> SPEAKER_01=Name` — this labels them and remembers them for next
  time. (Enrollment via `whosaid enroll <Name>` still works too.) Naming uses a cosine-similarity
  threshold (`--match-threshold`, default 0.50), so a short or noisy sample can fall just short of
  it. Check `registry_matches` in `<base>.diarization.json` for the exact similarity of every
  near-miss, then lower the threshold deliberately if a real speaker is being missed.
- **The same person shows up as two speakers (phantom split), or a known voice stays
  `UNIDENTIFIED`.** On long recordings the diarizer can split one voice across several clusters.
  A registry/enrolled voice names its single closest cluster, so the extra clusters used to stay
  unnamed. whosaid now runs an *absorb pass*: any still-unnamed cluster whose voiceprint is within
  `--absorb-threshold` (default 0.85) of a known voice is folded into that person, and the speaker
  cards merge those clusters into one card. To apply this to a transcript you already have, run
  `whosaid relabel <base> --auto` — it re-names from the registry + absorb pass with no
  re-transcription.
- **Distinct people get merged into one speaker (or the count is too low).** The speaker-embedding
  model must match the spoken language. whosaid defaults to an English-native model (NeMo
  TitaNet-small); on English audio the Mandarin-trained model cannot tell similar voices apart and
  collapses them. For
  predominantly Mandarin audio, set `DIARIZE_EMB_NAME` to the `…zh-cn…` model from the same release.
- **The speaker count looks one too high, with a cluster that has ~1 second of speech.** Forcing
  `--speakers N` too high can carve a phantom cluster out of crosstalk. Omit `--speakers` to
  auto-detect, which is usually more accurate.

## Testing

`./test/e2e.sh` is a fully offline smoke test: it synthesizes a two-speaker dialog with two macOS
`say` voices, builds a one-clip voice enrollment for one of them, and runs the real transcribe +
diarize + name pipeline against it end to end — then asserts the speaker-labeled transcript names
the enrolled speaker and labels the other speaker distinctly. Run `./bootstrap.sh` once first so the
models are cached locally; the test itself makes no network calls.

## License

MIT — see [LICENSE](LICENSE).
