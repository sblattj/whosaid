#!/bin/bash
#
# test/e2e.sh — offline end-to-end smoke test for whosaid.
#
# Requires the models to already be downloaded (run ./bootstrap.sh once
# first). Fully offline and self-contained otherwise: synthesizes a
# two-speaker dialog with two macOS `say` voices, builds a one-clip
# enrollment reference for one of them, and runs the real transcribe +
# diarize + name pipeline against it in a temp directory. Takes a few
# minutes (model load + inference on the synthesized audio).
#
# macOS/BSD only: relies on `say`, BSD grep/awk, bash 3.2 (no associative
# arrays, no bash-4-isms).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

PASS=0
TMP="$(mktemp -d)"

cleanup() {
  if [ "$PASS" -eq 1 ]; then
    rm -rf "$TMP"
  else
    echo "" >&2
    echo "FAIL: leaving temp dir for inspection: $TMP" >&2
  fi
}
trap cleanup EXIT

echo "== whosaid e2e: temp dir $TMP =="

# ---------------------------------------------------------------------------
# 1. Static checks — every source file must exist and parse cleanly. A
#    missing file is a failure, never a skip.
# ---------------------------------------------------------------------------
echo "-- static checks --"

for f in "$REPO/whosaid" "$REPO/bootstrap.sh" "$REPO/lib/transcribe_mlx.py" "$REPO/lib/diarize_sherpa.py"; do
  [ -f "$f" ] || fail "required source file missing: $f"
done

bash -n "$REPO/whosaid"      || fail "bash -n failed on whosaid"
bash -n "$REPO/bootstrap.sh" || fail "bash -n failed on bootstrap.sh"
bash -n "$SCRIPT_PATH"       || fail "bash -n failed on test/e2e.sh"

command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH"
python3 -m py_compile "$REPO/lib/transcribe_mlx.py" "$REPO/lib/diarize_sherpa.py" \
  || fail "python3 -m py_compile failed on lib/*.py"

echo "static checks OK"

# ---------------------------------------------------------------------------
# 2. Install contract: the command must work through a symlink outside the
#    checkout, because consumers depend on the executable rather than repo
#    internals. This is local and does not touch the user's real ~/.local/bin.
# ---------------------------------------------------------------------------
echo "-- checking install contract --"

mkdir -p "$TMP/bin"
WHOSAID_INSTALL_DIR="$TMP/bin" "$REPO/whosaid" install \
  || fail "whosaid install failed"
[ -L "$TMP/bin/whosaid" ] || fail "install did not create a command symlink"
WHOSAID_INSTALL_DIR="$TMP/bin" "$REPO/whosaid" install \
  || fail "re-running whosaid install was not idempotent"
"$TMP/bin/whosaid" help >/dev/null \
  || fail "installed command could not resolve its checkout through the symlink"

mkdir -p "$TMP/update-bin"
ln -s "/old/checkout/whosaid" "$TMP/update-bin/whosaid"
WHOSAID_INSTALL_DIR="$TMP/update-bin" "$REPO/whosaid" install \
  || fail "install could not update an existing command symlink"
[ "$(readlink "$TMP/update-bin/whosaid")" = "$REPO/whosaid" ] \
  || fail "install did not repoint an existing command symlink"

mkdir -p "$TMP/occupied-bin"
printf 'unrelated-command\n' > "$TMP/occupied-bin/whosaid"
set +e
WHOSAID_INSTALL_DIR="$TMP/occupied-bin" "$REPO/whosaid" install >/dev/null 2>&1
INSTALL_RC=$?
set -e
[ "$INSTALL_RC" -ne 0 ] || fail "install replaced an unrelated existing command"
grep -qx 'unrelated-command' "$TMP/occupied-bin/whosaid" \
  || fail "install mutated an unrelated existing command"

echo "install contract OK"

# ---------------------------------------------------------------------------
# 3. Pick two distinct `say` voices, in preference order.
# ---------------------------------------------------------------------------
echo "-- selecting say voices --"

command -v say >/dev/null 2>&1 || fail "macOS 'say' command not found"

AVAILABLE_VOICES="$(say -v '?' | awk '{print $1}')"
PREFERRED="Samantha Daniel Karen Moira Rishi Fred Alex"

VOICE_A=""
VOICE_B=""
for v in $PREFERRED; do
  if echo "$AVAILABLE_VOICES" | grep -qx "$v"; then
    if [ -z "$VOICE_A" ]; then
      VOICE_A="$v"
    elif [ -z "$VOICE_B" ]; then
      VOICE_B="$v"
      break
    fi
  fi
done

