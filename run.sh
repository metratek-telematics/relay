#!/usr/bin/env bash
# Relay launcher for Linux and macOS.
# Creates a local virtualenv on first run, checks the tools Relay drives, then starts the server.
set -euo pipefail

cd "$(dirname "$0")"

echo
echo "  Relay  -  your coding agents, working as a team"
echo

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.10+ was not found. Install it, for example: sudo apt install python3 python3-venv" >&2
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Relay needs Python 3.10 or newer; found $("$PY" --version 2>&1)." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import flask, waitress' >/dev/null 2>&1; then
  echo "Creating .venv and installing dependencies..."
  rm -rf .venv
  if ! "$PY" -m venv .venv >/dev/null 2>&1; then
    ver="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    echo "Could not create a virtual environment. On Ubuntu and Debian install the venv module:" >&2
    echo "  sudo apt install python3-venv    (or python${ver}-venv)" >&2
    rm -rf .venv
    exit 1
  fi
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi

command -v git >/dev/null 2>&1 || { echo "git is required: sudo apt install git" >&2; exit 1; }

found=0
for cli in codex claude gemini; do
  if command -v "$cli" >/dev/null 2>&1; then
    found=$((found + 1))
  else
    echo "  note: '$cli' is not on PATH, so it cannot be assigned a role."
  fi
done
if [ "$found" -lt 2 ]; then
  echo "  warning: Relay needs at least two of codex, claude and gemini installed and signed in." >&2
fi

host="${RELAY_HOST:-127.0.0.1}"
echo "Open: http://127.0.0.1:${RELAY_PORT:-8767}"
if [ "$host" != "127.0.0.1" ] && [ "$host" != "localhost" ]; then
  echo "  warning: RELAY_HOST=$host exposes Relay beyond this machine, and Relay has no login." >&2
fi
echo

export PYTHONUNBUFFERED=1
exec .venv/bin/python web_app.py --no-browser "$@"
