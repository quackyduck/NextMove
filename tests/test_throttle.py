"""Security behavior across independent application instances and concurrent calls."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from werkzeug.security import generate_password_hash

from jobtracker import create_app, throttle


@pytest.fixture
def build(tmp_path):
    config = {
        'TESTING': True, 'APP_ENV': 'production', 'SECRET_KEY': 's' * 40,
        'APP_PASSWORD_HASH': generate_password_hash('test-password'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "jobs.sqlite3"}',
        'UPLOAD_FOLDER': str(tmp_path / 'resumes'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'),
    }
    return lambda: create_app(config)


def test_budget_survives_restart_and_ignores_forged_ip(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    first = build().test_client()
    for _ in range(9):
        assert first.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'wrong'}).status_code == 401
    second = build().test_client()
    assert second.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'}).status_code == 200
    denied = first.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'},
                        headers={'X-Forwarded-For': '203.0.113.123'})
    assert denied.status_code == 429
    assert denied.headers['Retry-After'] == '300'
    monkeypatch.setattr(throttle.time, 'time', lambda: 1299)
    assert second.post('/api/v1/auth/login', json={}).status_code == 429
    monkeypatch.setattr(throttle.time, 'time', lambda: 1300)
    assert second.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'}).status_code == 200


def test_reservations_are_atomic_across_instances(build):
    apps = [build(), build()]

    def attempt(index):
        with apps[index % 2].app_context():
            return throttle.reserve_attempt()

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(attempt, range(24)))
    assert sum(outcomes) == 10
    with sqlite3.connect(apps[0].extensions['login_throttle_path']) as connection:
        assert connection.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 10


def test_storage_failure_denies_login_without_details(build, monkeypatch):
    app = build()

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('private database path')

    monkeypatch.setattr(throttle.sqlite3, 'connect', unavailable)
    response = app.test_client().post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'})
    assert response.status_code == 503
    assert b'private database path' not in response.data
    assert response.headers['Cache-Control'] == 'no-store'


def test_cross_origin_does_not_consume_budget_and_clock_rollback_preserves_it(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    app = build()
    client = app.test_client()
    for _ in range(12):
        assert client.post('/api/v1/auth/login', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
    with app.app_context():
        assert all(throttle.reserve_attempt() for _ in range(10))
        monkeypatch.setattr(throttle.time, 'time', lambda: 900)
        assert not throttle.reserve_attempt()
