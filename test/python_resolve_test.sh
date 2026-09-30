#!/bin/bash
#
# test/python_resolve_test.sh — offline test for the launcher's Python
# interpreter resolution (GitHub issue #52).
#
# lib/wsconfig.py reads whosaid.toml with tomllib (Python 3.11+). The launcher
# used to run bare `python3`, which on a stock Mac is 3.9: the config was
# silently ignored. The launcher now resolves ONE interpreter (WHOSAID_PYTHON,
# else the first of python3/python3.13/.12/.11 that is >= 3.11, else
# `uv python find '>=3.11'`) and runs every module with it.
#
# Fully offline: no models, no network, no audio. Uses `whosaid worklist`,
# whose owner comes from [workspace] owner in whosaid.toml when no meeting has
# a self role: with the config honored it prints "# Worklist: Bob_Example",
# with the config ignored it exits 1 ("no owner").
#
# The "old" interpreter is a stub python3 that answers the launcher's version
# probe as 3.9 and, for real work, runs a real >= 3.11 Python with tomllib and
# tomli shadowed by modules that raise ImportError — i.e. a Python 3.9 as far
# as load_config can tell.
#
# macOS/BSD only: bash 3.2 (no associative arrays, no bash-4-isms).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WHOSAID="$REPO/whosaid"

PASS=0
TEST_FAILED=0
TMP="$(mktemp -d)"

