#!/usr/bin/env bash
# Build the standalone macOS app (.app) for AutoTyper.
# Run from the client/ directory.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# Use the project venv if present, else the current python.
PY="python3"
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
fi

echo "==> Installing build deps"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt pyinstaller

echo "==> Cleaning previous build"
rm -rf build dist

echo "==> Building AutoTyper.app"
"$PY" -m PyInstaller autotyper.spec --noconfirm

echo "==> Done. App is at: dist/AutoTyper.app"
echo "    First launch: grant Accessibility permission in"
echo "    System Settings > Privacy & Security > Accessibility."
