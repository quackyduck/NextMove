#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
if [[ ! -e .env ]]; then
  .venv/bin/python - <<'PY'
import os
import secrets
from pathlib import Path
contents = Path('.env.example').read_text().replace('replace-with-a-random-secret', secrets.token_hex(32))
with os.fdopen(os.open('.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
    output.write(contents)
PY
fi
cd frontend
npm ci
npm run build
