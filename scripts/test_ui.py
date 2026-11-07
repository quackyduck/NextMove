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


def run_file(spec, command=None):
    if not (Path(os.environ.get('FRONTEND_DIST', ROOT / 'frontend/dist')) / 'index.html').is_file():
        raise SystemExit('Build frontend before running UI tests')
    with tempfile.TemporaryDirectory(prefix='jobtracker-ui-') as directory:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        environment = os.environ.copy()
        prefix = environment.get('APP_ROOT_PATH', '').rstrip('/')
        environment.update(APP_ENV='development', SECRET_KEY=secrets.token_hex(32),
                           APP_PASSWORD='isolated-browser-test', APP_PASSWORD_HASH='', BOOTSTRAP_ADMIN_EMAIL='',
                           AGENT_API_TOKEN=secrets.token_hex(32), FLASK_DEBUG='0',
                           DATABASE_URL=f'sqlite:///{directory}/test.sqlite3',
                           UPLOAD_FOLDER=f'{directory}/resumes',
                           E2E_BASE_URL=f'http://127.0.0.1:{port}', E2E_PASSWORD='isolated-browser-test', E2E_EMAIL='owner@localhost',
                           E2E_PYTHON=sys.executable,
                           # Email confirmation: messages land as .eml files the specs read (tests/mail.js).
                           MAIL_BACKEND='file', MAIL_FILE_DIR=f'{directory}/mail',
                           # Specs may name a client address with X-Forwarded-For (one per test), so the strict
                           # per-IP sign-up budget (5 per 5 minutes) applies per test, as for real visitors.
                           TRUST_PROXY='1',
                           PUBLIC_BASE_URL=f'http://127.0.0.1:{port}{prefix}')
        # Browser tests never send telemetry. telemetry.spec.js gets a fake project on Sentry's US ingest host
        # (the CSP must name it); the spec intercepts every request to it, so nothing leaves this machine.
        for name in ('SENTRY_DSN', 'SENTRY_DSN_FILE', 'SENTRY_FRONTEND_DSN'):
            environment.pop(name, None)
        if Path(spec).name == 'telemetry.spec.js':
            environment['SENTRY_FRONTEND_DSN'] = 'https://0123456789abcdef0123456789abcdef@o1.ingest.us.sentry.io/1'
        # One invite per suite lets browser tests create a second account.
        invite = subprocess.run([sys.executable, '-m', 'flask', '--app', 'jobtracker:create_app', 'invite', 'create'],
                                cwd=ROOT, env=environment, check=True, capture_output=True, text=True)
        environment['E2E_INVITE_CODE'] = invite.stdout.strip()
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
            return subprocess.run(command or ['npm', 'run', 'test:e2e', '--', spec], cwd=ROOT / 'frontend', env=environment).returncode
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def main():
    # Each browser suite gets separate data and a fresh persistent login budget.
    # Recovery tests deliberately perform multiple real login attempts; sharing
    # a limiter with unrelated suites would make test order cause false failures.
    specs = sys.argv[1:] or [str(path.relative_to(ROOT / 'frontend'))
                            for path in sorted((ROOT / 'frontend/tests').glob('*.spec.js'))]
    for spec in specs:
        result = run_file(spec)
        if result:
            return result
    return 0


if __name__ == '__main__':
    sys.exit(main())
