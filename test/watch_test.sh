#!/bin/bash
#
# test/watch_test.sh: offline end-to-end test for lib/watch.py (issue #14: the
# hands-free watcher, its launchd installer, and the Voice Memos helpers).
#
# Fully offline and self-contained: a temp workspace, a temp "source" folder
# of fake .m4a files (touch, with old mtimes), a FAKE `whosaid` on PATH that
# records its argv and creates the dated folder ingest would, a fake
# `shortcuts` that fails loudly if anything ever calls it, and a synthetic
# CloudRecordings.db for the store-backed memos commands. No launchd agent is
# ever loaded, no real Voice Memos store is read, nothing outside $TMP is
# written (the dedicated-interpreter dir is redirected into $TMP).
#
# Skips (exit 0, clear message) when python3 is missing. plutil (macOS) is
# used to lint the dry-run plist when present, otherwise plistlib alone.
# The launchctl-backed checks (status/uninstall) only run on macOS and only
# ever name throwaway labels of this test's own. The SwiftBar menubar group
# (issue #24) is pinned here only at the parser level (help lists it); its
# real coverage, offline and via the env overrides, is test/menubar_test.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCH_PY="$REPO/lib/watch.py"

PASS=0
TEST_FAILED=0
TMP="$(mktemp -d "${TMPDIR:-/tmp}/whosaid-watch-test.XXXXXX")"
# the watcher resolves paths, so compare against the physical temp dir
# (macOS /var and /tmp are symlinks into /private; a trailing slash in
# TMPDIR would also leave a doubled separator in the argv assertions)
TMP="$(cd "$TMP" && pwd -P)"

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

assert_text() {  # assert_text <ERE-pattern> <text> <what>
  if printf '%s\n' "$2" | grep -qE -- "$1"; then
    PASS=$((PASS + 1))
  else
    fail "$3: pattern [$1] not found in text: [$2]"
  fi
}

assert_no_text() {  # assert_no_text <ERE-pattern> <text> <what>
  if printf '%s\n' "$2" | grep -qE -- "$1"; then
    fail "$3: pattern [$1] unexpectedly found in text: [$2]"
  else
    PASS=$((PASS + 1))
  fi
}

assert_exists() {  # assert_exists <path> <what>
  if [ -e "$1" ]; then PASS=$((PASS + 1)); else fail "$2: missing: $1"; fi
}

assert_missing() {  # assert_missing <path> <what>
  if [ -e "$1" ]; then fail "$2: unexpectedly present: $1"; else PASS=$((PASS + 1)); fi
}

# run_watch <args...>: run lib/watch.py; leaves rc in RC, stdout in OUT,
# stderr in ERR (never trips set -e).
run_watch() {
  set +e
  OUT="$(python3 "$WATCH_PY" "$@" 2> "$TMP/.last.err")"
  RC=$?
  set -e
  ERR="$(cat "$TMP/.last.err")"
}

# calls: the fake whosaid's recorded argv lines (one per invocation)
calls() { cat "$FAKE_LOG" 2>/dev/null || true; }
reset_calls() { : > "$FAKE_LOG"; }

# old_file <path>: create an empty audio file whose mtime is long past stable
old_file() { : > "$1"; touch -t 202601010900 "$1"; }

echo "== watch.py e2e: temp dir $TMP =="

# ---------------------------------------------------------------------------
# Guards + static checks.
# ---------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP: python3 not found on PATH; test/watch_test.sh needs python3." >&2
  exit 0
fi
[ -f "$WATCH_PY" ] || fail "required source file missing: $WATCH_PY"
bash -n "$SCRIPT_PATH" || fail "bash -n failed on test/watch_test.sh"
python3 -m py_compile "$WATCH_PY" || fail "python3 -m py_compile failed on lib/watch.py"
run_watch --help
assert_eq "$RC" 0 "watch.py --help exits 0"
assert_text "run|install|uninstall|status|memos|menubar" "$OUT" "help lists the subcommands"
run_watch memos --help
assert_text "list|pull|delete|shortcut-recipe" "$OUT" "memos help lists its subcommands"
run_watch menubar --help
assert_eq "$RC" 0 "menubar --help exits 0"
assert_text "install.*uninstall.*status" "$OUT" "menubar help lists its subcommands"

# ---------------------------------------------------------------------------
# Fixtures: fake whosaid + fake shortcuts on PATH, isolated agent dir, no
# ambient workspace/env leaking in.
# ---------------------------------------------------------------------------
mkdir -p "$TMP/bin" "$TMP/agent-dir-unused"
export FAKE_LOG="$TMP/whosaid.calls"
reset_calls
cat > "$TMP/bin/whosaid" <<'FAKE'
#!/bin/bash
# fake whosaid: record argv (+ the offline env) and mimic ingest's folder layout
printf '%s\n' "$* [HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-}]" >> "$FAKE_LOG"
[ "${FAKE_FAIL:-0}" = "1" ] && exit 1
case "$1" in
  ingest)
    staged="$2"; into="$4"
    [ -f "$staged" ] || { echo "fake whosaid: staged file missing: $staged" >&2; exit 1; }
    d="$into/2026-01-01-0900-$(basename "$staged" .m4a)"
    mkdir -p "$d" && cp "$staged" "$d/transcript.m4a" && echo "stub" > "$d/transcript.txt"
    ;;
