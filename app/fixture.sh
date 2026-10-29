#!/bin/bash
# Build a tiny indexed workspace for app self-tests and screenshots: two calls and a chat,
# made-up names only, one unnamed voice. Prints the folder (ws/ and rec/ inside).
# Usage: app/fixture.sh [DIR]   (default: a new temp dir)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
F="${1:-$(mktemp -d -t whosaid-app-fixture)}"
W="$F/ws"
# Real workspaces name folders YYYY-MM-DD-HHMM and files transcript.*; calls carry no
# title (the source is a timestamped .m4a) and chats carry chat_name in the sidecar.
S="$W/2026-10-27-0900" P="$W/2026-10-28-1400" C="$W/2026-10-29-0800"
mkdir -p "$S" "$P" "$C" "$F/rec"
printf '[workspace]\nowner = "Alice_Example"\n[summarizer]\nengine = "none"\n' > "$W/whosaid.toml"
cat > "$S/transcript.speakers.txt" <<'T'
[00:00:01] Alice_Example: Morning. I will send the release notes today.
[00:00:06] Bob_Example: I can review the parser change after lunch.
[00:00:12] SPEAKER_02: Is the demo still on Friday?
T
cat > "$P/transcript.speakers.txt" <<'T'
[00:00:01] Bob_Example: Let us plan the next sprint.
[00:00:05] Alice_Example: I will draft the roadmap by Thursday.
T
cat > "$C/transcript.speakers.txt" <<'T'
[00:00:00] Bob_Example: Pushed the parser fix, can you look?
[00:01:30] Alice_Example: On it after standup.
T
for f in "$W"/*/*.speakers.txt; do sed 's/^\[[^]]*\] [^:]*: //' "$f" > "${f%.speakers.txt}.txt"; done
# Sidecars: the source block gives each folder a start time, a length and a kind.
side() { printf '{"source": {"creation_time": "%s", "duration_seconds": %s, "path": "%s", "kind": %s%s}}\n' "$2" "$3" "$4" "$5" "${6:-}" > "$1"; }
side "$S/transcript.diarization.json" 2026-10-27T16:00:00Z 905 "20261027 090000.m4a" null
side "$P/transcript.diarization.json" 2026-10-28T21:00:00Z 2710 20261028T210000Z-teams.m4a null
side "$C/transcript.diarization.json" 2026-10-29T15:00:00Z null "" '"teams-chat"' ', "chat_name": "Parser fix"'
# Silent audio of the real length so the manifest carries duration and source the way
# an ingested call does (written, never played).
silent() { ffmpeg -nostdin -loglevel error -y -f lavfi -i anullsrc=r=8000:cl=mono -t "$2" -c:a aac -b:a 8k -metadata creation_time="$3" "$1"; }
silent "$S/20261027 090000.m4a" 905 2026-10-27T16:00:00Z
silent "$P/20261028T210000Z-teams.m4a" 2710 2026-10-28T21:00:00Z
cat > "$S/action-items.md" <<'T'
# Action items

## 1. Own commitments
- **Alice_Example:** Send the release notes today.

## 2. Asks from the team
- **Bob_Example:** Review the parser change after lunch.
T
cat > "$S/action-items.json" <<'T'
{"transcript": "transcript.speakers.txt", "engine": "none", "speakers": ["Alice_Example", "Bob_Example", "SPEAKER_02"],
 "items": [{"line": 4, "owner": "Alice_Example", "text": "Send the release notes today.", "section": "Own commitments"},
           {"line": 7, "owner": "Bob_Example", "text": "Review the parser change after lunch.", "section": "Asks from the team"}]}
T
cat > "$S/transcript.speaker-cards.txt" <<'T'
SPEAKER_02 · 1 turn · 0m 05s
  "Is the demo still on Friday?"
T
"$REPO/whosaid" roll-up "$W" --action-items >&2
"$REPO/whosaid" index "$W" --no-embed >&2
echo "$F"
