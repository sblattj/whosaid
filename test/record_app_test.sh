#!/bin/bash
#
# test/record_app_test.sh: offline launcher test for `whosaid record --app`
# (ScreenCaptureKit app-audio capture). Covers help text and flag errors, which
# all exit before recording, plus a swiftc typecheck of lib/record_app.swift
# when swiftc is present (SKIP otherwise). Capture itself needs a display and
# a Screen Recording grant, so it is not exercised here.
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

run_w() {  # run_w <args...>; sets OUT (stdout+stderr) and RC; runs from $TMP
  OUT="$(cd "$TMP" && "$WHOSAID" "$@" 2>&1)" && RC=0 || RC=$?
}

echo "-- help documents --app --"
run_w help
assert_contains "$OUT" "whosaid record --app A" "help lists record --app"
assert_contains "$OUT" "--list-apps" "help lists --list-apps"
assert_contains "$OUT" "left = mic (you), right = the app (them)" "help documents the channel layout"

echo "-- record --help prints help instead of recording --"
for flag in --help -h; do
  # alarm: before the fix, --help started a mic recording that ran up to 3h.
  OUT="$(cd "$TMP" && perl -e 'alarm 30; exec @ARGV' "$WHOSAID" record $flag 2>&1)" && RC=0 || RC=$?
  assert_eq "$RC" 0 "'record $flag' exits 0"
  assert_contains "$OUT" "whosaid record --app A" "'record $flag' prints the help"
  case "$OUT" in *"whosaid: recording"*) fail "'record $flag' started a recording" ;; esac
  PASS=$((PASS + 1))
done
[ -z "$(ls "$TMP" 2>/dev/null)" ] || fail "record --help left files behind: $(ls "$TMP")"
PASS=$((PASS + 1))

echo "-- flag errors exit 2 before recording --"
run_w record --no-mic
assert_eq "$RC" 2 "'record --no-mic' without --app exits 2"
assert_contains "$OUT" "--no-mic only applies with --app" "explains --no-mic"
run_w record --app
assert_eq "$RC" 2 "'record --app' with no value exits 2"
assert_contains "$OUT" "--app needs an app" "explains the missing value"

echo "-- call flags (#119) exit 2 without --app or --until-call-ends --"
for flag in --wait-for-call --until-call-ends; do
  run_w record $flag
  assert_eq "$RC" 2 "'record $flag' without --app exits 2"
  assert_contains "$OUT" "only apply with --app" "explains $flag"
done
run_w record --app X --idle-seconds 10
assert_eq "$RC" 2 "'--idle-seconds' without --until-call-ends exits 2"
assert_contains "$OUT" "only applies with --until-call-ends" "explains --idle-seconds"
run_w record --app X --until-call-ends --idle-seconds 0
assert_eq "$RC" 2 "'--idle-seconds 0' exits 2"
run_w record --app X --until-call-ends --idle-seconds abc
assert_eq "$RC" 2 "'--idle-seconds abc' exits 2"
run_w record --into "$TMP/in"
assert_eq "$RC" 2 "'record --into' without --app or --browser exits 2"
assert_contains "$OUT" "--into only applies with --browser or --app" "explains --into"
[ ! -e "$TMP/in" ] || fail "record --into without a source created $TMP/in"
PASS=$((PASS + 1))
run_w help
assert_contains "$OUT" "--wait-for-call" "help lists --wait-for-call"
assert_contains "$OUT" "--until-call-ends" "help lists --until-call-ends"

echo "-- ingest defaults --me to the workspace owner (#119) --"
mkdir -p "$TMP/ws"
printf '[workspace]\nowner = "Pat_Example"\n' > "$TMP/ws/whosaid.toml"
assert_eq "$(env -u WHOSAID_OWNER python3 "$REPO/lib/wsconfig.py" owner "$TMP/ws")" "Pat_Example" "wsconfig owner reads [workspace] owner"
assert_eq "$(WHOSAID_OWNER=Env_Name python3 "$REPO/lib/wsconfig.py" owner "$TMP/ws")" "Env_Name" "WHOSAID_OWNER overrides the file"
assert_eq "$(env -u WHOSAID_OWNER python3 "$REPO/lib/wsconfig.py" owner "$TMP")" "" "no owner prints empty"
rm -rf "$TMP/ws" "$TMP/whosaid.toml"

echo "-- helper source typechecks --"
if command -v swiftc >/dev/null 2>&1; then
  swiftc -typecheck -swift-version 5 -target "$(uname -m)-apple-macos15.0" "$REPO/lib/record_app.swift"
  PASS=$((PASS + 1))
else
  echo "SKIP: swiftc not found"
fi

echo ""
echo "PASS: $PASS check(s)"
