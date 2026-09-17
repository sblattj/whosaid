#!/bin/bash
#
# test/version_test.sh — offline unit test for `whosaid version` / `--version`
# / `-V` (GitHub issue #9).
#
# Fast, fully offline: no models, no network, no audio. macOS/BSD only:
# BSD grep/awk, bash 3.2 (no associative arrays, no bash-4-isms).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WHOSAID="$REPO/whosaid"

PASS=0
TEST_FAILED=0

cleanup() {
  if [ "$TEST_FAILED" -ne 0 ]; then
    echo "" >&2
    echo "FAIL: $PASS check(s) passed before the failure" >&2
  fi
}
trap cleanup EXIT

fail() {
  TEST_FAILED=1
  echo "" >&2
  echo "FAIL: $1" >&2
  exit 1
}

assert_eq() {  # assert_eq <actual> <expected> <what>
  if [ "$1" = "$2" ]; then
    PASS=$((PASS + 1))
  else
    fail "$3 — expected [$2], got [$1]"
  fi
}

assert_grep() {  # assert_grep <ERE-pattern> <text> <what>
  if printf '%s\n' "$2" | grep -qE -- "$1"; then
    PASS=$((PASS + 1))
  else
    fail "$3 — pattern [$1] not found in text: [$2]"
  fi
}

refute_grep() {  # refute_grep <ERE-pattern> <text> <what>
  if printf '%s\n' "$2" | grep -qE -- "$1"; then
    fail "$3 — pattern [$1] unexpectedly found in text: [$2]"
  else
    PASS=$((PASS + 1))
  fi
}

# run_v <args...>: run whosaid; leaves rc in RC, stdout in OUT (never
# trips set -e).
run_v() {
  set +e
  OUT="$("$WHOSAID" "$@" 2>&1)"
  RC=$?
  set -e
}

echo "== whosaid version_test =="

# --- whosaid version ---------------------------------------------------
run_v version
assert_eq "$RC" "0" "'whosaid version' exits 0"
assert_grep '^whosaid [0-9]+\.[0-9]+\.[0-9]+' "$OUT" \
  "'whosaid version' stdout starts with 'whosaid X.Y.Z'"
VERSION_TOKENS="$(printf '%s\n' "$OUT" | awk '{print $1, $2}')"

# --- whosaid --version ---------------------------------------------------
run_v --version
assert_eq "$RC" "0" "'whosaid --version' exits 0"
assert_grep '^whosaid [0-9]+\.[0-9]+\.[0-9]+' "$OUT" \
  "'whosaid --version' stdout starts with 'whosaid X.Y.Z'"
refute_grep 'unknown option' "$OUT" \
  "'whosaid --version' does not report 'unknown option'"
LONGOPT_TOKENS="$(printf '%s\n' "$OUT" | awk '{print $1, $2}')"
assert_eq "$LONGOPT_TOKENS" "$VERSION_TOKENS" \
  "'whosaid --version' first token pair matches 'whosaid version'"

# --- whosaid -V ---------------------------------------------------
run_v -V
assert_eq "$RC" "0" "'whosaid -V' exits 0"
assert_grep '^whosaid [0-9]+\.[0-9]+\.[0-9]+' "$OUT" \
  "'whosaid -V' stdout starts with 'whosaid X.Y.Z'"
refute_grep 'unknown option' "$OUT" \
  "'whosaid -V' does not report 'unknown option'"
SHORTOPT_TOKENS="$(printf '%s\n' "$OUT" | awk '{print $1, $2}')"
assert_eq "$SHORTOPT_TOKENS" "$LONGOPT_TOKENS" \
  "'whosaid -V' first token pair matches 'whosaid --version'"

# --- whosaid <audio> --version (transcribe parser must accept it too) ---
run_v nonexistent-file.m4a --version
assert_eq "$RC" "0" "'whosaid <audio> --version' exits 0"
refute_grep 'unknown option' "$OUT" \
  "'whosaid <audio> --version' does not report 'unknown option'"

echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
