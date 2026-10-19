#!/usr/bin/env python3
"""Run browser tests against disposable storage; personal .env is never used for data."""
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    if not (Path(os.environ.get('FRONTEND_DIST', ROOT / 'frontend/dist')) / 'index.html').is_file():
        raise SystemExit('Build frontend before running UI tests')
    with tempfile.TemporaryDirectory(prefix='jobtracker-ui-') as directory:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        environment = os.environ.copy()
        prefix = environment.get('APP_ROOT_PATH', '').rstrip('/')
        environment.update(APP_ENV='development', SECRET_KEY=secrets.token_hex(32),
                           APP_PASSWORD='isolated-browser-test', APP_PASSWORD_HASH='',
                           AGENT_API_TOKEN=secrets.token_hex(32), FLASK_DEBUG='0',
                           DATABASE_URL=f'sqlite:///{directory}/test.sqlite3',
                           UPLOAD_FOLDER=f'{directory}/resumes',
                           E2E_BASE_URL=f'http://127.0.0.1:{port}', E2E_PASSWORD='isolated-browser-test')
        server = subprocess.Popen([sys.executable, '-m', 'flask', '--app', 'jobtracker:create_app',
                                   'run', '--host', '127.0.0.1', '--port', str(port),
                                   '--no-debugger', '--no-reload'], cwd=ROOT, env=environment)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError('UI test server exited before readiness')
                try:
                    with urllib.request.urlopen(environment['E2E_BASE_URL'] + prefix + '/health', timeout=1) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            else:
                raise RuntimeError('UI test server did not become healthy')
            return subprocess.run(['npm', 'run', 'test:e2e'], cwd=ROOT / 'frontend', env=environment).returncode
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == '__main__':
    sys.exit(main())
