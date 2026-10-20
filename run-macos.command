#!/bin/bash
set -e
cd "$(dirname "$0")"

APP_URL="http://127.0.0.1:8765"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_PY="$PWD/.venv/bin/python"

fail() {
  echo "MIDI-MP4: $1" >&2
  echo "Press Enter to close this window."
  read -r _ || true
  exit 1
}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python 3.11+ is required."
command -v npm >/dev/null 2>&1 || fail "Node.js/npm is required for the local score renderer."

if curl --silent --fail "$APP_URL/api/health" >/dev/null 2>&1; then
  open "$APP_URL"
  echo "MIDI-MP4 is already running at $APP_URL"
  exit 0
fi

if [ ! -x "$VENV_PY" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

if ! "$VENV_PY" -c 'import fastapi, uvicorn, music21, mido, numpy, verovio, cairosvg, PIL, playwright, httpx' >/dev/null 2>&1; then
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -r requirements.txt || fail "Python dependency installation failed."
fi

if [ ! -f node_modules/opensheetmusicdisplay/build/opensheetmusicdisplay.min.js ] && [ ! -f node_modules/opensheetmusicdisplay/build/opensheetmusicdisplay.js ]; then
  npm install --no-audit --no-fund || fail "npm dependency installation failed."
fi

if ! "$VENV_PY" -c 'from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); ok=Path(p.chromium.executable_path).exists(); p.stop(); raise SystemExit(0 if ok else 1)' >/dev/null 2>&1; then
  "$VENV_PY" -m playwright install chromium || fail "Could not install Playwright Chromium."
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "WARNING: FFmpeg is missing. Video export will be unavailable; MIDI and preview may still work."
fi

open "$APP_URL" >/dev/null 2>&1 || true
exec "$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8765
