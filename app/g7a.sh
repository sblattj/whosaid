#!/bin/bash
# G7a: replay a recorded (synthetic) call into a throwaway recordings folder while the
# app follows it, then report latency, topics, nodes, fps and CPU, and stitch the
# frames into a screen recording. Never touches ~/Recordings or a real workspace.
# Usage: app/g7a.sh <app> <out-dir> [fixture.jsonl] [interval-seconds] [lines-per-write]
set -u
APP=${1:?app}; OUTDIR=${2:?out-dir}
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(dirname "$HERE")
FIX=${3:-$REPO/test/fixtures/live/call.jsonl}; INTERVAL=${4:-0.5}; PER=${5:-2}
T=$(mktemp -d "${TMPDIR:-/tmp}/whosaid-app-fixture-live.XXXXXX"); REC="$T/rec"; mkdir -p "$REC" "$OUTDIR"
OUT="$OUTDIR/g7a"; LOG="$OUTDIR/g7a.replay.log"; CPU="$OUTDIR/g7a.cpu.log"
echo "g7a: recordings $REC, log $LOG"
# WebContent processes that exist before launch belong to other apps; count only new ones.
pre=" $(pgrep -f com.apple.WebKit.WebContent | tr '\n' ' ') "
SELFTEST_TIMEOUT=400 "$HERE/selftest.sh" "$APP" "$OUT" WHOSAID_RECORDINGS="$REC" WHOSAID_BIN="$REPO/whosaid" \
  WHOSAID_APP_SELFTEST_ITEMS=live WHOSAID_APP_SELFTEST_LIVE=360 >"$OUTDIR/g7a.selftest.log" 2>&1 &
st=$!
sleep 4  # let the window and the follower come up before the first line
python3 "$REPO/test/live_replay.py" --fixture "$FIX" --into "$REC" --interval "$INTERVAL" --lines-per-write "$PER" >"$LOG" 2>&1 &
rp=$!
# CPU of the app and its WebKit content process, once a second, while the self-test runs.
: >"$CPU"
while kill -0 "$st" 2>/dev/null; do
  app=$(pgrep -f "$APP/Contents/MacOS/" | head -1)
  if [[ -n "$app" ]]; then
    a=$(ps -o %cpu= -p "$app" | tr -d ' ')
    w=0
    for pid in $(pgrep -f com.apple.WebKit.WebContent); do
      [[ "$pre" == *" $pid "* ]] && continue
      w=$(awk -v a="$w" -v b="$(ps -o %cpu= -p "$pid" | tr -d ' ')" 'BEGIN {print a + b}')
    done
    echo "$(date +%s) app=$a webcontent=$w" >>"$CPU"
  fi
  sleep 1
done
wait "$rp" 2>/dev/null
cat "$OUT" 2>/dev/null || { echo "g7a: no self-test result"; exit 1; }
python3 - "$LOG" "$OUT" "$CPU" <<'EOF'
import re, sys, statistics as st
writes = [(int(m[0]), float(m[1])) for m in re.findall(r"write n=(\d+) wall=([\d.]+)", open(sys.argv[1]).read())]
seen = [(int(m[0]), float(m[1])) for m in re.findall(r"seen n=(\d+) wall=([\d.]+)", open(sys.argv[2]).read())]
lat = []
for n, w in writes:
    later = [t for k, t in seen if k >= n and t >= w - 0.01]
    if later:
        lat.append(min(later) - w)
print(f"latency writes={len(writes)} matched={len(lat)} max={max(lat):.1f}s median={st.median(lat):.1f}s" if lat else "latency none")
rows = [tuple(map(float, re.findall(r"=([\d.]+)", l))) for l in open(sys.argv[3]) if "app=" in l]
rows = rows[3:]  # skip launch
if rows:
    app = [r[0] for r in rows]; web = [r[1] for r in rows]
    print(f"cpu app median={st.median(app):.1f}% max={max(app):.1f}% webcontent median={st.median(web):.1f}% max={max(web):.1f}% samples={len(rows)}")
EOF
# The recording: the 2 s frames at 4 fps (never committed; it lands next to the shots).
if ls "$OUT".live-*.png >/dev/null 2>&1 && command -v ffmpeg >/dev/null; then
  ffmpeg -loglevel error -y -framerate 4 -pattern_type glob -i "$OUT.live-*.png" \
    -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -pix_fmt yuv420p "$OUTDIR/g7a-replay.mp4" && echo "recording $OUTDIR/g7a-replay.mp4"
fi
rm -rf "$T"
