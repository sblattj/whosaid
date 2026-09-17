#!/bin/bash
#
# test/enroll_from_file_test.sh — offline test for `whosaid enroll --from FILE
# [--ss T] [--t D|--to T] [--force]` (GitHub issue #1, part 1).
#
# Fully offline and self-contained: synthesizes ~40s of speech with macOS
# `say` (same technique as test/e2e.sh), builds an m4a fixture, then drives
# the CLI's --from window-extraction path against a temp WHOSAID_VOICE_REFS.
# No models, no diarization/transcription — this only exercises the enroll
# extraction + duration/silence gate + overwrite guard, so it is fast.
#
# macOS/BSD only: relies on `say`, BSD grep/awk, bash 3.2 (no associative
# arrays, no bash-4-isms). Skips (exit 0, clear message) when say/ffmpeg/
# ffprobe/shasum are missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WHOSAID_BIN="$REPO/whosaid"

for dep in say ffmpeg ffprobe shasum; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    echo "SKIP: '$dep' not found on PATH — this test is macOS-only." >&2
    exit 0
  fi
done

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

# assert helpers: every passing check increments PASS; any mismatch aborts
# (fail fast) with a message naming what and where.

assert_eq() {  # assert_eq <actual> <expected> <what>
  if [ "$1" = "$2" ]; then
    PASS=$((PASS + 1))
  else
    fail "$3 — expected [$2], got [$1]"
  fi
}

assert_true() {  # assert_true <0-or-1> <what>
  if [ "$1" -eq 0 ]; then
    PASS=$((PASS + 1))
  else
    fail "$2"
  fi
}

assert_in_range() {  # assert_in_range <value> <lo> <hi> <what>
  if awk -v v="$1" -v lo="$2" -v hi="$3" 'BEGIN{exit !((v+0) >= lo && (v+0) <= hi)}'; then
    PASS=$((PASS + 1))
  else
    fail "$4 — value [$1] not in range [$2, $3]"
  fi
}

assert_close() {  # assert_close <a> <b> <tolerance> <what>
  if awk -v a="$1" -v b="$2" -v tol="$3" 'BEGIN{d=a-b; if (d<0) d=-d; exit !(d <= tol)}'; then
    PASS=$((PASS + 1))
  else
    fail "$4 — [$1] and [$2] differ by more than [$3]"
  fi
}

export WHOSAID_VOICE_REFS="$TMP/voices"

# ---------------------------------------------------------------------------
# 1. Synthesize ~40s of speech with a `say` voice (same recipe as e2e.sh).
# ---------------------------------------------------------------------------
echo "== enroll_from_file_test: temp dir $TMP =="
echo "-- selecting a say voice --"

AVAILABLE_VOICES="$(say -v '?' | awk '{print $1}')"
PREFERRED="Samantha Daniel Karen Moira Rishi Fred Alex"
VOICE=""
for v in $PREFERRED; do
  if echo "$AVAILABLE_VOICES" | grep -qx "$v"; then
    VOICE="$v"
    break
  fi
done
[ -n "$VOICE" ] || fail "need one of these 'say' voices installed: $PREFERRED"
echo "voice: $VOICE"

echo "-- synthesizing ~40s source clip --"

SPEECH="Good morning everyone. I wanted to share a quick update on the community garden project before we get started today. Over the past month, several volunteers have been repairing the fence line and turning over the soil in preparation for the spring planting season. We still need a few more people to help build the raised beds near the north entrance, and any extra hands this Saturday would be greatly appreciated. Thanks again for all of your continued support on this wonderful shared project throughout the season, and I will send a follow-up email with the exact schedule once it is finalized."

say -v "$VOICE" -o "$TMP/raw.aiff" "$SPEECH" || fail "say failed to synthesize the source clip"
ffmpeg -y -v error -i "$TMP/raw.aiff" -c:a aac -b:a 96k "$TMP/x.m4a" \
  || fail "ffmpeg failed to encode the source clip to m4a"

