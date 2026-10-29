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

echo "-- a parent can stop record --app with SIGTERM (stub helper) --"
# A stub stands in for the ScreenCaptureKit helper at its cached path: it
# writes two 1 s legs, then waits for a signal like the real one. The driver
# runs the wrapper in its own process group, as an app would, and stops it
# either by SIGTERM to the wrapper pid (a parent) or SIGINT to the group
# (Ctrl-C). Before the fix SIGTERM killed the wrapper and orphaned the helper.
if command -v ffmpeg >/dev/null 2>&1 && [ "$(sw_vers -productVersion | cut -d. -f1)" -ge 15 ]; then
  STUB_CACHE="$TMP/cache"
  mkdir -p "$STUB_CACHE/whosaid"
  STUB_BIN="$STUB_CACHE/whosaid/record-app-$(shasum -a 256 "$REPO/lib/record_app.swift" | cut -c1-12)"
  cat > "$STUB_BIN" <<'STUB'
#!/usr/bin/env python3
import os, signal, subprocess, sys, time
args = sys.argv[1:]
out = args[args.index("--out-dir") + 1]
state = os.environ["STUB_STATE"]
for leg, freq in (("mic", 300), ("app", 500)):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=f={freq}:d=1", "-ar", "16000", "-ac", "1",
                    os.path.join(out, f"{leg}.wav")], check=True)
got = []
def on_signal(signum, _frame):
    got.append(signal.Signals(signum).name)
signal.signal(signal.SIGINT, on_signal)
signal.signal(signal.SIGTERM, on_signal)
open(state + ".ready", "w").write(str(os.getpid()))
deadline = time.time() + 30
while not got and time.time() < deadline:
    time.sleep(0.05)
time.sleep(0.3)  # finishing; a duplicate signal would land here
open(state + ".signals", "w").write(" ".join(got))
sys.exit(0 if got else 9)
STUB
  chmod +x "$STUB_BIN"
  cat > "$TMP/drive.py" <<'DRIVE'
import os, signal, subprocess, sys, time
whosaid, mode, tmp = sys.argv[1:4]
state = os.path.join(tmp, f"stub-{mode}")
env = dict(os.environ, XDG_CACHE_HOME=os.path.join(tmp, "cache"), STUB_STATE=state)
log = open(os.path.join(tmp, f"{mode}.log"), "w")
p = subprocess.Popen([whosaid, "record", "--app", "StubApp", "--into", os.path.join(tmp, f"out-{mode}")],
                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                     env=env, start_new_session=True, text=True)
for _ in range(300):
    if os.path.exists(state + ".ready"):
        break
    time.sleep(0.05)
if mode == "term":
    os.kill(p.pid, signal.SIGTERM)
else:
    os.killpg(p.pid, signal.SIGINT)
try:
    printed, _ = p.communicate(timeout=30)
    rc = p.returncode
except subprocess.TimeoutExpired:
    os.killpg(p.pid, signal.SIGKILL)
    printed, rc = "", "timeout"
time.sleep(0.2)
stub_pid = open(state + ".ready").read().strip() if os.path.exists(state + ".ready") else ""
alive = bool(stub_pid) and subprocess.run(["kill", "-0", stub_pid], capture_output=True).returncode == 0
if alive:
    os.kill(int(stub_pid), signal.SIGKILL)
sigs = open(state + ".signals").read() if os.path.exists(state + ".signals") else ""
print(f"rc={rc}|alive={int(alive)}|signals={sigs}|out={printed.strip()}")
DRIVE
  for mode in term int; do
    RES="$(python3 -I "$TMP/drive.py" "$WHOSAID" "$mode" "$TMP")"
    assert_contains "$RES" "rc=0|" "$mode: the wrapper exits 0"
    assert_contains "$RES" "|alive=0|" "$mode: no helper is left running"
    case "$mode" in
      term) assert_contains "$RES" "|signals=SIGTERM|" "term: the helper gets exactly one SIGTERM" ;;
      int)  assert_contains "$RES" "|signals=SIGINT|" "int: the helper gets exactly one SIGINT" ;;
    esac
    M4A="${RES##*|out=}"
    [ -s "$M4A" ] || fail "$mode: no output file ($RES)"
    PASS=$((PASS + 1))
    assert_eq "$(ffprobe -v error -show_entries stream=channels -of csv=p=0 "$M4A")" "2" "$mode: output is stereo"
    assert_eq "$(ffprobe -v error -show_entries format_tags=comment -of default=nw=1:nk=1 "$M4A")" "whosaid:channels=mic,app" "$mode: output carries the channel tag"
  done
