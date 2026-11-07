"""Resume Agent harness API: service-signed accounts, grant-scoped /me routes, drafts, activation."""
import hashlib
import hmac
import io
import json
import secrets
import sqlite3
import time

import pytest
from legacy import rewind_resume_templates
from sqlalchemy import text
from werkzeug.datastructures import FileStorage
from werkzeug.security import generate_password_hash

from jobtracker import create_app, harness, mailer, migrations, taskqueue
from accounts import verify_all, add_user, last_link, solve_proof

from test_documents import PDF, docx

SECRET = 'harness-service-secret'
FIELDS = {'title': 'Platform Engineer', 'company': 'Acme', 'url': 'https://jobs.example.com/1',
          'salary': '', 'notes': '', 'description': 'Operate Kubernetes platform services.'}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, 'extract_posting', lambda url: {'fields': {**FIELDS, 'url': url}, 'warnings': []})
    return create_app({
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'test-secret',
        'APP_PASSWORD_HASH': generate_password_hash('test-password'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "test.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'resumes'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
        'HARNESS_SERVICE_SECRET': SECRET, 'PUBLIC_BASE_URL': 'https://nextmove.example.com',
    })


@pytest.fixture()
def client(app):
    return app.test_client()


def signed(path, body=b'', secret=SECRET, timestamp=None, nonce=None):
    timestamp = str(int(timestamp if timestamp is not None else time.time()))
    nonce = nonce or secrets.token_hex(16)
    message = f'{timestamp}\n{nonce}\nPOST\n{path}\n{hashlib.sha256(body).hexdigest()}'.encode()
    return {'X-RA-Timestamp': timestamp, 'X-RA-Nonce': nonce,
            'X-RA-Signature': hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()}


def account(client):
    response = client.post('/api/v1/harness/accounts', headers=signed('/api/v1/harness/accounts'))
    assert response.status_code == 201, response.json
    return response.json['user_id'], {'Authorization': 'Bearer ' + response.json['grant']}


def upload_resume(client, grant, data=None, name='resume.docx'):
    return client.post('/api/v1/me/master-resumes', headers=grant,
                       data={'file': (io.BytesIO(data or docx()), name)})


def add_job(client, app, grant):
    created = client.post('/api/v1/me/jobs', headers=grant, json={'url': FIELDS['url']})
    assert created.status_code == 202, created.json
    taskqueue.drain(app)
    return created.json


def save_draft(client, grant, job_id, resume_id, request_id='rq_0001', revision=1, pdf=True, **fields):
    draft = {'request_id': request_id, 'revision': revision, 'job_id': job_id,
             'master_resume_version_id': resume_id, 'bullets': [], 'skills': [], **fields}
    data = {'draft': json.dumps(draft), 'docx': (io.BytesIO(docx()), 'draft.docx')}
    if pdf:
        data['pdf'] = (io.BytesIO(PDF), 'draft.pdf')
    return client.post('/api/v1/me/drafts', headers=grant, data=data)


def test_account_creation_requires_a_valid_fresh_signature(client, app):
    path = '/api/v1/harness/accounts'
    assert client.post(path).status_code == 401
    assert client.post(path, headers=signed(path, secret='wrong')).status_code == 401
    assert client.post(path, headers=signed(path, timestamp=time.time() - 3600)).status_code == 401
    replay = signed(path)
    assert client.post(path, headers=replay).status_code == 201
    assert client.post(path, headers=replay).status_code == 401
    app.config['HARNESS_SERVICE_SECRET'] = None
    assert client.post(path, headers=signed(path)).status_code == 401


def test_messaging_account_has_no_browser_sign_in_and_only_a_grant_hash(client, app):
    user_id, grant = account(client)
    with app.extensions['jobtracker_db']['engine'].connect() as connection:
        user = connection.execute(text('SELECT email, password_hash FROM users WHERE id=:id'), {'id': user_id}).one()
        hashes = connection.execute(text('SELECT token_hash FROM harness_grants')).scalars().all()
    assert tuple(user) == (None, None)
    raw = grant['Authorization'].removeprefix('Bearer ')
    assert hashes == [hashlib.sha256(raw.encode()).hexdigest()]
    # The grant is not a browser or agent credential.
    assert client.get('/api/v1/jobs', headers=grant).status_code == 401
    assert client.get('/api/v1/agent-connection', headers=grant).status_code == 401


def test_master_resume_round_trip_and_validation(client):
    _, grant = account(client)
    created = upload_resume(client, grant)
    assert created.status_code == 201 and created.json['version'] == 1
    again = upload_resume(client, grant)
    assert again.status_code == 200 and again.json['created'] is False
    file = client.get(f"/api/v1/me/master-resumes/{created.json['id']}/file", headers=grant)
    assert file.status_code == 200 and file.data == docx()
    assert file.headers['X-Original-Filename'] == 'resume.docx'
    bad = upload_resume(client, grant, data=b'not a docx', name='resume.docx')
    assert bad.status_code == 400


def test_harness_master_uploads_follow_a_promoted_default(client, app):
    user_id, grant = account(client)
    first = upload_resume(client, grant).json
    with app.app_context():
        from jobtracker import resume_files, resume_templates
        other = resume_templates.create_template(user_id, {'name': 'Engineering manager'})['id']
        resume_files.save_upload(user_id, 'master', FileStorage(io.BytesIO(b'EM v1'), filename='em.txt'), other)
        resume_templates.update_template(user_id, other, {'is_default': True})
    second = upload_resume(client, grant, data=b'Engineering manager resume', name='resume.txt').json
    assert (second['version'], second['created']) == (2, True)
    # Drafts may start from any template's version, not just the default's.
    assert save_draft(client, grant, add_job(client, app, grant)['id'], first['id']).status_code == 201


def test_job_link_is_extracted_on_the_queue(client, app):
    _, grant = account(client)
    job = add_job(client, app, grant)
    assert job['posting'] is None and job['extraction_id']
    status = client.get(f"/api/v1/me/extractions/{job['extraction_id']}", headers=grant).json
    assert status['status'] == 'succeeded'
    filled = client.get(f"/api/v1/me/jobs/{job['id']}", headers=grant).json
    assert filled['title'] == 'Platform Engineer' and filled['posting'] == FIELDS['description']
    assert filled['stage'] == 'needs_review'


def test_unreadable_posting_fails_and_a_pasted_one_is_saved(client, app, monkeypatch):
    def unreadable(url):
        raise harness.services.ServiceError('No role details were found.', 422)
    monkeypatch.setattr(harness, 'extract_posting', unreadable)
    _, grant = account(client)
    job = add_job(client, app, grant)
    status = client.get(f"/api/v1/me/extractions/{job['extraction_id']}", headers=grant).json
    assert status['status'] == 'failed' and 'No role details' in status['error']
    pasted = client.put(f"/api/v1/me/jobs/{job['id']}/posting", headers=grant, json={'text': 'Pasted posting'})
    assert pasted.status_code == 200 and pasted.json['posting'] == 'Pasted posting'
    assert client.put(f"/api/v1/me/jobs/{job['id']}/posting", headers=grant, json={'text': ' '}).status_code == 400


def test_drafts_are_idempotent_and_only_approval_files_them(client, app):
    _, grant = account(client)
    resume = upload_resume(client, grant).json
    job = add_job(client, app, grant)
    first = save_draft(client, grant, job['id'], resume['id'])
    assert first.status_code == 201 and first.json['approved'] is False
    redelivered = save_draft(client, grant, job['id'], resume['id'])
    assert redelivered.status_code == 200 and redelivered.json['id'] == first.json['id']
    with app.app_context():
        from jobtracker import services
        user_id = harness.authenticate_grant(grant['Authorization'][7:], 'jobs:read')
        assert services.list_resumes(user_id, job['id']) == []  # unapproved: nothing exported
        approved = client.post(f"/api/v1/me/drafts/{first.json['id']}/approve", headers=grant)
        assert approved.status_code == 200 and approved.json['approved'] is True
        resumes = services.list_resumes(user_id, job['id'])
        assert sorted(r['original_filename'] for r in resumes) == ['tailored-resume-r1.docx', 'tailored-resume-r1.pdf']
        assert [r['source']['resume_id'] for r in resumes] == [resume['id']] * 2  # The draft's template version.
        again = client.post(f"/api/v1/me/drafts/{first.json['id']}/approve", headers=grant)
        assert again.status_code == 200 and len(services.list_resumes(user_id, job['id'])) == 2
        assert services.get_job(user_id, job['id'])['stage'] == 'needs_review'  # stage is the user's call


def test_approval_files_drafts_under_the_harness_file_stem(client, app):
    _, grant = account(client)
    resume = upload_resume(client, grant).json
    job = add_job(client, app, grant)
    for bad in ('../escape', 'a/b', 'name.docx', '', 'x' * 121):
        assert save_draft(client, grant, job['id'], resume['id'], file_stem=bad).status_code == 400
    draft = save_draft(client, grant, job['id'], resume['id'], file_stem='NicolasMelo_Resume_Acme_SrEng').json
    client.post(f"/api/v1/me/drafts/{draft['id']}/approve", headers=grant)
    with app.app_context():
        from jobtracker import services
        user_id = harness.authenticate_grant(grant['Authorization'][7:], 'jobs:read')
        assert sorted(r['original_filename'] for r in services.list_resumes(user_id, job['id'])) == [
            'NicolasMelo_Resume_Acme_SrEng.docx', 'NicolasMelo_Resume_Acme_SrEng.pdf']


def test_draft_needs_an_owned_job_and_resume_and_a_docx(client, app):
    _, grant = account(client)
    _, other = account(client)
    resume = upload_resume(client, grant).json
    job = add_job(client, app, grant)
    assert save_draft(client, other, job['id'], resume['id']).status_code == 404
    response = client.post('/api/v1/me/drafts', headers=grant, data={'draft': json.dumps(
        {'request_id': 'rq_9', 'revision': 1, 'job_id': job['id'], 'master_resume_version_id': resume['id']})})
    assert response.status_code == 400
    assert client.post('/api/v1/me/drafts', headers=grant, data={'draft': '[1]'}).status_code == 400


def test_revoked_grant_is_refused(client, app):
    user_id, grant = account(client)
    with app.app_context():
        harness.revoke_grants(user_id)
    assert upload_resume(client, grant).status_code == 401


def merge(app, *args):
    return app.test_cli_runner().invoke(args=['user', 'merge-messaging', *map(str, args)])


def test_merge_gives_a_messaging_accounts_grant_to_an_existing_account(client, app):
    source, grant = account(client)
    activation_token(client, grant)
    result = merge(app, source, 1)
    assert result.exit_code == 0, result.output
    assert 'Moved 1 harness grant(s)' in result.output
    assert upload_resume(client, grant).status_code == 201
    with app.app_context():
        engine = app.extensions['jobtracker_db']['engine']
        with engine.connect() as connection:
            assert connection.execute(text('SELECT user_id FROM master_resumes')).scalars().all() == [1]
            assert connection.scalar(text('SELECT count(*) FROM users WHERE id = :id'), {'id': source}) == 0
            assert connection.scalar(text('SELECT count(*) FROM activation_tokens')) == 0
    link = client.post('/api/v1/me/activation-links', headers=grant)
    assert link.json == {'url': 'https://nextmove.example.com/', 'activated': True}


def test_merge_refuses_accounts_with_data_or_web_sign_in(client, app):
    source, grant = account(client)
    upload_resume(client, grant)
    for args, message in (((source, 1), 'already has data in resume_templates'),
                          ((1, source), 'has web sign-in'), ((source, source), 'two different'),
                          ((source, 99), 'No account with ID 99')):
        result = merge(app, *args)
        assert result.exit_code != 0 and message in result.output, result.output
    with app.app_context():
        with app.extensions['jobtracker_db']['engine'].connect() as connection:
            assert connection.execute(text('SELECT user_id FROM harness_grants')).scalars().all() == [source]


def test_https_is_required_outside_development_unless_allowed(client, app):
    _, grant = account(client)
    app.config['APP_ENV'] = 'production'
    assert client.get('/api/v1/me/jobs/x', headers=grant).status_code == 403
    app.config['HARNESS_ALLOW_HTTP'] = True
    assert client.get('/api/v1/me/jobs/x', headers=grant).status_code == 404


def activation_token(client, grant):
    link = client.post('/api/v1/me/activation-links', headers=grant)
    assert link.status_code == 201 and link.json['activated'] is False
    assert link.json['url'].startswith('https://nextmove.example.com/?activate=')
    return link.json['url'].split('activate=')[1]


def activate(client, token, email='chat@example.com', password='chat-password-1'):
    return client.post('/api/v1/auth/activate', json={'token': token, 'email': email, 'password': password,
                                                      'password_confirmation': password,
                                                      'proof': solve_proof(client, 'activate')})


def user_row(app, user_id):
    with app.extensions['jobtracker_db']['engine'].connect() as connection:
        return connection.execute(text('SELECT email, password_hash, email_verified_at FROM users WHERE id=:id'),
                                  {'id': user_id}).one()


def test_activation_binds_the_email_only_once_confirmed_and_then_signs_in(client, app):
    user_id, grant = account(client)
    token = activation_token(client, grant)
    # Link previews and scanners only read; reading never consumes the link.
    assert client.get('/api/v1/auth/activation', query_string={'token': token}).json == {'valid': True}
    assert client.get('/api/v1/auth/activation', query_string={'token': token}).json == {'valid': True}
    done = activate(client, token)
    assert done.status_code == 202 and 'Set-Cookie' in done.headers
    assert client.get('/api/v1/auth/session').json['authenticated'] is False
    # Still messaging-only: the chosen email is not bound until the link is confirmed; the harness still works.
    assert user_row(app, user_id) == (None, None, None)
    assert client.post('/api/v1/me/activation-links', headers=grant).json['activated'] is False
    assert client.get('/api/v1/auth/activation', query_string={'token': token}).json == {'valid': False}
    assert activate(app.test_client(), token, email='other@example.com').status_code == 410
    taskqueue.drain(app)
    confirmed = client.post('/api/v1/auth/email-verification/confirm', json={'token': last_link('chat@example.com')})
    assert confirmed.status_code == 200 and confirmed.json['user']['email'] == 'chat@example.com'
    email, password_hash, verified_at = user_row(app, user_id)
    assert email == 'chat@example.com' and password_hash and verified_at
    fresh = app.test_client()
    assert fresh.post('/api/v1/auth/login', json={'email': 'chat@example.com',
                                                  'password': 'chat-password-1'}).status_code == 200
    # An activated account gets the dashboard URL, not a new link.
    assert client.post('/api/v1/me/activation-links', headers=grant).json == {
        'url': 'https://nextmove.example.com/', 'activated': True}


def test_activation_never_reveals_taken_emails_and_rejects_expired_and_unknown_links(client, app):
    user_id, grant = account(client)
    token = activation_token(client, grant)
    # An address that already has an account gets the same 202; the worker mails a notice instead of a link.
    assert activate(client, token, email='taken@example.com').status_code == 202
    add_user(app, 'taken@example.com')
    taskqueue.drain(app)
    notice = mailer.outbox[-1]
    assert notice['To'] == 'taken@example.com' and '#token=' not in notice.get_body(('plain',)).get_content()
    assert user_row(app, user_id) == (None, None, None)
    token = activation_token(client, grant)
    assert activate(client, 'x' * 40).status_code == 410
    with app.extensions['jobtracker_db']['engine'].begin() as connection:
        connection.execute(text("UPDATE activation_tokens SET expires_at='2000-01-01 00:00:00'"))
    assert activate(client, token).status_code == 410


def test_a_victim_cannot_confirm_an_activation_someone_else_started(client, app):
    """The attacker's phone activates with the victim's address; the victim opens the link without the password."""
    user_id, grant = account(client)
    attacker = app.test_client()
    assert activate(attacker, activation_token(client, grant), email='victim@example.com').status_code == 202
    taskqueue.drain(app)
    victim = app.test_client()
    link = last_link('victim@example.com')
    check = victim.post('/api/v1/auth/email-verification/check', json={'token': link}).json
    assert check['valid'] and check['needs_password'] and check['kind'] == 'activation'
    assert victim.post('/api/v1/auth/email-verification/confirm', json={'token': link}).json['code'] == 'password_required'
    for _ in range(5):
        assert victim.post('/api/v1/auth/email-verification/confirm',
                           json={'token': link, 'password': 'a-guess-123'}).json['code'] == 'wrong_password'
    assert victim.post('/api/v1/auth/email-verification/confirm',
                       json={'token': link, 'password': 'chat-password-1'}).status_code == 410  # Dead after 5.
    assert user_row(app, user_id) == (None, None, None)


def test_schema_11_upgrade_keeps_users_and_the_id_sequence(app, tmp_path):
    engine = app.extensions['jobtracker_db']['engine']
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, email, password_hash, session_version, owns_legacy_tokens, "
                                "created_at) VALUES (7, 'seven@example.com', 'hash', 3, 0, '2026-01-01 00:00:00')"))
        connection.execute(text('DELETE FROM users WHERE id=7'))
    path = app.config['DATABASE_URL'].removeprefix('sqlite:///')
    engine.dispose()
    with sqlite3.connect(path) as connection:
        rewind_resume_templates(connection)
        connection.executescript(
            'PRAGMA foreign_keys=OFF; DROP TABLE oauth_tokens; DROP TABLE oauth_codes; DROP TABLE oauth_grants; '
            'DROP TABLE oauth_clients; DROP TABLE tailored_drafts; DROP TABLE harness_grants; '
            'DROP TABLE activation_tokens; ALTER TABLE users RENAME TO users_v11; '
            'CREATE TABLE users (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, email VARCHAR(254) NOT NULL UNIQUE, '
            "password_hash VARCHAR(512) NOT NULL, session_version INTEGER NOT NULL, owns_legacy_tokens BOOLEAN DEFAULT '0' "
            'NOT NULL, created_at DATETIME NOT NULL, CHECK (session_version > 0)); '
            'INSERT INTO users SELECT * FROM users_v11; DROP TABLE users_v11; '
            "UPDATE sqlite_sequence SET seq=7 WHERE name='users'; UPDATE schema_version SET version=10;")
    upgraded = create_app(dict(app.config))
    with upgraded.extensions['jobtracker_db']['engine'].begin() as connection:
        assert connection.execute(text('SELECT version FROM schema_version')).scalar() == migrations.SCHEMA_VERSION
        assert connection.execute(text('SELECT id, email, session_version FROM users')).all() == [(1, 'owner@localhost', 1)]
        new_id = connection.execute(text("INSERT INTO users (email, password_hash, session_version, created_at) "
                                         "VALUES (NULL, NULL, 1, '2026-01-01 00:00:00') RETURNING id")).scalar()
    assert new_id == 8  # the deleted account's ID 7 is never reissued
    verify_all(upgraded)  # Schema 15: upgraded accounts are unverified until the operator (or a link) verifies them.
    login = upgraded.test_client().post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'})
    assert login.status_code == 200


