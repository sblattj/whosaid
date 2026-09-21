#!/usr/bin/env bash
# ==============================================================================
# bootstrap.sh — whosaid setup: capability checks, dependency install, model
# pre-download. Idempotent — safe to re-run any time; each step detects
# already-done and says so.
#
# Usage: ./bootstrap.sh [--yes] [--force] [--build-ffmpeg]
#   --yes     Skip confirmation prompts (installs/continues automatically).
#   --force   Install even if this checkout lives in a temp directory (see
#             the temp-directory guard below). Passed through to `whosaid
#             install --force` at step 8.
#   --build-ffmpeg  Prefer a verified user-local FFmpeg source build when either
#                   ffmpeg or ffprobe is missing (requires Command Line Tools).
# ==============================================================================
set -euo pipefail

# GUI shells and launchd do not load a user's shell profile.  Keep the same
# user-local prefixes the watcher supplies to its LaunchAgent.
export PATH="$HOME/.local/bin:$HOME/bin:$HOME/homebrew/bin:$HOME/homebrew/sbin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:$PATH"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

YES=0
FORCE=0
BUILD_FFMPEG=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    --force) FORCE=1 ;;
    --build-ffmpeg) BUILD_FFMPEG=1 ;;
    --help|-h)
      echo "Usage: whosaid setup [--yes] [--force] [--build-ffmpeg]"
      echo "Use installed uv/ffmpeg/ffprobe or install dependencies and download models."
      echo "--build-ffmpeg builds verified FFmpeg source into ~/.local/bin using Command Line Tools."
      exit 0 ;;
    *) echo "whosaid setup: unknown option: $a (see --help)" >&2; exit 2 ;;
  esac
done

# ---- temp-directory checkout guard (before the multi-minute downloads below) ------
# Refuse fast, before steps 5-7 pull ~1.5 GB of models, if this checkout lives
# under a temp directory (e.g. bootstrapped from /private/tmp/whosaid-latest/):
# once the OS cleans that directory up, step 8's install symlink would dangle
# with no diagnostic pointing at the real cause (GitHub issue #10). Delegates
# to `whosaid install --check-only` — an internal-only mode (not documented in
# `whosaid --help`) that runs just the is_temp_path guard and exits 0/1
# without touching the filesystem — so both scripts agree on one definition
# of "temp directory".
if [[ "$FORCE" -eq 0 ]] && ! "$REPO_DIR/whosaid" install --check-only; then
  exit 1
fi

log() { echo "whosaid: $*" >&2; }
step() { echo "" >&2; log "[$1/8] $2"; }

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

# ---- 2. package manager (optional) ----------------------------------------------
step 2 "checking optional package manager"
if command -v brew >/dev/null 2>&1; then
  log "✓ Homebrew found: $(brew --version 2>/dev/null | head -1)"
else
  log "Homebrew not found; continuing with user-local tools and the official uv installer."
fi

