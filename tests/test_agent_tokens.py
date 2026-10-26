"""Browser-only token onboarding, secret handling, persistence and revocation."""
from datetime import timedelta
import hashlib
import json

import pytest
from sqlalchemy import select, text, update
from werkzeug.security import generate_password_hash

from jobtracker import agent_tokens, create_app, credentials, db
from jobtracker.models import AgentToken, IngestionRequest, now
from test_api import app, client, login  # noqa: F401


def create(client, headers, **changes):
    return client.post('/api/v1/agent-tokens', headers=headers, json={
        'name': 'My agent', 'current_password': 'test-password', 'expires_in_days': 90, **changes,
    })


def bearer(response):
    return {'Authorization': 'Bearer ' + response.json['token']}


def test_creation_shows_secret_once_and_connection_is_non_mutating(client, app):
    browser = login(client)
    created = create(client, browser)
    assert created.status_code == 201
    assert created.headers['Cache-Control'] == 'no-store'
    raw, item = created.json['token'], created.json['agent_token']
    assert raw.startswith('nm_ingest_') and item['prefix'] == raw[:18]
    assert item['status'] == 'active' and item['last_used_at'] is None
    assert item['permissions'] == ['roles:ingest']
    with app.app_context():
        row = db.get_session().get(AgentToken, item['id'])
        assert row.token_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in str(db.get_session().execute(text('SELECT * FROM agent_tokens')).all())
    checked = client.get('/api/v1/agent-connection', headers=bearer(created))
    assert checked.status_code == 200 and checked.json['connected'] is True
    assert checked.json['agent_token']['last_used_at'] is not None
    listed = client.get('/api/v1/agent-tokens')
    assert listed.status_code == 200 and raw not in listed.get_data(as_text=True)
    assert 'token_hash' not in listed.get_data(as_text=True)
    with app.app_context():
        assert db.get_session().scalar(text('SELECT count(*) FROM jobs')) == 0
        assert db.get_session().scalar(text('SELECT count(*) FROM ingestion_requests')) == 0
    assert client.get('/api/v1/agent-connection', headers={'Authorization': 'Bearer ' + item['prefix']}).status_code == 401
    assert client.get('/api/v1/agent-connection', headers={'Authorization': 'Bearer ' + hashlib.sha256(raw.encode()).hexdigest()}).status_code == 401


def test_management_requires_browser_and_csrf(client):
    assert client.get('/api/v1/agent-tokens').status_code == 401
    assert create(client, {}).status_code == 401
    browser = login(client)
    assert create(client, {}).status_code == 403
    broad = {'Authorization': 'Bearer test-agent-token'}
    assert client.get('/api/v1/agent-tokens', headers=broad).status_code == 403
    assert create(client, broad).status_code == 403
    created = create(client, browser)
    token = bearer(created)
    path = '/api/v1/agent-tokens/' + created.json['agent_token']['id'] + '/revoke'
    assert client.get('/api/v1/agent-tokens', headers=token).status_code == 401
    assert create(client, token).status_code == 401
    assert client.post(path, headers=token).status_code == 401
    assert client.post(path).status_code == 403
    for endpoint in ['/api/v1/jobs', '/api/v1/resumes/missing/download', '/api/v1/extractions/missing']:
        assert client.get(endpoint, headers=token).status_code == 401


@pytest.mark.parametrize('changes', [
    {'name': ''}, {'name': 'x' * 81}, {'name': None}, {'expires_in_days': 0},
    {'expires_in_days': 366}, {'expires_in_days': True}, {'permissions': ['admin']}, {'current_password': None},
])
def test_creation_validation(client, changes):
    response = create(client, login(client), **changes)
    assert response.status_code == 400 and 'token' not in response.json


def test_wrong_password_budget_and_password_change_race(client, app, monkeypatch):
    browser = login(client)
    assert create(client, browser, current_password='wrong').status_code == 400
    original = agent_tokens.check_password_hash
    def changed(stored, password):
        observed = credentials.read()
        assert credentials.replace(observed, generate_password_hash('changed-password'))
        return original(stored, password)
    monkeypatch.setattr(agent_tokens, 'check_password_hash', changed)
    assert create(client, browser).status_code == 409
    with app.app_context():
        assert db.get_session().scalar(text('SELECT count(*) FROM agent_tokens')) == 0


