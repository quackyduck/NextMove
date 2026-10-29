"""Onboarding state machine and master resume API (schema 10)."""
import io
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect

from jobtracker import credentials, migrations, onboarding
from test_api import app, client, login  # noqa: F401
from test_documents import PDF, docx

PASSWORD = 'Onboarding-pass-2026'


def signup(client, app, email='new@example.com'):
    with app.app_context():
        code, = credentials.create_invites(1, 7)
    result = client.post('/api/v1/auth/signup', json={
        'email': email, 'password': PASSWORD, 'password_confirmation': PASSWORD, 'invite_code': code})
    assert result.status_code == 201, result.json
    return {'X-CSRF-Token': result.json['csrf_token']}


def statuses(client):
    return {step['id']: step['status'] for step in client.get('/api/v1/onboarding').json['steps']}


def upload(client, headers, data=PDF, filename='resume.pdf'):
    return client.post('/api/v1/profile/resume', headers=headers,
                       data={'file': (io.BytesIO(data), filename)}, content_type='multipart/form-data')


def test_new_signup_runs_guided_flow_and_registry_shape(client, app):
    signup(client, app)
    state = client.get('/api/v1/onboarding').json
    assert state['guided_active'] is True and state['next_step'] == 'upload_resume'
    assert [(step['id'], step['version'], step['status'], step['guided']) for step in state['steps']] == [
        ('create_account', 1, 'done', False), ('upload_resume', 1, 'pending', True),
        ('connect_agent', 1, 'pending', True)]
    assert all(step['title'] for step in state['steps'])


def test_skip_resume_after_logout_and_finish(client, app):
    headers = signup(client, app)
    skipped = client.post('/api/v1/onboarding/steps/upload_resume', json={'status': 'skipped'}, headers=headers)
    assert skipped.status_code == 200
    assert skipped.json['guided_active'] is True  # connect_agent still untouched
    assert skipped.json['next_step'] == 'connect_agent'
    assert client.post('/api/v1/auth/logout', headers=headers).status_code == 200
    assert client.get('/api/v1/onboarding').status_code == 401
    result = client.post('/api/v1/auth/login', json={'email': 'new@example.com', 'password': PASSWORD})
    headers = {'X-CSRF-Token': result.json['csrf_token']}
    state = client.get('/api/v1/onboarding').json
    assert state['guided_active'] is True
    assert statuses(client) == {'create_account': 'done', 'upload_resume': 'skipped', 'connect_agent': 'pending'}
    state = client.post('/api/v1/onboarding/steps/connect_agent', json={'status': 'skipped'}, headers=headers).json
    assert state['guided_active'] is False
    # A skipped step can still be completed later from the checklist.
    assert upload(client, headers).status_code == 201
    assert statuses(client)['upload_resume'] == 'done'
    # And a later skip never undoes completed work.
    client.post('/api/v1/onboarding/steps/upload_resume', json={'status': 'skipped'}, headers=headers)
    assert statuses(client)['upload_resume'] == 'done'


def test_done_is_derived_from_facts_not_assertions(client, app):
    headers = signup(client, app)
    for step in ('upload_resume', 'connect_agent'):
        result = client.post(f'/api/v1/onboarding/steps/{step}', json={'status': 'done'}, headers=headers)
        assert result.status_code == 409
    token = client.post('/api/v1/agent-tokens', headers=headers, json={
        'name': 'Agent', 'current_password': PASSWORD, 'permissions': ['roles:search']}).json['token']
    assert statuses(client)['connect_agent'] == 'pending'  # Created but never used.
    assert client.get('/api/v1/agent-connection', headers={'Authorization': f'Bearer {token}'}).status_code == 200
    assert statuses(client)['connect_agent'] == 'done'
    agent_token = client.get('/api/v1/agent-tokens').json['tokens'][0]
    client.post(f'/api/v1/agent-tokens/{agent_token["id"]}/revoke', headers=headers)
    assert statuses(client)['connect_agent'] == 'done'  # Revoking keeps the history.
    assert client.post('/api/v1/onboarding/steps/connect_agent', json={'status': 'done'}, headers=headers).status_code == 200


