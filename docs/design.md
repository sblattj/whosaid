# whosaid — design

Local, GPU-accelerated, speaker-attributed transcription for Apple Silicon Macs. One command turns
an audio file (or a live mic recording) into a transcript where each turn is labeled with *who said
it* — and after a one-time voice enrollment, your own turns are labeled with your name. Nothing —
audio, text, or embeddings — ever leaves the machine. No API keys, no Hugging Face token, no
gated models.

## Why this exists

- **Whisper is local and excellent, but it doesn't know who spoke.** Diarization tooling that does
  (pyannote on torch) typically needs a HF token for gated models and a heavy Python environment.
- **whosaid composes two fully-open pieces**: MLX Whisper (Apple's MLX framework — transcription on
  the Mac GPU via Metal) and sherpa-onnx offline diarization (pyannote segmentation-3.0 ONNX + a
  NeMo TitaNet-small speaker embedding by default — ungated GitHub-release models, CPU; the
  embedding is configurable via `DIARIZE_EMB_NAME`, with 3D-Speaker ERes2Net (English) and a zh-cn
  model as opt-in alternatives). Ephemeral `uv` environments mean no persistent Python install.
- **Enrollment names the clusters.** Diarization alone yields `SPEAKER_00/01`. whosaid matches each
  cluster's voice embedding against reference clips in `voices/` by cosine similarity, so
  `Alice.wav` makes the transcript read `Alice: …`.

## Components

```
whosaid              # CLI dispatcher (bash): setup | install | enroll | record | relabel | doctor | <audio files>
bootstrap.sh         # capability check, dependency install, model pre-download
lib/transcribe_mlx.py  # MLX Whisper runner (hallucination-hardened)
lib/diarize_sherpa.py  # diarization + voice-ref cluster naming
lib/mcp_server.py    # MCP stdio server: exposes transcribe/relabel/list/doctor/enroll/samples to AI agents (whosaid mcp)
voices/              # enrollment clips: <Name>.wav (16 kHz mono; contents gitignored)
recordings/          # `whosaid record` output (gitignored)
test/e2e.sh          # offline end-to-end smoke test (synthesizes a 2-voice dialog with `say`)
```

### `bootstrap.sh` (= `whosaid setup`)

Checks, in order, loud on failure: macOS + `arm64` (hard fail — MLX requires Apple Silicon),
Homebrew, `ffmpeg` and `uv` (offers `brew install` for missing ones; `--yes` skips prompts),
~4 GB free disk. Then pre-downloads everything so first use has no surprise waits: warms the `uv`
environments, fetches the default Whisper model (`mlx-community/whisper-large-v3-turbo`, ~1.5 GB)
via `huggingface_hub`, and fetches the sherpa segmentation + embedding models (~30 MB) via
`lib/diarize_sherpa.py --ensure-models-only`. Idempotent — safe to re-run. Ends by pointing at
`./whosaid enroll`.

### `whosaid enroll [Name]`

Prints the first paragraph of the Rainbow Passage (public-domain, phonetically balanced — the
standard enrollment text in speech science), records up to 45 s from the mic via
ffmpeg/avfoundation (Ctrl-C stops early; 15 s minimum enforced), then verifies the capture is not
silent: macOS denies an ungranted microphone by feeding **silent zeros, not an error**, so a mean
volume ≤ −85 dB means the terminal lacks Microphone permission and enroll fails with System
Settings instructions. A good capture is converted to 16 kHz mono WAV at `voices/<Name>.wav`.
Repeatable for any number of people; every clip in `voices/` names its cluster in future runs.

### `whosaid record [--label L]`

Foreground mic capture to `recordings/<utc-ts>[-label].m4a` (Ctrl-C to stop — ffmpeg finalizes the
m4a on SIGINT; 3 h safety cap), the same silent-capture check, then the recording feeds directly
into the transcribe flow. Deliberately interactive-only in v1: no daemon, no launchd.

### `whosaid <audio>…` (default command)

The pipeline, per file:

1. **Transcribe** — `uv run --with mlx-whisper` invokes `lib/transcribe_mlx.py` (never the bare
   `mlx_whisper` CLI: the CLI's single-temperature default disables Whisper's temperature-fallback
   ladder and lets long recordings collapse into one repeated token). The helper keeps the fallback
   tuple, sets `condition_on_previous_text=False`, and uses `hallucination_silence_threshold` so
   dead air doesn't spawn repeated-token filler. Writes `.txt/.srt/.vtt/.tsv/.json` per `--format`.
