#!/bin/bash
#
# test/run_all.sh: run every offline suite (test/*_test.py, *_test.sh, *_test.js) with the
# dependencies it needs, one process per file, and print one PASS/FAIL line per file plus a
# summary. Exits 1 when any file fails. (#237)
#
#   ./test/run_all.sh                 # every suite
#   ./test/run_all.sh drafts backup   # only files whose name contains one of the words
#
# Python suites run under `uv run` with pyproject.toml's `test` dependency group (numpy, mcp[cli],
# websockets), so a stdlib-only suite and an MCP or CDP suite share one command. JavaScript
# suites that import bun:test run under `bun test`, the rest under node.
# Each file gets WHOSAID_TEST_FILE_TIMEOUT seconds (default 900). Logs go to $WHOSAID_TEST_LOGS
# (default a new temp dir, printed at the end). Nothing here plays audio or touches the real
# workspace; the suites themselves work in temp dirs.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
LIMIT="${WHOSAID_TEST_FILE_TIMEOUT:-900}"
LOGS="${WHOSAID_TEST_LOGS:-$(mktemp -d -t whosaid-tests)}"
mkdir -p "$LOGS"

# The test dependency group in pyproject.toml, as --with flags (one source for the list).
PY=(uv run --quiet)
while IFS= read -r dep; do
  [ -n "$dep" ] && PY+=(--with "$dep")
done < <(python3 -c 'import sys, tomllib; print("\n".join(tomllib.load(open(sys.argv[1], "rb"))["dependency-groups"]["test"]))' "$REPO/pyproject.toml")
[ "${#PY[@]}" -gt 2 ] || { echo "run_all: no [dependency-groups] test in pyproject.toml" >&2; exit 2; }
PY+=(python)

with_deadline() {  # macOS has no GNU timeout
  perl -e 'alarm shift; exec @ARGV or die "exec $ARGV[0]: $!"' "$LIMIT" "$@"
}

wanted() {  # wanted <basename> [filters...]
  local name="$1"; shift
  [ "$#" -eq 0 ] && return 0
  local w
  for w in "$@"; do
    case "$name" in *"$w"*) return 0 ;; esac
  done
  return 1
}

cd "$REPO" || exit 2
pass=0
failed=()
for f in test/*_test.py test/*_test.sh test/*_test.js; do
  name="$(basename "$f")"
  wanted "$name" "$@" || continue
  log="$LOGS/$name.log"
  start=$SECONDS
  case "$f" in
    *.py) with_deadline "${PY[@]}" "$f" >"$log" 2>&1 ;;
    *.sh) with_deadline bash "$f" >"$log" 2>&1 ;;
    *.js)
      if grep -q "from 'bun:test'\|from \"bun:test\"" "$f"; then
        with_deadline bun test "./$f" >"$log" 2>&1
      else
        with_deadline node "$f" >"$log" 2>&1
      fi ;;
  esac
  rc=$?
  secs=$((SECONDS - start))
  if [ "$rc" -eq 0 ]; then
    pass=$((pass + 1))
    printf 'PASS  %-44s %4ss\n' "$name" "$secs"
  else
    failed+=("$name")
    printf 'FAIL  %-44s %4ss  rc=%s  log: %s\n' "$name" "$secs" "$rc" "$log"
  fi
done

echo ""
echo "passed: $pass  failed: ${#failed[@]}  logs: $LOGS"
if [ "${#failed[@]}" -ne 0 ]; then
  printf '  failed: %s\n' "${failed[@]}"
  exit 1
fi
