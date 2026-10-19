#!/usr/bin/env python3
"""Isolated production-Gunicorn smoke: no personal data or production secrets.

Emulates the one trusted HTTPS proxy on loopback. This does not verify real TLS.
Build the frontend with NEXT_MOVE_BASE_PATH=/nextmove/ before running.
"""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parent.parent


def main():
    with tempfile.TemporaryDirectory(prefix='nextmove-production-test-') as directory:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        environment = os.environ.copy()
        environment.update(
            APP_ENV='production', SECRET_KEY='disposable-test-secret-' + 'x' * 40,
            APP_PASSWORD_HASH=generate_password_hash('disposable-test-password'),
            APP_PASSWORD='', AGENT_API_TOKEN='', APP_ROOT_PATH='/nextmove',
            APP_ALLOWED_HOSTS='nicolasmelo.com,127.0.0.1', TRUST_PROXY='1',
            DATABASE_URL=f'sqlite:///{directory}/jobs.sqlite3',
            UPLOAD_FOLDER=f'{directory}/resumes',
            LOGIN_THROTTLE_DATABASE=f'{directory}/login-throttle.sqlite3',
            GUNICORN_BIND=f'127.0.0.1:{port}',
        )

        def request(path, payload=None, headers=None, method=None):
            connection = HTTPConnection('127.0.0.1', port, timeout=5)
            supplied = {'Host': 'nicolasmelo.com', 'X-Forwarded-Proto': 'https',
                        'Origin': 'https://nicolasmelo.com'}
            if headers:
                supplied.update(headers)
            body = json.dumps(payload) if payload is not None else None
            if body:
                supplied['Content-Type'] = 'application/json'
            try:
                connection.request(method or ('POST' if body else 'GET'), path, body, supplied)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        def start():
            process = subprocess.Popen(
                [str(ROOT / '.venv/bin/gunicorn'), '--config', 'deploy/gunicorn.conf.py',
                 'jobtracker:create_app()'], cwd=ROOT, env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('Isolated Gunicorn exited before readiness')
                    try:
                        if request('/nextmove/health')[0] == 200:
                            return process
                    except OSError:
                        pass
                    time.sleep(0.1)
                raise RuntimeError('Isolated Gunicorn readiness timed out')
            except BaseException:
                stop(process)
                raise

        def stop(process):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        process = start()
        try:
            assert request('/nextmove?from=home')[0] == 308
            assert request('/nextmove?from=home')[1]['Location'] == '/nextmove/?from=home'
            assert request('/health')[0] == 404
            assert request('/nextmove/health', headers={'Host': 'evil.example'})[0] == 400
            status, _, html = request('/nextmove/')
            assert status == 200 and b'/nextmove/assets/' in html
            assert request('/nextmove/api/v1/jobs')[0] == 401
            assert request('/nextmove/api/v1/auth/login', {'password': 'disposable-test-password'},
                           {'Origin': 'https://evil.example'})[0] == 403
            status, headers, data = request('/nextmove/api/v1/auth/login', {'password': 'disposable-test-password'})
            assert status == 200
            cookie = headers['Set-Cookie']
            assert all(part in cookie for part in ('Secure;', 'HttpOnly;', 'SameSite=Lax', 'Path=/nextmove/'))
            authenticated = {'Cookie': cookie.split(';', 1)[0], 'X-CSRF-Token': json.loads(data)['csrf_token']}
            assert request('/nextmove/api/v1/jobs', {'title': 'Disposable production test'}, authenticated)[0] == 201
            for _ in range(9):
                assert request('/nextmove/api/v1/auth/login', {'password': 'wrong'})[0] == 401
            assert request('/nextmove/api/v1/auth/login', {'password': 'disposable-test-password'})[0] == 429
            stop(process)
            process = start()
            assert request('/nextmove/api/v1/auth/login', {'password': 'disposable-test-password'})[0] == 429
            status, _, data = request('/nextmove/api/v1/jobs', headers=authenticated)
            assert status == 200 and json.loads(data)['jobs'][0]['title'] == 'Disposable production test'
            print('Production Gunicorn smoke passed: prefix/assets, host/origin, Secure scoped cookie, authenticated write, restart persistence, persistent throttle.')
        finally:
            stop(process)
    return 0


if __name__ == '__main__':
    sys.exit(main())
