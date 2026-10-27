"""Tenant isolation matrix — RELEASE BLOCKER.

User A owns a job, contact, resume (and its file), activity history, extraction
preview, agent token and ingestion receipt. User B tries every endpoint with
A's IDs through every credential B can hold: a browser session, a managed agent
token, and the deprecated environment token (which acts for user #1, so B is
user #1 here and A is a signed-up account). Every attempt must look exactly like
a missing ID (404) or be refused before routing (401/403), and must change
nothing of A's.

To extend: add each new route to ENDPOINTS (takes an ID) or COLLECTIONS (no ID)
below. `test_every_route_is_classified` fails until you do.
"""
from dataclasses import dataclass, field
import hashlib
import io
from pathlib import Path

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from jobtracker import create_app, credentials, extraction_tasks, ingestion, taskqueue
from jobtracker.models import Contact, now

B_PASSWORD = 'b-owner-password'
A_PASSWORD = 'a-tenant-password'
LEGACY = 'legacy-environment-token-' + 'x' * 16
FIELDS = {'title': 'A secret role', 'company': 'Acme Secret', 'url': 'https://example.com/a-secret'}
KEY = 'tenant-a-retry-key-' + 'k' * 16


@dataclass(frozen=True)
class Endpoint:
    method: str
    rule: str             # Flask rule, for the coverage check
    path: str             # with {job}, {resume}, {extraction}, {token}, {receipt}
    modes: tuple          # credentials this route accepts: session, agent, legacy
    body: dict = field(default_factory=dict)
    upload: bool = False


ENDPOINTS = [
    Endpoint('GET', '/api/v1/jobs/<job_id>', '/api/v1/jobs/{job}', ('session', 'legacy')),
    Endpoint('PATCH', '/api/v1/jobs/<job_id>', '/api/v1/jobs/{job}', ('session', 'legacy'),
             {'title': 'Hijacked', 'stage': 'offer'}),
    Endpoint('GET', '/api/v1/jobs/<job_id>/activities', '/api/v1/jobs/{job}/activities', ('session', 'legacy')),
    Endpoint('GET', '/api/v1/jobs/<job_id>/contacts', '/api/v1/jobs/{job}/contacts', ('session', 'legacy')),
    Endpoint('POST', '/api/v1/jobs/<job_id>/contacts', '/api/v1/jobs/{job}/contacts', ('session', 'legacy'),
             {'name': 'Intruder'}),
    Endpoint('GET', '/api/v1/jobs/<job_id>/resumes', '/api/v1/jobs/{job}/resumes', ('session', 'legacy')),
    Endpoint('POST', '/api/v1/jobs/<job_id>/resumes', '/api/v1/jobs/{job}/resumes', ('session', 'legacy', 'agent'),
             upload=True),
    Endpoint('GET', '/api/v1/resumes/<resume_id>/download', '/api/v1/resumes/{resume}/download', ('session', 'legacy')),
    Endpoint('GET', '/api/v1/extractions/<task_id>', '/api/v1/extractions/{extraction}', ('session', 'legacy')),
    Endpoint('POST', '/api/v1/agent-tokens/<token_id>/revoke', '/api/v1/agent-tokens/{token}/revoke', ('session',)),
    Endpoint('GET', '/api/v1/ingestions/<task_id>', '/api/v1/ingestions/{receipt}', ('agent',)),
]

