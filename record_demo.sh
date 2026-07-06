#!/usr/bin/env bash
# Record a CodeHydra demo GIF for README / LinkedIn
# Requirements: asciinema, agg  (brew install asciinema agg)
#
# Usage:  bash record_demo.sh
#
# The script starts recording, opens codehydra, and gives you a cue card.
# You type the demo live — keeps it natural. Stop with /exit, GIF auto-builds.

set -e

CAST=assets/demo.cast
GIF=assets/demo.gif
mkdir -p assets

cat <<'CUE'
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CODEHYDRA DEMO — CUE CARD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. codehydra starts — let the header appear (~2s)
  2. Type this prompt and press Enter:
       Explain async/await vs threads in Python. Be concise.
  3. Wait for the full response to stream in
  4. Type:  /status   (shows which CLIs are authenticated)
  5. Press: Ctrl+Y    (copies last response — confirmation appears)
  6. Type:  /exit
  7. Recording stops → GIF builds automatically

  Terminal will resize to 100×32 for the recording.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CUE

echo ""
read -r -p "Ready? Press Enter to start recording (Ctrl+C to abort)…" _

# Resize terminal for consistent GIF output
printf '\e[8;32;100t'
sleep 0.5

echo "▶ Recording started. Follow the cue card above."
echo ""

asciinema rec \
  --overwrite \
  --title "CodeHydra — auto-fallback TUI for Claude/Agy/Codex" \
  --command "codehydra" \
  "$CAST"

echo ""
echo "▶ Building GIF…"

agg \
  --font-size 14 \
  --theme dracula \
  --cols 100 \
  --rows 32 \
  --speed 1.5 \
  "$CAST" "$GIF"

echo ""
echo "✓ Done:  $GIF"
echo ""
echo "Next steps:"
echo "  1. Trim the cast if needed:  asciinema cut --start 2 --end 60 $CAST > assets/demo_trimmed.cast"
echo "  2. Drop the GIF into README: ![demo](assets/demo.gif)"
echo "  3. Upload the GIF directly to LinkedIn post for autoplay"
