#!/bin/bash
# Build app/ax-dump.swift once (rebuilt when the source changes) and run it.
# Usage: app/ax-dump.sh <pid> [--text] [...]   (see ax-dump.swift)
HERE=$(cd "$(dirname "$0")" && pwd); BIN="$HERE/.build/ax-dump"
if [[ ! -x "$BIN" || "$HERE/ax-dump.swift" -nt "$BIN" ]]; then
  mkdir -p "$HERE/.build" && swiftc -O -o "$BIN" "$HERE/ax-dump.swift" || exit 1
fi
exec "$BIN" "$@"