# Routes without an ID; each has a dedicated test below proving B sees only B's data.
COLLECTIONS = {
    ('GET', '/api/v1/jobs'): 'test_lists_and_search_exclude_other_tenants',
    ('POST', '/api/v1/jobs'): 'test_creations_belong_to_the_caller',
    ('GET', '/api/v1/jobs/search'): 'test_lists_and_search_exclude_other_tenants',
    ('POST', '/api/v1/jobs/extract'): 'test_creations_belong_to_the_caller',
    ('GET', '/api/v1/agent-tokens'): 'test_lists_and_search_exclude_other_tenants',
    ('POST', '/api/v1/agent-tokens'): 'test_creations_belong_to_the_caller',
    ('GET', '/api/v1/agent-connection'): 'test_agent_connection_and_session_report_the_caller',
    ('POST', '/api/v1/ingestions'): 'test_idempotency_keys_are_per_tenant',
    ('GET', '/api/v1/auth/session'): 'test_agent_connection_and_session_report_the_caller',
    ('POST', '/api/v1/auth/signup'): 'test_accounts.py',
    ('POST', '/api/v1/auth/login'): 'test_accounts.py',
    ('POST', '/api/v1/auth/password'): 'test_password_change_is_per_account',
    ('POST', '/api/v1/auth/logout'): 'test_password_change_is_per_account',
    ('GET', '/api/v1/health'): 'public',
    # Phase 1: caller-scoped, browser-session only (no IDs in the path).
    ('GET', '/api/v1/onboarding'): 'test_onboarding.py',
    ('POST', '/api/v1/onboarding/steps/<step_id>'): 'test_onboarding.py',
    ('GET', '/api/v1/profile/resume'): 'test_onboarding.py',
    ('POST', '/api/v1/profile/resume'): 'test_onboarding.py',
    ('GET', '/api/v1/profile/resume/download'): 'test_onboarding.py',
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction_tasks, 'extract_posting', lambda url: {'fields': FIELDS, 'warnings': []})
    monkeypatch.setattr(ingestion, 'extract_posting', lambda url: {'fields': {**FIELDS, 'url': url}, 'warnings': []})
    app = create_app({
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'tenant-test-secret',
        'APP_PASSWORD_HASH': generate_password_hash(B_PASSWORD),  # user #1 = B
        'DATABASE_URL': f'sqlite:///{tmp_path / "tenants.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'AGENT_API_TOKEN': LEGACY,
        'SESSION_COOKIE_SECURE': False,
    })
    with app.app_context():
        [code] = credentials.create_invites(1, 7)
    a = app.test_client()
    signup = a.post('/api/v1/auth/signup', json={'email': 'a@example.com', 'password': A_PASSWORD,
                                                  'password_confirmation': A_PASSWORD, 'invite_code': code})
    assert signup.status_code == 201
    a_headers = {'X-CSRF-Token': signup.json['csrf_token']}
    job = a.post('/api/v1/jobs', json=FIELDS, headers=a_headers).json
    a.post(f"/api/v1/jobs/{job['id']}/contacts", json={'name': 'A contact', 'email': 'c@example.com'}, headers=a_headers)
    resume = a.post(f"/api/v1/jobs/{job['id']}/resumes", headers=a_headers,
                    data={'file': (io.BytesIO(b'A private resume'), 'a.txt')}).json
    preview = a.post('/api/v1/jobs/extract', json={'url': FIELDS['url']}, headers=a_headers).json
    a_token = a.post('/api/v1/agent-tokens', headers=a_headers, json={
        'name': 'A agent', 'current_password': A_PASSWORD, 'permissions': ['roles:search', 'roles:ingest']}).json
    a_agent = {'Authorization': 'Bearer ' + a_token['token']}
    receipt = a.post('/api/v1/ingestions', json={'url': 'https://example.com/a-ingested'},
                     headers={**a_agent, 'Idempotency-Key': KEY}).json
    taskqueue.drain(app)

    b = app.test_client()
    login = b.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': B_PASSWORD})
    b_session = {'X-CSRF-Token': login.json['csrf_token']}
    b_token = b.post('/api/v1/agent-tokens', headers=b_session, json={
        'name': 'B agent', 'current_password': B_PASSWORD, 'permissions': ['roles:search', 'roles:ingest']}).json
    ids = {'job': job['id'], 'resume': resume['id'], 'extraction': preview['id'],
           'token': a_token['agent_token']['id'], 'receipt': receipt['id']}
    return dict(app=app, a=a, a_headers=a_headers, a_agent=a_agent, ids=ids, b=b,
                credentials={'session': (b, b_session),
                             'agent': (app.test_client(), {'Authorization': 'Bearer ' + b_token['token']}),
                             'legacy': (app.test_client(), {'Authorization': 'Bearer ' + LEGACY})})


