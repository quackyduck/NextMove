#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
cd frontend
npm run build
# Browser tests require the isolated test server documented in README.
if [[ "${1:-}" == '--ui' ]]; then
  cd ..
  .venv/bin/python scripts/test_ui.py
fi