SRC_DUR="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/x.m4a")"
echo "source clip: $TMP/x.m4a (${SRC_DUR}s)"
if ! awk -v d="$SRC_DUR" 'BEGIN{exit !((d+0) >= 30)}'; then
  fail "synthesized source clip too short (${SRC_DUR}s) for this test's windows — need >= 30s"
fi

# ---------------------------------------------------------------------------
# (a) enroll Bob --from x.m4a --ss 5 --t 20: exit 0, Bob.wav ~20s, mono 16k.
# ---------------------------------------------------------------------------
echo "-- (a) --ss/--t window --"

set +e
OUT_A="$("$WHOSAID_BIN" enroll Bob --from "$TMP/x.m4a" --ss 5 --t 20 2>&1)"
RC_A=$?
set -e
[ "$RC_A" -eq 0 ] || fail "(a) enroll --ss/--t exited $RC_A: $OUT_A"
PASS=$((PASS + 1))

BOB_WAV="$WHOSAID_VOICE_REFS/Bob.wav"
assert_true "$([ -f "$BOB_WAV" ] && echo 0 || echo 1)" "(a) Bob.wav was not created"

DUR_A="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$BOB_WAV")"
assert_in_range "$DUR_A" 19 21 "(a) Bob.wav duration"

CH_SR_A="$(ffprobe -v error -show_entries stream=channels,sample_rate -of csv=p=0 "$BOB_WAV")"
assert_eq "$CH_SR_A" "16000,1" "(a) Bob.wav channels,sample_rate"

# ---------------------------------------------------------------------------
# (b) --ss 0:05 --to 0:25 gives the same duration as --ss 5 --t 20.
# ---------------------------------------------------------------------------
echo "-- (b) --ss/--to window (M:SS form) --"

set +e
OUT_B="$("$WHOSAID_BIN" enroll BobTo --from "$TMP/x.m4a" --ss 0:05 --to 0:25 2>&1)"
RC_B=$?
set -e
[ "$RC_B" -eq 0 ] || fail "(b) enroll --ss/--to exited $RC_B: $OUT_B"
PASS=$((PASS + 1))

BOBTO_WAV="$WHOSAID_VOICE_REFS/BobTo.wav"
assert_true "$([ -f "$BOBTO_WAV" ] && echo 0 || echo 1)" "(b) BobTo.wav was not created"

DUR_B="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$BOBTO_WAV")"
assert_close "$DUR_A" "$DUR_B" 0.1 "(b) --ss/--t vs --ss/--to duration"

# ---------------------------------------------------------------------------
# (c) --t 5 (below the 15s floor): nonzero exit, "15" in stderr, no file.
# ---------------------------------------------------------------------------
echo "-- (c) below the 15s floor --"

set +e
ERR_C="$("$WHOSAID_BIN" enroll TooShort --from "$TMP/x.m4a" --t 5 2>&1 1>/dev/null)"
RC_C=$?
set -e
[ "$RC_C" -ne 0 ] || fail "(c) enroll --t 5 unexpectedly exited 0"
PASS=$((PASS + 1))
assert_true "$(echo "$ERR_C" | grep -q '15' && echo 0 || echo 1)" "(c) stderr missing '15': $ERR_C"
assert_true "$([ ! -e "$WHOSAID_VOICE_REFS/TooShort.wav" ] && echo 0 || echo 1)" "(c) TooShort.wav should not exist"

# ---------------------------------------------------------------------------
# (d) re-running without --force leaves Bob.wav untouched; --force succeeds.
# ---------------------------------------------------------------------------
echo "-- (d) overwrite guard + --force --"

SHA_BEFORE="$(shasum -a 256 "$BOB_WAV" | awk '{print $1}')"