def a_state(world):
    """Everything A can observe; must be identical before and after B's attempts."""
    a, headers, ids = world['a'], world['a_headers'], world['ids']
    job = ids['job']
    return {
        'job': a.get(f'/api/v1/jobs/{job}').json,
        'contacts': a.get(f'/api/v1/jobs/{job}/contacts').json,
        'resumes': a.get(f'/api/v1/jobs/{job}/resumes').json,
        'activities': a.get(f'/api/v1/jobs/{job}/activities').json,
        'download': a.get(f"/api/v1/resumes/{ids['resume']}/download").data,
        'preview': a.get(f"/api/v1/extractions/{ids['extraction']}").json,
        # last_used_at moves when this helper reads the receipt with A's own token.
        'tokens': [{**token, 'last_used_at': None} for token in a.get('/api/v1/agent-tokens').json['tokens']],
        'receipt': a.get(f"/api/v1/ingestions/{ids['receipt']}", headers=world['a_agent']).json,
    }


def send(client, headers, endpoint, ids):
    path = endpoint.path.format(**ids)
    if endpoint.upload:
        return client.post(path, headers=headers, data={'file': (io.BytesIO(b'intruder'), 'b.txt')})
    if endpoint.method == 'GET':
        return client.get(path, headers=headers)
    return client.open(path, method=endpoint.method, json=endpoint.body, headers=headers)


@pytest.mark.parametrize('mode', ['session', 'agent', 'legacy'])
@pytest.mark.parametrize('endpoint', ENDPOINTS, ids=lambda e: f'{e.method} {e.rule}')
def test_other_tenants_ids_are_not_found(world, endpoint, mode):
    before = a_state(world)
    client, headers = world['credentials'][mode]
    response = send(client, headers, endpoint, world['ids'])
    if mode in endpoint.modes:
        assert response.status_code == 404, response.json
        # Indistinguishable from an ID that never existed.
        unknown = send(client, headers, endpoint, {key: 'f' * 8 + '-0000-4000-8000-' + '0' * 12
                                                   for key in world['ids']})
        assert unknown.status_code == 404 and unknown.json == response.json
    else:
        assert response.status_code in (401, 403)
    assert a_state(world) == before


def test_every_route_is_classified(world):
    covered = {(endpoint.method, endpoint.rule) for endpoint in ENDPOINTS} | set(COLLECTIONS)
    for rule in world['app'].url_map.iter_rules():
        if not rule.rule.startswith('/api/'):
            continue
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            assert (method, rule.rule) in covered, f'Classify {method} {rule.rule} in the tenant isolation suite'


def test_lists_and_search_exclude_other_tenants(world):
    for mode, (client, headers) in world['credentials'].items():
        if mode != 'agent':
            assert client.get('/api/v1/jobs', headers=headers).json['jobs'] == []
        search = client.get('/api/v1/jobs/search', headers=headers,
                            query_string={'company': FIELDS['company'], 'title': FIELDS['title'], 'url': FIELDS['url']})
        assert search.status_code == 200 and search.json['matches'] == [] and search.json['total'] == 0
    client, headers = world['credentials']['session']
    assert [token['name'] for token in client.get('/api/v1/agent-tokens', headers=headers).json['tokens']] == ['B agent']
    # And A still sees its own.
    assert len(world['a'].get('/api/v1/jobs').json['jobs']) == 2


def test_creations_belong_to_the_caller(world):
    client, headers = world['credentials']['session']
    created = client.post('/api/v1/jobs', json={'title': 'B role'}, headers=headers).json
    assert world['a'].get(f"/api/v1/jobs/{created['id']}").status_code == 404
    preview = client.post('/api/v1/jobs/extract', json={'url': 'https://example.com/b'}, headers=headers).json
    assert world['a'].get(f"/api/v1/extractions/{preview['id']}").status_code == 404
    taskqueue.drain(world['app'])
    assert client.get(f"/api/v1/extractions/{preview['id']}", headers=headers).json['status'] == 'succeeded'
    assert all(token['name'] == 'A agent' for token in world['a'].get('/api/v1/agent-tokens').json['tokens'])


def test_idempotency_keys_are_per_tenant(world):
    client, headers = world['credentials']['agent']
    # B reusing A's key creates B's own receipt; it neither replays nor reveals A's.
    response = client.post('/api/v1/ingestions', json={'url': 'https://example.com/b-role'},
                           headers={**headers, 'Idempotency-Key': KEY})
    assert response.status_code == 202 and response.json['id'] != world['ids']['receipt']
    taskqueue.drain(world['app'])
    result = client.get(f"/api/v1/ingestions/{response.json['id']}", headers=headers).json
    session, browser = world['credentials']['session']
    assert session.get(f"/api/v1/jobs/{result['job_id']}", headers=browser).status_code == 200
    assert world['a'].get(f"/api/v1/jobs/{result['job_id']}").status_code == 404


