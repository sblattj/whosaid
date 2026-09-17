# Changelog

All notable changes to whosaid are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Meeting workspaces (GitHub issue #2).** A dated, auditable home for a recurring meeting
  series (`lib/workspace.py`): every recording transcribed into a `YYYY-MM-DD-HHMM` folder,
  per-meeting action items, and one roll-up across the whole workspace — still entirely offline.
- **`whosaid ingest` — dated, idempotent batch folders.** Transcribes a batch into folders named
  from each recording's container `creation_time` (rendered in `--tz`, mtime fallback),
  idempotent by source sha256, passing through all transcribe flags.
- **Pluggable action-items hook.** `--hook CMD` (or the `WHOSAID_ACTION_ITEMS_HOOK` environment
  variable) receives the speaker-labeled transcript on stdin plus `WHOSAID_SPEAKERS` /
  `WHOSAID_TRANSCRIPT_PATH`, and its stdout becomes the meeting's `action-items.md`. With no hook
  a skeleton is written instead, so the default stays fully offline.
- **`whosaid roll-up` — coverage index with a nothing-missing audit.** `_INDEX.md` lists one row
  per meeting (created, duration, transcribed/diarized/action-items) and flags orphan directories
  and stale manifest entries, alongside a recurring-topics section.
- **Living, deduplicated action-item corpus.** With `--action-items`, `_ACTION-ITEMS.md` folds
  every meeting's items into stable `AI-001` ids (never renumbered) with `first_seen` /
  `last_seen`, occurrence lists, and open/ongoing/resolved statuses that survive re-runs.
  Incremental and append-only by default (`--rebuild` to reset); state is plain JSON
  (`_workspace.json`, `_action-items.json`) that is safe to hand-edit.

### Fixed

- **Phantom speaker clusters on long recordings (GitHub issues #5, #6, #7, #8, #11).** Four
  related fixes so a long meeting no longer fragments into duplicate/unidentified speakers:
  - **Absorb pass.** After the registry one-best and `--ref` passes, every still-unnamed cluster
    whose centroid cosine to a known voice (registry entry or `--ref` clip) is `>=`
    `--absorb-threshold` (default `0.85`, env `WHOSAID_ABSORB_THRESHOLD`) is folded into that
    person. A person split across several clusters is named on all of them, and the speaker cards
    now render **one card per name** with the combined turns/talk time (was one card per cluster).
  - **`--ref` no longer double-names.** The `--ref` pass only considers still-unnamed clusters and
    skips any name the registry already assigned, so an enrolled voice plus a registry entry for the
    same person can't produce two cards for them.
  - **Auto speaker-count cap guard.** When farthest-first speaker-count estimation saturates at the
    cap (20), it is re-estimated with progressively lower merge thresholds until the count drops
    below the cap, instead of handing k-means a `k` of 20 that shatters real voices. The
    over-segmentation WARN also now fires when the final count equals the cap.
- **Registry entries computed with a different embedding model no longer mis-match** in
  `relabel --auto`: candidate voiceprints are filtered to the sidecar's own embedding model.

### Added

- **`whosaid relabel <base> --auto` — re-apply naming with no re-diarization.** Reloads the cached
  `<base>.diarization.json`, re-runs registry matching + the absorb pass, and rewrites
  `<base>.speakers.txt` / `<base>.speaker-cards.txt` (and the sidecar's names). Picks up voices you
  enrolled after the transcript was made and merges phantom splits. Accepts the meeting-workspace
  layout (`base` = `transcript`). The MCP `whosaid_relabel` tool gains an `auto` parameter.
- **`--absorb-threshold F` transcribe/relabel flag** (env `WHOSAID_ABSORB_THRESHOLD`, default
  `0.85`) controlling the absorb pass above.

## [1.1.0] — 2026-08-18

### Added

- **MCP server (`whosaid mcp`).** A stdio MCP (Model Context Protocol) server (`lib/mcp_server.py`,
  built on the official `mcp` Python SDK's `FastMCP`) exposes whosaid to AI agents. Launched via
  `whosaid mcp`, using the same ephemeral `uv run --with "mcp[cli]"` pattern as the rest of
  whosaid — no persistent install.
- **Five tools**, all prefixed `whosaid_`: `whosaid_transcribe`, `whosaid_relabel`,
  `whosaid_list_speakers` (read-only), `whosaid_doctor` (read-only), and
  `whosaid_enroll_from_file`. Every tool shells the existing `whosaid` CLI rather than
  reimplementing the pipeline, so the MCP surface and the CLI can't drift apart.
- **`whosaid://guide` resource** — an on-demand deep reference (full flag/env list, the long-audio
  parallel path, the cosine-match threshold) an agent can read without it bloating every tool's
  always-loaded description.

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

[Unreleased]: https://github.com/sblattj/whosaid/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/sblattj/whosaid/releases/tag/v1.1.0
[1.0.2]: https://github.com/sblattj/whosaid/releases/tag/v1.0.2
[1.0.1]: https://github.com/sblattj/whosaid/releases/tag/v1.0.1
[1.0.0]: https://github.com/sblattj/whosaid/releases/tag/v1.0.0
