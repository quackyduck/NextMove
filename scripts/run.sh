#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f frontend/dist/index.html ]]; then
  echo 'Build the frontend first: cd frontend && npm ci && npm run build' >&2
  exit 1
fi
export APP_ENV=development
export FLASK_DEBUG=0
# The queue worker reads posting URLs; it stops with this script.
.venv/bin/python -m jobtracker.worker &
worker=$!
trap 'kill "$worker" 2>/dev/null; wait "$worker" 2>/dev/null' EXIT
.venv/bin/python -m flask --app jobtracker:create_app run --host 127.0.0.1 --port 5001 --no-debugger --no-reload
