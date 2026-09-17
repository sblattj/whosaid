#!/bin/bash
#
# test/samples_test.sh — offline test for `whosaid samples <base>` (GitHub
# issue #1, "nice-to-haves": per-speaker sample-clip export for human
# verification).
#
# Fully offline and self-contained: synthesizes ~30s of speech with macOS
# `say` (same technique as test/enroll_from_file_test.sh), hand-writes a
# <base>.diarization.json sidecar with two speaker clusters whose segments
# point into that audio (no model, no diarization run), then drives the real
# `whosaid samples` CLI against it. Exercises longest-segment picking, the
# --seconds clamp, naming (SPEAKER_NN vs SPEAKER_NN-Name), output format
# (mono/16k), --per-speaker, --json, and the source/--audio fallback.
#
# macOS/BSD only: relies on `say`, BSD grep/awk, bash 3.2 (no associative
# arrays, no bash-4-isms). Skips (exit 0, clear message) when say/ffmpeg/
# ffprobe/python3 are missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WHOSAID_BIN="$REPO/whosaid"

for dep in say ffmpeg ffprobe python3; do
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

assert_close() {  # assert_close <a> <b> <tolerance> <what>
  if awk -v a="$1" -v b="$2" -v tol="$3" 'BEGIN{d=a-b; if (d<0) d=-d; exit !(d <= tol)}'; then
    PASS=$((PASS + 1))
  else
    fail "$4 — [$1] and [$2] differ by more than [$3]"
  fi
}

# ---------------------------------------------------------------------------
# 1. Synthesize ~30-40s of speech with a `say` voice (same recipe as
#    enroll_from_file_test.sh).
# ---------------------------------------------------------------------------
echo "== samples_test: temp dir $TMP =="
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

echo "-- synthesizing ~30-40s source clip --"

SPEECH="Good afternoon team. Here is a short recap of where the archive migration stands before the next check-in. The old file server has been fully mirrored onto the new storage array, and checksums confirm every folder matches byte for byte. We are still waiting on legal to sign off on the retention policy for the oldest client records before we can decommission the original drives. If anyone runs into a broken link in the shared drive this week, please flag it in the tracker rather than fixing it directly so we can see the full pattern of what moved. I will circulate an updated timeline once the retention question is settled, hopefully by the end of the week."

say -v "$VOICE" -o "$TMP/raw.aiff" "$SPEECH" || fail "say failed to synthesize the source clip"
ffmpeg -y -v error -i "$TMP/raw.aiff" -ac 1 -ar 16000 "$TMP/audio.wav" \
  || fail "ffmpeg failed to encode the source clip to wav"

SRC_DUR="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TMP/audio.wav")"
echo "source clip: $TMP/audio.wav (${SRC_DUR}s)"
if ! awk -v d="$SRC_DUR" 'BEGIN{exit !((d+0) >= 26)}'; then
  fail "synthesized source clip too short (${SRC_DUR}s) for this test's segment windows — need >= 26s"
fi

AUDIO_ABS="$TMP/audio.wav"
BASE="audio"
SIDECAR="$TMP/$BASE.diarization.json"

# ---------------------------------------------------------------------------
# 2. Hand-write the sidecar: two clusters over that audio, no model/diarizer
#    run. SPEAKER_00=Alice gets a 12s longest segment (> --seconds default 8,
#    so the export clamps) plus a shorter 4s second segment (for --per-speaker
#    2). SPEAKER_01 stays unnamed, with a 5s longest segment (under the
#    --seconds clamp) plus a 2s second segment.
# ---------------------------------------------------------------------------
cat > "$SIDECAR" <<JSONEOF
{
  "base": "$BASE",
  "emb_model": "test",
  "num_speakers": 2,
  "names": {"SPEAKER_00": "Alice", "SPEAKER_01": "SPEAKER_01"},
  "registry_matches": [],
  "source": {"path": "$AUDIO_ABS", "duration_seconds": $SRC_DUR, "creation_time": null},
  "segments": [
    {"start": 0.0, "end": 12.0, "speaker": "SPEAKER_00"},
    {"start": 12.0, "end": 16.0, "speaker": "SPEAKER_00"},
    {"start": 16.0, "end": 21.0, "speaker": "SPEAKER_01"},
    {"start": 21.0, "end": 23.0, "speaker": "SPEAKER_01"}
  ],
  "cluster_emb": {},
  "whisper_json": null
}
JSONEOF

