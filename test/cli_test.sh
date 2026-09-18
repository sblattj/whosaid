#!/bin/bash
#
# test/cli_test.sh: offline test for the launcher's workspace-search surface
# (GitHub issue #14): `whosaid index | search | context | graph | wiki | watch |
# memos`, plus the `--index` / `--engine` additions to ingest and roll-up.
#
# Sections 1-3 need nothing but bash and the launcher: help text, usage hints,
# and the -h/--help paths. Section 4 is an end-to-end run over a synthetic
# two-meeting workspace (placeholder speakers, hand-written transcripts in
# the real diarizer format) and only runs when lib/search.py and lib/graph.py
# both exist; otherwise it prints a SKIP line and the test still passes.
# Section 5 pushes a stub ingest through `--index` and needs ffmpeg/ffprobe
# for the 1s fixture (SKIP without them). No models, no network: the index is
# built with --no-embed so Ollama is never contacted.
#
# macOS/BSD only: BSD grep/awk, bash 3.2 (no associative arrays, no bash-4-isms).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WHOSAID="$REPO/whosaid"
SEARCH_PY="$REPO/lib/search.py"
GRAPH_PY="$REPO/lib/graph.py"

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
    fail "$3: expected [$2], got [$1]"
  fi
}

assert_ne() {  # assert_ne <actual> <unexpected> <what>
  if [ "$1" != "$2" ]; then
    PASS=$((PASS + 1))
  else
    fail "$3: expected anything but [$2], got [$1]"
  fi
}

assert_text() {  # assert_text <ERE-pattern> <text> <what>
  if printf '%s\n' "$2" | grep -qE -- "$1"; then
    PASS=$((PASS + 1))
  else
    fail "$3: pattern [$1] not found in text: [$2]"
  fi
}

assert_file() {  # assert_file <path> <what>
  if [ -s "$1" ]; then
    PASS=$((PASS + 1))
  else
    fail "$2: missing or empty: $1"
  fi
}

# run_w <args...>: run the launcher; leaves rc in RC and combined output in
# OUT (never trips set -e). Every command here is expected to be offline.
run_w() {
  set +e
  OUT="$("$WHOSAID" "$@" 2>&1)"
  RC=$?
  set -e
}

echo "== whosaid cli_test: temp dir $TMP =="

# ---------------------------------------------------------------------------
# 0. Static checks.
# ---------------------------------------------------------------------------
[ -f "$WHOSAID" ] || fail "launcher missing: $WHOSAID"
bash -n "$WHOSAID" || fail "bash -n failed on whosaid"
PASS=$((PASS + 1))
bash -n "$SCRIPT_PATH" || fail "bash -n failed on test/cli_test.sh"
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# 1. `whosaid help` names every new command, flag, and env var.
# ---------------------------------------------------------------------------
echo "-- help --"

run_w help
assert_eq "$RC" 0 "'whosaid help' exits 0"
for token in \
  'whosaid index <ws>' \
  'whosaid search <ws>' \
  'whosaid context <ws>' \
  'whosaid graph <ws>' \
  'whosaid wiki <ws>' \
  'whosaid watch run\|install\|uninstall\|status' \
  'whosaid memos list\|pull\|delete\|shortcut-recipe' \
  '^WORKSPACE SEARCH$' \
  '^WATCH$' \
  '--engine E' \
  '--index' \
  'WHOSAID_WORKSPACE' \
  'WHOSAID_OWNER' \
  'WHOSAID_OLLAMA' \
  'WHOSAID_SUMMARIZER_MODEL' \
  'WHOSAID_BIN' \
  '_search\.db' \
  '_WIKI\.md' \
  'whosaid\.toml'; do
  assert_text "$token" "$OUT" "'whosaid help' mentions [$token]"
done

# ---------------------------------------------------------------------------
# 2. Missing-argument paths exit non-zero with a usage hint, before any
#    module is loaded (so they hold even on a partial checkout).
# ---------------------------------------------------------------------------
echo "-- usage hints --"

run_w search
assert_ne "$RC" 0 "'whosaid search' with no args exits non-zero"
assert_text 'usage: whosaid search' "$OUT" "'whosaid search' prints a usage hint"

run_w context
assert_ne "$RC" 0 "'whosaid context' with no args exits non-zero"
assert_text 'usage: whosaid context' "$OUT" "'whosaid context' prints a usage hint"