def test_wrong_password_is_throttled(client):
    browser = login(client)
    for _ in range(9):
        assert create(client, browser, current_password='wrong').status_code == 400
    assert create(client, browser).status_code == 429


def test_revoking_one_token_preserves_other_tokens_and_restart_state(client, app):
    browser = login(client)
    first = create(client, browser)
    second = create(client, browser, name='Other agent')
    path = '/api/v1/agent-tokens/' + first.json['agent_token']['id'] + '/revoke'
    assert client.post(path, headers=browser).json['agent_token']['status'] == 'revoked'
    assert client.post(path, headers=browser).status_code == 200
    assert client.get('/api/v1/agent-connection', headers=bearer(first)).status_code == 401
    assert client.get('/api/v1/agent-connection', headers=bearer(second)).status_code == 200
    fresh = create_app(dict(app.config)).test_client()
    assert fresh.get('/api/v1/agent-connection', headers=bearer(first)).status_code == 401
    assert fresh.get('/api/v1/agent-connection', headers=bearer(second)).status_code == 200
    assert client.post('/api/v1/agent-tokens/missing/revoke', headers=browser).status_code == 404


def test_expired_and_wrong_scope_tokens_fail_closed(client, app):
    created = create(client, login(client))
    identifier = created.json['agent_token']['id']
    with app.app_context():
        db.get_session().execute(update(AgentToken).where(AgentToken.id == identifier).values(expires_at=now() - timedelta(seconds=1)))
        db.get_session().commit()
    assert client.get('/api/v1/agent-connection', headers=bearer(created)).status_code == 401
    assert client.get('/api/v1/agent-tokens').json['tokens'][0]['status'] == 'expired'
    with app.app_context():
        db.get_session().execute(update(AgentToken).where(AgentToken.id == identifier).values(expires_at=now() + timedelta(days=1), permissions=['future:scope']))
        db.get_session().commit()
    assert client.get('/api/v1/agent-connection', headers=bearer(created)).status_code == 401


def test_no_token_exists_by_default_or_via_legacy_environment_setting(client, app):
    raw = 'test-only-legacy-token-' + 'x' * 32
    app.config['INGEST_API_TOKEN_SHA256'] = hashlib.sha256(raw.encode()).hexdigest()
    assert client.get('/api/v1/agent-connection', headers={'Authorization': 'Bearer ' + raw}).status_code == 401
    assert client.get('/api/v1/agent-connection').status_code == 401


def test_receipts_are_private_to_their_token_and_keys_cannot_be_reused_by_another(client, app):
    browser = login(client)
    first, second = create(client, browser), create(client, browser, name='Second agent')
    with app.app_context():
        task = IngestionRequest(token_id=first.json['agent_token']['id'], idempotency_key='private-retry-key-' + 'a' * 20,
                                url='https://example.com/job', status='failed', result={'error': 'Private receipt'})
        db.get_session().add(task)
        db.get_session().commit()
        identifier, key = task.id, task.idempotency_key
    assert client.get('/api/v1/ingestions/' + identifier, headers=bearer(first)).status_code == 200
    assert client.get('/api/v1/ingestions/' + identifier, headers=bearer(second)).status_code == 404
    assert client.post('/api/v1/ingestions', json={'url': 'https://example.com/job'},
                       headers={**bearer(second), 'Idempotency-Key': key}).status_code == 409


def test_active_token_limit(client, app):
    browser = login(client)
    with app.app_context():
        for index in range(20):
            db.get_session().add(AgentToken(name='Existing', prefix='nm_ingest_example', token_hash=hashlib.sha256(str(index).encode()).hexdigest(),
                                          permissions=['roles:ingest'], expires_at=now() + timedelta(days=90)))
        db.get_session().commit()
    assert create(client, browser).status_code == 409


