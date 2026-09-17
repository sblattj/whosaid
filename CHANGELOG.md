# Changelog

All notable changes to whosaid are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Temp-directory install guard (GitHub issue #10).** `whosaid install` (and `./bootstrap.sh`,
  before its multi-minute model downloads) now refuse to install from a checkout under `/tmp`,
  `/private/tmp`, `/var/tmp`, or `$TMPDIR` unless `--force` is given, since the installed symlink
  would silently dangle once the OS cleans that directory up. `whosaid doctor` now also reports a
  dangling or foreign install symlink so a "command not found" regression points at the real cause.
- **Match-threshold flag, machine-readable match confidence, source metadata (GitHub issue #1, parts 2–3).**
  `whosaid <audio> --match-threshold F` (alias `--ref-threshold`, env `WHOSAID_MATCH_THRESHOLD`) now
  passes the registry/reference match gate through from the CLI, `ingest`, `relabel` and the MCP
  tools, and its default is raised from `0.40` to `0.50` — real meeting audio produced wrong
  assertions in the 0.40–0.53 band, while genuine matches score far higher. Clusters below the
  threshold keep their `SPEAKER_NN` label. Every naming decision, near-misses included, is written
  to `<base>.diarization.json` as `registry_matches`
  (`{cluster, name, similarity, threshold, matched, pass}`), refreshed by `relabel --auto`, and the
  sidecar also gains `source` (`path`, `duration_seconds`, `creation_time`) so no separate `ffprobe`
  is needed for per-meeting timestamps.
- **`whosaid enroll --from FILE` (GitHub issue #1, part 1).** Enroll a voice from an existing
  recording instead of the mic: `--ss`/`--t`/`--to` cut a time window (seconds or `M:SS`/`H:MM:SS`),
  the same ≥15s/non-silent check runs on the extracted clip, and `--force` allows overwriting an
  existing `voices/<Name>.wav`.
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
- **`whosaid version` / `--version` (GitHub issue #9).** Prints the installed version (and, from
  a git checkout, a `git describe` suffix); also exposed as the `version` field in the MCP
  `whosaid_doctor` tool's report.

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
- **Auto speaker-count estimation (GitHub issue #5, #6 header warning, #1 min/max speakers).**
  Replaces the farthest-first count (and its cap-retry ladder) with average-linkage
  agglomerative clustering of the per-turn voiceprints, cut at cosine `0.58` (env
  `WHOSAID_COUNT_THRESHOLD`), using the nearest-neighbour-chain algorithm so it stays O(n^2):
  1000 turns estimate in **0.009 s**. The cap of 20 is now a **bound, not a target** — an
  estimate that lands on it is treated as a failed estimate rather than a result.
  - Measured on a purpose-built 17.8-minute, 6-voice, 48-turn synthetic meeting (macOS `say`,
    65-72 embedded turns): raw `say` audio is too clean to fragment, so base and new both
    return the true 6. Re-mixed with 8 per-turn channel profiles (band-limiting, room/headset
    EQ, level offsets, pink noise) the true count is still 6, base returns 6, and the new
    estimator returns **6** — an earlier cut of 0.45, calibrated from the 0.6-0.8 same-speaker
    figure quoted in the issue, returned 5 and was corrected by measurement.
  - Real per-turn TitaNet-small similarity through this pipeline is **intra-speaker ~0.90,
    inter-speaker ~0.25** (not the 0.6-0.8 the issue assumes, which is the low tail); k=6 holds
    for any cut in `[0.55, 0.61]` on the channel-varied fixture and `[0.44, 0.61]` on the clean
    one, and 0.58 is the midpoint of the intersection.
  - **`--min-speakers N` / `--max-speakers N`** (issue #1) clamp the auto estimate on
    `whosaid`, the diarizer and the `whosaid_transcribe` MCP tool; `--max-speakers` also lowers
    the cap, `--speakers N` still forces an exact count, and `min > max` is rejected. The
    whole-file (<15 min) path passes an exact count only when `min == max` and otherwise
    reports the range as unenforced, since sherpa's `FastClustering` has no notion of a range.
  - **The count warning now reaches the artifact (issue #6).** When the count is untrustworthy
    a `# WARNING: ...` line is written into `<base>.speaker-cards.txt` directly under the count
    line, and `count_warning` plus a `count_estimate` record (`method`, `threshold`, `k`,
    `raw_k`, `cap`, `min`, `max`, `saturated`) appear in `<base>.diarization.json`, in the JSON
    on stdout, and in the MCP result.
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
