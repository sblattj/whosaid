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
  the Mac GPU via Metal) and sherpa-onnx offline diarization (pyannote segmentation-3.0 ONNX +
  3D-Speaker ERes2Net embeddings — ungated GitHub-release models, CPU). Ephemeral `uv` environments
  mean no persistent Python install.
- **Enrollment names the clusters.** Diarization alone yields `SPEAKER_00/01`. whosaid matches each
  cluster's voice embedding against reference clips in `voices/` by cosine similarity, so
  `Alice.wav` makes the transcript read `Alice: …`.

## Components

```
whosaid              # CLI dispatcher (bash): setup | enroll | record | doctor | <audio files>
bootstrap.sh         # capability check, dependency install, model pre-download
lib/transcribe_mlx.py  # MLX Whisper runner (hallucination-hardened)
lib/diarize_sherpa.py  # diarization + voice-ref cluster naming
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
   pyannote segmentation-3.0 finds speech turns, ERes2Net embeds them, clustering groups them
   (`--speakers N` hints the count), every `voices/*.wav` is embedded and matched to clusters by
   cosine similarity (≥ 0.40 names the cluster). Writes `<base>.rttm` and merges with the Whisper
   segments into `<base>.speakers.txt` — the speaker-labeled transcript.
3. Flags: `-o/--outdir`, `-m/--model`, `--accurate` (full large-v3 instead of turbo),
   `-l/--lang`, `-f/--format`, `-n/--name`, `--speakers N`, `--no-diarize`.
   Env: `WHOSAID_MODEL`, `WHOSAID_LANG`, `WHOSAID_VOICE_REFS`, `WHOSAID_REC_DEVICE`; the model
   cache locations honor `HF_HOME` (Whisper) and `SHERPA_DIARIZE_CACHE` (diarization).

### `whosaid doctor`

Re-runs the capability checks read-only and reports: arch/OS, brew/ffmpeg/uv versions, Whisper and
sherpa model cache state, enrolled voices, and the avfoundation audio device list.

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
dialog with `--speakers 2`, and asserts the `.speakers.txt` exists, contains `Alice:` plus exactly
one other speaker label, and has a plausible number of turns. Also runs `bash -n` /
`python3 -m py_compile` syntax checks over the sources.

## Non-goals (v1)

- No PyPI/npm packaging (`git clone` + `./bootstrap.sh` is the install story; a `uvx` package is a
  possible later evolution).
- No daemonized/background recording, no watchers.
- No cloud fallback of any kind — local-only is the point, not a default.