set +e
OUT_D1="$("$WHOSAID_BIN" enroll Bob --from "$TMP/x.m4a" --ss 5 --t 20 2>&1)"
RC_D1=$?
set -e
[ "$RC_D1" -ne 0 ] || fail "(d) re-enroll without --force unexpectedly exited 0"
PASS=$((PASS + 1))

SHA_AFTER="$(shasum -a 256 "$BOB_WAV" | awk '{print $1}')"
assert_eq "$SHA_AFTER" "$SHA_BEFORE" "(d) Bob.wav sha256 changed without --force"

set +e
OUT_D2="$("$WHOSAID_BIN" enroll Bob --from "$TMP/x.m4a" --ss 6 --t 20 --force 2>&1)"
RC_D2=$?
set -e
[ "$RC_D2" -eq 0 ] || fail "(d) enroll --force exited $RC_D2: $OUT_D2"
PASS=$((PASS + 1))

SHA_FORCED="$(shasum -a 256 "$BOB_WAV" | awk '{print $1}')"
if [ "$SHA_FORCED" = "$SHA_BEFORE" ]; then
  fail "(d) --force did not actually overwrite Bob.wav"
fi
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# (e) --t together with --to: nonzero exit.
# ---------------------------------------------------------------------------
echo "-- (e) --t and --to together --"

set +e
"$WHOSAID_BIN" enroll Conflict --from "$TMP/x.m4a" --t 5 --to 0:10 >/dev/null 2>&1
RC_E=$?
set -e
[ "$RC_E" -ne 0 ] || fail "(e) --t + --to unexpectedly exited 0"
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# (f) --ss without --from: nonzero exit.
# ---------------------------------------------------------------------------
echo "-- (f) --ss without --from --"

set +e
"$WHOSAID_BIN" enroll NoFrom --ss 5 >/dev/null 2>&1
RC_F=$?
set -e
[ "$RC_F" -ne 0 ] || fail "(f) --ss without --from unexpectedly exited 0"
PASS=$((PASS + 1))

# ---------------------------------------------------------------------------
# (g) a silent input: nonzero exit, "SILENT" in stderr.
# ---------------------------------------------------------------------------
echo "-- (g) silent input --"

ffmpeg -y -v error -f lavfi -i "anullsrc=r=16000:cl=mono" -t 20 -c:a pcm_s16le "$TMP/silent.wav" \
  || fail "ffmpeg failed to synthesize the silent fixture"

set +e
ERR_G="$("$WHOSAID_BIN" enroll Silent --from "$TMP/silent.wav" 2>&1 1>/dev/null)"
RC_G=$?
set -e
[ "$RC_G" -ne 0 ] || fail "(g) silent input unexpectedly exited 0"
PASS=$((PASS + 1))
assert_true "$(echo "$ERR_G" | grep -q 'SILENT' && echo 0 || echo 1)" "(g) stderr missing 'SILENT': $ERR_G"
assert_true "$([ ! -e "$WHOSAID_VOICE_REFS/Silent.wav" ] && echo 0 || echo 1)" "(g) Silent.wav should not exist"

# ---------------------------------------------------------------------------
# (h) an unknown option is rejected, never falls through to mic recording.
# ---------------------------------------------------------------------------
echo "-- (h) unknown option --"

# No GNU `timeout` on stock macOS; a perl alarm wrapper is a safety net in
# case of a regression that falls through to the (up to 45s) mic capture —
# a correct implementation returns immediately and never trips the alarm.
set +e
ERR_H="$(perl -e 'alarm shift; exec @ARGV' 10 "$WHOSAID_BIN" enroll Bob --bogus </dev/null 2>&1 1>/dev/null)"
RC_H=$?
set -e
[ "$RC_H" -ne 0 ] || fail "(h) --bogus unexpectedly exited 0"
PASS=$((PASS + 1))
assert_true "$(echo "$ERR_H" | grep -qi 'unknown option' && echo 0 || echo 1)" "(h) stderr missing 'unknown option': $ERR_H"

echo ""
echo "PASS: $PASS assertions"