def test_v5_upgrade_preserves_legacy_receipts_jobs_and_password(client, app):
    browser = login(client)
    saved = client.post('/api/v1/jobs', json={'title': 'Preserved'}, headers=browser).json
    with app.app_context():
        before = credentials.read()
    engine = app.extensions['jobtracker_db']['engine']
    with engine.begin() as connection:
        connection.execute(text('DROP TABLE ingestion_requests'))
        connection.execute(text('DROP TABLE agent_tokens'))
        connection.execute(text('CREATE TABLE ingestion_requests (id VARCHAR(36) PRIMARY KEY, idempotency_key VARCHAR(128) UNIQUE, url TEXT, status VARCHAR(20), result JSON, created_at DATETIME)'))
        connection.execute(text("INSERT INTO ingestion_requests VALUES ('old', 'old-key', 'https://example.com', 'succeeded', :result, '2026-10-25 00:00:00')"), {'result': json.dumps({'job_id': saved['id']})})
        connection.execute(text('UPDATE schema_version SET version=5'))
    fresh = create_app(dict(app.config))
    with fresh.app_context():
        assert credentials.read() == before
        assert db.get_session().scalar(text('SELECT version FROM schema_version')) == db.SCHEMA_VERSION
        old = db.get_session().get(IngestionRequest, 'old')
        assert old.result['job_id'] == saved['id'] and old.token_id is None
        assert db.get_session().scalar(select(AgentToken.id)) is None


@pytest.mark.parametrize('scopes', [['roles:search'], ['roles:ingest'], ['roles:search', 'roles:ingest']])
def test_managed_permissions_are_independent_and_revocation_covers_search(client, scopes):
    browser = login(client)
    created = create(client, browser, permissions=scopes)
    assert created.status_code == 201
    headers = bearer(created)
    assert client.get('/api/v1/agent-connection', headers=headers).status_code == 200
    expected = 200 if 'roles:search' in scopes else 401
    assert client.get('/api/v1/jobs/search?company=Example', headers=headers).status_code == expected
    # Valid ingestion permission reaches payload validation; search-only stops at auth.
    assert client.post('/api/v1/ingestions', headers=headers, json={}).status_code == (400 if 'roles:ingest' in scopes else 401)
    assert client.get('/api/v1/jobs', headers=headers).status_code == 401
    path = '/api/v1/agent-tokens/' + created.json['agent_token']['id'] + '/revoke'
    assert client.post(path, headers=browser).status_code == 200
    assert client.get('/api/v1/jobs/search?company=Example', headers=headers).status_code == 401


def test_stage_upgrade_preserves_children_versions_and_foreign_key_enforcement(client, app):
    import io
    from sqlalchemy.exc import IntegrityError
    browser = login(client)
    job = client.post('/api/v1/jobs', json={'title': 'Preserved'}, headers=browser).json
    assert job['stage'] == 'interesting'
    contact = client.post(f"/api/v1/jobs/{job['id']}/contacts", json={'name': 'Pat'}, headers=browser).json
    resume = client.post(f"/api/v1/jobs/{job['id']}/resumes", data={'file': (io.BytesIO(b'resume'), 'resume.txt')}, headers=browser).json
    engine = app.extensions['jobtracker_db']['engine']
    with engine.begin() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        # Recreate the actual old CHECK constraint, rather than only relabeling a new schema.
        schema = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='jobs'").scalar()
        old_schema = schema.replace('CREATE TABLE jobs', 'CREATE TABLE jobs_old').replace("'needs_review',", '')
        connection.exec_driver_sql('PRAGMA defer_foreign_keys=ON')
        connection.exec_driver_sql(old_schema)
        connection.exec_driver_sql('INSERT INTO jobs_old SELECT * FROM jobs')
        connection.exec_driver_sql('DROP TABLE jobs')
        connection.exec_driver_sql('ALTER TABLE jobs_old RENAME TO jobs')
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        connection.exec_driver_sql('PRAGMA defer_foreign_keys=OFF')
        connection.execute(text('UPDATE schema_version SET version=6'))
        snapshots = {table: connection.execute(text(f'SELECT * FROM {table} ORDER BY id')).all()
                     for table in ['jobs', 'contacts', 'resumes', 'activities']}
    fresh = create_app(dict(app.config))
    with fresh.extensions['jobtracker_db']['engine'].connect() as connection:
        for table, rows in snapshots.items():
            assert connection.execute(text(f'SELECT * FROM {table} ORDER BY id')).all() == rows
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE contacts SET job_id='missing' WHERE id=:id"), {'id': contact['id']})
        connection.rollback()
    updated = client.patch(f"/api/v1/jobs/{job['id']}", json={'stage': 'needs_review', 'version': job['version']}, headers=browser)
    assert updated.status_code == 200
    promoted = client.patch(f"/api/v1/jobs/{job['id']}", json={'stage': 'interesting', 'version': updated.json['version']}, headers=browser)
    assert promoted.status_code == 200
    assert client.get(f"/api/v1/resumes/{resume['id']}/download").data == b'resume'
