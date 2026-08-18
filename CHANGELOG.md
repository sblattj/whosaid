# Changelog

All notable changes to whosaid are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.2] — 2026-08-18

Documentation-accuracy patch — no change to the transcription or diarization
pipeline. A `/cbm-atlas` architecture audit confirmed the README already matches
the code; the drift was in the design doc, now corrected.

### Documentation

- **`docs/design.md` refreshed to the current architecture.** The default
  speaker-embedding model is corrected to NeMo TitaNet-small (it had named
  3D-Speaker ERes2Net, now listed only as an opt-in `DIARIZE_EMB_NAME`
  alternative). Added the `install` and `relabel` subcommands, the persistent
  local speaker registry, `.speaker-cards.txt`, the `.diarization.json` sidecar,
  and the parallel long-audio path so the doc matches the implementation.
- **README wording tightened.** The long-audio parallel path now says it
  "recovers the same speakers" as a single-pass run rather than "the same
  result" — the whole-file and chunked paths use different clustering algorithms,
  so bit-identical output isn't guaranteed.

### Build

- `bootstrap.sh`'s disk-space check now distinguishes the ~4 GB recommended free
  space from the ~1.5 GB Whisper model download.
- The generated `/cbm-atlas` output directory (`.cbm-atlas/`) is now gitignored.

## [1.0.1] — 2026-08-17

Documentation and test-coverage patch — no change to the transcription or
diarization pipeline.

### Documentation

- **README tuned for discoverability.** An above-the-fold summary now names the
  terms people search for — speaker diarization, Whisper speech-to-text, "who
  spoke when," offline/on-device, voice-based speaker recognition — and the
  comparison section opens with a "MacWhisper / whisperX / aTrain alternative"
  framing.

### Tests

- **Expanded `test/e2e.sh` coverage.** Asserts every output format and artifact
  from a run (`.srt`, `.vtt`, `.tsv`, `.json`, `.rttm`, the speaker-cards file,
  and the `.diarization.json` sidecar); smoke-tests `whosaid doctor`'s
  embedding-model and registry report; and exercises `--no-diarize` (plain
  transcript, no `.speakers.txt`).

## [1.0.0] — 2026-08-17

First stable release. whosaid is local, speaker-attributed transcription for
Apple Silicon: it turns an audio file into a transcript where every turn is
attributed to a person — who said what — with nothing ever leaving your Mac.

### Highlights

- **Speaker-attributed transcripts.** MLX Whisper (Metal GPU) transcribes,
  sherpa-onnx diarization (CPU) finds who spoke when, and the two are merged into
  `<base>.speakers.txt` with per-turn speaker labels and timestamps.
- **Names speakers by voice.** A one-time ~45s enrollment (`whosaid enroll`)
  teaches whosaid your voice, so your turns read as your name instead of
  `SPEAKER_00`.
- **Persistent cross-meeting speaker identity.** Identify someone once with
  `whosaid relabel` and their voiceprint is saved to a private local registry
  (`~/.config/whosaid/speakers.json`), so they are auto-named in every future
  transcript — a persistence most transcription tools don't offer.
- **Tells you how many people spoke.** The distinct-speaker count is
  auto-detected and reported up front, with per-speaker turn counts and talk
  time.
- **Speaker cards.** For each speaker, `<base>.speaker-cards.txt` holds a few
  representative snippets so you can read a couple of lines and know who each
  cluster is — then name them with `whosaid relabel`.
- **Fast on long recordings.** Audio over ~15 min is diarized in parallel time
  windows and stitched back into consistent speakers by a single global
  clustering pass over every turn's voiceprint — several times faster, with the
  same result as a single-pass run.
- **No accounts, no API keys, no Hugging Face token.** Every model comes from an
  open, ungated source and is cached locally after the first download.

### Commands

- `whosaid <audio>…` — transcribe + diarize + label one or more files.
- `whosaid enroll [Name]` — record ~45s and name a voice.
- `whosaid record [--label L]` — capture from the mic, then transcribe.
- `whosaid relabel <base> SPEAKER_02=Name …` — name clusters from the speaker
  cards and persist their voiceprints (no re-transcription).
- `whosaid doctor` — read-only environment / model / registry report.
- `whosaid setup` (`./bootstrap.sh`) — dependency check + model pre-download.
- `whosaid install` — install/update the `~/.local/bin/whosaid` command symlink.

### Notes

- **The default speaker-embedding model is NeMo TitaNet-small** (English-native,
  ~2.5× faster than ERes2Net in sherpa's benchmark). Set `DIARIZE_EMB_NAME` to
  select the ERes2Net (en) or `…zh-cn…` (Mandarin) models from the same release.
- **`--chunk-seconds` is honored at any length.** Auto-chunking still only engages
  past ~15 min, but passing an explicit `--chunk-seconds` forces the parallel
  path on shorter audio too.
- **`whosaid doctor`** reports the active speaker-embedding model and the local
  registry path + voiceprint count.
- **Hallucination-hardened transcription.** whosaid calls the `mlx-whisper`
  library directly with the temperature-fallback ladder enabled,
  `condition_on_previous_text` off, and a hallucination-silence threshold — the
  configuration that avoids Whisper's repetition-collapse on long audio.
- **Output files:** `.txt`, `.srt`, `.vtt`, `.tsv`, `.json` (transcription), plus
  `.rttm`, `.speakers.txt`, `.speaker-cards.txt`, and a `.diarization.json`
  sidecar that makes `relabel` instant.
- **Privacy by construction:** audio, text, and voice embeddings never leave the
  machine; the speaker registry lives outside the repo and is never uploaded.

### Requirements

- An Apple Silicon Mac (MLX runs on the GPU via Metal); macOS.
- `ffmpeg` and `uv` (Homebrew). Python is used only through ephemeral `uv`
  environments — no persistent install is left behind.

[1.0.2]: https://github.com/sblattj/whosaid/releases/tag/v1.0.2
[1.0.1]: https://github.com/sblattj/whosaid/releases/tag/v1.0.1
[1.0.0]: https://github.com/sblattj/whosaid/releases/tag/v1.0.0
