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
            return throttle.reserve_login('owner@localhost')

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
        assert all(throttle.reserve_login('owner@localhost') for _ in range(10))
        monkeypatch.setattr(throttle.time, 'time', lambda: 900)
        assert not throttle.reserve_login('owner@localhost')


def login(client, email, password='wrong-password', address='198.51.100.1'):
    return client.post('/api/v1/auth/login', json={'email': email, 'password': password},
                       environ_base={'REMOTE_ADDR': address}).status_code


def test_account_budget_stops_one_attacker_but_not_the_owner(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    client = build().test_client()
    for _ in range(throttle.MAX_ATTEMPTS):
        assert login(client, 'owner@localhost', address='198.51.100.1') == 401
    # The attacker's address is stopped for this account...
    assert login(client, 'owner@localhost', 'test-password', address='198.51.100.1') == 429
    # ...but the owner, from their own network, can still sign in.
    assert login(client, 'owner@localhost', 'test-password', address='203.0.113.9') == 200
    # Other accounts, known or unknown (429 never reveals existence), keep their budgets.
    assert login(client, 'someone@example.com', address='203.0.113.9') == 401
    for _ in range(throttle.MAX_ATTEMPTS):
        assert login(client, 'nobody@example.com', address='203.0.113.10') == 401
    assert login(client, 'nobody@example.com', address='203.0.113.10') == 429


def test_distributed_guessing_is_capped_per_account(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    client = build().test_client()
    attempts = 0
    for address in range(100):
        for _ in range(throttle.CLEAN_CLIENT_TRIES):
            if attempts < throttle.MAX_CLEAN_CLIENT_ATTEMPTS:
                assert login(client, 'owner@localhost', address=f'10.0.{address // 250}.{address % 250}') == 401
                attempts += 1
    # Every further address is refused until the window passes, even with the right password.
    assert login(client, 'owner@localhost', 'test-password', address='192.0.2.200') == 429
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000 + throttle.WINDOW_SECONDS)
    assert login(client, 'owner@localhost', 'test-password', address='192.0.2.200') == 200


def test_ip_budget_limits_spraying_across_accounts(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    client = build().test_client()
    for index in range(throttle.MAX_IP_ATTEMPTS):
        assert login(client, f'user{index}@example.com') == 401
    assert login(client, 'fresh@example.com') == 429
    # The owner, from a different address, is unaffected by that client.
    assert login(client, 'owner@localhost', 'test-password', address='192.0.2.50') == 200


def test_no_global_budget_many_addresses_cannot_lock_everyone_out(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    client = build().test_client()
    # Seven exhausted addresses would have spent the old global budget of 200.
    monkeypatch.setattr(throttle, 'MAX_IP_ATTEMPTS', 3)
    for address in range(70):
        for index in range(throttle.MAX_IP_ATTEMPTS):
            login(client, f'spray{index}@example.com', address=f'10.1.0.{address}')
    assert login(client, 'owner@localhost', 'test-password', address='192.0.2.99') == 200


def test_login_abuse_does_not_block_the_signed_in_owner(build, monkeypatch):
    monkeypatch.setattr(throttle.time, 'time', lambda: 1000)
    client = build().test_client()
    owner = client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'},
                        environ_base={'REMOTE_ADDR': '192.0.2.1'})
    headers = {'X-CSRF-Token': owner.json['csrf_token']}
    for address in range(20):
        for _ in range(throttle.CLEAN_CLIENT_TRIES):
            login(client.application.test_client(), 'owner@localhost', address=f'10.2.0.{address}')
    changed = client.post('/api/v1/auth/password', base_url='https://localhost', headers=headers,
                          environ_base={'REMOTE_ADDR': '192.0.2.1'},
                          json={'current_password': 'test-password', 'new_password': 'changed-password',
                                'confirm_password': 'changed-password'})
    assert changed.status_code == 200


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


# Schema 15: per-budget windows, /64 grouping, canonical address keys ------------------
def test_long_budgets_survive_the_short_prune_and_short_ones_still_expire(build, monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr(throttle.time, 'time', lambda: clock['now'])
    app = build()
    with app.test_request_context('/'):
        assert throttle.reserve([('day-key', 1, throttle.DAY_SECONDS)])
        assert throttle.reserve([('short-key', 1)])
        clock['now'] += throttle.WINDOW_SECONDS + 1
        assert throttle.reserve([('short-key', 1)])  # 5-minute budget freed as before.
        assert not throttle.reserve([('day-key', 1, throttle.DAY_SECONDS)])  # Not pruned at 300 s.
        clock['now'] += throttle.DAY_SECONDS
        assert throttle.reserve([('day-key', 1, throttle.DAY_SECONDS)])


def test_mixed_window_reservations_are_all_or_nothing(build):
    app = build()
    with app.test_request_context('/'):
        assert throttle.reserve([('a', 1, 60), ('b', 5, throttle.DAY_SECONDS)])
        assert not throttle.reserve([('a', 1, 60), ('b', 5, throttle.DAY_SECONDS)])
        assert throttle.used('b', throttle.DAY_SECONDS) == 1
        assert throttle.remaining([('b', 2, throttle.DAY_SECONDS)])
        assert throttle.used('b', throttle.DAY_SECONDS) == 1  # remaining() never records.
        with pytest.raises(ValueError):
            throttle.reserve([('c', 1, throttle.MAX_WINDOW_SECONDS + 1)])


@pytest.mark.parametrize('address, grouped', [
    ('2001:db8:1:2:3:4:5:6', '2001:db8:1:2::/64'), ('2001:db8:1:2::ffff', '2001:db8:1:2::/64'),
    ('::ffff:192.0.2.7', '192.0.2.7'), ('192.0.2.7', '192.0.2.7'), ('not-an-ip', 'raw:not-an-ip'), (None, 'raw:none'),
    ('', 'raw:none'), ('x' * 100, 'raw:' + 'x' * 64),
    ('fe80::1%eth0', 'fe80::/64')])
def test_client_addresses_group_ipv6_by_64(address, grouped):
    assert throttle.group_address(address) == grouped


def test_one_ipv6_prefix_is_one_client(build):
    client = build().test_client()
    for index in range(throttle.MAX_IP_ATTEMPTS):
        assert login(client, f'user{index}@example.com', address=f'2001:db8:0:1::{index + 1:x}') == 401
    assert login(client, 'fresh@example.com', address='2001:db8:0:1:ffff::1') == 429
    assert login(client, 'owner@localhost', 'test-password', address='2001:db8:0:2::1') == 200


@pytest.mark.parametrize('first, second, same', [
    ('a.b+x@googlemail.com', 'ab@gmail.com', True), ('a+x@example.com', 'a@example.com', True),
    ('A.B@Example.com', 'a.b@example.com', True), ('a.b@example.com', 'ab@example.com', False),
    ('a+x+y@gmail.com', 'a@gmail.com', True)])
def test_address_keys_canonicalize_for_throttling_only(first, second, same):
    assert (throttle.address_key(first) == throttle.address_key(second)) is same


def test_per_address_budget_has_a_clean_client_allowance(build):
    app = build()

    def attempt(address, email='victim@example.com'):
        with app.test_request_context('/', environ_base={'REMOTE_ADDR': address}):
            return throttle.reserve_per_address('kind', email)
    # One client spends the shared 3 (tags and Gmail-style variants count as the same address)...
    assert attempt('198.51.100.1', 'victim+1@example.com') and attempt('198.51.100.1', 'victim+2@example.com')
    assert attempt('198.51.100.1')
    assert not attempt('198.51.100.1')
    # ...but a client with fewer than 2 tries for that address still gets through, up to 6 in total.
    assert attempt('198.51.100.2') and attempt('198.51.100.2') and attempt('198.51.100.3')
    assert not attempt('198.51.100.4')


def test_mail_trigger_budget_is_five_per_five_minutes_and_eight_per_day(build, monkeypatch):
    clock = {'now': 1000.0}
    monkeypatch.setattr(throttle.time, 'time', lambda: clock['now'])
    app = build()
    with app.test_request_context('/', environ_base={'REMOTE_ADDR': '203.0.113.5'}):
        assert all(throttle.reserve_mail_trigger() for _ in range(5))
        assert not throttle.reserve_mail_trigger()
        clock['now'] += throttle.WINDOW_SECONDS + 1
        assert all(throttle.reserve_mail_trigger() for _ in range(3))
        assert not throttle.reserve_mail_trigger()  # 8 for the day.