def test_draft_metadata_with_an_unpaired_surrogate_is_refused(client, app):
    _, grant = account(client)
    resume = upload_resume(client, grant).json
    job = add_job(client, app, grant)
    response = save_draft(client, grant, job['id'], resume['id'], request_id='rq_\ud800')
    assert response.status_code == 400 and response.json == {'error': 'draft must be a JSON object'}
    assert save_draft(client, grant, job['id'], resume['id'], note='\U0001F600').status_code == 201


def test_templates_list_every_template_with_a_readable_latest_version(client, app):
    user_id, grant = account(client)
    assert client.get('/api/v1/me/templates', headers=grant).json == {'templates': []}
    main = upload_resume(client, grant).json
    with app.app_context():
        from jobtracker import resume_files, resume_templates
        other = resume_templates.create_template(user_id, {'name': 'Engineering manager', 'description': 'EM roles'})['id']
        resume_files.save_upload(user_id, 'master', FileStorage(io.BytesIO(b'EM v1'), filename='em.txt'), other)
        resume_files.save_upload(user_id, 'master', FileStorage(io.BytesIO(b'EM v2'), filename='em.txt'), other)
    templates = client.get('/api/v1/me/templates', headers=grant).json['templates']
    assert [(t['name'], t['is_default'], t['version_count']) for t in templates] == \
        [('Main resume', True, 1), ('Engineering manager', False, 2)]
    assert templates[0]['latest']['id'] == main['id'] and templates[1]['description'] == 'EM roles'
    # The listing never exposes where files are stored, and each latest version downloads through the grant.
    assert 'stored_filename' not in json.dumps(templates)
    latest = templates[1]['latest']
    assert latest['version'] == 2
    assert client.get(f"/api/v1/me/master-resumes/{latest['id']}/file", headers=grant).data == b'EM v2'


def test_templates_list_needs_a_live_grant(client, app):
    user_id, grant = account(client)
    upload_resume(client, grant)
    assert client.get('/api/v1/me/templates').status_code == 401
    assert client.get('/api/v1/me/templates', headers={'Authorization': 'Bearer nm_harness_forged'}).status_code == 401
    with app.app_context():
        harness.revoke_grants(user_id)
    assert client.get('/api/v1/me/templates', headers=grant).status_code == 401


def test_activation_uses_the_per_address_budget_too(client, app):
    """A chat user minting activation links cannot mail one address more than sign-up could."""
    _, grant = account(client)
    mailer.outbox.clear()
    for index in range(4):
        response = activate(client, activation_token(client, grant), email=f'target+{index}@example.com')
        assert response.status_code == 202  # Same answer when over budget; the link is still used up.
        taskqueue.drain(app)
    with app.extensions['jobtracker_db']['engine'].connect() as connection:
        used_links = connection.execute(text('SELECT COUNT(*) FROM activation_tokens WHERE used_at IS NOT NULL')).scalar()
    sent = [message for message in mailer.outbox if message['To'].startswith('target+')]
    assert len(sent) == 3 and used_links == 4  # The canonical address (no +tag) got its 3 for the day.
