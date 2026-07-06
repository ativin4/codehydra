#!/usr/bin/env bash
# Record the scripted CodeHydra demo — fully automated, no live API calls.
# Requirements: asciinema, agg, ffmpeg  (brew install asciinema agg ffmpeg)
#
# Usage:  bash record_demo.sh
#
# demo_driver.py spawns codehydra in a throwaway git repo with the canned
# DemoGateway script (assets/demo_script.json) and feeds a choreographed
# keystroke sequence showcasing: slash autocomplete, thinking spinner,
# per-file diff view, mid-stream fallback recovery, and /cost.
#
# Outputs: assets/demo.gif (README) and assets/demo_v2.mp4 (posts).

set -e
cd "$(dirname "$0")"

CAST=assets/demo.cast
GIF=assets/demo.gif
MP4=assets/demo_v2.mp4
PY=.venv/bin/python
mkdir -p assets

echo "▶ Recording scripted demo (~50s)…"
asciinema rec \
  --overwrite \
  --cols 100 --rows 32 \
  --title "CodeHydra — one TUI for Claude, Gemini & Codex with auto-fallback" \
  --command "$PY demo_driver.py" \
  "$CAST"

echo "▶ Building GIF…"
agg \
  --font-size 14 \
  --theme dracula \
  --cols 100 \
  --rows 32 \
  "$CAST" "$GIF"

echo "▶ Building MP4…"
ffmpeg -y -loglevel error -i "$GIF" \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \
  -c:v libx264 -preset slow -crf 18 \
  -movflags +faststart -pix_fmt yuv420p \
  "$MP4"

echo ""
echo "✓ $GIF"
echo "✓ $MP4"