def test_agent_connection_and_session_report_the_caller(world):
    client, headers = world['credentials']['agent']
    assert client.get('/api/v1/agent-connection', headers=headers).json['agent_token']['name'] == 'B agent'
    session, _ = world['credentials']['session']
    assert session.get('/api/v1/auth/session').json['user']['email'] == 'owner@localhost'
    assert world['a'].get('/api/v1/auth/session').json['user']['email'] == 'a@example.com'


def test_password_change_is_per_account(world):
    b, headers = world['credentials']['session']
    changed = b.post('/api/v1/auth/password', headers=headers, json={
        'current_password': B_PASSWORD, 'new_password': 'b-new-password', 'confirm_password': 'b-new-password'})
    assert changed.status_code == 200
    # A's session and password are untouched; B's other sessions would be revoked.
    assert world['a'].get('/api/v1/jobs').status_code == 200
    assert world['a'].post('/api/v1/auth/logout', headers=world['a_headers']).status_code == 200
    assert world['a'].post('/api/v1/auth/login', json={'email': 'a@example.com', 'password': A_PASSWORD}).status_code == 200


def test_files_live_under_their_owners_prefix(world):
    root = Path(world['app'].config['UPLOAD_FOLDER'])
    files = [path for path in root.rglob('*') if path.is_file()]
    assert len(files) == 1 and files[0].parent == root / '2'
    assert files[0].read_bytes() == b'A private resume'
    assert (root / '2').stat().st_mode & 0o777 == 0o700


def test_database_rejects_children_that_cross_tenants(world):
    engine = world['app'].extensions['jobtracker_db']['engine']
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(Contact).values(id='cross', user_id=1, job_id=world['ids']['job'], name='x',
                                                  email='', notes='', created_at=now()))
    with engine.connect() as connection:
        tables = ['jobs', 'contacts', 'resumes', 'activities', 'agent_tokens', 'extraction_tasks',
                  'ingestion_requests', 'queued_tasks']
        for table in tables:
            assert connection.scalar(text(f'SELECT count(*) FROM {table} WHERE user_id IS NULL')) == 0


def test_queue_tasks_cannot_finish_another_tenants_record(world):
    """A forged task naming A's preview but owned by B must not touch it."""
    app = world['app']
    engine = app.extensions['jobtracker_db']['engine']
    a, headers = world['a'], world['a_headers']
    preview = a.post('/api/v1/jobs/extract', json={'url': 'https://example.com/a2'}, headers=headers).json
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM queued_tasks WHERE status='queued'"))
        taskqueue.enqueue(connection, 1, 'extract_preview', {'task_id': preview['id'], 'url': 'https://example.com/x'})
    taskqueue.drain(app)
    assert a.get(f"/api/v1/extractions/{preview['id']}").json['status'] == 'running'


def test_token_hashes_are_unique_across_tenants(world):
    with world['app'].extensions['jobtracker_db']['engine'].connect() as connection:
        hashes = connection.execute(text('SELECT token_hash FROM agent_tokens')).scalars().all()
    assert len(hashes) == len(set(hashes)) == 2
    assert hashlib.sha256(LEGACY.encode()).hexdigest() not in hashes


def test_legacy_tokens_act_only_for_the_flagged_owner(world):
    """The environment tokens never act for a signed-up account."""
    engine = world['app'].extensions['jobtracker_db']['engine']
    client, headers = world['credentials']['legacy']
    with engine.begin() as connection:
        assert connection.execute(text('SELECT id FROM users WHERE owns_legacy_tokens=1')).scalars().all() == [1]
        connection.execute(text('UPDATE users SET owns_legacy_tokens=0'))
    assert client.get('/api/v1/jobs', headers=headers).status_code == 401
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text('UPDATE users SET owns_legacy_tokens=1'))  # at most one owner
    with engine.begin() as connection:
        connection.execute(text('UPDATE users SET owns_legacy_tokens=1 WHERE id=1'))
    assert client.get('/api/v1/jobs', headers=headers).json['jobs'] == []
