#!/bin/sh
# Dependencies are installed at container start (as required), then the API
# is launched. The image itself only carries a base Python interpreter.
set -e

echo "[entrypoint] Installing dependencies from requirements.txt ..."
pip install --no-cache-dir -r /app/requirements.txt

echo "[entrypoint] Starting uvicorn on ${HOST:-0.0.0.0}:${PORT:-8000} ..."
exec uvicorn app.main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --log-level "${LOG_LEVEL:-info}"
