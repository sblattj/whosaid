#!/bin/bash
# Offline production-entry tests for bootstrap's no-Brew toolchain gates.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"; PASS=0; FAILED=0
cleanup() { [ "$FAILED" = 0 ] && rm -rf "$TMP" || echo "kept $TMP" >&2; }
trap cleanup EXIT
fail() { FAILED=1; echo "FAIL: $1" >&2; exit 1; }
eq() { [ "$1" = "$2" ] && PASS=$((PASS+1)) || fail "$3: expected [$2], got [$1]"; }
has() { grep -qE -- "$1" "$2" && PASS=$((PASS+1)) || fail "$3"; }

make_case() {
  CASE="$TMP/$1"; HOME="$CASE/home"; BIN="$HOME/.local/bin"; LOG="$CASE/calls"
  mkdir -p "$BIN" "$CASE/repo/lib"
  cp "$ROOT/bootstrap.sh" "$CASE/repo/bootstrap.sh"; chmod +x "$CASE/repo/bootstrap.sh"
  cat > "$CASE/repo/whosaid" <<'EOF'
#!/bin/bash
[ "$1" = install ] && exit 0
exit 1
EOF
  chmod +x "$CASE/repo/whosaid"
  : > "$LOG"
  export HOME BIN LOG CASE
  BASH_ENV_FILE="$CASE/no-brew.sh"
  cat > "$BASH_ENV_FILE" <<'EOF'
command() {
  if [ "$1" = -v ] && [ "$2" = brew ]; then return 1; fi
  builtin command "$@"
}
EOF
}
tool() { cat > "$BIN/$1"; chmod +x "$BIN/$1"; }
run() { set +e; (cd "$CASE/repo" && HOME="$HOME" PATH="/usr/bin:/bin" BASH_ENV="${BASH_ENV_FILE:-/dev/null}" ./bootstrap.sh "$@") >"$CASE/out" 2>"$CASE/err"; RC=$?; set -e; }

echo "== bootstrap_test: $TMP =="

# 1. All three tools in ~/.local/bin: no Brew is necessary and the real setup
# entry point reaches its final install step without curl/network activity.
make_case preinstalled
tool uname <<'EOF'
#!/bin/bash
[ "$1" = -s ] && echo Darwin || echo arm64
EOF
for name in ffmpeg ffprobe; do tool "$name" <<'EOF'
#!/bin/bash
exit 0
EOF
done
tool uv <<'EOF'
#!/bin/bash
echo "uv $*" >> "$LOG"
exit 0
EOF
tool curl <<'EOF'
#!/bin/bash
echo "curl $*" >> "$LOG"
exit 99
EOF
run --yes --force
eq "$RC" 0 "preinstalled no-Brew bootstrap exits 0"
has 'ffmpeg and ffprobe already installed' "$CASE/err" "preinstalled pair is accepted"
if grep -q '^curl ' "$LOG"; then fail "preinstalled bootstrap unexpectedly downloaded a tool"; fi
PASS=$((PASS+1))

# 2. ffmpeg alone is insufficient: the production entry point must name
# ffprobe and stop before attempting a source or package-manager install.
make_case ffprobe-missing
tool uname <<'EOF'
#!/bin/bash
[ "$1" = -s ] && echo Darwin || echo arm64
EOF
for name in ffmpeg uv; do tool "$name" <<'EOF'
#!/bin/bash
exit 0
EOF
done
BASH_ENV_FILE="$CASE/hide-ffprobe.sh"
cat > "$BASH_ENV_FILE" <<'EOF'
command() {
  if [ "$1" = -v ] && [ "$2" = brew ]; then return 1; fi
  if [ "$1" = -v ] && [ "$2" = ffprobe ]; then return 1; fi
  builtin command "$@"
}
EOF
run --force </dev/null
BASH_ENV_FILE=""
eq "$RC" 1 "missing ffprobe bootstrap exits 1"
has 'ffmpeg and ffprobe are both required' "$CASE/err" "missing ffprobe is diagnosed as required pair"

