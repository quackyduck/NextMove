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
    assert second.post('/api/v1/auth/login', json={'email': ' OWNER@localhost '}).status_code == 429
    monkeypatch.setattr(throttle.time, 'time', lambda: 1300)
    assert second.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'}).status_code == 200


def test_reservations_are_atomic_across_instances(build):
    apps = [build(), build()]

    def attempt(index):
        with apps[index % 2].test_request_context():
            return throttle.reserve_attempt('owner@localhost')

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(attempt, range(24)))
    assert sum(outcomes) == 10
    with sqlite3.connect(apps[0].extensions['login_throttle_path']) as connection:
        assert connection.execute("SELECT COUNT(*) FROM keyed_attempts WHERE key LIKE 'account:%'").fetchone()[0] == 10
        # Only keyed HMACs persist: never the email or client address.
        stored = ' '.join(row[0] for row in connection.execute('SELECT key FROM keyed_attempts'))
        assert 'owner' not in stored and '127.0.0.1' not in stored


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
    with app.test_request_context():
        assert all(throttle.reserve_attempt('owner@localhost') for _ in range(10))
        monkeypatch.setattr(throttle.time, 'time', lambda: 900)
        assert not throttle.reserve_attempt('owner@localhost')


def login(client, email, password='wrong-password', address='198.51.100.1'):
    return client.post('/api/v1/auth/login', json={'email': email, 'password': password},
                       environ_base={'REMOTE_ADDR': address}).status_code


def test_account_budget_is_per_account_and_ignores_client_address(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    client = build().test_client()
    for index in range(10):
        assert login(client, 'owner@localhost', address=f'198.51.100.{index}') == 401
    # A distributed guesser is still stopped at the account budget...
    assert login(client, 'owner@localhost', 'test-password', address='203.0.113.9') == 429
    # ...while other accounts (including unknown ones, so 429 does not reveal
    # existence) keep their own budgets.
    assert login(client, 'someone@example.com', address='203.0.113.9') == 401
    for _ in range(9):
        assert login(client, 'nobody@example.com', address='203.0.113.10') == 401
    assert login(client, 'nobody@example.com', address='203.0.113.10') == 401
    assert login(client, 'nobody@example.com', address='203.0.113.10') == 429


def test_ip_budget_limits_spraying_across_accounts(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    client = build().test_client()
    for index in range(throttle.MAX_IP_ATTEMPTS):
        assert login(client, f'user{index}@example.com') == 401
    assert login(client, 'fresh@example.com') == 429
    # The owner, from a different address, is unaffected by that client.
    assert login(client, 'owner@localhost', 'test-password', address='192.0.2.50') == 200


def test_global_budget_caps_total_password_hashing(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    monkeypatch.setattr(throttle, 'MAX_GLOBAL_ATTEMPTS', 3)
    client = build().test_client()
    for index in range(3):
        assert login(client, f'user{index}@example.com', address=f'192.0.2.{index}') == 401
    assert login(client, 'owner@localhost', 'test-password', address='192.0.2.99') == 429


def test_trusted_proxy_uses_forwarded_client_address(build, monkeypatch, tmp_path):
    from jobtracker import create_app
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    app = build()
    proxied = create_app({**app.config, 'TRUST_PROXY': True}).test_client()
    for index in range(throttle.MAX_IP_ATTEMPTS):
        response = proxied.post('/api/v1/auth/login', json={'email': f'u{index}@example.com', 'password': 'x'},
                                headers={'X-Forwarded-For': '203.0.113.7'})
        assert response.status_code == 401
    blocked = proxied.post('/api/v1/auth/login', json={'email': 'z@example.com', 'password': 'x'},
                           headers={'X-Forwarded-For': '203.0.113.7'})
    assert blocked.status_code == 429
    other = proxied.post('/api/v1/auth/login', json={'email': 'z@example.com', 'password': 'x'},
                         headers={'X-Forwarded-For': '203.0.113.8'})
    assert other.status_code == 401
