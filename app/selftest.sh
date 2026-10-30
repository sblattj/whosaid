#!/bin/bash
# Run the app in self-test mode and capture only its own window on request.
# Usage: selftest.sh <app> <out-prefix> [VAR=value ...]  (env for the app)
APP=${1:?app}; OUT=${2:?out}; shift 2
rm -f "$OUT" "$OUT".*
envargs=(); for kv in "$@"; do envargs+=(--env "$kv"); done
# A fixture run must never see the real folders, which can come from this shell
# or from the launchd user environment (`launchctl setenv`). Blank them first
# (the app treats an empty value as unset); the VAR=value args win after that.
for v in WHOSAID_WORKSPACE WHOSAID_RECORDINGS WHOSAID_BIN WHOSAID_SPEAKER_DB; do unset "$v"; done
blank=(--env WHOSAID_WORKSPACE= --env WHOSAID_RECORDINGS= --env WHOSAID_BIN= --env WHOSAID_SPEAKER_DB=)
# A copy left running ignores new --env values ("already running"): stop it first.
pkill -f "$APP/Contents/MacOS/" 2>/dev/null && sleep 1
# -n: a second copy, so an installed whosaid that is already running is left alone.
open -n "${blank[@]}" "${envargs[@]}" --env WHOSAID_APP_SELFTEST="$OUT" "$APP"
end=$((SECONDS + ${SELFTEST_TIMEOUT:-90})); last=""
while (( SECONDS < end )) && [[ ! -s "$OUT" ]]; do
  if [[ -f "$OUT.req" ]]; then
    req=$(cat "$OUT.req"); 
    if [[ "$req" != "$last" ]]; then
      read -r png wid <<<"$req"; screencapture -x -o -l "$wid" "$png"; last="$req"
    fi
  fi
  sleep 0.2
done
rm -f "$OUT.req"
cat "$OUT" 2>/dev/null || { echo "selftest: no result in ${SELFTEST_TIMEOUT:-90}s"; exit 1; }
