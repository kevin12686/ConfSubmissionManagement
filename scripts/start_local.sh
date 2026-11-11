#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${DJANGO_HOST:-127.0.0.1}"
PORT="${DJANGO_PORT:-8000}"
SERVER_URL="http://${HOST}:${PORT}/"

if [ ! -e ".venv" ] && [ ! -L ".venv" ]; then
    if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
        echo "Python 3.12 or newer was not found. Install it or set PYTHON_BIN to its executable."
        exit 1
    fi
    if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
        echo "Python 3.12 or newer is required. Set PYTHON_BIN to a supported interpreter and retry."
        exit 1
    fi
    echo "Creating local virtual environment..."
    if ! "$PYTHON_BIN" -m venv .venv; then
        echo "Could not create .venv. Check Python venv support; rename any incomplete .venv before retrying."
        exit 1
    fi
fi

PYTHON="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) and sys.prefix != sys.base_prefix else 1)' \
    || ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "The existing .venv is incomplete, broken, or does not use Python 3.12 or newer."
    echo "Rename only .venv, then rerun with a supported PYTHON_BIN. Do not remove conference data."
    exit 1
fi

echo "Installing/updating Python packages..."
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements.txt
"$PYTHON" -m pip check

if [ ! -f scripts/check_comparison_runtime.py ]; then
    echo "Startup checker is missing. Update or restore the complete project checkout."
    exit 1
fi
echo "Checking comparison runtime..."
"$PYTHON" scripts/check_comparison_runtime.py

echo "Running Django system checks..."
"$PYTHON" manage.py check

echo "Preparing local data folders..."
mkdir -p \
    data/reports \
    data/extraction_results \
    data/plagiarism_reports \
    data/media

echo "Applying database migrations..."
"$PYTHON" manage.py migrate

echo
echo "Conference Final Manager is starting."
echo "Open: ${SERVER_URL}"
echo "Press Ctrl+C in this window to stop the server."
echo

if [ "$(uname -s)" = "Darwin" ] && [ "${OPEN_BROWSER:-1}" != "0" ]; then
    (sleep 2; open "${SERVER_URL}") >/dev/null 2>&1 &
fi

exec "$PYTHON" manage.py runserver "${HOST}:${PORT}"
