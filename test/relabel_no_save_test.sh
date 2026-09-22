#!/bin/bash
#
# test/relabel_no_save_test.sh — offline launcher test for the relabel
# transcript-only flags (GitHub issue #19): `whosaid relabel ... --no-save`,
# `--force`, and `--note TEXT` must parse as flag passthroughs (not targets or
# SPEAKER_XX=Name specs) and reach the diarizer side.
#
# Fast, fully offline: every path tested exits in resolve_sidecar before any
# transcription, model load, or network access (each fails on a nonexistent
# sidecar). macOS/BSD only: BSD grep, bash 3.2 (no associative arrays,
# no bash-4-isms).

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

echo "-- --no-save parses and hits the sidecar-missing path --"
run_w relabel does-not-exist SPEAKER_00=Alice --no-save
assert_eq "$RC" 1 "'relabel ... --no-save' with no sidecar exits 1"
assert_contains "$OUT" "could not find a diarization sidecar" "'--no-save' reaches sidecar resolution"
assert_not_contains "$OUT" "unexpected arg" "'--no-save' is not rejected as an unexpected arg"

echo "-- --force parses and hits the sidecar-missing path --"
run_w relabel does-not-exist SPEAKER_00=Alice --force
assert_eq "$RC" 1 "'relabel ... --force' with no sidecar exits 1"
assert_contains "$OUT" "could not find a diarization sidecar" "'--force' reaches sidecar resolution"
assert_not_contains "$OUT" "unexpected arg" "'--force' is not rejected as an unexpected arg"

echo "-- --note parses (with its TEXT argument) --"
run_w relabel does-not-exist SPEAKER_00=Alice --note "why not"
assert_eq "$RC" 1 "'relabel ... --note' with no sidecar exits 1"
assert_contains "$OUT" "could not find a diarization sidecar" "'--note' reaches sidecar resolution"
assert_not_contains "$OUT" "unexpected arg" "'--note' and its text are not rejected as unexpected args"

echo "-- all three flags together --"
run_w relabel does-not-exist SPEAKER_00=Alice --no-save --force --note x
assert_eq "$RC" 1 "'relabel ... --no-save --force --note' with no sidecar exits 1"
assert_contains "$OUT" "could not find a diarization sidecar" "the flag combination reaches sidecar resolution"
assert_not_contains "$OUT" "unexpected arg" "the flag combination is not rejected as unexpected args"

echo "-- --fold-unknown validation and passthrough --"
run_w relabel does-not-exist SPEAKER_00=Alice --fold-unknown
assert_eq "$RC" 1 "'--fold-unknown' without '--auto' exits 1"
assert_contains "$OUT" "--fold-unknown requires --auto" "cached unknown folding requires auto mode"
assert_not_contains "$OUT" "could not find a diarization sidecar" "invalid fold mode fails before sidecar resolution"

run_w relabel does-not-exist --auto --fold-unknown
assert_eq "$RC" 1 "'--auto --fold-unknown' with no sidecar exits 1"
assert_contains "$OUT" "could not find a diarization sidecar" "valid fold mode reaches sidecar resolution"
assert_not_contains "$OUT" "unexpected arg" "'--fold-unknown' is accepted by the shell parser"

echo "-- help documents the new flags --"
run_w help
assert_eq "$RC" 0 "'whosaid help' exits 0"
assert_contains "$OUT" "--no-save" "'whosaid help' documents --no-save"
assert_contains "$OUT" "--force" "'whosaid help' documents --force"
assert_contains "$OUT" "--fold-unknown" "'whosaid help' documents cached unknown folding"

echo "-- no-arg usage hint --"
run_w relabel
assert_eq "$RC" 1 "'whosaid relabel' with no args exits 1"
assert_contains "$OUT" "SPEAKER_02=Jane" "the no-arg usage hint names the SPEAKER_XX=Name form"

echo ""
echo "OK: $PASS check(s) passed"