2. **Diarize** — `uv run --with sherpa-onnx --with numpy` invokes `lib/diarize_sherpa.py`:
   pyannote segmentation-3.0 finds speech turns, a NeMo TitaNet-small embedding (the default —
   configurable via `DIARIZE_EMB_NAME`, with 3D-Speaker ERes2Net (English) or a zh-cn model as
   opt-in alternatives) embeds them, clustering groups them (`--speakers N` hints the count), then
   every `voices/*.wav` — plus every voiceprint in the persistent local speaker registry
   (`~/.config/whosaid/speakers.json`, overridable via `WHOSAID_SPEAKER_DB`; voiceprints are keyed
   by embedding model, so switching models re-enrolls) — is embedded and matched to clusters by
   cosine similarity (≥ `--match-threshold`, default 0.50, names the cluster; below it the cluster
   keeps its `SPEAKER_NN` label, and every decision including near-misses is recorded in the
   sidecar's `registry_matches`). Writes `<base>.rttm`, merges with the Whisper
   segments into `<base>.speakers.txt` (the speaker-labeled transcript), and emits a
   `<base>.speaker-cards.txt` (one card per speaker — turn count, talk time, representative
   snippets — to tell who each `SPEAKER_NN` is) plus a `<base>.diarization.json` sidecar (cached
   segments + per-cluster voiceprints).
3. Flags: `-o/--outdir`, `-m/--model`, `--accurate` (full large-v3 instead of turbo),
   `-l/--lang`, `-f/--format`, `-n/--name`, `--speakers N`, `--no-diarize`, and the long-audio
   controls `--no-chunk`, `-j/--jobs`, `--chunk-seconds`.
   Env: `WHOSAID_MODEL`, `WHOSAID_LANG`, `WHOSAID_VOICE_REFS`, `WHOSAID_SPEAKER_DB`,
   `DIARIZE_EMB_NAME`, `WHOSAID_REC_DEVICE`, `WHOSAID_INSTALL_DIR`; the model cache locations honor
   `HF_HOME` (Whisper) and `SHERPA_DIARIZE_CACHE` (diarization).

**Long recordings run in parallel.** Audio over ~15 min (900 s) auto-chunks: the file is split into
non-overlapping time windows that are segmented and embedded concurrently across a process pool,
then a single global clustering pass over every window's voiceprints recovers speakers that stay
consistent across window boundaries — in practice the same speakers as a single-pass run, finishing
several times faster. `--no-chunk` forces a single pass; `-j/--jobs` and `--chunk-seconds` tune the
worker count and window length.

**Relabel without re-diarizing.** `whosaid relabel <base> SPEAKER_02=Jane …` reads the
`<base>.diarization.json` sidecar, rewrites `<base>.speakers.txt` and `<base>.speaker-cards.txt`
with the new names, and saves each named voiceprint to the local registry — so that person is
auto-named in future transcripts. No re-transcription, no re-diarization.

### `whosaid doctor`

Re-runs the capability checks read-only and reports: arch/OS, brew/ffmpeg/uv versions, Whisper and
sherpa model cache state, enrolled voices, and the avfoundation audio device list.

### MCP server (`whosaid mcp`)

`whosaid mcp` runs a stdio MCP (Model Context Protocol) server — `lib/mcp_server.py`, built on the
official `mcp` Python SDK's `FastMCP` — so AI agents can drive whosaid directly, launched the same
ephemeral way as everything else in whosaid: `uv run --with "mcp[cli]"`, no persistent install. The
server never reimplements the pipeline — every tool **shells the existing `whosaid` CLI** as a
subprocess, so the MCP surface and the CLI can't drift apart.

Six tools, all prefixed `whosaid_`:

- **`whosaid_transcribe`** — runs the transcribe + diarize pipeline on a file and returns the output
  paths, speaker cards, and a `next_step` pointing the agent at whichever speakers still need naming.
- **`whosaid_relabel`** — maps `SPEAKER_NN` clusters to names from the cached diarization sidecar (no
  re-transcription, no re-diarization) and persists them to the speaker registry.
- **`whosaid_list_speakers`** *(read-only)* — lists enrolled voice clips and registry-known speakers.
- **`whosaid_doctor`** *(read-only)* — the same environment/model-cache readiness report as
  `whosaid doctor`, for an agent to run before attempting a transcribe that might fail.
- **`whosaid_enroll_from_file`** — enrolls a named voice from an existing audio clip (no mic).
- **`whosaid_samples`** — exports one short representative WAV per speaker cluster (the longest
  diarized segment, clamped to `seconds`) so an agent (or the human it's helping) can listen and
  confirm an identity before trusting a label.

Cross-cutting behavior — the local-only guarantee, the output-file contract, the
transcribe-then-relabel workflow — lives once in the server's `instructions`, loaded up front rather
than repeated in every tool description; deeper reference (the full flag/env list, the long-audio
parallel path, the cosine-match threshold) is a `whosaid://guide` resource an agent reads on demand.
The interactive `enroll` and `record` commands are deliberately **not** exposed as tools — both need
a live terminal and microphone access, which an MCP client doesn't have. Same guarantee as the CLI:
audio, text, and voice embeddings never leave the machine.

## Error handling

- Diarization failure is **never fatal to the transcript** — warn and keep the Whisper output.
- Exit codes are **artifact-verified**: the transcribe flow exits non-zero unless the files a
  consumer would read actually exist and are non-empty (never trusts a step's own report).
- Every environment guard fails loud with the fix in the message (brew install line, System
  Settings path for mic permission, Apple-Silicon requirement).

## Testing

`test/e2e.sh` is fully offline and self-contained: it synthesizes a two-speaker dialog with two
macOS `say` voices, builds an enrollment clip for one of them ("Alice") into a temp voices dir
(`WHOSAID_VOICE_REFS` override — the repo's `voices/` is never touched), runs `./whosaid` on the
dialog with `--speakers 2`, and asserts the `.speakers.txt` exists, contains `Alice:` plus at least
one other `SPEAKER_NN` label, and has a plausible number of turns. Also runs `bash -n` /
`python3 -m py_compile` syntax checks over the sources.

## Non-goals (v1)

- No PyPI/npm packaging (`git clone` + `./bootstrap.sh` is the install story; a `uvx` package is a
  possible later evolution).
- No daemonized/background recording, no watchers.
- No cloud fallback of any kind — local-only is the point, not a default.
