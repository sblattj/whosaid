#!/bin/bash
#
# test/install_guard_test.sh — offline test for the temp-directory install
# guard (GitHub issue #10):
#
#   - `whosaid install` from a checkout under a temp directory refuses
#     (exit 1, no symlink created, "temp" mentioned on stderr) unless
#     --force is given.
#   - `whosaid install --force` from the same checkout proceeds and
#     creates the symlink.
#   - `whosaid install --check-only` (bootstrap.sh's internal pre-flight
#     mode) exits 1 there too, without touching the filesystem.
#   - `whosaid doctor` reports a dangling install symlink (the "command
#     not found" symptom this issue is about) with a DANGLING line.
#
# Fully offline, no network, no models. bash 3.2 compatible (macOS
# system bash). Uses real temp directories via mktemp -d/-t and cleans
# up on EXIT.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
TEST_FAILED=0

# A real temp-directory checkout: copy just the whosaid script (install and
# doctor never touch lib/) into a fresh dir under $TMPDIR.
CHECKOUT="$(mktemp -d -t whosaid-guard-checkout)"
INSTALL_DIR="$(mktemp -d)"
CHECKONLY_DIR="$(mktemp -d)"
DANGLE_DIR="$(mktemp -d)"

cleanup() {
  if [ "$TEST_FAILED" -eq 0 ]; then
    rm -rf "$CHECKOUT" "$INSTALL_DIR" "$CHECKONLY_DIR" "$DANGLE_DIR"
  else
    echo "" >&2
    echo "FAIL: $PASS check(s) passed before the failure; leaving temp dirs for inspection:" >&2
    echo "  $CHECKOUT" >&2
    echo "  $INSTALL_DIR" >&2
    echo "  $CHECKONLY_DIR" >&2
    echo "  $DANGLE_DIR" >&2
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

assert_grep() {  # assert_grep <ERE-pattern> <file> <what>
  if grep -qE -- "$1" "$2"; then
    PASS=$((PASS + 1))
  else
    fail "$3 — pattern [$1] not found in $2"
  fi
}

cp "$REPO/whosaid" "$CHECKOUT/whosaid"
chmod +x "$CHECKOUT/whosaid"

# whosaid resolves its own checkout dir with `cd -P` (symlink-resolved), so on
# macOS $TMPDIR paths under /var (-> /private/var) come back rooted at
# /private/var even though we invoke via the /var/... alias. Compare against
# the same resolution so this isn't a false failure.
CHECKOUT_RESOLVED="$(cd -P "$CHECKOUT" && pwd)"

echo "== install_guard_test: temp checkout $CHECKOUT =="

# ---------------------------------------------------------------------------
# 1. install (no --force) from a temp-directory checkout: refuse, no symlink,
#    "temp" mentioned on stderr.
set +e
WHOSAID_INSTALL_DIR="$INSTALL_DIR" "$CHECKOUT/whosaid" install \
  >"$CHECKOUT/.out1" 2>"$CHECKOUT/.err1"
RC=$?
set -e
assert_eq "$RC" 1 "install (no --force) from temp checkout exits 1"
if [ -e "$INSTALL_DIR/whosaid" ] || [ -L "$INSTALL_DIR/whosaid" ]; then
  fail "install (no --force) created something at $INSTALL_DIR/whosaid"
fi
PASS=$((PASS + 1))
assert_grep 'temp' "$CHECKOUT/.err1" "install refusal mentions 'temp' on stderr"

# ---------------------------------------------------------------------------
# 2. install --force from the same temp-directory checkout: proceed, symlink
#    created and pointing at the checkout.
set +e
WHOSAID_INSTALL_DIR="$INSTALL_DIR" "$CHECKOUT/whosaid" install --force \
  >"$CHECKOUT/.out2" 2>"$CHECKOUT/.err2"
RC=$?
set -e
assert_eq "$RC" 0 "install --force from temp checkout exits 0"
if [ ! -L "$INSTALL_DIR/whosaid" ]; then
  fail "install --force did not create a symlink at $INSTALL_DIR/whosaid"
fi
PASS=$((PASS + 1))
LINK_TARGET="$(readlink "$INSTALL_DIR/whosaid")"
assert_eq "$LINK_TARGET" "$CHECKOUT_RESOLVED/whosaid" "install --force symlink points at the temp checkout"

# ---------------------------------------------------------------------------
# 3. install --check-only from the temp-directory checkout: exit 1, no
#    filesystem changes (bootstrap.sh's pre-flight mode).
set +e
WHOSAID_INSTALL_DIR="$CHECKONLY_DIR" "$CHECKOUT/whosaid" install --check-only \
  >"$CHECKOUT/.out3" 2>"$CHECKOUT/.err3"
RC=$?
set -e
assert_eq "$RC" 1 "install --check-only from temp checkout exits 1"
if [ -e "$CHECKONLY_DIR/whosaid" ] || [ -L "$CHECKONLY_DIR/whosaid" ]; then
  fail "install --check-only touched the filesystem at $CHECKONLY_DIR/whosaid"
fi
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# 4. whosaid doctor (run from the REAL, non-temp worktree checkout) detects a
#    dangling install symlink and says so.
ln -s /nonexistent/whosaid "$DANGLE_DIR/whosaid"
set +e
WHOSAID_INSTALL_DIR="$DANGLE_DIR" "$REPO/whosaid" doctor \
  >"$CHECKOUT/.doctor.out" 2>"$CHECKOUT/.doctor.err"
set -e
assert_grep 'DANGLING' "$CHECKOUT/.doctor.out" "doctor reports the dangling install symlink"

# ---------------------------------------------------------------------------
echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
echo "(temp dirs will be removed on exit)"