esac
exit 0
FAKE
cat > "$TMP/bin/shortcuts" <<'FAKE'
#!/bin/bash
printf 'shortcuts %s\n' "$*" >> "$FAKE_LOG"
exit 99
FAKE
chmod +x "$TMP/bin/whosaid" "$TMP/bin/shortcuts"
export PATH="$TMP/bin:$PATH"
export WHOSAID_WATCH_AGENT_DIR="$TMP/agent-dir"
unset WHOSAID_WORKSPACE WHOSAID_BIN WHOSAID_ACCURATE WHOSAID_ACTION_ITEMS_HOOK HF_HUB_OFFLINE WHOSAID_PRETEND_NON_ADMIN 2>/dev/null || true

WS="$TMP/ws"; SRC="$TMP/src"
mkdir -p "$WS" "$SRC"
old_file "$SRC/old1.m4a"
old_file "$SRC/old2.m4a"
: > "$SRC/fresh.m4a"                 # mtime = now: still syncing
old_file "$SRC/notes.txt"            # not audio: ignored
mkdir -p "$SRC/subdir"               # directories: ignored

# ---------------------------------------------------------------------------
# 1. run --dry-run: lists stable recordings, skips the fresh one, writes nothing
# ---------------------------------------------------------------------------
echo "-- 1. run --dry-run"
run_watch run --into "$WS" --source "$SRC" --dry-run
assert_eq "$RC" 0 "dry run exits 0"
assert_text "still syncing \([0-9]+s until stable\): fresh\.m4a" "$ERR" "fresh file reported as syncing"
assert_text "2 new recording\(s\): old1\.m4a, old2\.m4a" "$ERR" "stable files listed"
assert_text "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2} whosaid: " "$ERR" "log lines are timestamped"
assert_no_text "notes\.txt|subdir" "$ERR" "non-audio entries ignored"
assert_missing "$WS/.watch_state.json" "dry run writes no state"
assert_eq "$(calls | wc -l | tr -d ' ')" 0 "dry run calls no whosaid"

# ---------------------------------------------------------------------------
# 2. run --seed marks everything done; the next run has nothing to do
# ---------------------------------------------------------------------------
echo "-- 2. run --seed"
run_watch run --into "$WS" --source "$SRC" --seed
assert_eq "$RC" 0 "seed exits 0"
assert_text "seeded 3 recording\(s\) as done" "$ERR" "seed counts every audio file"
assert_exists "$WS/.watch_state.json" "seed writes the state file"
SEEDED="$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1]))["processed"]; print(len(s), sum(1 for v in s.values() if v.get("seeded")))' "$WS/.watch_state.json")"
assert_eq "$SEEDED" "3 3" "state holds 3 seeded entries"
assert_text "old1\.m4a:0" "$(cat "$WS/.watch_state.json")" "state keys are name:size"
run_watch run --into "$WS" --source "$SRC"
assert_eq "$RC" 0 "run after seed exits 0"
assert_text "no new recordings\." "$ERR" "nothing new after seed"
assert_eq "$(calls | wc -l | tr -d ' ')" 0 "no whosaid calls after seed"

# ---------------------------------------------------------------------------
# 3. run ingests once: argv, ordering, staging cleanup, state, idempotence
# ---------------------------------------------------------------------------
echo "-- 3. run ingests"
WS2="$TMP/ws2"; mkdir -p "$WS2"
# a real pass waits (bounded by max_wait_seconds) for syncing files to settle;
# zero the bound here so this section stays fast (section 6b exercises the wait)
printf '[watch]\nmax_wait_seconds = 0\n' > "$WS2/whosaid.toml"
reset_calls
run_watch run --into "$WS2" --source "$SRC" --engine mlx --accurate
assert_eq "$RC" 0 "ingest run exits 0"
CALLS="$(calls)"
assert_eq "$(printf '%s\n' "$CALLS" | wc -l | tr -d ' ')" 4 "two ingests + roll-up + index"
assert_eq "$(printf '%s\n' "$CALLS" | sed -n 1p | cut -d' ' -f1)" "ingest" "first call is ingest"
assert_text "^ingest $WS2/\.watch_staging/old1\.m4a --into $WS2 --folder-by created --action-items --commitments --engine mlx --accurate" "$CALLS" "ingest argv (staged copy, --into, --folder-by created, --action-items, --commitments, engine, accurate)"
assert_text "^ingest .* --commitments" "$CALLS" "ingest always passes --commitments (dev-commitments ride along)"
assert_text "^ingest $WS2/\.watch_staging/old2\.m4a --into $WS2 " "$CALLS" "second ingest argv"
assert_eq "$(printf '%s\n' "$CALLS" | sed -n 3p | cut -d' ' -f1-3)" "roll-up $WS2 --action-items" "roll-up follows the ingests"
assert_eq "$(printf '%s\n' "$CALLS" | sed -n 4p | cut -d' ' -f1-2)" "index $WS2" "index follows roll-up"
assert_text "\[HF_HUB_OFFLINE=\]" "$(printf '%s\n' "$CALLS" | sed -n 1p)" "no offline env without --offline"
assert_exists "$WS2/2026-01-01-0900-old1/transcript.txt" "fake ingest created the dated folder"
assert_exists "$WS2/2026-01-01-0900-old2/transcript.m4a" "second dated folder"
assert_eq "$(ls -A "$WS2/.watch_staging" | wc -l | tr -d ' ')" 0 "staged copies removed after ingest"
assert_text "ok ingested old1\.m4a" "$ERR" "success logged"
assert_text "done: 2/2 ingested\." "$ERR" "summary line"
STATE2="$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1]))["processed"]; print(len(s), sorted(s), all("at" in v and not v.get("seeded") for v in s.values()))' "$WS2/.watch_state.json")"
assert_eq "$STATE2" "2 ['old1.m4a:0', 'old2.m4a:0'] True" "state records both ingests with timestamps"