run_w graph
assert_ne "$RC" 0 "'whosaid graph' with no args exits non-zero"
assert_text 'usage: whosaid graph' "$OUT" "'whosaid graph' prints a usage hint"

run_w graph "$TMP"
assert_ne "$RC" 0 "'whosaid graph <ws>' without a view exits non-zero"
assert_text 'need a view' "$OUT" "'whosaid graph <ws>' says a view is needed"

run_w watch
assert_ne "$RC" 0 "'whosaid watch' with no args exits non-zero"
assert_text 'usage: whosaid watch' "$OUT" "'whosaid watch' prints a usage hint"

run_w watch frobnicate
assert_ne "$RC" 0 "'whosaid watch frobnicate' exits non-zero"
assert_text 'unknown subcommand' "$OUT" "'whosaid watch frobnicate' names the bad subcommand"

run_w memos
assert_ne "$RC" 0 "'whosaid memos' with no args exits non-zero"
assert_text 'usage: whosaid memos' "$OUT" "'whosaid memos' prints a usage hint"

run_w index --bogus-flag
assert_ne "$RC" 0 "'whosaid index --bogus-flag' exits non-zero"
assert_text 'unknown option|module not found' "$OUT" "'whosaid index --bogus-flag' is rejected"

# ---------------------------------------------------------------------------
# 3. -h/--help on every new command answers with the relevant help section
#    and exits 0, whether or not the modules exist.
# ---------------------------------------------------------------------------
echo "-- help paths --"

for cmd in index search context graph wiki; do
  run_w "$cmd" --help
  assert_eq "$RC" 0 "'whosaid $cmd --help' exits 0"
  assert_text '^WORKSPACE SEARCH$' "$OUT" "'whosaid $cmd --help' prints the WORKSPACE SEARCH section"
  run_w "$cmd" -h
  assert_eq "$RC" 0 "'whosaid $cmd -h' exits 0"
done
for cmd in watch memos; do
  run_w "$cmd" --help
  assert_eq "$RC" 0 "'whosaid $cmd --help' exits 0"
  assert_text '^WATCH$' "$OUT" "'whosaid $cmd --help' prints the WATCH section"
  assert_text 'whosaid memos delete' "$OUT" "'whosaid $cmd --help' covers the memos helpers"
done

run_w ingest --help
assert_eq "$RC" 0 "'whosaid ingest --help' exits 0"
assert_text '\-\-engine E' "$OUT" "'whosaid ingest --help' documents --engine"
run_w roll-up --help
assert_eq "$RC" 0 "'whosaid roll-up --help' exits 0"
assert_text "Run 'whosaid index <ws>' after the roll-up" "$OUT" "'whosaid roll-up --help' documents --index"

# ---------------------------------------------------------------------------
# 4. End-to-end over a synthetic workspace (needs lib/search.py + lib/graph.py).
# ---------------------------------------------------------------------------
echo "-- end-to-end --"

# make_ws <dir>: two dated meetings with speaker-labeled transcripts and
# per-meeting action items, all placeholder names.
make_ws() {
  local ws="$1"
  mkdir -p "$ws/2026-09-16-0900" "$ws/2026-09-17-0900"
  cat > "$ws/2026-09-16-0900/transcript.speakers.txt" <<'EOF'
# Speakers (2): Alice_Example, Bob_Example
[00:00:01] Alice_Example: Let's start with the roadmap for next quarter.
[00:00:06] Bob_Example: I will review the budget spreadsheet before Thursday.
[00:00:14] Alice_Example: Great, and I will send the draft out to the team today.
EOF
  cat > "$ws/2026-09-16-0900/action-items.md" <<'EOF'
# Action items: 2026-09-16-0900

- **Bob_Example:** review the budget spreadsheet before Thursday
- **Alice_Example:** send the draft out to the team today
EOF
  cat > "$ws/2026-09-17-0900/transcript.speakers.txt" <<'EOF'
# Speakers (3): Alice_Example, Bob_Example, Carol_Example
[00:00:02] Carol_Example: PR 42 is ready for review, can someone take a look?
[00:00:09] Bob_Example: I can take it after standup.
[00:00:15] Alice_Example: Also the onboarding checklist needs an update this week.
EOF
  cat > "$ws/2026-09-17-0900/action-items.md" <<'EOF'
# Action items: 2026-09-17-0900

- **Bob_Example:** review PR 42 after standup
- **Alice_Example:** update the onboarding checklist this week
EOF
  cat > "$ws/whosaid.toml" <<'EOF'
[workspace]
owner = "Alice_Example"

[groups]
team = ["Bob_Example", "Carol_Example"]
EOF
}