cleanup() {
  if [ "$TEST_FAILED" -eq 0 ]; then
    rm -rf "$TMP"
  else
    echo "" >&2
    echo "FAIL: $PASS check(s) passed before the failure; leaving temp dir for inspection: $TMP" >&2
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

echo "== whosaid python_resolve_test =="

# --- a real Python >= 3.11 to stand in for the good interpreter ------------
REAL=""
for cand in python3 python3.14 python3.13 python3.12 python3.11; do
  p="$(command -v "$cand" 2>/dev/null || true)"
  if [ -n "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    REAL="$p"; break
  fi
done
if [ -z "$REAL" ] && command -v uv >/dev/null 2>&1; then
  p="$(uv python find '>=3.11' 2>/dev/null | head -n 1 || true)"
  [ -n "$p" ] && REAL="$p"
fi
if [ -z "$REAL" ]; then
  echo "SKIP: no Python >= 3.11 on this machine — test/python_resolve_test.sh needs one." >&2
  exit 0
fi

# --- fixtures ------------------------------------------------------------------
WS="$TMP/ws"
mkdir -p "$WS/2026-09-15-0900"
printf '[workspace]\nowner = "Bob_Example"\n' > "$WS/whosaid.toml"
cat > "$WS/2026-09-15-0900/commitments.json" <<'JSON'
{"roles": {}, "items": [
  {"speaker": "Bob_Example", "speaker_role": null, "text": "I'll send the agenda", "time": "00:00:04",
   "cue": "i'll send", "negative": false, "priority": "normal"}
]}
JSON

# blocked/: shadows tomllib and tomli so the stub behaves like Python 3.9.
BLOCK="$TMP/blocked"
mkdir -p "$BLOCK"
for m in tomllib tomli; do
  echo "raise ImportError('$m is not available (simulated Python < 3.11)')" > "$BLOCK/$m.py"
done

# OLD/python3: a "3.9" python3.
OLD="$TMP/old"
mkdir -p "$OLD"
cat > "$OLD/python3" <<STUB
#!/bin/sh
# stub "Python 3.9": fails the launcher's >= 3.11 version probe
case "\$*" in *version_info*) exit 1 ;; esac
PYTHONPATH="$BLOCK" exec "$REAL" "\$@"
STUB
chmod +x "$OLD/python3"

# NEW/python3.13: a good interpreter that is NOT named python3.
NEW="$TMP/new"
mkdir -p "$NEW"
ln -s "$REAL" "$NEW/python3.13"

# Minimal PATH: no python3 other than the stub. The launcher appends Homebrew
# and ~/.local dirs itself, so the no-interpreter case (section 3) runs a copy
# of the launcher without that line.
BASE_PATH="/usr/bin:/bin:/usr/sbin:/sbin"

# run_ws <launcher> <path> [VAR=val ...]: whosaid worklist "$WS"; sets RC, OUT.
run_ws() {
  local launcher="$1" path="$2"; shift 2
  set +e
  OUT="$(env -u WHOSAID_PYTHON -u WHOSAID_OWNER ${1+"$@"} PATH="$path" \
         WHOSAID_TODAY=2026-09-17 WHOSAID_OLLAMA=http://127.0.0.1:9 \
         "$launcher" worklist "$WS" 2>&1)"
  RC=$?
  set -e
}

# --- 1. old python3 first, good python3.13 behind it -> config honored -------
run_ws "$WHOSAID" "$OLD:$NEW:$BASE_PATH"
assert_eq "$RC" "0" "1. python3 is 3.9, python3.13 is reachable: worklist exits 0"
assert_grep '^# Worklist: Bob_Example' "$OUT" \
  "1. the launcher skipped the 3.9 python3, so the whosaid.toml owner was honored"
refute_grep 'WARN whosaid.toml' "$OUT" "1. no whosaid.toml WARN"

# --- 2. WHOSAID_PYTHON wins ----------------------------------------------------
# 2a: over a PATH whose only python3 is the 3.9 stub.
run_ws "$WHOSAID" "$OLD:$BASE_PATH" "WHOSAID_PYTHON=$REAL"
assert_eq "$RC" "0" "2a. WHOSAID_PYTHON over a 3.9-only PATH: exits 0"
assert_grep '^# Worklist: Bob_Example' "$OUT" "2a. the WHOSAID_PYTHON interpreter is used"

# 2b: with a good python3.13 also on PATH, a tracing wrapper proves which ran.
TRACE="$TMP/trace"
cat > "$TMP/traced-python" <<TRACED
#!/bin/sh
echo "traced" >> "$TRACE"
exec "$REAL" "\$@"
TRACED
chmod +x "$TMP/traced-python"
: > "$TRACE"
run_ws "$WHOSAID" "$NEW:$BASE_PATH" "WHOSAID_PYTHON=$TMP/traced-python"
assert_eq "$RC" "0" "2b. WHOSAID_PYTHON with python3.13 also on PATH: exits 0"
assert_grep '^traced$' "$(cat "$TRACE")" "2b. WHOSAID_PYTHON ran, not the PATH python3.13"

# --- 3. nothing >= 3.11: exit 1 with the actionable error ------------------------
# A copy of the launcher minus its PATH-extension line (so a Homebrew python on
# this machine cannot rescue it), with lib/ linked back to the repo. PATH holds
# only the 3.9 stub and the system dirs: no uv, nothing >= 3.11.
NOCOPY="$TMP/launcher"
mkdir -p "$NOCOPY"
grep -v '^export PATH="\$PATH:\$HOME/.local/bin' "$WHOSAID" > "$NOCOPY/whosaid"
[ "$(wc -l < "$NOCOPY/whosaid")" -eq "$(( $(wc -l < "$WHOSAID") - 1 ))" ] \
  || fail "3. expected to strip exactly the PATH-extension line from the launcher copy"
chmod +x "$NOCOPY/whosaid"
ln -s "$REPO/lib" "$NOCOPY/lib"
if PATH="$BASE_PATH" command -v uv >/dev/null 2>&1; then
  fail "3. test setup: uv is on the minimal PATH, so the uv fallback would rescue the launcher"
fi
run_ws "$NOCOPY/whosaid" "$OLD:$BASE_PATH"
assert_eq "$RC" "1" "3. no Python >= 3.11 anywhere: worklist exits 1"
assert_grep 'WHOSAID_PYTHON' "$OUT" "3. the error names WHOSAID_PYTHON"
assert_grep 'uv python install 3\.13' "$OUT" "3. the error names 'uv python install 3.13'"
assert_eq "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')" "1" "3. the error is one line"

# 3b: version needs no Python and still runs.
set +e
OUT="$(PATH="$OLD:$BASE_PATH" "$NOCOPY/whosaid" version 2>&1)"; RC=$?
set -e
assert_eq "$RC" "0" "3b. 'whosaid version' still works with no Python >= 3.11"

# --- 4. doctor prints the resolved interpreter and its version ---------------------
set +e
OUT="$(env -u WHOSAID_PYTHON PATH="$OLD:$NEW:$BASE_PATH" "$WHOSAID" doctor 2>&1)"
set -e
assert_grep "python: .*python3\.13 \([0-9]+\.[0-9]+\.[0-9]+\)" "$OUT" \
  "4. doctor reports the resolved interpreter (python3.13) and its version"

# --- 5. wsconfig itself: no tomllib and no tomli -> loud, specific WARN -----------
set +e
OUT="$(PYTHONPATH="$BLOCK:$REPO/lib" "$REAL" -c '
import sys, pathlib
import wsconfig
cfg = wsconfig.load_config(pathlib.Path(sys.argv[1]))
print("owner=%r" % cfg["workspace"].get("owner"))
' "$WS" 2>&1)"
RC=$?
set -e
assert_eq "$RC" "0" "5. load_config without tomllib/tomli does not crash"
assert_grep 'whosaid\.toml IGNORED \(needs Python 3\.11\+ or tomli\): owner/aliases/groups/summarizer fall back to defaults' \
  "$OUT" "5. the WARN says plainly what is lost"

echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