# ---------------------------------------------------------------------------
# (a) default run (--per-speaker 1): exit 0, one file per cluster, correct
#     names, durations, format, and TSV line count.
# ---------------------------------------------------------------------------
echo "-- (a) default: one clip per cluster --"

set +e
OUT_A="$("$WHOSAID_BIN" samples "$BASE" -o "$TMP" 2>"$TMP/err-a.log")"
RC_A=$?
set -e
[ "$RC_A" -eq 0 ] || fail "(a) samples exited $RC_A: $(cat "$TMP/err-a.log")"
PASS=$((PASS + 1))

SAMPLES_DIR="$TMP/$BASE.samples"
SPK00_WAV="$SAMPLES_DIR/SPEAKER_00-Alice.wav"
SPK01_WAV="$SAMPLES_DIR/SPEAKER_01.wav"

assert_true "$([ -f "$SPK00_WAV" ] && echo 0 || echo 1)" "(a) SPEAKER_00-Alice.wav was not created"
assert_true "$([ -f "$SPK01_WAV" ] && echo 0 || echo 1)" "(a) SPEAKER_01.wav was not created"

# expected: min(longest segment, --seconds default 8) — SPEAKER_00's longest
# segment is 12s (clamped to 8), SPEAKER_01's longest is 5s (under the clamp).
DUR00="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$SPK00_WAV")"
assert_close "$DUR00" 8.0 0.5 "(a) SPEAKER_00-Alice.wav duration"
DUR01="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$SPK01_WAV")"
assert_close "$DUR01" 5.0 0.5 "(a) SPEAKER_01.wav duration"

CH_SR_00="$(ffprobe -v error -show_entries stream=channels,sample_rate -of csv=p=0 "$SPK00_WAV")"
assert_eq "$CH_SR_00" "16000,1" "(a) SPEAKER_00-Alice.wav channels,sample_rate"
CH_SR_01="$(ffprobe -v error -show_entries stream=channels,sample_rate -of csv=p=0 "$SPK01_WAV")"
assert_eq "$CH_SR_01" "16000,1" "(a) SPEAKER_01.wav channels,sample_rate"

TSV_LINES="$(printf '%s' "$OUT_A" | grep -c '^')"
assert_eq "$TSV_LINES" "2" "(a) TSV line count"

# ---------------------------------------------------------------------------
# (b) --json: parses, 2 entries.
# ---------------------------------------------------------------------------
echo "-- (b) --json output --"

OUT_B_ROOT="$TMP/out-b"
mkdir -p "$OUT_B_ROOT"

set +e
OUT_B="$("$WHOSAID_BIN" samples "$SIDECAR" -o "$OUT_B_ROOT" --json 2>"$TMP/err-b.log")"
RC_B=$?
set -e
[ "$RC_B" -eq 0 ] || fail "(b) samples --json exited $RC_B: $(cat "$TMP/err-b.log")"
PASS=$((PASS + 1))

JSON_LINE_B="$(printf '%s\n' "$OUT_B" | tail -n 1)"
set +e
python3 -c '
import json, sys
d = json.loads(sys.argv[1])
samples = d.get("samples")
assert isinstance(samples, list) and len(samples) == 2, d
' "$JSON_LINE_B"
RC_B_PARSE=$?
set -e
assert_true "$RC_B_PARSE" "(b) --json output did not parse with 2 entries: $JSON_LINE_B"

# ---------------------------------------------------------------------------
# (c) a sidecar WITHOUT 'source' fails mentioning --audio, and succeeds when
#     --audio is given.
# ---------------------------------------------------------------------------
echo "-- (c) sidecar without 'source' + --audio fallback --"