reset_calls
run_watch run --into "$WS2" --source "$SRC"
assert_eq "$RC" 0 "second run exits 0"
assert_text "no stable recordings yet \(1 still syncing\)" "$ERR" "second run: nothing new, fresh still syncing"
assert_eq "$(calls | wc -l | tr -d ' ')" 0 "second run calls nothing"

# the fresh file ages past stable_seconds: picked up on the next pass
touch -t 202601011000 "$SRC/fresh.m4a"
reset_calls
run_watch run --into "$WS2" --source "$SRC" --offline
assert_eq "$RC" 0 "third run exits 0"
assert_text "1 new recording\(s\): fresh\.m4a" "$ERR" "aged file becomes new"
assert_text "^ingest $WS2/\.watch_staging/fresh\.m4a .*\[HF_HUB_OFFLINE=1\]" "$(calls)" "--offline sets HF_HUB_OFFLINE for whosaid"
assert_eq "$(calls | wc -l | tr -d ' ')" 3 "one ingest + roll-up + index"

# a failing ingest is not recorded and triggers no roll-up; it retries next pass
old_file "$SRC/broken.m4a"
reset_calls
FAKE_FAIL=1 python3 "$WATCH_PY" run --into "$WS2" --source "$SRC" 2> "$TMP/.fail.err"; RC=$?
assert_eq "$RC" 0 "a failed ingest still exits 0 (launchd-friendly)"
assert_text "whosaid exited 1 on broken\.m4a; will retry" "$(cat "$TMP/.fail.err")" "failure logged"
assert_eq "$(calls | wc -l | tr -d ' ')" 1 "failed ingest: no roll-up/index"
assert_no_text "broken\.m4a" "$(cat "$WS2/.watch_state.json")" "failed ingest not recorded"
reset_calls
run_watch run --into "$WS2" --source "$SRC"
assert_text "ok ingested broken\.m4a" "$ERR" "retried on the next pass"

# --whosaid and $WHOSAID_BIN override PATH lookup
cp "$TMP/bin/whosaid" "$TMP/whosaid-alt"; chmod +x "$TMP/whosaid-alt"
old_file "$SRC/alt.m4a"
reset_calls
run_watch run --into "$WS2" --source "$SRC" --whosaid "$TMP/whosaid-alt"
assert_text "> whosaid-alt ingest alt\.m4a" "$ERR" "--whosaid names the launcher used"

# ---------------------------------------------------------------------------
# 4. the lock refuses a concurrent run
# ---------------------------------------------------------------------------
echo "-- 4. lock"
old_file "$SRC/locked.m4a"
reset_calls
LOCKOUT="$(python3 - "$WATCH_PY" "$WS2" "$SRC" <<'PY'
import fcntl, subprocess, sys
watch, ws, src = sys.argv[1:4]
lock = open(f"{ws}/.watch.lock", "w")
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
p = subprocess.run([sys.executable, watch, "run", "--into", ws, "--source", src],
                   capture_output=True, text=True)
print(p.returncode)
print(p.stderr.strip())
PY
)"
assert_eq "$(printf '%s\n' "$LOCKOUT" | sed -n 1p)" 0 "locked-out run exits 0"
assert_text "another run is active; exiting\." "$LOCKOUT" "lock refuses the concurrent run"
assert_eq "$(calls | wc -l | tr -d ' ')" 0 "locked-out run ingests nothing"
rm -f "$SRC/locked.m4a" "$SRC/alt.m4a" "$SRC/broken.m4a"

# ---------------------------------------------------------------------------
# 5. missing source folder -> exit 3 with the Full Disk Access explanation
# ---------------------------------------------------------------------------
echo "-- 5. unreadable source"
run_watch run --into "$WS2" --source "$TMP/does-not-exist"
assert_eq "$RC" 3 "absent source exits 3"
assert_text "cannot read the source folder" "$ERR" "TCC/absent explanation"

# ---------------------------------------------------------------------------
# 6. whosaid.toml [watch] config: source, stable_seconds, interval_seconds
# ---------------------------------------------------------------------------
echo "-- 6. config"
WS3="$TMP/ws3"; SRC3="$TMP/src3"; mkdir -p "$WS3" "$SRC3"
cat > "$WS3/whosaid.toml" <<EOF
[watch]
source = "$SRC3"
stable_seconds = 1
interval_seconds = 300
EOF
: > "$SRC3/cfg.m4a"
sleep 2
run_watch run --into "$WS3" --dry-run
assert_eq "$RC" 0 "config-driven dry run exits 0"
assert_text "1 new recording\(s\): cfg\.m4a" "$ERR" "source + stable_seconds read from whosaid.toml"

