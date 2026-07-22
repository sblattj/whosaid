#!/usr/bin/env bash
# ==============================================================================
# bootstrap.sh — whosaid setup: capability checks, dependency install, model
# pre-download. Idempotent — safe to re-run any time; each step detects
# already-done and says so.
#
# Usage: ./bootstrap.sh [--yes]
#   --yes   Skip confirmation prompts (installs/continues automatically).
# ==============================================================================
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

YES=0
for a in "$@"; do
  [[ "$a" == "--yes" ]] && YES=1
done

log() { echo "whosaid: $*" >&2; }
step() { echo "" >&2; log "[$1/7] $2"; }

# confirm "question" -> 0 (proceed) when --yes was given or the user answers y/Y.
confirm() {
  [[ "$YES" -eq 1 ]] && return 0
  local ans=""
  read -r -p "whosaid: $1 [y/N] " ans || true
  [[ "$ans" =~ ^[Yy]$ ]]
}

MODEL="${WHOSAID_MODEL:-mlx-community/whisper-large-v3-turbo}"

# ---- 1. platform ---------------------------------------------------------------
step 1 "checking platform"
if [[ "$(uname -s)" != "Darwin" ]]; then
  log "FATAL: whosaid requires macOS."
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  log "FATAL: whosaid requires an Apple Silicon Mac — MLX runs on the Mac GPU via Metal."
  exit 1
fi
log "✓ Apple Silicon macOS detected"

# ---- 2. Homebrew -----------------------------------------------------------------
step 2 "checking Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  log "FATAL: Homebrew is required. Install it from https://brew.sh then re-run ./bootstrap.sh."
  exit 1
fi
log "✓ Homebrew found: $(brew --version 2>/dev/null | head -1)"

# ---- 3. ffmpeg + uv ---------------------------------------------------------------
step 3 "checking ffmpeg and uv"
if command -v ffmpeg >/dev/null 2>&1; then
  log "✓ ffmpeg already installed"
else
  if confirm "ffmpeg not found — install via 'brew install ffmpeg'?"; then
    brew install ffmpeg
    log "✓ ffmpeg installed"
  else
    log "FATAL: ffmpeg is required. Install with: brew install ffmpeg"
    exit 1
  fi
fi
if command -v uv >/dev/null 2>&1; then
  log "✓ uv already installed"
else
  if confirm "uv not found — install via 'brew install uv'?"; then
    brew install uv
    log "✓ uv installed"
  else
    log "FATAL: uv is required. Install with: brew install uv"
    exit 1
  fi
fi

# ---- 4. disk space ---------------------------------------------------------------
step 4 "checking disk space"
hf_home="${HF_HOME:-$HOME/.cache/huggingface}"
model_slug="models--${MODEL//\//--}"
if ls -d "$hf_home/hub/$model_slug"* >/dev/null 2>&1; then
  log "✓ Whisper model already cached ($MODEL) — no download needed"
else
  avail_kb="$(df -k "$HOME" | awk 'NR==2{print $4}')"
  avail_gb=$(( avail_kb / 1024 / 1024 ))
  if [[ "$avail_gb" -lt 4 ]]; then
    log "WARNING: only ~${avail_gb} GB free on \$HOME; the Whisper model needs ~4 GB."
    if ! confirm "continue anyway?"; then
      log "aborted — free up disk space and re-run."
      exit 1
    fi
  else
    log "✓ ~${avail_gb} GB free on \$HOME — enough for the Whisper model download"
  fi
fi

# ---- 5. warm the uv environments ---------------------------------------------------
step 5 "warming uv environments (ephemeral, no global Python install)"
log "warming mlx-whisper environment (first run may take a minute)..."
uv run --quiet --with mlx-whisper python -c "import mlx_whisper"
log "✓ mlx-whisper environment ready"
log "warming sherpa-onnx environment..."
uv run --quiet --with sherpa-onnx --with numpy python -c "import sherpa_onnx, numpy"
log "✓ sherpa-onnx environment ready"

# ---- 6. pre-download the Whisper model ----------------------------------------------
step 6 "downloading Whisper model ($MODEL)"
if ls -d "$hf_home/hub/$model_slug"* >/dev/null 2>&1; then
  log "✓ already cached, skipping download"
else
  uv run --quiet --with mlx-whisper python -c "from huggingface_hub import snapshot_download; print(snapshot_download('$MODEL'))"
  log "✓ Whisper model downloaded"
fi

# ---- 7. sherpa diarization models -----------------------------------------------------
step 7 "downloading sherpa diarization models"
uv run --quiet --with sherpa-onnx --with numpy python "$REPO_DIR/lib/diarize_sherpa.py" --ensure-models-only
log "✓ sherpa diarization models ready"

echo "" >&2
log "bootstrap complete."
log "macOS will ask for Microphone permission the first time you run 'enroll' or 'record'"
log "— bootstrap cannot pre-grant it; approve it when prompted."
log "Next: ./whosaid enroll"