@pytest.mark.parametrize('step,body,status', [
    ('unknown', {'status': 'skipped'}, 404),
    ('create_account', {'status': 'skipped'}, 400),
    ('upload_resume', {'status': 'pending'}, 400),
    ('upload_resume', {'status': 'skipped', 'extra': 1}, 400),
    ('upload_resume', None, 400),
])
def test_invalid_transitions(client, app, step, body, status):
    headers = signup(client, app)
    kwargs = {'json': body} if body is not None else {'data': 'x'}
    assert client.post(f'/api/v1/onboarding/steps/{step}', headers=headers, **kwargs).status_code == status


def test_migrated_owner_gets_checklist_not_guided_flow(client):
    login(client)
    state = client.get('/api/v1/onboarding').json
    assert state['guided_active'] is False and state['next_step'] is None
    assert [step['status'] for step in state['steps']] == ['done', 'pending', 'pending']


def test_new_step_and_version_bump_for_existing_users(client, app, monkeypatch):
    headers = signup(client, app)
    client.post('/api/v1/onboarding/steps/upload_resume', json={'status': 'skipped'}, headers=headers)
    client.post('/api/v1/onboarding/steps/connect_agent', json={'status': 'skipped'}, headers=headers)
    alerts = onboarding.Step('job_alerts', 1, 'Turn on job alerts')
    bumped = onboarding.Step('upload_resume', 2, 'Upload your resume', guided=True, completed=onboarding.has_resume)
    registry = (onboarding.REGISTRY[0], bumped, onboarding.REGISTRY[2], alerts)
    monkeypatch.setattr(onboarding, 'REGISTRY', registry)
    monkeypatch.setattr(onboarding, 'STEPS', {step.id: step for step in registry})
    state = client.get('/api/v1/onboarding').json
    assert {step['id']: step['status'] for step in state['steps']} == {
        'create_account': 'done', 'upload_resume': 'pending', 'connect_agent': 'skipped', 'job_alerts': 'pending'}
    # New and re-offered steps show on the checklist; they never restart guided setup.
    assert state['guided_active'] is False and state['next_step'] is None
    # Steps without a completion fact accept an explicit done.
    result = client.post('/api/v1/onboarding/steps/job_alerts', json={'status': 'done'}, headers=headers)
    assert {step['id']: step['status'] for step in result.json['steps']}['job_alerts'] == 'done'


def test_onboarding_and_profile_are_per_user_and_browser_only(client, app):
    headers = signup(client, app, 'first@example.com')
    assert upload(client, headers).status_code == 201
    client.post('/api/v1/onboarding/steps/connect_agent', json={'status': 'skipped'}, headers=headers)
    client.post('/api/v1/auth/logout', headers=headers)
    other = app.test_client()
    other_headers = signup(other, app, 'second@example.com')
    assert other.get('/api/v1/profile/resume').json == {'resume': None, 'versions': []}
    assert other.get('/api/v1/profile/resume/download').status_code == 404
    assert [step['status'] for step in other.get('/api/v1/onboarding').json['steps']] == ['done', 'pending', 'pending']
    assert other.get('/api/v1/onboarding').json['next_step'] == 'upload_resume'
    token = other.post('/api/v1/agent-tokens', headers=other_headers, json={
        'name': 'Agent', 'current_password': PASSWORD, 'permissions': ['roles:search', 'roles:ingest']}).json['token']
    bearer = app.test_client()
    for authorization in (f'Bearer {token}', 'Bearer test-agent-token'):
        for method, path in (('get', '/api/v1/onboarding'), ('get', '/api/v1/profile/resume'),
                             ('get', '/api/v1/profile/resume/download'), ('post', '/api/v1/profile/resume')):
            assert getattr(bearer, method)(path, headers={'Authorization': authorization}).status_code in (401, 403)
    assert other.post('/api/v1/profile/resume', data={}).status_code == 403  # CSRF