if [ -z "$VOICE_A" ] || [ -z "$VOICE_B" ]; then
  fail "need at least two of these 'say' voices installed: $PREFERRED (available: $(echo "$AVAILABLE_VOICES" | tr '\n' ' '))"
fi

echo "voice A (will be enrolled as Alice): $VOICE_A"
echo "voice B (left unenrolled):           $VOICE_B"

# ---------------------------------------------------------------------------
# 4. Synthesize a 2-speaker dialog: 6 alternating utterances, 0.8s gaps.
# ---------------------------------------------------------------------------
echo "-- synthesizing dialog --"

command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg not found on PATH"

U1="The weather this weekend looks perfect for a hike. The forecast is calling for clear skies and mild temperatures all the way through Sunday afternoon."
U2="That is great news. I was thinking about trying that new trail near the reservoir, as long as the ground is not too muddy from last week's rain."
U3="I tried a new pasta recipe last night with roasted garlic and lemon. It turned out much better than I expected for a first attempt."
U4="I have been meaning to cook more at home instead of ordering takeout so often. Maybe you can send me that recipe sometime this week."
U5="We are planning a trip to Portugal this fall. I have heard the coastal towns are beautiful and a lot quieter than the big cities."
U6="That sounds wonderful. I visited Lisbon a few years ago, and the food alone made the entire trip worth it."

TEXTS=("$U1" "$U2" "$U3" "$U4" "$U5" "$U6")
VOICES=("$VOICE_A" "$VOICE_B" "$VOICE_A" "$VOICE_B" "$VOICE_A" "$VOICE_B")

ffmpeg -y -v error -f lavfi -i "anullsrc=r=16000:cl=mono" -t 0.8 -c:a pcm_s16le "$TMP/silence.wav" \
  || fail "ffmpeg failed to synthesize the inter-utterance silence clip"

LIST="$TMP/concat_list.txt"
: > "$LIST"

i=1
while [ "$i" -le 6 ]; do
  idx=$((i - 1))
  text="${TEXTS[$idx]}"
  voice="${VOICES[$idx]}"
  raw="$TMP/u${i}.aiff"
  wav="$TMP/u${i}.wav"

  say -v "$voice" -o "$raw" "$text" || fail "say failed to synthesize utterance $i"
  ffmpeg -y -v error -i "$raw" -ar 16000 -ac 1 -c:a pcm_s16le "$wav" \
    || fail "ffmpeg failed to normalize utterance $i to 16kHz mono WAV"

  echo "file '$wav'" >> "$LIST"
  if [ "$i" -lt 6 ]; then
    echo "file '$TMP/silence.wav'" >> "$LIST"
  fi

  i=$((i + 1))
done

ffmpeg -y -v error -f concat -safe 0 -i "$LIST" -c copy "$TMP/dialog.wav" \
  || fail "ffmpeg failed to concatenate the dialog from utterance clips"

echo "dialog synthesized: $TMP/dialog.wav"

# ---------------------------------------------------------------------------
# 5. Build the enrollment reference: ~20s of different text, voice A, saved
#    as Alice.wav in a temp voice-refs dir (never the repo's real voices/).
# ---------------------------------------------------------------------------
echo "-- building enrollment reference --"

mkdir -p "$TMP/refs"

ENROLL_TEXT="Good morning everyone. I wanted to share a quick update on the community garden project before we get started today. Over the past month, several volunteers have been repairing the fence line and turning over the soil in preparation for the spring planting season. We still need a few more people to help build the raised beds near the north entrance, and any extra hands this Saturday would be greatly appreciated."

say -v "$VOICE_A" -o "$TMP/enroll_raw.aiff" "$ENROLL_TEXT" \
  || fail "say failed to synthesize the enrollment reference"
ffmpeg -y -v error -i "$TMP/enroll_raw.aiff" -ar 16000 -ac 1 -c:a pcm_s16le "$TMP/refs/Alice.wav" \
  || fail "ffmpeg failed to normalize the enrollment reference to 16kHz mono WAV"

echo "enrollment reference: $TMP/refs/Alice.wav"

# ---------------------------------------------------------------------------
# 6. Run the pipeline against the temp voice-refs dir. The repo's real
#    voices/ directory must never be touched.
# ---------------------------------------------------------------------------
echo "-- running whosaid --"

[ -x "$REPO/whosaid" ] || fail "whosaid is not executable: $REPO/whosaid"

REPO_VOICES_BEFORE=""
if [ -d "$REPO/voices" ]; then
  REPO_VOICES_BEFORE="$(ls -la "$REPO/voices" 2>/dev/null || true)"
fi