# 6b. the bounded wait: a pass with only a syncing file stays alive until it
# settles (stable_seconds), then ingests it; max_wait_seconds bounds the wait
echo "-- 6b. wait loop"
WS4="$TMP/ws4"; SRC4="$TMP/src4"; mkdir -p "$WS4" "$SRC4"
printf '[watch]\nstable_seconds = 2\nmax_wait_seconds = 10\n' > "$WS4/whosaid.toml"
: > "$SRC4/settling.m4a"
reset_calls
START=$(date +%s)
run_watch run --into "$WS4" --source "$SRC4"
ELAPSED=$(( $(date +%s) - START ))
assert_eq "$RC" 0 "wait-loop run exits 0"
assert_text "still syncing \([0-9]+s until stable\): settling\.m4a" "$ERR" "wait loop saw the syncing file"
assert_text "  waiting [0-9]+s \.\.\." "$ERR" "wait loop slept"
assert_text "ok ingested settling\.m4a" "$ERR" "wait loop ingested once the file settled"
[ "$ELAPSED" -ge 2 ] && [ "$ELAPSED" -le 12 ] || fail "wait loop took ${ELAPSED}s; expected between 2 and 12"
PASS=$((PASS + 1))
: > "$SRC4/never-settles.m4a"
printf '[watch]\nstable_seconds = 60\nmax_wait_seconds = 1\n' > "$WS4/whosaid.toml"
run_watch run --into "$WS4" --source "$SRC4"
assert_eq "$RC" 0 "bounded wait exits 0"
assert_text "no stable recordings yet \(1 still syncing\)\." "$ERR" "max_wait_seconds bounds the wait"

# ---------------------------------------------------------------------------
# 7. install --dry-run: valid plist, label, WatchPaths, interval, env, interpreter
# ---------------------------------------------------------------------------
echo "-- 7. install --dry-run"
PLIST_BEFORE="$(ls "$HOME/Library/LaunchAgents" 2>/dev/null | grep -c '^com\.whosaid\.watch\.' || true)"
export WHOSAID_ACTION_ITEMS_HOOK="$TMP/hook.sh"
run_watch install --into "$WS2" --source "$SRC" --dry-run --interval 123 --offline \
  --env WHOSAID_ACCURATE=1 --env FOO=bar --seed
unset WHOSAID_ACTION_ITEMS_HOOK
assert_eq "$RC" 0 "install --dry-run exits 0"
printf '%s\n' "$OUT" > "$TMP/dry.plist"
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TMP/dry.plist" >/dev/null || fail "plutil -lint rejected the dry-run plist"
  PASS=$((PASS + 1))
fi
EXPECT_LABEL="$(python3 -c 'import hashlib,sys,pathlib; print("com.whosaid.watch."+hashlib.sha256(str(pathlib.Path(sys.argv[1]).resolve()).encode()).hexdigest()[:8])' "$WS2")"
PL="$(python3 - "$TMP/dry.plist" "$WS2" "$SRC" "$WATCH_PY" <<'PY'
import plistlib, sys
d = plistlib.load(open(sys.argv[1], "rb"))
ws, src, watch = sys.argv[2:5]
pa = d["ProgramArguments"]; env = d["EnvironmentVariables"]
print(d["Label"])
print(pa[1] == watch and pa[2] == "run" and "--into" in pa and pa[pa.index("--into")+1] == ws)
print("--source" in pa and pa[pa.index("--source")+1] == src and "--offline" in pa)
print(d["WatchPaths"] == [src], d["StartInterval"], d["RunAtLoad"], d["ThrottleInterval"], d["Nice"])
print(d["StandardOutPath"] == f"{ws}/.watch.log" and d["StandardErrorPath"] == f"{ws}/.watch.log")
print(env["PATH"].split(":")[0].endswith("/.local/bin") and "/opt/homebrew/bin" in env["PATH"] and "/usr/bin" in env["PATH"])
print(env.get("HF_HUB_OFFLINE"), env.get("HF_HUB_DISABLE_TELEMETRY"), env.get("TRANSFORMERS_OFFLINE"))
print(env.get("WHOSAID_ACCURATE"), env.get("FOO"), env.get("WHOSAID_ACTION_ITEMS_HOOK", "").endswith("/hook.sh"))
print(pa[0])
PY
)"
assert_eq "$(printf '%s\n' "$PL" | sed -n 1p)" "$EXPECT_LABEL" "default label = com.whosaid.watch.<sha256(ws)[:8]>"
assert_eq "$(printf '%s\n' "$PL" | sed -n 2p)" "True" "ProgramArguments: interpreter, lib/watch.py, run, --into ws"
assert_eq "$(printf '%s\n' "$PL" | sed -n 3p)" "True" "ProgramArguments carry --source and --offline"
assert_eq "$(printf '%s\n' "$PL" | sed -n 4p)" "True 123 True 60 5" "WatchPaths=source, StartInterval=--interval, RunAtLoad, Throttle 60, Nice 5"
assert_eq "$(printf '%s\n' "$PL" | sed -n 5p)" "True" "stdout/stderr -> <ws>/.watch.log"
assert_eq "$(printf '%s\n' "$PL" | sed -n 6p)" "True" "PATH env for launchd"
assert_eq "$(printf '%s\n' "$PL" | sed -n 7p)" "1 1 1" "--offline bakes the HF offline trio"
assert_eq "$(printf '%s\n' "$PL" | sed -n 8p)" "1 bar True" "--env pairs and WHOSAID_ACTION_ITEMS_HOOK passthrough"
assert_eq "$(printf '%s\n' "$PL" | sed -n 9p)" "$TMP/agent-dir/bin/whosaid-watch" "default interpreter is the dedicated whosaid-watch binary"
assert_text "== DRY RUN" "$ERR" "dry run announces itself"
assert_text "would provision the dedicated interpreter" "$ERR" "dry run describes provisioning"
assert_text "venv --copies" "$ERR" "provisioning uses venv --copies"
assert_text "codesign -f -s -" "$ERR" "provisioning ad-hoc signs the binary"
assert_text "would seed first: .*run --into $WS2 --seed" "$ERR" "dry run describes --seed"
assert_text "would run: launchctl bootout .*$EXPECT_LABEL.*bootstrap .*$EXPECT_LABEL\.plist.*enable" "$ERR" "dry run describes the launchctl steps"
assert_missing "$TMP/agent-dir" "dry run provisions nothing"
assert_missing "$HOME/Library/LaunchAgents/$EXPECT_LABEL.plist" "dry run writes no plist"
PLIST_AFTER="$(ls "$HOME/Library/LaunchAgents" 2>/dev/null | grep -c '^com\.whosaid\.watch\.' || true)"
assert_eq "$PLIST_AFTER" "$PLIST_BEFORE" "no LaunchAgents plist appeared"

