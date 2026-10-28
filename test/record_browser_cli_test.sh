#!/bin/bash
#
# test/record_browser_cli_test.sh: offline launcher test for
# `whosaid record --browser` (CDP tab capture, GitHub issue #118). Covers help
# text, flag errors that exit before recording, and the dead-port exit 6. It
# never records. The dead-port check calls lib/record_browser.py and is SKIPped
# while that file is absent.
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
    fail "$3 - expected [$2], got [$1]"
  fi
}

assert_contains() {  # assert_contains <haystack> <needle> <what>
  if printf '%s\n' "$1" | grep -qF -- "$2"; then
    PASS=$((PASS + 1))
  else
    fail "$3 - expected to find [$2] in [$1]"
  fi
}

run_w() {  # run_w <args...>; sets OUT (stdout+stderr) and RC; runs from $TMP, 60s cap
  OUT="$(cd "$TMP" && perl -e 'alarm 60; exec @ARGV' "$WHOSAID" "$@" 2>&1)" && RC=0 || RC=$?
}

echo "-- help documents --browser --"
run_w help
assert_contains "$OUT" "whosaid record --browser" "help lists record --browser"
assert_contains "$OUT" "--remote-debugging-port" "help names the browser flag"

echo "-- flag errors exit 2 before recording --"
run_w record --browser --app X
assert_eq "$RC" 2 "'record --browser --app X' exits 2"
assert_contains "$OUT" "mutually exclusive" "explains --browser/--app"
run_w record --cdp-port 1
assert_eq "$RC" 2 "'record --cdp-port 1' without --browser exits 2"
assert_contains "$OUT" "only apply with --browser" "explains --cdp-port"
run_w record --into /tmp/x
assert_eq "$RC" 2 "'record --into /tmp/x' without --browser exits 2"
run_w record --leave
assert_eq "$RC" 2 "'record --leave' without --browser exits 2"
run_w record --no-mic
assert_eq "$RC" 2 "'record --no-mic' alone still exits 2"
[ ! -e /tmp/x ] || [ -d /tmp/x ] || fail "unexpected /tmp/x"
PASS=$((PASS + 1))

echo "-- dead CDP port exits 6 and leaves no file --"
if [ -f "$REPO/lib/record_browser.py" ]; then
  mkdir -p "$TMP/in"
  run_w record --browser --cdp-port 1 --into "$TMP/in"
  assert_eq "$RC" 6 "dead CDP port exits 6"
  [ -z "$(ls "$TMP/in" 2>/dev/null)" ] || fail "dead-port run left files: $(ls "$TMP/in")"
  PASS=$((PASS + 1))
else
  echo "SKIP: lib/record_browser.py not present yet"
fi

echo ""
echo "PASS: $PASS check(s)"