# Registry isolation: point the speaker registry at a throwaway temp file for the
# WHOLE test. The user's real registry (~/.config/whosaid/speakers.json) must be
# neither read (it would make auto-naming non-deterministic) nor written. Capture
# its checksum up front so we can prove at the end it never changed.
SPEAKER_DB="$TMP/speakers.json"
export WHOSAID_SPEAKER_DB="$SPEAKER_DB"
REAL_DB="$HOME/.config/whosaid/speakers.json"
REAL_DB_BEFORE=""
[ -f "$REAL_DB" ] && REAL_DB_BEFORE="$(shasum "$REAL_DB" | awk '{print $1}')"

# A dotted -n exercises the dot-sanitize: mlx-whisper's writer runs its own
# splitext on the base, so "call.v1" must be sanitized to "call_v1" or every
# output file would be truncated at the dot (and the CLI would report a false
# failure). BASE is the sanitized name every output file should carry.
BASE="call_v1"

mkdir -p "$TMP/out"
RUN_LOG="$TMP/run.log"

set +e
WHOSAID_VOICE_REFS="$TMP/refs" "$REPO/whosaid" "$TMP/dialog.wav" -n "call.v1" --speakers 2 -o "$TMP/out" > "$RUN_LOG" 2>&1
RC=$?
set -e

if [ "$RC" -ne 0 ]; then
  echo "---- whosaid run log ----" >&2
  cat "$RUN_LOG" >&2
  fail "whosaid exited $RC (expected 0)"
fi

if [ -d "$REPO/voices" ]; then
  REPO_VOICES_AFTER="$(ls -la "$REPO/voices" 2>/dev/null || true)"
  [ "$REPO_VOICES_BEFORE" = "$REPO_VOICES_AFTER" ] || fail "the repo's real voices/ directory changed during the test run"
fi

# ---------------------------------------------------------------------------
# 7. Assertions on the output.
# ---------------------------------------------------------------------------
echo "-- checking output --"

TXT="$TMP/out/$BASE.txt"
SPEAKERS="$TMP/out/$BASE.speakers.txt"

# Dotted-name sanitize: outputs must carry the sanitized base (call_v1.*), and NO
# file truncated at the dot (call.*) may exist.
[ -s "$TXT" ] || fail "missing or empty plain transcript: $TXT (dot-sanitize of -n 'call.v1' -> '$BASE' may have failed)"
[ ! -e "$TMP/out/call.txt" ] || fail "found truncated-at-dot output $TMP/out/call.txt — the -n dot-sanitize regressed"
[ -s "$SPEAKERS" ] || fail "missing or empty speaker-labeled transcript: $SPEAKERS"

grep -q 'Alice:' "$SPEAKERS" \
  || fail "'Alice:' label not found in $SPEAKERS (enrollment cosine-match failed to name the enrolled speaker)"

grep -qE 'SPEAKER_[0-9]+' "$SPEAKERS" \
  || fail "no unnamed 'SPEAKER_NN' label found in $SPEAKERS (expected the unenrolled second voice to appear as a raw cluster label)"

TURN_PATTERN='^\[[^]]*\][[:space:]]*[^:]+:'
TURN_COUNT="$(grep -cE "$TURN_PATTERN" "$SPEAKERS" || true)"
[ "$TURN_COUNT" -ge 2 ] || fail "expected at least 2 speaker-turn lines in $SPEAKERS, found $TURN_COUNT"

SPEAKER_LABELS="$(grep -oE "$TURN_PATTERN" "$SPEAKERS" | sed -E 's/^\[[^]]*\][[:space:]]*//; s/:$//' | sort -u | tr '\n' ' ')"

# ---------------------------------------------------------------------------
# 8. Registry: relabel persists a voiceprint, and a later run auto-names it.
#    Covers `whosaid relabel` (cluster -> name, persisted) and the auto-name
#    reload path. Uses the cached sidecar + a diarize-only reload (no Whisper).
# ---------------------------------------------------------------------------
echo "-- checking registry relabel + auto-name reload --"

UNNAMED="$(grep -oE 'SPEAKER_[0-9]+' "$SPEAKERS" | head -1)"
[ -n "$UNNAMED" ] || fail "could not find an unnamed SPEAKER_NN cluster to relabel"

set +e
"$REPO/whosaid" relabel "$BASE" "$UNNAMED=Bob" -o "$TMP/out" > "$TMP/relabel.log" 2>&1
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  cat "$TMP/relabel.log" >&2
  fail "whosaid relabel exited $RC (expected 0)"
fi

grep -q 'Bob:' "$SPEAKERS" \
  || fail "relabel did not rewrite $SPEAKERS to name '$UNNAMED' as Bob"
if grep -qE "^\[[^]]*\][[:space:]]*$UNNAMED:" "$SPEAKERS"; then
  fail "relabel left the raw '$UNNAMED' label in $SPEAKERS"