# 3. A local/nonstandard Brew must be forced to use a bottle. A bottle failure
# must stop, rather than silently falling into an expensive source build.
make_case bottle-failure
tool uname <<'EOF'
#!/bin/bash
[ "$1" = -s ] && echo Darwin || echo arm64
EOF
tool brew <<'EOF'
#!/bin/bash
echo "brew $*" >> "$LOG"
[ "$1" = --version ] && { echo Brew; exit 0; }
exit 9
EOF
tool uv <<'EOF'
#!/bin/bash
exit 0
EOF
BASH_ENV_FILE="$CASE/hide-media.sh"
cat > "$BASH_ENV_FILE" <<'EOF'
command() {
  if [ "$1" = -v ] && { [ "$2" = ffmpeg ] || [ "$2" = ffprobe ]; }; then return 1; fi
  builtin command "$@"
}
EOF
run --yes --force
BASH_ENV_FILE=""
eq "$RC" 1 "bottle failure bootstrap exits 1"
has '^brew install --force-bottle ffmpeg$' "$LOG" "Brew ffmpeg install requires a bottle"
has 'could not supply a bottle' "$CASE/err" "bottle failure offers actionable source route"

# 4. The pinned uv archive checksum must fail closed. The fake curl creates a
# payload, shasum rejects it, and no uv executable may be installed.
make_case checksum-failure
tool uname <<'EOF'
#!/bin/bash
[ "$1" = -s ] && echo Darwin || echo arm64
EOF
for name in ffmpeg ffprobe; do tool "$name" <<'EOF'
#!/bin/bash
exit 0
EOF
done
tool curl <<'EOF'
#!/bin/bash
while [ "$#" -gt 0 ]; do
  [ "$1" = -o ] && { printf bad > "$2"; exit 0; }
  shift
done
exit 1
EOF
tool shasum <<'EOF'
#!/bin/bash
exit 1
EOF
BASH_ENV_FILE="$CASE/hide-uv.sh"
cat > "$BASH_ENV_FILE" <<'EOF'
command() {
  if [ "$1" = -v ] && [ "$2" = brew ]; then return 1; fi
  if [ "$1" = -v ] && [ "$2" = uv ]; then return 1; fi
  builtin command "$@"
}
EOF
run --yes --force
BASH_ENV_FILE=""
eq "$RC" 1 "checksum failure bootstrap exits 1"
[ ! -e "$HOME/.local/bin/uv" ] || fail "checksum failure installed uv"
PASS=$((PASS+1))

# The explicit source route bypasses an available brew and verifies FFmpeg
# bytes before extraction/build. These mocks must never call host installers.
make_case ffmpeg-checksum-failure
tool brew <<'EOF'
#!/bin/bash
echo "brew $*" >> "$LOG"
[ "$1" = --version ] && { echo Brew; exit 0; }
exit 99
EOF
tool uv <<'EOF'
#!/bin/bash
exit 0
EOF
tool curl <<'EOF'
#!/bin/bash
while [ "$#" -gt 0 ]; do
  [ "$1" = -o ] && { printf bad > "$2"; exit 0; }
  shift
done
exit 1
EOF
tool shasum <<'EOF'
#!/bin/bash
exit 1
EOF
BASH_ENV_FILE="$CASE/hide-media.sh"
cat > "$BASH_ENV_FILE" <<'EOF'
command() {
  if [ "$1" = -v ] && { [ "$2" = ffmpeg ] || [ "$2" = ffprobe ]; }; then return 1; fi
  builtin command "$@"
}
EOF
run --yes --force --build-ffmpeg
eq "$RC" 1 "FFmpeg checksum failure exits nonzero"
if grep -q '^brew install' "$LOG"; then fail "explicit source route called brew install"; fi
PASS=$((PASS+1))
[ ! -e "$HOME/.local/bin/ffmpeg" ] && [ ! -e "$HOME/.local/bin/ffprobe" ] || fail "bad FFmpeg archive installed tools"
PASS=$((PASS+1))

echo "PASS: $PASS checks passed (bootstrap.sh)"
