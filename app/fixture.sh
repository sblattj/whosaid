#!/bin/bash
# Build a tiny indexed workspace for app self-tests and screenshots: two meetings,
# made-up names only, one unnamed voice. Prints the folder (ws/ and rec/ inside).
# Usage: app/fixture.sh [DIR]   (default: a new temp dir)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
F="${1:-$(mktemp -d -t whosaid-app-fixture)}"
W="$F/ws"
mkdir -p "$W/2026-10-27-0900-standup" "$W/2026-10-28-1400-planning" "$F/rec"
printf '[workspace]\nowner = "Alice_Example"\n[summarizer]\nengine = "none"\n' > "$W/whosaid.toml"
cat > "$W/2026-10-27-0900-standup/standup.speakers.txt" <<'T'
[00:00:01] Alice_Example: Morning. I will send the release notes today.
[00:00:06] Bob_Example: I can review the parser change after lunch.
[00:00:12] SPEAKER_02: Is the demo still on Friday?
T
cat > "$W/2026-10-28-1400-planning/planning.speakers.txt" <<'T'
[00:00:01] Bob_Example: Let us plan the next sprint.
[00:00:05] Alice_Example: I will draft the roadmap by Thursday.
T
for f in "$W"/*/*.speakers.txt; do sed 's/^\[[^]]*\] [^:]*: //' "$f" > "${f%.speakers.txt}.txt"; done
"$REPO/whosaid" index "$W" --no-embed >&2
echo "$F"
