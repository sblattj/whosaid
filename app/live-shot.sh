#!/bin/bash
# Screenshot the Live now page for one live session, built by this checkout's code.
# Copies the session into a throwaway recordings folder (never ~/Recordings), so the
# rebuilt insights land in the copy, plus an empty throwaway workspace (never ~/Desktop).
# Usage: app/live-shot.sh <app> <session.jsonl> <out.png>
set -u
APP=${1:?app}; JL=${2:?session.jsonl}; PNG=${3:?out.png}
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(dirname "$HERE")
T=$(mktemp -d "${TMPDIR:-/tmp}/whosaid-app-fixture-shot.XXXXXX"); mkdir -p "$T/rec/.live" "$T/ws"
cp "$JL" "$T/rec/.live/$(basename "$JL")"
# an AI sidecar next to the session (<stem>.ai.json) comes along, so the AI cards show
AI="${JL%.jsonl}.ai.json"; [[ -f "$AI" ]] && cp "$AI" "$T/rec/.live/"
# build the insights up front: the self-test shoots the page after 600 ms, before an in-app rebuild ends
"$REPO/whosaid" live insights build --jsonl "$T/rec/.live/$(basename "$JL")" >/dev/null 2>&1 || { echo "live-shot: insights build failed"; exit 1; }
SELFTEST_TIMEOUT=${SELFTEST_TIMEOUT:-60} "$HERE/selftest.sh" "$APP" "$T/out" WHOSAID_RECORDINGS="$T/rec" WHOSAID_WORKSPACE="$T/ws" \
  WHOSAID_BIN="$REPO/whosaid" WHOSAID_APP_SELFTEST_ITEMS=live >"$T/selftest.log" 2>&1
st=$?
if [[ -f "$T/out.live.png" ]]; then mv "$T/out.live.png" "$PNG"; echo "$PNG"; [[ -f "$T/out.live.png.ax.txt" ]] && mv "$T/out.live.png.ax.txt" "$PNG.ax.txt" && echo "$PNG.ax.txt"; else cat "$T/selftest.log"; st=1; fi
rm -rf "$T"
exit $st