# --interpreter, --label, and the config interval are honoured
run_watch install --into "$WS3" --dry-run --interpreter /usr/bin/python3 --label com.example.watch-test --no-open
assert_eq "$RC" 0 "install --dry-run with --interpreter/--label exits 0"
printf '%s\n' "$OUT" > "$TMP/dry2.plist"
PL2="$(python3 - "$TMP/dry2.plist" "$SRC3" <<'PY'
import plistlib, sys
d = plistlib.load(open(sys.argv[1], "rb"))
print(d["Label"], d["ProgramArguments"][0], d["StartInterval"], d["WatchPaths"] == [sys.argv[2]],
      "--source" in d["ProgramArguments"], "HF_HUB_OFFLINE" in d["EnvironmentVariables"])
PY
)"
assert_eq "$PL2" "com.example.watch-test /usr/bin/python3 300 True False False" "--label, --interpreter, [watch] interval_seconds and config source honoured; no --source/offline baked in"
assert_text "using the existing interpreter /usr/bin/python3" "$ERR" "--interpreter skips provisioning"
assert_no_text "would provision" "$ERR" "no provisioning with --interpreter"
assert_text "FDA +-> not needed" "$ERR" "a source outside ~/Library needs no Full Disk Access"
run_watch install --into "$WS3" --dry-run --env NOEQUALS
assert_eq "$RC" 1 "malformed --env is rejected"

# ---------------------------------------------------------------------------
# 7b. --no-fda: the non-admin path (is_admin hook, ~/Recordings default,
#     ~/Library refusal, dry-run admin note)
# ---------------------------------------------------------------------------
echo "-- 7b. install --no-fda"
NONADMIN="$(WHOSAID_PRETEND_NON_ADMIN=1 python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import watch; print(watch.is_admin())' "$REPO/lib")"
assert_eq "$NONADMIN" "False" "WHOSAID_PRETEND_NON_ADMIN=1 makes is_admin() False"
run_watch install --into "$WS2" --no-fda --dry-run
assert_eq "$RC" 0 "install --no-fda --dry-run exits 0"
printf '%s\n' "$OUT" > "$TMP/nofda.plist"
NFA="$(python3 - "$TMP/nofda.plist" <<'PY'
import pathlib, plistlib, sys
d = plistlib.load(open(sys.argv[1], "rb"))
rec = str(pathlib.Path.home() / "Recordings")
pa = d["ProgramArguments"]
print(d["WatchPaths"] == [rec], "--source" in pa and pa[pa.index("--source") + 1] == rec)
PY
)"
assert_eq "$NFA" "True True" "--no-fda with nothing configured pins WatchPaths/--source to ~/Recordings"
assert_text "FDA +-> not needed" "$ERR" "--no-fda default source needs no Full Disk Access"
NFA_INTERPRETER="$(python3 - "$TMP/nofda.plist" <<'PY'
import plistlib, sys
print(plistlib.load(open(sys.argv[1], "rb"))["ProgramArguments"][0])
PY
)"
assert_eq "$NFA_INTERPRETER" "$(python3 -c 'import sys; print(sys.executable)')" "--no-fda reuses the launcher interpreter instead of provisioning an FDA copy"
assert_text "no dedicated interpreter is needed for --no-fda" "$ERR" "--no-fda explains why copied-venv provisioning was skipped"
# a temp HOME so ~/Library is safe to fake: a --no-fda source inside it must be refused
NFA_HOME="$TMP/fake-home"; mkdir -p "$NFA_HOME/Library/Mobile Documents"
HOME="$NFA_HOME" run_watch install --into "$WS2" --no-fda --dry-run \
  --source "$NFA_HOME/Library/Mobile Documents"
