#!/bin/sh
# Builds the offline wordgraph bundle the app ships. Usage: app/wordgraph/build.sh [bun]
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$(cd "$HERE/.." && pwd)/Sources/WhosaidApp/Resources/wordgraph"
BUN=${1:-${BUN:-bun}}
mkdir -p "$OUT"
"$BUN" build "$HERE/src/index.ts" --minify --target=browser --format=iife --outfile "$OUT/index.js"
cp "$HERE/index.html" "$OUT/index.html"
cp "$HERE/src/wordgraph.css" "$OUT/wordgraph.css"
echo "wrote $OUT"
