#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${DJANGO_HOST:-127.0.0.1}"
PORT="${DJANGO_PORT:-8000}"
SERVER_URL="http://${HOST}:${PORT}/"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Python 3 was not found. Install Python 3 first, then run this script again."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating local virtual environment..."
    "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate

echo "Installing/updating Python packages..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Preparing local data folders..."
mkdir -p \
    data/incoming \
    data/active_final \
    data/old_versions \
    data/reports \
    data/extraction_results \
    data/plagiarism_reports \
    data/media

echo "Applying database migrations..."
python manage.py migrate

echo
echo "Conference Final Manager is starting."
echo "Open: ${SERVER_URL}"
echo "Press Ctrl+C in this window to stop the server."
echo

if [ "$(uname -s)" = "Darwin" ] && [ "${OPEN_BROWSER:-1}" != "0" ]; then
    (sleep 2; open "${SERVER_URL}") >/dev/null 2>&1 &
fi

python manage.py runserver "${HOST}:${PORT}"