assert_eq "$RC" 1 "--no-fda with a source under ~/Library exits 1"
assert_text "outside ~/Library" "$ERR" "--no-fda refusal asks for a folder outside ~/Library"
assert_text "Recordings|Dropbox" "$ERR" "--no-fda refusal suggests ~/Recordings or a Dropbox folder"
# a Library default source + non-admin: the dry run warns about the admin wall
HOME="$NFA_HOME" WHOSAID_PRETEND_NON_ADMIN=1 run_watch install --into "$WS2" --dry-run
assert_eq "$RC" 0 "dry run with a Library source and no admin exits 0"
assert_text "note: you are not an admin; .*README 'No admin rights\?'" "$ERR" "dry run notes the admin wall for non-admins"

# A reinstall keeps explicit certificate configuration from the existing plist,
# while a new --env value takes precedence.  This uses an isolated HOME and the
# real install dry-run entry point; it never loads a LaunchAgent.
TLS_HOME="$TMP/tls-home"; TLS_LABEL="com.example.watch-tls"; mkdir -p "$TLS_HOME/Library/LaunchAgents"
python3 - "$TLS_HOME/Library/LaunchAgents/$TLS_LABEL.plist" <<'PY'
import plistlib, sys
plistlib.dump({"Label": "com.example.watch-tls", "EnvironmentVariables": {
    "PATH": "/custom/tools:/old/path", "SSL_CERT_FILE": "/corp/old.pem", "UV_SYSTEM_CERTS": "1", "UV_NATIVE_TLS": "1", "FOO": "old"
}}, open(sys.argv[1], "wb"))
PY
HOME="$TLS_HOME" run_watch install --into "$WS2" --source "$SRC" --dry-run --label "$TLS_LABEL" --env FOO=new
assert_eq "$RC" 0 "certificate-configured reinstall dry run exits 0"
printf '%s\n' "$OUT" > "$TMP/tls.plist"
TLS_ENV="$(python3 - "$TMP/tls.plist" <<'PY'
import plistlib, sys
e = plistlib.load(open(sys.argv[1], "rb"))["EnvironmentVariables"]
print(e.get("SSL_CERT_FILE"), e.get("UV_SYSTEM_CERTS"), e.get("UV_NATIVE_TLS"), e.get("FOO"), e["PATH"].split(":")[0], ".local/bin" in e["PATH"])
PY
)"
assert_eq "$TLS_ENV" "/corp/old.pem 1 1 new /custom/tools True" "reinstall retains certificate env and explicit PATH precedence"

# Candidate selection skips a copied-venv failure and accepts the Python found
# by uv.  The subprocesses are mocked; this isolates selection from this Mac's
# installed interpreters while exercising the production function.
PROBE_SELECTION="$(PYTHONPATH="$REPO/lib" python3 - <<'PY'
import subprocess, sys
from pathlib import Path
from unittest.mock import patch
import watch
class R:
    def __init__(self, rc, out=""): self.returncode, self.stdout = rc, out
calls = []
def run(argv, **kwargs):
    calls.append(argv)
    if argv[1:3] == ["python", "find"]: return R(0, "/uv/python3.12\n")
    return R(0 if argv[0] == "/uv/python3.12" else 1)
with patch.object(watch.shutil, "which", return_value="/fake/uv"), \
     patch.object(watch.subprocess, "run", side_effect=run), \
     patch.object(watch.os, "access", return_value=True):
    print(watch.compatible_copying_interpreter() == "/uv/python3.12", any(a[1:3] == ["python", "find"] for a in calls))
PY
)"
assert_eq "$PROBE_SELECTION" "True True" "copied-venv selection falls through to uv-managed Python"

# ---------------------------------------------------------------------------
# 7c. overlap guard: the watched recordings source and the meeting workspace
#     must never be the same folder or nested inside each other, in either
#     direction, including through a symlink alias or the --no-fda default.
# ---------------------------------------------------------------------------
echo "-- 7c. overlap guard"
OV="$TMP/overlap"
mkdir -p "$OV/rec" "$OV/rec/ws-inner" "$OV/ws" "$OV/ws/rec-inner"
ln -s "$OV/rec" "$OV/rec-alias"

# same folder for both --into and --source
run_watch install --into "$OV/rec" --source "$OV/rec" --dry-run
assert_eq "$RC" 1 "install refuses source == workspace"
assert_text "must be separate folders" "$ERR" "same-folder refusal names the overlap"
assert_text "$OV/rec" "$ERR" "same-folder refusal names the resolved path"
assert_no_text "== DRY RUN" "$ERR" "same-folder refusal never reaches the dry-run report"

# workspace nested inside the source
run_watch install --into "$OV/rec/ws-inner" --source "$OV/rec" --dry-run
assert_eq "$RC" 1 "install refuses workspace nested inside the source"
assert_text "must be separate folders" "$ERR" "nested-workspace refusal names the overlap"

# source nested inside the workspace
run_watch install --into "$OV/ws" --source "$OV/ws/rec-inner" --dry-run
assert_eq "$RC" 1 "install refuses source nested inside the workspace"
assert_text "must be separate folders" "$ERR" "nested-source refusal names the overlap"