else
  echo "SKIP: needs ffmpeg and macOS 15+"
fi

echo "-- record --watch-call: flags and help --"
run_w record --watch-call
assert_eq "$RC" 2 "'--watch-call' without --app exits 2"
assert_contains "$OUT" "--watch-call only applies with --app" "explains --watch-call without --app"
run_w record --watch-call --app X --into "$TMP/in"
assert_eq "$RC" 2 "'--watch-call --into' exits 2"
assert_contains "$OUT" "cannot be combined" "explains the --watch-call conflict"
run_w record --watch-call --app X --wait-for-call
assert_eq "$RC" 2 "'--watch-call --wait-for-call' exits 2"
run_w record --watch-call --app X --interval 0
assert_eq "$RC" 2 "'--interval 0' exits 2"
run_w record --watch-call --app X --interval abc
assert_eq "$RC" 2 "'--interval abc' exits 2"
run_w record --app X --interval 1
assert_eq "$RC" 2 "'--interval' without --watch-call exits 2"
run_w help
assert_contains "$OUT" "--watch-call" "help lists --watch-call"
assert_contains "$OUT" "--interval S" "help lists --interval"

echo "-- record --watch-call: real helper, app not running --"
if command -v swiftc >/dev/null 2>&1 && [ "$(sw_vers -productVersion | cut -d. -f1)" -ge 15 ]; then
  cat > "$TMP/watch.py" <<'WATCH'
import json, os, signal, subprocess, sys, time
whosaid, tmp = sys.argv[1:3]
out = os.path.join(tmp, "watch.out")
p = subprocess.Popen([whosaid, "record", "--watch-call", "--app", "com.example.notrunning", "--interval", "0.2"],
                     stdin=subprocess.DEVNULL, stdout=open(out, "w"), stderr=open(os.path.join(tmp, "watch.err"), "w"),
                     start_new_session=True)
# The first run builds the helper (about a minute); the first line marks it up.
for _ in range(2400):
    if os.path.getsize(out) > 0 or p.poll() is not None:
        break
    time.sleep(0.1)
time.sleep(1.5)
t0 = time.time()
os.kill(p.pid, signal.SIGTERM)
try:
    rc = p.wait(timeout=2)
except subprocess.TimeoutExpired:
    os.killpg(p.pid, signal.SIGKILL)
    rc = "timeout"
took = time.time() - t0
lines = open(out).read().splitlines()
ok = len(lines) == 1
rec = json.loads(lines[0]) if ok else {}
left = subprocess.run(["pgrep", "-f", "record-app-.*--watch-call"], capture_output=True, text=True).stdout.strip()
print(f"rc={rc}|fast={int(took < 2)}|lines={len(lines)}|running={rec.get('running')}|mic={rec.get('mic')}"
      f"|keys={','.join(rec)}|left={left}|")
WATCH
  RES="$(python3 -I "$TMP/watch.py" "$WHOSAID" "$TMP")"
  assert_contains "$RES" "rc=0|fast=1|lines=1|" "watch-call: exits 0 promptly with exactly one line"
  assert_contains "$RES" "|running=False|mic=False|" "watch-call: an unlaunched app is running:false, mic:false"
  assert_contains "$RES" "|keys=app,running,mic,speaker,at|" "watch-call: keys are in order"
  assert_contains "$RES" "|left=|" "watch-call: no helper is left running"
else
  echo "SKIP: needs swiftc and macOS 15+"
fi

echo "-- helper source typechecks --"
if command -v swiftc >/dev/null 2>&1; then
  swiftc -typecheck -swift-version 5 -target "$(uname -m)-apple-macos15.0" "$REPO/lib/record_app.swift"
  PASS=$((PASS + 1))
else
  echo "SKIP: swiftc not found"
fi

echo ""
echo "PASS: $PASS check(s)"
