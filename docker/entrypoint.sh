#!/usr/bin/env bash
set -euo pipefail

cd /app

DATABASE_PATH="${SMS_DATABASE_PATH:-/app/data/db.sqlite3}"
DATABASE_DIR="$(dirname "$DATABASE_PATH")"
mkdir -p "$DATABASE_DIR"

mkdir -p \
    data/incoming \
    data/active_final \
    data/old_versions \
    data/reports \
    data/extraction_results \
    data/plagiarism_reports \
    data/media

if [ "${SMS_RUN_MIGRATIONS:-1}" != "0" ]; then
    python manage.py migrate --noinput
fi

exec python manage.py runserver "${DJANGO_HOST:-0.0.0.0}:${DJANGO_PORT:-8000}"
