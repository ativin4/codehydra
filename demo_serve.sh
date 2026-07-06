#!/usr/bin/env bash
# textual-serve command for the scripted browser demo (used by make_demo.py).
# Recreates a throwaway git repo and launches codehydra in DemoGateway mode,
# so every "Restart" in the browser starts from a clean, deterministic state.
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
DEMO_DIR="${CODEHYDRA_DEMO_DIR:-/tmp/codehydra_demo_repo}"

rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR"
cd "$DEMO_DIR"
git init -q
printf '.codehydra/\n' > .gitignore
git add .gitignore
git commit -qm init

export CODEHYDRA_DEMO_SCRIPT="$REPO/assets/demo_script.json"
export CODEHYDRA_ENABLE_SUBAGENTS=0

exec "$REPO/.venv/bin/codehydra"
