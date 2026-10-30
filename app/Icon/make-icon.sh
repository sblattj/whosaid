#!/bin/zsh
# Rebuild app/Icon/AppIcon.icns from draw-icon.swift. Needs swift, sips, iconutil (macOS).
set -euo pipefail
here=${0:A:h}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
swift "$here/draw-icon.swift" "$tmp/icon.png" >/dev/null
set=$tmp/AppIcon.iconset
mkdir "$set"
for s in 16 32 128 256 512; do
  sips -z $s $s "$tmp/icon.png" --out "$set/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$tmp/icon.png" --out "$set/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$set" -o "$here/AppIcon.icns"
echo "$here/AppIcon.icns"
