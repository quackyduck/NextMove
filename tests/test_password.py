"""Password persistence, revocation, browser-only mutation, and races."""
import sqlite3
from pathlib import Path

import pytest
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import text

from jobtracker import create_app, credentials, db
from jobtracker import auth as auth_module


@pytest.fixture
def config(tmp_path):
    return {'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'password-test-secret',
            'APP_PASSWORD_HASH': generate_password_hash('original-password'),
            'DATABASE_URL': f'sqlite:///{tmp_path / "jobs.sqlite3"}',
            'UPLOAD_FOLDER': str(tmp_path / 'resumes'), 'AGENT_API_TOKEN': 'agent-token',
            'SESSION_COOKIE_SECURE': False}


@pytest.fixture
def app(config):
    return create_app(config)


def login(client, password='original-password'):
    response = client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': password})
    assert response.status_code == 200
    return {'X-CSRF-Token': response.json['csrf_token']}


def payload(**changes):
    return {'current_password': 'original-password', 'new_password': 'updated-password',
            'confirm_password': 'updated-password', **changes}


def test_change_persists_revokes_other_and_copied_sessions_and_rotates_csrf(app, config):
    first, second, copied = (app.test_client() for _ in range(3))
    headers = login(first)
    login(second)
    cookie_name = app.config['SESSION_COOKIE_NAME']
    copied.set_cookie(cookie_name, first.get_cookie(cookie_name).value)
    response = first.post('/api/v1/auth/password', json=payload(), headers=headers)
    assert response.status_code == 200
    assert response.json['csrf_token'] != headers['X-CSRF-Token']
    assert 'password_hash' not in response.json and 'session_version' not in response.json
    assert first.get('/api/v1/jobs').status_code == 200
    assert second.get('/api/v1/jobs').status_code == 401
    assert copied.get('/api/v1/auth/session').json['authenticated'] is False
    assert first.post('/api/v1/jobs', json={'title': 'Engineer'}, headers=headers).status_code == 403
    new_headers = {'X-CSRF-Token': response.json['csrf_token']}
    assert first.post('/api/v1/jobs', json={'title': 'Engineer'}, headers=new_headers).status_code == 201
    with app.app_context():
        record = credentials.read(1)
        assert check_password_hash(record.password_hash, 'updated-password')
        assert record.session_version == 2
        assert record.password_hash != 'updated-password'
    # A restart with the original env bootstrap hash must not undo the change.
    fresh = create_app(config).test_client()
    assert fresh.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'original-password'}).status_code == 401
    login(fresh, 'updated-password')
    assert fresh.get('/api/v1/jobs').json['jobs'][0]['title'] == 'Engineer'
    assert Path(config['DATABASE_URL'].removeprefix('sqlite:///')).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('changes,message', [
    ({'current_password': 'wrong'}, 'current password'),
    ({'new_password': 'short', 'confirm_password': 'short'}, '8 and 128'),
    ({'new_password': 'x'*129, 'confirm_password': 'x'*129}, '8 and 128'),
    ({'confirm_password': 'different-password'}, 'do not match'),
    ({'new_password': None}, '8 and 128'),
    ({'current_password': []}, 'current password'),
])
def test_invalid_change_preserves_credential_and_session(app, changes, message):
    client = app.test_client()
    headers = login(client)
    response = client.post('/api/v1/auth/password', json=payload(**changes), headers=headers)
    assert response.status_code == 400
    assert message in response.json['error']
    assert client.get('/api/v1/jobs').status_code == 200
    with app.app_context():
        assert credentials.read(1).session_version == 1
        assert check_password_hash(credentials.read(1).password_hash, 'original-password')


def test_change_requires_browser_session_and_csrf(app):
    client = app.test_client()
    assert client.post('/api/v1/auth/password', json=payload()).status_code == 401
    assert client.post('/api/v1/auth/password', json=payload(), headers={'Authorization': 'Bearer agent-token'}).status_code == 403
    headers = login(client)
    assert client.post('/api/v1/auth/password', json=payload()).status_code == 403
    assert client.post('/api/v1/auth/password', json=[], headers=headers).status_code == 400
    assert client.post('/api/v1/auth/password', json={**payload(), 'extra': True}, headers=headers).status_code == 400


def test_current_password_verifications_share_persistent_throttle(app):
    client = app.test_client()
    headers = login(client)
    for _ in range(10):
        assert client.post('/api/v1/auth/password', json=payload(current_password='wrong'), headers=headers).status_code == 400
    response = client.post('/api/v1/auth/password', json=payload(), headers=headers)
    assert response.status_code == 429
    assert 'Retry-After' in response.headers
    with app.app_context(): assert credentials.read(1).session_version == 1


def test_credential_read_failure_fails_closed(app, monkeypatch):
    client = app.test_client()
    headers = login(client)
    def fail(*_args): raise credentials.Unavailable()
    monkeypatch.setattr(credentials, 'read', fail)
    monkeypatch.setattr(credentials, 'find', fail)
    assert client.get('/api/v1/jobs').status_code == 503
    assert client.get('/api/v1/auth/session').status_code == 503
    assert client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'original-password'}).status_code == 503
    assert client.post('/api/v1/auth/password', json=payload(), headers=headers).status_code == 503