# a symlinked alias of the same directory must not defeat the guard
run_watch install --into "$OV/rec" --source "$OV/rec-alias" --dry-run
assert_eq "$RC" 1 "install refuses a symlinked alias of the workspace"
assert_text "must be separate folders" "$ERR" "symlink-alias refusal names the overlap"

# a normal, disjoint pair is still accepted
run_watch install --into "$OV/ws" --source "$OV/rec" --dry-run
assert_eq "$RC" 0 "install accepts a disjoint workspace/source pair"
assert_text "== DRY RUN" "$ERR" "disjoint pair reaches the dry-run report"
assert_no_text "must be separate folders" "$ERR" "disjoint pair triggers no overlap message"

# `watch run` refuses the same overlap, before taking the lock
run_watch run --into "$OV/rec" --source "$OV/rec"
assert_eq "$RC" 1 "run refuses source == workspace"
assert_text "must be separate folders" "$ERR" "run refusal names the overlap"
assert_missing "$OV/rec/.watch.lock" "run refusal happens before the lock is taken"

# the guard sees the FINAL source, including the --no-fda ~/Recordings default
# (nothing configured, no --source): here that default collides with --into itself.
OV_HOME="$OV/no-fda-home"; mkdir -p "$OV_HOME/Recordings"
HOME="$OV_HOME" run_watch install --into "$OV_HOME/Recordings" --no-fda --dry-run
assert_eq "$RC" 1 "install refuses when the --no-fda default source equals the workspace"
assert_text "must be separate folders" "$ERR" "no-fda-default refusal names the overlap"

# ---------------------------------------------------------------------------
# 8. memos list / pull on a plain folder
# ---------------------------------------------------------------------------
echo "-- 8. memos on a plain folder"
run_watch memos list --source "$SRC"
assert_eq "$RC" 0 "memos list exits 0"
assert_text "^old1\.m4a " "$OUT" "memos list shows old1.m4a"
assert_text "^fresh\.m4a " "$OUT" "memos list shows fresh.m4a"
assert_no_text "notes\.txt" "$OUT" "memos list skips non-audio"
run_watch memos list --source "$SRC" --json
assert_eq "$RC" 0 "memos list --json exits 0"
LJ="$(printf '%s\n' "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["kind"], len(d["recordings"]), sorted(r["file"] for r in d["recordings"]))')"
assert_eq "$LJ" "folder 3 ['fresh.m4a', 'old1.m4a', 'old2.m4a']" "memos list --json on a folder"
run_watch memos list --source "$TMP/nope"
assert_eq "$RC" 3 "memos list on an absent folder exits 3"

PULL="$TMP/pulled"; mkdir -p "$PULL"
touch -t 202601011200 "$SRC/fresh.m4a"   # newest by mtime
run_watch memos pull --source "$SRC" --latest -o "$PULL"
assert_eq "$RC" 0 "memos pull --latest exits 0"
assert_eq "$OUT" "$PULL/fresh.m4a" "pull prints the destination"
assert_exists "$PULL/fresh.m4a" "newest recording copied"
run_watch memos pull --source "$SRC" --title old1 -o "$PULL"
assert_eq "$RC" 0 "memos pull --title (plain folder: file stem) exits 0"
assert_exists "$PULL/old1.m4a" "titled recording copied"
run_watch memos pull --source "$SRC" --title old1 -o "$PULL"
assert_eq "$RC" 1 "pull refuses to overwrite"
run_watch memos delete "old1" --yes --source "$SRC"
assert_eq "$RC" 1 "memos delete refuses a plain folder"
assert_text "not a Voice Memos store" "$ERR" "delete explains why (deletes go through the app)"

# ---------------------------------------------------------------------------
# 9. memos on a synthetic Voice Memos store (CloudRecordings.db copy, read-only)
# ---------------------------------------------------------------------------
echo "-- 9. memos on a synthetic store"
STORE="$TMP/store"; mkdir -p "$STORE"
old_file "$STORE/20260101 090000-AAAA.m4a"
old_file "$STORE/20260102 090000-BBBB.m4a"
old_file "$STORE/dup1.m4a"; old_file "$STORE/dup2.m4a"
python3 - "$STORE/CloudRecordings.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("create table ZCLOUDRECORDING (Z_PK integer primary key, ZENCRYPTEDTITLE text, "
            "ZPATH text, ZDURATION real, ZDATE real, ZEVICTIONDATE real)")