if [ ! -f "$SEARCH_PY" ] || [ ! -f "$GRAPH_PY" ]; then
  echo "SKIP: end-to-end section needs lib/search.py and lib/graph.py (missing: $([ -f "$SEARCH_PY" ] || printf 'search.py ')$([ -f "$GRAPH_PY" ] || printf 'graph.py'))" >&2
else
  command -v python3 >/dev/null 2>&1 || fail "python3 is required for the end-to-end section"
  python3 -m py_compile "$SEARCH_PY" || fail "python3 -m py_compile failed on lib/search.py"
  python3 -m py_compile "$GRAPH_PY" || fail "python3 -m py_compile failed on lib/graph.py"

  WS="$TMP/ws"
  make_ws "$WS"

  # roll-up first so the manifest + corpus exist for the graph tables; the
  # audit flags the missing audio but exits 0.
  run_w roll-up "$WS" --action-items
  assert_eq "$RC" 0 "roll-up on the synthetic workspace exits 0"
  assert_file "$WS/_workspace.json" "roll-up wrote the manifest"
  assert_file "$WS/_ACTION-ITEMS.md" "roll-up wrote the corpus"

  run_w index "$WS" --no-embed
  assert_eq "$RC" 0 "'whosaid index <ws> --no-embed' exits 0 (output: $OUT)"
  assert_file "$WS/_search.db" "index wrote _search.db"
  assert_file "$WS/_WIKI.md" "index wrote _WIKI.md"
  assert_text 'index complete' "$OUT" "index reports completion"

  run_w search "$WS" "budget"
  assert_eq "$RC" 0 "'whosaid search <ws> budget' exits 0"
  assert_text 'budget' "$OUT" "search finds the planted 'budget' line"
  assert_text 'Bob_Example' "$OUT" "search hit names the speaker"
  assert_text '2026-09-16-0900' "$OUT" "search hit names the meeting"
  assert_text '00:00:06' "$OUT" "search hit carries the timestamp"

  run_w search "$WS" "budget" --mode exact --speaker Bob_Example -k 3 --json
  assert_eq "$RC" 0 "'whosaid search ... --json' exits 0"
  JSONCHECK="$(printf '%s\n' "$OUT" | python3 -c '