def test_upload_versions_metadata_download_and_storage(client, app, tmp_path):
    headers = signup(client, app)
    first = upload(client, headers, PDF, '../../My Résumé.pdf')
    assert first.status_code == 201
    assert first.json['resume']['version'] == 1 and first.json['resume']['original_filename'] == 'My Résumé.pdf'
    assert 'stored_filename' not in first.json['resume']
    assert first.json['steps'][1]['status'] == 'done'
    assert first.json['guided_active'] is True  # connect_agent still has no row
    same = upload(client, headers, PDF, 'again.pdf')
    assert same.status_code == 200 and same.json['created'] is False and same.json['resume']['version'] == 1
    second = upload(client, headers, docx(), 'cv.docx')
    assert second.status_code == 201 and second.json['resume']['version'] == 2
    listing = client.get('/api/v1/profile/resume').json
    assert listing['resume']['version'] == 2 and [item['version'] for item in listing['versions']] == [2, 1]
    latest = client.get('/api/v1/profile/resume/download')
    assert latest.data == docx() and 'cv.docx' in latest.headers['Content-Disposition']
    old = client.get('/api/v1/profile/resume/download?version=1')
    assert old.data == PDF and old.mimetype == 'application/pdf'
    assert client.get('/api/v1/profile/resume/download?version=x').status_code == 400
    assert client.get('/api/v1/profile/resume/download?version=9').status_code == 404
    user_id = client.get('/api/v1/auth/session').json['user']['id']
    master = tmp_path / 'resumes' / str(user_id) / 'master'
    names = sorted(path.name for path in master.iterdir() if path.is_file())
    assert len(names) == 2 and all(len(name.split('.')[0]) == 32 for name in names)
    assert list((master / '.incoming').iterdir()) == []
    assert not any('Résumé' in path.name or 'cv' in path.name for path in (tmp_path / 'resumes').rglob('*'))


def test_upload_rejections_leave_nothing_behind(client, app, tmp_path):
    headers = signup(client, app)
    assert upload(client, headers, b'not a pdf', 'fake.pdf').status_code == 400
    assert upload(client, headers, docx(extra=[('word/vbaProject.bin', b'\0' * 32)]), 'm.docx').status_code == 400
    too_big = upload(client, headers, b'a' * (5 * 1024 * 1024 + 1), 'big.txt')
    assert too_big.status_code == 413 and too_big.is_json
    huge = upload(client, headers, b'a' * (6 * 1024 * 1024), 'huge.txt')
    assert huge.status_code == 413
    assert client.post('/api/v1/profile/resume', headers=headers, data={},
                       content_type='multipart/form-data').status_code == 400
    assert client.get('/api/v1/profile/resume').json['resume'] is None
    assert statuses(client)['upload_resume'] == 'pending'
    assert [path for path in (tmp_path / 'resumes').rglob('*') if path.is_file()] == []


def test_version_cap(client, app, monkeypatch):
    from jobtracker import profile
    monkeypatch.setattr(profile, 'MAX_VERSIONS', 2)
    headers = signup(client, app)
    assert upload(client, headers, b'one', 'a.txt').status_code == 201
    assert upload(client, headers, b'two', 'b.txt').status_code == 201
    assert upload(client, headers, b'three', 'c.txt').status_code == 409


def test_download_refuses_tampered_storage_names(client, app):
    headers = signup(client, app)
    upload(client, headers)
    with app.app_context():
        engine = app.extensions['jobtracker_db']['engine']
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE master_resumes SET stored_filename='../../test.sqlite3'")
    assert client.get('/api/v1/profile/resume/download').status_code == 404


def test_schema_10_migration_matches_fresh_schema(app, tmp_path):
    from jobtracker import create_app
    from werkzeug.security import generate_password_hash
    fresh = inspect(create_engine(app.config['DATABASE_URL']))
    legacy = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(fresh.bind.url.database)
    connection.execute(f"VACUUM INTO '{legacy}'")
    connection.close()
    downgrade = sqlite3.connect(legacy)
    downgrade.executescript('DROP TABLE tailored_drafts; DROP TABLE harness_grants; DROP TABLE activation_tokens; '
                            'DROP TABLE onboarding_steps; DROP TABLE master_resumes; UPDATE schema_version SET version=9;')
    downgrade.close()
    create_app({'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'test-secret',
                'APP_PASSWORD_HASH': generate_password_hash('test-password'),
                'DATABASE_URL': f'sqlite:///{legacy}', 'UPLOAD_FOLDER': str(tmp_path / 'legacy-uploads')})
    migrated = inspect(create_engine(f'sqlite:///{legacy}'))
    for table in ('onboarding_steps', 'master_resumes'):
        shape = lambda inspector: sorted((c['name'], str(c['type']), c['nullable']) for c in inspector.get_columns(table))
        assert shape(migrated) == shape(fresh)
        assert migrated.get_pk_constraint(table)['constrained_columns'] == fresh.get_pk_constraint(table)['constrained_columns']
    version = sqlite3.connect(legacy).execute('SELECT version FROM schema_version').fetchone()[0]
    assert version == migrations.SCHEMA_VERSION
    assert sqlite3.connect(legacy).execute('SELECT COUNT(*) FROM onboarding_steps').fetchone()[0] == 0