def test_concurrent_change_cannot_be_overwritten_by_stale_hash_verification(app, monkeypatch):
    client = app.test_client()
    headers = login(client)
    original_hash = generate_password_hash
    def concurrent_hash(value):
        with app.app_context():
            assert credentials.replace(credentials.read(1), original_hash('concurrent-password'))
        return original_hash(value)
    monkeypatch.setattr(auth_module, 'generate_password_hash', concurrent_hash)
    response = client.post('/api/v1/auth/password', json=payload(), headers=headers)
    assert response.status_code == 409
    assert client.get('/api/v1/jobs').status_code == 401
    with app.app_context(): assert check_password_hash(credentials.read(1).password_hash, 'concurrent-password')


def test_login_observing_old_hash_never_receives_new_session_generation(app, monkeypatch):
    client = app.test_client()
    original_check = check_password_hash
    def concurrent_reset(stored_hash, value):
        with app.app_context():
            assert credentials.replace(credentials.read(1), generate_password_hash('recovered-password'))
        return original_check(stored_hash, value)
    monkeypatch.setattr(auth_module, 'check_password_hash', concurrent_reset)
    assert client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'original-password'}).status_code == 200
    assert client.get('/api/v1/jobs').status_code == 401


def test_cli_recovery_is_hidden_persistent_and_revokes_sessions(app, config):
    client = app.test_client()
    login(client)
    result = app.test_cli_runner().invoke(args=['reset-workspace-password'], input='recovery-password\nrecovery-password\n')
    assert result.exit_code == 0
    assert 'recovery-password' not in result.output
    assert client.get('/api/v1/jobs').status_code == 401
    login(create_app(config).test_client(), 'recovery-password')


def test_v2_upgrade_preserves_workspace_and_seeds_existing_bootstrap_once(app, config):
    client = app.test_client()
    headers = login(client)
    job = client.post('/api/v1/jobs', json={'title': 'Existing data'}, headers=headers).json
    app.extensions['jobtracker_db']['sessions'].remove()
    from legacy import downgrade_to_v8
    downgrade_to_v8(app)
    filename = config['DATABASE_URL'].removeprefix('sqlite:///')
    with sqlite3.connect(filename) as c:
        c.execute('DROP TABLE workspace_credentials')
        c.execute('UPDATE schema_version SET version=2')
    upgraded = create_app(config)
    fresh = upgraded.test_client()
    login(fresh)
    assert fresh.get('/api/v1/jobs').json['jobs'][0]['id'] == job['id']
    with upgraded.app_context():
        assert credentials.read(1).session_version == 1
        assert db.get_session().scalar(text('SELECT version FROM schema_version')) == db.SCHEMA_VERSION
    # Pre-upgrade cookies have no generation and must be explicitly reauthenticated.
    with fresh.session_transaction() as state: state.pop('session_version')
    assert fresh.get('/api/v1/auth/session').json['authenticated'] is False


def test_password_write_failure_preserves_old_credential_and_current_session(app, monkeypatch):
    client = app.test_client()
    headers = login(client)
    def fail(*_args): raise credentials.Unavailable()
    monkeypatch.setattr(credentials, 'replace', fail)
    response = client.post('/api/v1/auth/password', json=payload(), headers=headers)
    assert response.status_code == 503
    assert client.get('/api/v1/jobs').status_code == 200
    with app.app_context():
        assert credentials.read(1).session_version == 1
        assert check_password_hash(credentials.read(1).password_hash, 'original-password')


@pytest.mark.parametrize('password', [None, [], 'x'*129])
def test_malformed_or_oversized_login_rejected_before_hashing(app, monkeypatch, password):
    def should_not_hash(*_args): raise AssertionError('Unexpected password hashing')
    monkeypatch.setattr(auth_module, 'check_password_hash', should_not_hash)
    assert app.test_client().post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': password}).status_code == 401


def test_deleted_account_is_never_reseeded_from_bootstrap_password(app, config):
    client = app.test_client()
    headers = login(client)
    assert client.post('/api/v1/auth/password', json=payload(), headers=headers).status_code == 200
    with app.app_context():
        db.get_session().execute(text('DELETE FROM users'))
        db.get_session().commit()
    # The live session for a deleted account ends; the environment hash is not reinstated.
    assert client.get('/api/v1/jobs').status_code == 401
    assert client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'original-password'}).status_code == 401
    restarted = create_app(config)
    assert restarted.test_client().post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'original-password'}).status_code == 401
    with app.app_context():
        assert db.get_session().scalar(text('SELECT count(*) FROM users')) == 0


def test_failed_bootstrap_rolls_back_schema_upgrade(tmp_path, config, monkeypatch):
    app = create_app(config)
    app.extensions['jobtracker_db']['sessions'].remove()
    from legacy import downgrade_to_v8
    from jobtracker import migrations
    downgrade_to_v8(app)
    filename = config['DATABASE_URL'].removeprefix('sqlite:///')
    with sqlite3.connect(filename) as c:
        c.execute('DROP TABLE workspace_credentials')
        c.execute('UPDATE schema_version SET version=2')
    def fail(*_args): raise RuntimeError('Simulated bootstrap failure')
    # Seeding happens in v2->v3; failing at the last step must undo it too.
    monkeypatch.setattr(migrations, 'upgrade_v8_to_v9', fail)
    with pytest.raises(RuntimeError, match='Simulated bootstrap failure'):
        create_app(config)
    with sqlite3.connect(filename) as c:
        assert c.execute('SELECT version FROM schema_version').fetchone() == (2,)
        assert c.execute("SELECT name FROM sqlite_master WHERE name='workspace_credentials'").fetchone() is None