import json, sys
raw = sys.stdin.read()
start = raw.find("[")
hits = json.loads(raw[start:])
assert isinstance(hits, list) and len(hits) >= 1, hits
assert any("budget" in h.get("text", "") for h in hits), hits
assert all(h.get("speaker") == "Bob_Example" for h in hits), hits
print("ok")
')" || fail "search --json output did not parse as a hit list"
  assert_eq "$JSONCHECK" "ok" "search --json parses, filters by --speaker, and contains the hit"

  run_w search "$WS" "no_such_token_zzz"
  assert_eq "$RC" 0 "search with no hits still exits 0"

  run_w context "$WS" 2026-09-16-0900 00:00:06
  assert_eq "$RC" 0 "'whosaid context <ws> <meeting> <HH:MM:SS>' exits 0"
  assert_text 'Bob_Example: I will review the budget' "$OUT" "context prints the turn at the timestamp"
  assert_text 'Alice_Example' "$OUT" "context prints the surrounding turns"

  run_w graph "$WS" meetings
  assert_eq "$RC" 0 "'whosaid graph <ws> meetings' exits 0"
  assert_text '2026-09-16-0900' "$OUT" "graph meetings lists meeting 1"
  assert_text '2026-09-17-0900' "$OUT" "graph meetings lists meeting 2"

  run_w graph "$WS" speakers
  assert_eq "$RC" 0 "'whosaid graph <ws> speakers' exits 0"
  assert_text 'Alice_Example' "$OUT" "graph speakers lists the placeholder speaker"

  run_w graph "$WS" items --json
  assert_eq "$RC" 0 "'whosaid graph <ws> items --json' exits 0"
  assert_text 'AI-001' "$OUT" "graph items carries the corpus ids"

  run_w graph "$WS" item AI-001
  assert_eq "$RC" 0 "'whosaid graph <ws> item AI-001' exits 0"
  assert_text 'budget' "$OUT" "graph item shows the item text"

  run_w graph "$WS" person Alice_Example
  assert_eq "$RC" 0 "'whosaid graph <ws> person Alice_Example' exits 0"

  run_w graph "$WS" prs
  assert_eq "$RC" 0 "'whosaid graph <ws> prs' exits 0"

  run_w wiki "$WS" --stdout
  assert_eq "$RC" 0 "'whosaid wiki <ws> --stdout' exits 0"
  assert_text '2026-09-16-0900' "$OUT" "wiki --stdout mentions a meeting"

  # <ws> omitted: the modules fall back to $WHOSAID_WORKSPACE.
  set +e
  OUT="$(WHOSAID_WORKSPACE="$WS" "$WHOSAID" search "budget" 2>&1)"
  RC=$?
  set -e
  assert_eq "$RC" 0 "'whosaid search <query>' with WHOSAID_WORKSPACE set exits 0"
  assert_text 'budget' "$OUT" "search via WHOSAID_WORKSPACE finds the hit"

  set +e
  OUT="$(WHOSAID_WORKSPACE="$WS" "$WHOSAID" graph meetings 2>&1)"
  RC=$?
  set -e
  assert_eq "$RC" 0 "'whosaid graph meetings' (view first, no <ws>) exits 0 with WHOSAID_WORKSPACE"
  assert_text '2026-09-17-0900' "$OUT" "graph meetings via WHOSAID_WORKSPACE lists a meeting"

  # roll-up --index chains into index (wiki regenerated).
  rm -f "$WS/_WIKI.md"
  run_w roll-up "$WS" --action-items --index
  assert_eq "$RC" 0 "'whosaid roll-up <ws> --action-items --index' exits 0"
  assert_file "$WS/_WIKI.md" "roll-up --index regenerated _WIKI.md"

  # doctor reports the workspace's index status and never fails.
  set +e
  OUT="$(WHOSAID_WORKSPACE="$WS" "$WHOSAID" doctor 2>&1)"
  RC=$?
  set -e
  assert_eq "$RC" 0 "'whosaid doctor' exits 0 with WHOSAID_WORKSPACE set"
  assert_text 'Ollama' "$OUT" "doctor prints the Ollama line"
  assert_text 'workspace: ' "$OUT" "doctor prints the workspace line"
  assert_text '_search\.db' "$OUT" "doctor prints the index status"

  # index on a nonexistent workspace fails and names the failing step.
  run_w index "$TMP/does-not-exist" --no-embed
  assert_ne "$RC" 0 "'whosaid index <missing dir>' exits non-zero"
  assert_text 'step failed' "$OUT" "'whosaid index <missing dir>' names the failing step"

  # ---------------------------------------------------------------------------
  # 5. ingest --index: stub transcription (WHOSAID_INGEST_DRYRUN), then the
  #    roll-up + index chain runs. Needs ffmpeg/ffprobe for the 1s fixture.
  # ---------------------------------------------------------------------------
  echo "-- ingest --index --"
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    WS2="$TMP/ws-ingest"
    make_ws "$WS2"
    ffmpeg -y -v error -f lavfi -i "anullsrc=r=16000:cl=mono" -t 1 \
      -metadata creation_time="2026-09-18T15:00:00Z" -c:a aac "$TMP/stub.m4a" \
      || fail "ffmpeg failed to synthesize the stub m4a"
    set +e
    OUT="$(WHOSAID_INGEST_DRYRUN=1 "$WHOSAID" ingest "$TMP/stub.m4a" --into "$WS2" --tz UTC --index 2>&1)"
    RC=$?
    set -e
    assert_eq "$RC" 0 "'whosaid ingest ... --index' (dry-run stub) exits 0 (output: $OUT)"
    [ -d "$WS2/2026-09-18-1500" ] || fail "ingest did not create the dated folder"
    PASS=$((PASS + 1))
    assert_text 'index complete' "$OUT" "ingest --index ran the index step"
    assert_file "$WS2/_search.db" "ingest --index built _search.db"
    assert_file "$WS2/_WIKI.md" "ingest --index wrote _WIKI.md"
    assert_file "$WS2/_ACTION-ITEMS.md" "ingest --index ran roll-up --action-items"
  else
    echo "SKIP: ingest --index section needs ffmpeg and ffprobe" >&2
  fi
fi

# ---------------------------------------------------------------------------
echo ""
echo "== PASS =="
echo "$PASS check(s) passed, 0 failed"
echo "(temp dir $TMP will be removed on exit)"