NOSRC_BASE="nosource"
NOSRC_SIDECAR="$TMP/$NOSRC_BASE.diarization.json"
cat > "$NOSRC_SIDECAR" <<JSONEOF
{
  "base": "$NOSRC_BASE",
  "emb_model": "test",
  "num_speakers": 2,
  "names": {"SPEAKER_00": "Alice", "SPEAKER_01": "SPEAKER_01"},
  "registry_matches": [],
  "segments": [
    {"start": 0.0, "end": 12.0, "speaker": "SPEAKER_00"},
    {"start": 16.0, "end": 21.0, "speaker": "SPEAKER_01"}
  ],
  "cluster_emb": {},
  "whisper_json": null
}
JSONEOF

set +e
ERR_C="$("$WHOSAID_BIN" samples "$NOSRC_BASE" -o "$TMP" 2>&1 1>/dev/null)"
RC_C=$?
set -e
[ "$RC_C" -ne 0 ] || fail "(c) samples on a sourceless sidecar unexpectedly exited 0"
PASS=$((PASS + 1))
assert_true "$(echo "$ERR_C" | grep -q -- '--audio' && echo 0 || echo 1)" "(c) stderr missing '--audio': $ERR_C"

set +e
OUT_C2="$("$WHOSAID_BIN" samples "$NOSRC_BASE" -o "$TMP" --audio "$AUDIO_ABS" 2>"$TMP/err-c2.log")"
RC_C2=$?
set -e
[ "$RC_C2" -eq 0 ] || fail "(c) samples --audio exited $RC_C2: $(cat "$TMP/err-c2.log")"
PASS=$((PASS + 1))

assert_true "$([ -f "$TMP/$NOSRC_BASE.samples/SPEAKER_00-Alice.wav" ] && echo 0 || echo 1)" \
  "(c) --audio fallback did not create SPEAKER_00-Alice.wav"

# ---------------------------------------------------------------------------
# (d) --per-speaker 2: up to 2 files per cluster, second one carries a '-2'
#     suffix, and its duration matches the cluster's second-longest segment.
# ---------------------------------------------------------------------------
echo "-- (d) --per-speaker 2 --"

OUT_D_ROOT="$TMP/out-d"
mkdir -p "$OUT_D_ROOT"

set +e
OUT_D="$("$WHOSAID_BIN" samples "$SIDECAR" -o "$OUT_D_ROOT" --per-speaker 2 2>"$TMP/err-d.log")"
RC_D=$?
set -e
[ "$RC_D" -eq 0 ] || fail "(d) samples --per-speaker 2 exited $RC_D: $(cat "$TMP/err-d.log")"
PASS=$((PASS + 1))

D_DIR="$OUT_D_ROOT/$BASE.samples"
assert_true "$([ -f "$D_DIR/SPEAKER_00-Alice.wav" ] && echo 0 || echo 1)" "(d) SPEAKER_00-Alice.wav missing"
assert_true "$([ -f "$D_DIR/SPEAKER_00-Alice-2.wav" ] && echo 0 || echo 1)" "(d) SPEAKER_00-Alice-2.wav missing"
assert_true "$([ -f "$D_DIR/SPEAKER_01.wav" ] && echo 0 || echo 1)" "(d) SPEAKER_01.wav missing"
assert_true "$([ -f "$D_DIR/SPEAKER_01-2.wav" ] && echo 0 || echo 1)" "(d) SPEAKER_01-2.wav missing"

# second-longest segments: SPEAKER_00 4s (12-16), SPEAKER_01 2s (21-23) — both
# under the 8s clamp, so exported at their own length.
DUR00_2="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$D_DIR/SPEAKER_00-Alice-2.wav")"
assert_close "$DUR00_2" 4.0 0.5 "(d) SPEAKER_00-Alice-2.wav duration"
DUR01_2="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$D_DIR/SPEAKER_01-2.wav")"
assert_close "$DUR01_2" 2.0 0.5 "(d) SPEAKER_01-2.wav duration"

echo ""
echo "PASS: $PASS assertions"
