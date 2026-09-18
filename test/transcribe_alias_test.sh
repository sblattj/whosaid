#!/bin/bash
#
# test/transcribe_alias_test.sh — offline launcher test for the `transcribe`
# subcommand alias (GitHub issue #18): `whosaid transcribe FILE ...` must
# behave exactly like `whosaid FILE ...`, instead of parsing `transcribe`
# as a second input file.
#
# Fast, fully offline: every path tested exits before any transcription,
# model load, or network access (each fails on a nonexistent input file).
# macOS/BSD only: BSD grep, bash 3.2 (no associative arrays, no bash-4-isms).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WHOSAID="$REPO/whosaid"

PASS=0
TEST_FAILED=0
TMP="$(mktemp -d)"

cleanup() {
  if [ "$TEST_FAILED" -ne 0 ]; then
    echo "" >&2
    echo "FAIL: $PASS check(s) passed before the failure; leaving temp dir for inspection: $TMP" >&2
  else
    rm -rf "$TMP"
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

assert_contains() {  # assert_contains <haystack> <needle> <what>
  if printf '%s\n' "$1" | grep -qF -- "$2"; then
    PASS=$((PASS + 1))
  else
    fail "$3 — expected to find [$2] in [$1]"
  fi
}

assert_not_contains() {  # assert_not_contains <haystack> <needle> <what>
  if printf '%s\n' "$1" | grep -qF -- "$2"; then
    fail "$3 — expected NOT to find [$2] in [$1]"
  else
    PASS=$((PASS + 1))
  fi
}

run_w() {  # run_w <args...>; sets OUT (stdout+stderr) and RC; runs from $TMP
  OUT="$(cd "$TMP" && "$WHOSAID" "$@" 2>&1)" && RC=0 || RC=$?
}

echo "-- syntax --"
bash -n "$WHOSAID"
PASS=$((PASS + 1))

echo "-- help mentions the alias --"
run_w transcribe --help
assert_eq "$RC" 0 "'whosaid transcribe --help' exits 0"
assert_contains "$OUT" "USAGE" "'whosaid transcribe --help' prints help"
assert_contains "$OUT" "whosaid transcribe <audio>" "'whosaid transcribe --help' documents the alias"

echo "-- transcribe token is not an input file --"
run_w transcribe does-not-exist.m4a --speakers 7
assert_eq "$RC" 1 "'whosaid transcribe missing.m4a --speakers 7' exits 1"
assert_contains "$OUT" "file not found: does-not-exist.m4a" "points at the real input"
assert_not_contains "$OUT" "file not found: transcribe" "never treats 'transcribe' as a file"

echo "-- --name works with the alias --"
run_w transcribe does-not-exist.m4a -n meeting
assert_eq "$RC" 1 "'whosaid transcribe missing.m4a -n meeting' exits 1"
assert_contains "$OUT" "file not found: does-not-exist.m4a" "points at the real input"
assert_not_contains "$OUT" "--name only works with a single input" "single input is not miscounted"

echo "-- alias with no input --"
run_w transcribe
assert_eq "$RC" 1 "'whosaid transcribe' (no file) exits 1"
assert_contains "$OUT" "no audio file given." "asks for a file"

echo ""
echo "OK: $PASS check(s) passed"