fi

[ -s "$SPEAKER_DB" ] || fail "relabel did not create the speaker registry at $SPEAKER_DB"
grep -q '"Bob"' "$SPEAKER_DB" \
  || fail "relabel did not persist Bob's voiceprint into $SPEAKER_DB"

# Reload: re-diarize the SAME dialog reading the temp registry (no Whisper —
# reuse the cached whisper json) and confirm Bob is auto-named from it.
mkdir -p "$TMP/reload"
set +e
uv run --quiet --with sherpa-onnx --with numpy python "$REPO/lib/diarize_sherpa.py" \
  "$TMP/dialog.wav" --whisper-json "$TMP/out/$BASE.json" \
  --outdir "$TMP/reload" --name reload --num-speakers 2 \
  > "$TMP/reload.log" 2>&1
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  cat "$TMP/reload.log" >&2
  fail "reload diarization exited $RC (expected 0)"
fi
grep -q 'Bob:' "$TMP/reload/reload.speakers.txt" \
  || fail "auto-name reload failed: Bob was not recovered from the registry in reload.speakers.txt"

echo "registry relabel + reload OK"

# ---------------------------------------------------------------------------
# 9. Parallel chunked diarization must match the single-pass speaker set.
#    Forces the parallel path on the short clip via an explicit --chunk-seconds
#    and asserts it (a) took the parallel path and (b) recovered the same
#    speaker count as --no-chunk. Diarize-only (reuses the cached whisper json).
# ---------------------------------------------------------------------------
echo "-- checking parallel vs single-pass diarization --"

nspk() {  # num_speakers from the diarizer's final JSON line on stdout (arg: file)
  python3 -c 'import json,sys
data=None
for ln in open(sys.argv[1]):
    ln=ln.strip()
    if ln.startswith("{"):
        try:
            data=json.loads(ln)
        except Exception:
            pass
print(data.get("num_speakers","?") if data else "?")' "$1"
}

mkdir -p "$TMP/nochunk" "$TMP/chunk"
set +e
uv run --quiet --with sherpa-onnx --with numpy python "$REPO/lib/diarize_sherpa.py" \
  "$TMP/dialog.wav" --whisper-json "$TMP/out/$BASE.json" \
  --outdir "$TMP/nochunk" --name d --num-speakers 2 --no-chunk \
  > "$TMP/nochunk.out" 2> "$TMP/nochunk.log"
RC_NC=$?
uv run --quiet --with sherpa-onnx --with numpy python "$REPO/lib/diarize_sherpa.py" \
  "$TMP/dialog.wav" --whisper-json "$TMP/out/$BASE.json" \
  --outdir "$TMP/chunk" --name d --num-speakers 2 --chunk-seconds 15 --jobs 2 \
  > "$TMP/chunk.out" 2> "$TMP/chunk.log"
RC_CH=$?
set -e
[ "$RC_NC" -eq 0 ] || { cat "$TMP/nochunk.log" >&2; fail "no-chunk diarization exited $RC_NC"; }
[ "$RC_CH" -eq 0 ] || { cat "$TMP/chunk.log" >&2; fail "chunked diarization exited $RC_CH"; }

grep -q 'parallel diarization' "$TMP/chunk.log" \
  || { cat "$TMP/chunk.log" >&2; fail "the --chunk-seconds run did NOT take the parallel path (chunk-gate regressed)"; }

NC_N="$(nspk "$TMP/nochunk.out")"
CH_N="$(nspk "$TMP/chunk.out")"
[ "$NC_N" = "2" ] || fail "no-chunk diarization found $NC_N speakers, expected 2"
[ "$CH_N" = "2" ] || fail "chunked diarization found $CH_N speakers, expected 2"
[ "$NC_N" = "$CH_N" ] || fail "parallel ($CH_N) and single-pass ($NC_N) speaker counts differ"

echo "parallel vs single-pass OK ($CH_N speakers each)"

# ---------------------------------------------------------------------------
# 10. Isolation proof: the user's REAL registry was never touched.
# ---------------------------------------------------------------------------
REAL_DB_AFTER=""
[ -f "$REAL_DB" ] && REAL_DB_AFTER="$(shasum "$REAL_DB" | awk '{print $1}')"
[ "$REAL_DB_BEFORE" = "$REAL_DB_AFTER" ] \
  || fail "the user's real speaker registry ($REAL_DB) changed during the test — isolation broke"

PASS=1

echo ""
echo "== PASS =="
echo "speaker turns:     $TURN_COUNT"
echo "speakers detected: $SPEAKER_LABELS"
echo "(temp dir $TMP will be removed on exit)"
