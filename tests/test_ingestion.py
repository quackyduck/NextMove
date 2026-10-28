"""Scoped authentication, retry safety, and atomic URL ingestion."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import threading
import time

import pytest
from sqlalchemy import select, text, update

from jobtracker import create_app, db, ingestion, services, taskqueue
from jobtracker.models import Activity, AgentToken, IngestionRequest, now
from test_api import app, client, login  # noqa: F401

TOKEN = 'disposable-ingest-token-' + 'x' * 32
KEY = 'request-id-' + 'a' * 24
TOKEN_ID = '11111111-1111-4111-8111-111111111111'
FIELDS = {'title': 'Engineer', 'company': 'Acme', 'url': 'https://example.com/job',
          'salary': 'USD 150000 / year', 'notes': 'Key qualifications\nPython', 'description': 'Build systems.'}


@pytest.fixture
def headers(app):
    with app.app_context():
        db.get_session().add(AgentToken(user_id=1, id=TOKEN_ID, name='Test agent', prefix=TOKEN[:18],
            token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(), permissions=['roles:ingest'], expires_at=now() + timedelta(days=90)))
        db.get_session().commit()
    return {'Authorization': 'Bearer ' + TOKEN, 'Idempotency-Key': KEY}


@pytest.fixture(autouse=True)
def extractor(monkeypatch):
    monkeypatch.setattr(ingestion, 'extract_posting', lambda url: {'fields': {**FIELDS, 'url': url}, 'warnings': []})


def submit(client, headers, url=FIELDS['url']):
    return client.post('/api/v1/ingestions', json={'url': url}, headers=headers)


def wait(client, headers, identifier):
    """Run the queue worker in-process until idle, then read the receipt."""
    taskqueue.drain(client.application)
    response = client.get('/api/v1/ingestions/' + identifier, headers=headers)
    assert response.status_code == 200
    assert response.json['status'] != 'running', 'Ingestion did not finish'
    return response.json


def test_token_disabled_by_default_and_separate_from_other_auth(client, app, headers):
    assert submit(client, {'Idempotency-Key': KEY}).status_code == 401
    assert submit(client, {**headers, 'Authorization': 'Bearer wrong'}).status_code == 401
    assert submit(client, {**headers, 'Authorization': 'Bearer test-agent-token'}).status_code == 401
    browser = login(client)
    assert submit(client, {**browser, 'Idempotency-Key': KEY}).status_code == 401
    assert client.post('/api/v1/ingestions?token=' + TOKEN, json={'url': FIELDS['url']}, headers=browser).status_code == 401
    for path in ['/api/v1/jobs', '/api/v1/extractions/missing', '/api/v1/resumes/missing/download']:
        assert client.get(path, headers=headers).status_code == 401
    assert client.post('/api/v1/auth/password', json={}, headers=headers).status_code == 401
    assert client.get('/api/v1/ingestions/missing').status_code == 401
    assert client.get('/api/v1/ingestions/missing', headers=headers).status_code == 404


def test_production_requires_https(client, app, headers):
    app.config['APP_ENV'] = 'production'
    assert submit(client, headers).status_code == 403
    response = client.post('/api/v1/ingestions', base_url='https://localhost', json={'url': FIELDS['url']}, headers=headers)
    assert response.status_code == 202
    # Poll over HTTPS too; no browser cookie is needed.
    taskqueue.drain(app)
    result = client.get(response.headers['Location'], base_url='https://localhost', headers=headers)
    assert result.json['status'] == 'succeeded'


@pytest.mark.parametrize('changes', [{'Idempotency-Key': ''}, {'Idempotency-Key': 'short'}, {'Idempotency-Key': 'x' * 129}])
def test_requires_valid_idempotency_key(client, headers, changes):
    assert submit(client, {**headers, **changes}).status_code == 400


def test_payload_validation(client, headers):
    for body in [[], {}, {'url': 'file:///etc/passwd'}, {'url': FIELDS['url'], 'token': TOKEN}]:
        assert client.post('/api/v1/ingestions', json=body, headers=headers).status_code == 400


def test_saves_role_and_replays_across_restart_and_age(client, app, headers):
    response = submit(client, headers)
    assert response.status_code == 202
    assert response.headers['Location'].endswith('/ingestions/' + response.json['id'])
    result = wait(client, headers, response.json['id'])
    assert result['status'] == 'succeeded'
    jobs = client.get('/api/v1/jobs', headers={'Authorization': 'Bearer test-agent-token'}).json['jobs']
    assert len(jobs) == 1 and jobs[0]['id'] == result['job_id']
    assert all(jobs[0][field] == value for field, value in FIELDS.items())
    assert jobs[0]['stage'] == 'needs_review'
    with app.app_context():
        assert len(db.get_session().scalars(select(Activity)).all()) == 1
        db.get_session().execute(update(IngestionRequest).values(created_at=now() - timedelta(days=2)))
        db.get_session().commit()
    restarted = create_app(dict(app.config)).test_client()
    replay = submit(restarted, headers)
    assert replay.status_code == 200 and replay.json == result
    assert submit(restarted, headers, 'https://example.com/different').status_code == 409


def test_concurrent_retries_share_one_task_and_ack_does_not_fetch(client, app, headers, monkeypatch):
    calls = []
    def tracked(url):
        calls.append(url)
        return {'fields': FIELDS, 'warnings': []}
    monkeypatch.setattr(ingestion, 'extract_posting', tracked)
    def request(_):
        return submit(app.test_client(), headers)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request, range(2)))
    assert time.monotonic() - started < 1
    assert all(response.status_code == 202 for response in responses)
    assert responses[0].json['id'] == responses[1].json['id']
    assert calls == []  # The web request only enqueues; the worker fetches.
    assert wait(client, headers, responses[0].json['id'])['status'] == 'succeeded'
    assert len(calls) == 1


def test_shared_capacity_and_replay_when_full(client, headers):
    first = submit(client, headers)
    second = submit(client, {**headers, 'Idempotency-Key': KEY + 'b'})
    assert second.status_code == 202
    assert submit(client, headers).json == first.json
    assert submit(client, {**headers, 'Idempotency-Key': KEY + 'c'}).status_code == 429
    assert client.post('/api/v1/jobs/extract', json={'url': FIELDS['url']}, headers={'Authorization': 'Bearer test-agent-token'}).status_code == 429
    wait(client, headers, first.json['id'])
    wait(client, headers, second.json['id'])


def test_hourly_limit_is_persistent_and_does_not_block_replay(client, app, headers):
    with app.app_context():
        session = db.get_session()
        for index in range(30):
            session.add(IngestionRequest(user_id=1, token_id=TOKEN_ID, idempotency_key=KEY + str(index), url=FIELDS['url'], status='failed', result={'error': 'Example failure'}))
        session.commit()
    assert submit(client, headers).status_code == 429
    assert submit(client, {**headers, 'Idempotency-Key': KEY + '0'}).status_code == 200


def test_creation_and_success_are_atomic(client, app, headers, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('Internal details must not be returned')
    monkeypatch.setattr(services, '_activity', fail)
    response = submit(client, headers)
    result = wait(client, headers, response.json['id'])
    assert result['status'] == 'failed' and 'Internal details' not in result['error']
    with app.app_context():
        assert db.get_session().scalar(text('SELECT count(*) FROM jobs')) == 0
        assert db.get_session().scalar(text('SELECT count(*) FROM activities')) == 0
    assert submit(client, headers).json == result


def test_expired_worker_cannot_create_late_role(client, app, headers, monkeypatch):
    def slow(url):
        # The deadline passes while the worker is fetching the page.
        with app.app_context():
            db.get_session().execute(update(IngestionRequest).values(created_at=now() - timedelta(seconds=61)))
            db.get_session().commit()
        return {'fields': FIELDS, 'warnings': []}
    monkeypatch.setattr(ingestion, 'extract_posting', slow)
    response = submit(client, headers)
    assert wait(client, headers, response.json['id'])['status'] == 'failed'
    # Also prove that retrying finalization cannot create a late role.
    with app.app_context():
        services.complete_ingestion(1, response.json['id'], FIELDS, [])
        assert db.get_session().scalar(text('SELECT count(*) FROM jobs')) == 0


def test_private_url_fails_without_saving(client, headers, monkeypatch):
    from jobtracker.services import ServiceError
    def fail(url):
        raise ServiceError('Private addresses are unavailable')
    monkeypatch.setattr(ingestion, 'extract_posting', fail)
    response = submit(client, headers, 'http://127.0.0.1/internal')
    result = wait(client, headers, response.json['id'])
    assert result['status'] == 'failed' and 'Private' in result['error']
    assert 'job_id' not in result


def test_schema_four_upgrade_preserves_jobs_and_preview_tasks(client, app):
    browser = login(client)
    job = client.post('/api/v1/jobs', json={'title': 'Keep this role'}, headers=browser).json
    from legacy import downgrade_to_v8
    downgrade_to_v8(app)
    engine = app.extensions['jobtracker_db']['engine']
    with engine.begin() as connection:
        connection.execute(text('DROP TABLE ingestion_requests'))
        connection.execute(text('UPDATE schema_version SET version=4'))
    fresh = create_app(dict(app.config))
    with fresh.app_context():
        assert db.get_session().scalar(text('SELECT version FROM schema_version')) == db.SCHEMA_VERSION
        assert services.get_job(1, job['id'])['title'] == 'Keep this role'
        assert db.get_session().scalar(text('SELECT count(*) FROM ingestion_requests')) == 0


def test_prefixed_ingestion_location(client, app, headers):
    prefixed = create_app({**dict(app.config), 'APP_ROOT_PATH': '/nextmove'}).test_client()
    response = prefixed.post('/nextmove/api/v1/ingestions', base_url='http://localhost', json={'url': FIELDS['url']}, headers=headers)
    assert response.status_code == 202
    assert response.headers['Location'] == '/nextmove/api/v1/ingestions/' + response.json['id']
    taskqueue.drain(app)
    result = prefixed.get(response.headers['Location'], base_url='http://localhost', headers=headers)
    assert result.json['status'] == 'succeeded'


def test_named_token_search_then_ingest_then_find_duplicate(client):
    browser = login(client)
    created = client.post('/api/v1/agent-tokens', headers=browser, json={
        'name': 'Intake agent', 'current_password': 'test-password',
        'permissions': ['roles:search', 'roles:ingest'],
    })
    assert created.status_code == 201
    headers = {'Authorization': 'Bearer ' + created.json['token'], 'Idempotency-Key': KEY}
    query = {'url': FIELDS['url'], 'company': FIELDS['company'], 'title': FIELDS['title']}
    assert client.get('/api/v1/jobs/search', query_string=query, headers=headers).json['matches'] == []
    task = submit(client, headers).json
    result = wait(client, headers, task['id'])
    assert result['status'] == 'succeeded'
    matches = client.get('/api/v1/jobs/search', query_string=query, headers=headers).json['matches']
    assert matches[0]['classification'] == 'duplicate'
    assert matches[0]['id'] == result['job_id'] and matches[0]['stage'] == 'needs_review'
    assert 'notes' not in matches[0]