# ---- 3. ffmpeg + uv ---------------------------------------------------------------
step 3 "checking ffmpeg, ffprobe, and uv"
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  log "✓ ffmpeg and ffprobe already installed"
else
  if [[ "$BUILD_FFMPEG" -eq 0 ]] && command -v brew >/dev/null 2>&1 && confirm "ffmpeg/ffprobe not found — install a Homebrew bottle?"; then
    # A user-local Homebrew prefix may not have a bottle. Never quietly turn
    # setup into a multi-hour source build: require a bottle or explain the
    # verified-source route below.
    brew install --force-bottle ffmpeg || { log "Homebrew could not supply a bottle; rerun with --build-ffmpeg for the verified source route."; exit 1; }
  elif [[ "$BUILD_FFMPEG" -eq 1 ]] || confirm "ffmpeg/ffprobe not found — build FFmpeg 9.0.2 from verified official source into ~/.local (requires Command Line Tools)?"; then
    command -v make >/dev/null 2>&1 && command -v clang >/dev/null 2>&1 || { log "FATAL: source build needs Xcode Command Line Tools (run: xcode-select --install), make, and clang."; exit 1; }
    ff_tmp="$(mktemp -d)"
    trap 'rm -rf "$ff_tmp"' EXIT
    ff_url="https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz"
    # Verified against FFmpeg's detached release signature with the public key
    # fingerprint FCF986EA15E6E293A5644F10B4322F04D67658D8 published at
    # https://ffmpeg.org/download.html. End users need only macOS shasum.
    ff_sha256="8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
    curl --fail --location --proto '=https' --tlsv1.2 "$ff_url" -o "$ff_tmp/ffmpeg.tar.xz"
    printf '%s  %s\n' "$ff_sha256" "$ff_tmp/ffmpeg.tar.xz" | shasum -a 256 -c -
    tar -xJf "$ff_tmp/ffmpeg.tar.xz" -C "$ff_tmp"
    ff_jobs="$(sysctl -n hw.ncpu)"
    [[ "$ff_jobs" -gt 8 ]] && ff_jobs=8
    # Avoid accidentally linking against optional libraries in a development
    # machine's Homebrew tree. Keep the native macOS capture/media frameworks.
    (cd "$ff_tmp/ffmpeg-9.0.2" && ./configure --prefix="$HOME/.local" \
      --disable-debug --disable-doc --disable-ffplay --disable-autodetect \
      --enable-avfoundation --enable-audiotoolbox --enable-videotoolbox --enable-pthreads \
      && make -j"$ff_jobs" ffmpeg ffprobe)
    mkdir -p "$HOME/.local/bin"
    install -m 0755 "$ff_tmp/ffmpeg-9.0.2/ffmpeg" "$HOME/.local/bin/ffmpeg"
    install -m 0755 "$ff_tmp/ffmpeg-9.0.2/ffprobe" "$HOME/.local/bin/ffprobe"
    rm -rf "$ff_tmp"
    trap - EXIT
    hash -r
  else
    log "FATAL: ffmpeg and ffprobe are both required. Use --build-ffmpeg to build verified official source into ~/.local, or install a provider build with its published checksum in ~/.local/bin."
    exit 1
  fi
fi
if command -v uv >/dev/null 2>&1; then
  log "✓ uv already installed"
else
  if command -v brew >/dev/null 2>&1 && confirm "uv not found — install via 'brew install uv'?"; then
    brew install uv
  elif confirm "uv not found — download verified Astral uv 0.12.17 into ~/.local/bin?"; then
    # Pin the official Apple-Silicon archive and its SHA-256.  Do not use the
    # convenience installer here: its checksum helper requires GNU sha256sum,
    # which a stock macOS installation does not provide.
    uv_tmp="$(mktemp -d)"
    trap 'rm -rf "$uv_tmp"' EXIT
    uv_url="https://github.com/astral-sh/uv/releases/download/0.12.17/uv-aarch64-apple-darwin.tar.gz"
    uv_sha256="85f00cbdc6dd3e97eba4c31b4d014375a9fdfe8f570023b84e5102fc3456896b"
    curl --fail --location --proto '=https' --tlsv1.2 "$uv_url" -o "$uv_tmp/uv.tar.gz"
    printf '%s  %s\n' "$uv_sha256" "$uv_tmp/uv.tar.gz" | shasum -a 256 -c -
    tar -xzf "$uv_tmp/uv.tar.gz" -C "$uv_tmp"
    mkdir -p "$HOME/.local/bin"
    install -m 0755 "$uv_tmp/uv-aarch64-apple-darwin/uv" "$HOME/.local/bin/uv"
    install -m 0755 "$uv_tmp/uv-aarch64-apple-darwin/uvx" "$HOME/.local/bin/uvx"
    hash -r
  else
    log "FATAL: uv is required. Install it with the official installer: curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=\"$HOME/.local/bin\" UV_NO_MODIFY_PATH=1 sh"
    exit 1
  fi
fi
command -v uv >/dev/null 2>&1 || { log "FATAL: uv installation finished but uv is not executable on PATH."; exit 1; }
command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 || { log "FATAL: ffmpeg installation did not provide both ffmpeg and ffprobe."; exit 1; }

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
    log "WARNING: only ~${avail_gb} GB free on \$HOME; ~4 GB free disk is recommended (the Whisper model download is ~1.5 GB, plus working headroom)."
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

# ---- 8. install the command -------------------------------------------------------
step 8 "installing the whosaid command"
if [[ "$FORCE" -eq 1 ]]; then
  "$REPO_DIR/whosaid" install --force
else
  "$REPO_DIR/whosaid" install
fi

echo "" >&2
log "bootstrap complete."
log "macOS will ask for Microphone permission the first time you run 'enroll' or 'record'"
log "— bootstrap cannot pre-grant it; approve it when prompted."
log "Next: whosaid enroll"