con.executemany("insert into ZCLOUDRECORDING (ZENCRYPTEDTITLE, ZPATH, ZDURATION, ZDATE, ZEVICTIONDATE) values (?,?,?,?,?)", [
    ("Standup", "20260101 090000-AAAA.m4a", 61.4, 788000000.0, None),
    ("Retro", "20260102 090000-BBBB.m4a", 1800.0, 788100000.0, 788200000.0),
    ("Dup", "dup1.m4a", 1.0, 788300000.0, None),
    ("Dup", "dup2.m4a", 2.0, 788400000.0, None),
])
con.commit(); con.close()
PY
DB_SHA_BEFORE="$(shasum "$STORE/CloudRecordings.db" | cut -d' ' -f1)"
run_watch memos list --source "$STORE"
assert_eq "$RC" 0 "memos list on a store exits 0"
assert_text "^Standup +20260101 090000-AAAA\.m4a +61 " "$OUT" "store listing: title, file, seconds"
assert_text "^Retro .* RECENTLY DELETED$" "$OUT" "store listing marks Recently Deleted"
run_watch memos list --source "$STORE" --json
LJ2="$(printf '%s\n' "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); r=d["recordings"]; print(d["kind"], [x["title"] for x in r], [x["recently_deleted"] for x in r], r[0]["seconds"])')"
assert_eq "$LJ2" "store ['Standup', 'Retro', 'Dup', 'Dup'] [False, True, False, False] 61" "memos list --json on a store"
run_watch memos pull --source "$STORE" --title Standup -o "$PULL"
assert_eq "$RC" 0 "memos pull --title maps the title through the db"
assert_exists "$PULL/20260101 090000-AAAA.m4a" "titled memo copied out of the store"
run_watch memos delete "Nope" --yes --source "$STORE"
assert_eq "$RC" 1 "delete of an unknown title is refused"
assert_text "no memo titled exactly 'Nope'" "$ERR" "unknown title message"
run_watch memos delete "Dup" --yes --source "$STORE"
assert_eq "$RC" 1 "delete of an ambiguous title is refused"
assert_text "2 memos share the title 'Dup'" "$ERR" "ambiguous title message"
run_watch memos delete "Standup" --source "$STORE" < /dev/null
assert_eq "$RC" 1 "delete without --yes and no tty input aborts"
assert_text "aborted\." "$OUT" "abort message"
assert_eq "$(shasum "$STORE/CloudRecordings.db" | cut -d' ' -f1)" "$DB_SHA_BEFORE" "the store database is never modified"
assert_no_text "^shortcuts " "$(calls)" "no memos command ever invoked shortcuts"
run_watch memos shortcut-recipe --no-sign
assert_eq "$RC" 0 "shortcut-recipe --no-sign exits 0"
assert_text "Delete Recordings" "$OUT" "recipe names the app's action (plural)"
assert_text "Name it exactly:  Delete Voice Memo" "$OUT" "recipe names the Shortcut"
assert_text "iCloud stays in sync" "$OUT" "recipe explains why the app intent is used"
assert_text "Recently Deleted" "$OUT" "recipe mentions the 30-day Recently Deleted"
assert_no_text "^shortcuts " "$(calls)" "--no-sign never invokes shortcuts"

# ---------------------------------------------------------------------------
# 10. status / uninstall on never-installed throwaway labels (macOS only:
#     they shell out to launchctl print/bootout, which is read-only for a
#     label that does not exist)
# ---------------------------------------------------------------------------
if [ "$(uname -s)" = "Darwin" ]; then
  echo "-- 10. status / uninstall (never installed)"
  THROWAWAY="com.whosaid.watch.test-$$"
  run_watch status --json --label "$THROWAWAY"
  assert_eq "$RC" 0 "status --json on a never-installed label exits 0"
  SJ="$(printf '%s\n' "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["label"], d["installed"], d["loaded"], d["last_exit_status"], d["processed"])')"
  assert_eq "$SJ" "$THROWAWAY False False None 0" "status --json reports not installed, not loaded"
  run_watch status --into "$WS2"
  assert_eq "$RC" 0 "status --into exits 0"
  assert_text "^installed: +no" "$OUT" "status text: not installed"
  assert_text "^loaded: +no" "$OUT" "status text: not loaded"
  assert_text "^processed: +5 recording\(s\), 0 seeded" "$OUT" "status counts the workspace state"
  assert_text "^label: +$EXPECT_LABEL" "$OUT" "status derives the default label from the workspace"
  run_watch status --json --into "$WS"
  SJ2="$(printf '%s\n' "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["processed"], d["seeded"], d["state_file"].endswith("/.watch_state.json"))')"
  assert_eq "$SJ2" "3 3 True" "status --json counts seeded entries"

  # uninstall --purge on a throwaway label: removes state/lock/staging and the
  # (redirected, fake) interpreter dir, keeps the log
  mkdir -p "$TMP/agent-dir/bin"; : > "$TMP/agent-dir/bin/whosaid-watch"
  : > "$WS2/.watch.log"
  mkdir -p "$WS2/.watch_staging"
  run_watch uninstall --label "$THROWAWAY" --into "$WS2" --purge
  assert_eq "$RC" 0 "uninstall of a never-installed label exits 0"
  assert_text "was not loaded" "$ERR" "uninstall reports not loaded"
  assert_text "no plist at " "$ERR" "uninstall reports no plist"
  assert_missing "$WS2/.watch_state.json" "--purge removed the state file"
  assert_missing "$WS2/.watch.lock" "--purge removed the lock"
  assert_missing "$WS2/.watch_staging" "--purge removed staging"
  assert_exists "$WS2/.watch.log" "--purge keeps the log"
  assert_missing "$TMP/agent-dir" "--purge removed the dedicated interpreter dir"
  assert_exists "$WS2/2026-01-01-0900-old1/transcript.txt" "--purge never touches meeting folders"
  assert_text "Full Disk Access" "$ERR" "uninstall reminds about the FDA entry"
else
  echo "-- 10. status / uninstall skipped (not macOS)"
fi

echo ""
echo "PASS: $PASS checks passed (lib/watch.py)"
