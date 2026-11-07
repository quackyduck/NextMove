"""Data deletion and export (schema 17): self-service deletes, operator account deletion, export, ledger, sweeper."""
from contextlib import closing
from datetime import timedelta
import hashlib
import io
import json
import logging
import os
import sqlite3
import time
import zipfile

import pytest
from sqlalchemy import insert, inspect, select, text, update
from werkzeug.security import generate_password_hash

from jobtracker import create_app, credentials, deletion, harness, mailer, migrations, password_reset, taskqueue
from jobtracker.db import Base
from jobtracker.models import (ActivationToken, Activity, Contact, DataExport, Deletion, EmailConfirmation,
                               ExtractionTask, HarnessGrant, IngestionRequest, InviteCode, Job, MasterResume,
                               QueuedTask, Resume, ResumeTemplate, TailoredDraft, User, new_id, now)

from accounts import add_user, sign_in
from legacy import database_path, rewind_data_deletion
from test_password_reset import agents_live, connect_agents, harness_live

PASSWORD = 'test-password'
OWNER_B = 'other@example.com'


@pytest.fixture
def config(tmp_path, monkeypatch):
    for name in list(mailer.SETTINGS):
        monkeypatch.delenv(name, raising=False)
    return {
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'deletion-test-secret-0123456789abcdef',
        'APP_PASSWORD_HASH': generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "deletion.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
        'PUBLIC_BASE_URL': 'https://example.test', 'APP_ROOT_PATH': '', 'MAIL_BACKEND': 'memory',
        'MAIL_REPLY_TO': 'support@example.test', 'BOOTSTRAP_ADMIN_EMAIL': 'owner@example.com',
    }


@pytest.fixture
def app(config):
    mailer.outbox.clear()
    yield create_app(config)
    mailer.outbox.clear()


@pytest.fixture
def client(app):
    return app.test_client()


def engine(app):
    return app.extensions['jobtracker_db']['engine']


def root(app, user_id):
    return os.path.join(app.config['UPLOAD_FOLDER'], str(user_id))


def files_of(app, user_id):
    base = root(app, user_id)
    return sorted(os.path.relpath(os.path.join(d, f), base) for d, _, fs in os.walk(base) for f in fs) \
        if os.path.isdir(base) else []


def upload(client, headers, path, data, name='r.txt', **form):
    return client.post(path, headers=headers, data={'file': (io.BytesIO(data), name), **form})


def populate(app, client, email, password=PASSWORD):
    """A verified account with a role (contact, note, resume, tailored resume, draft), two templates with versions,
    and every kind of credential and pending row. Returns ids and the signed-in CSRF headers."""
    user_id = add_user(app, email, password=password)
    headers = sign_in(client, email, password)
    job = client.post('/api/v1/jobs', headers=headers, json={'title': 'Canary role', 'company': 'Canary Co',
                                                              'notes': f'canary-note-{email}'}).json
    client.post(f"/api/v1/jobs/{job['id']}/contacts", headers=headers, json={'name': 'Recruiter'})
    main = upload(client, headers, '/api/v1/profile/resume', b'Main resume v1 canary\n').json['resume']
    other = client.post('/api/v1/templates', headers=headers, json={'name': 'Manager'}).json
    manager = upload(client, headers, f"/api/v1/templates/{other['id']}/versions", b'Manager v1\n').json['resume']
    upload(client, headers, f"/api/v1/templates/{other['id']}/versions", b'Manager v2\n')
    plain = upload(client, headers, f"/api/v1/jobs/{job['id']}/resumes", b'Plain job resume\n').json
    tailored = upload(client, headers, f"/api/v1/jobs/{job['id']}/resumes", b'Tailored from manager v1\n',
                      'tailored.txt', source_resume_id=manager['id']).json
    drafts = os.path.join(root(app, user_id), 'drafts')
    os.makedirs(drafts, exist_ok=True)
    with open(os.path.join(drafts, f'draft-{user_id}.docx'), 'wb') as handle:
        handle.write(b'draft bytes')
    access, agent = connect_agents(app, user_id)
    with engine(app).begin() as connection:
        connection.execute(insert(TailoredDraft).values(
            id=new_id(), user_id=user_id, job_id=job['id'], master_resume_id=manager['id'], request_id=f'r{user_id}',
            revision=1, draft={}, docx_filename=f'draft-{user_id}.docx', created_at=now()))
        grant = harness.create_grant(connection, user_id)
        connection.execute(insert(ActivationToken).values(id=new_id(), user_id=user_id, token_hash='a' * 63 + str(user_id % 10),
                                                          created_at=now(), expires_at=now() + timedelta(days=1)))
        connection.execute(insert(ExtractionTask).values(id=new_id(), user_id=user_id,
                                                         status='succeeded', result={}, created_at=now()))
        connection.execute(insert(IngestionRequest).values(
            id=new_id(), user_id=user_id, idempotency_key=f'canary-key-{user_id}-' + 'k' * 16, url='https://x.test/p',
            status='succeeded', result={'job_id': job['id']}, created_at=now()))
        connection.execute(insert(EmailConfirmation).values(
            id=new_id(), kind='existing', email=email, user_id=user_id, password_attempts=0, created_at=now(),
            delivery='sent'))
        connection.execute(insert(EmailConfirmation).values(
            id=new_id(), kind='signup', email=email, password_hash='x', password_attempts=0, created_at=now(),
            delivery='queued'))
        connection.execute(insert(InviteCode).values(code_hash=hashlib.sha256(email.encode()).hexdigest(), prefix='abc',
                                                     created_at=now(), expires_at=now() + timedelta(days=1),
                                                     used_at=now(), used_by=user_id))
        taskqueue.enqueue(connection, None, 'password_reset_request', {'email': email})
        password_reset.mint_reset_token(connection, user_id, 60, 'operator')
    return {'user_id': user_id, 'headers': headers, 'job': job, 'main': main, 'other': other, 'manager': manager,
            'plain': plain, 'tailored': tailored, 'access': access, 'agent': agent, 'grant': grant}


def code(app, user_id, purpose='delete'):
    with app.app_context():
        return deletion.issue_support_code(user_id, purpose)


def delete_cli(app, user_id, *extra, purpose='delete', with_code=True):
    args = ['user', 'delete', '--id', str(user_id), '--yes', *extra]
    if with_code:
        args += ['--code', code(app, user_id, purpose)]
    return app.test_cli_runner().invoke(args=args)


def scan(app, needle):
    """Every row of every table, as text; and the raw database bytes."""
    hits = []
    with engine(app).connect() as connection:
        for table in inspect(connection).get_table_names():
            for row in connection.execute(text(f'SELECT * FROM "{table}"')):
                if needle in repr(row):
                    hits.append(table)
    return hits


def raw_bytes(app):
    path = database_path(app)
    data = b''
    for suffix in ('', '-wal'):
        if os.path.exists(str(path) + suffix):
            with open(str(path) + suffix, 'rb') as handle:
                data += handle.read()
    return data


# Coverage guards -----------------------------------------------------------------
def test_every_table_and_file_column_is_handled():
    owned = {'users'}
    changed = True
    while changed:  # Tables referencing users, directly or through tables that do.
        changed = False
        for table in Base.metadata.sorted_tables:
            if table.name not in owned and any(fk.column.table.name in owned for fk in table.foreign_keys):
                owned.add(table.name)
                changed = True
    assert owned - set(deletion.ACCOUNT_TABLES) == set(), 'add the table to deletion.ACCOUNT_TABLES'
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.name.endswith(('filename', 'stored_name')) and column.name != 'original_filename':
                assert (table.name, column.name) in deletion.FILE_COLUMNS, (table.name, column.name)


def test_destructive_routes_are_browser_only(app):
    destructive = {rule.rule: rule for rule in app.url_map.iter_rules()
                   if getattr(app.view_functions[rule.endpoint], 'destructive', False)}
    assert len(destructive) == 6
    for rule in app.url_map.iter_rules():
        looks = 'DELETE' in rule.methods or 'delet' in rule.rule
        if rule.rule.startswith('/api/') and looks:
            assert rule.rule in destructive, f'{rule.rule} deletes but is not marked @destructive'
    from jobtracker import mcp
    assert not [name for name in mcp.TOOLS if 'delete' in name or 'remove' in name]
    assert "can't delete anything" in mcp.INSTRUCTIONS
    spec = json.dumps(app.test_client().get('/api/v1/openapi.json').json).lower()
    assert '"delete"' not in spec and '/deletion' not in spec


# Account deletion -----------------------------------------------------------------
def test_user_delete_removes_everything_and_nothing_else(app, client, caplog):
    caplog.set_level(logging.INFO, logger='jobtracker.accounts')
    victim = populate(app, client, 'victim@example.com')
    keep = populate(app, app.test_client(), OWNER_B)
    with engine(app).connect() as connection:
        before_other = {table: connection.execute(text(f'SELECT * FROM "{table}"')).fetchall()
                        for table in inspect(connection).get_table_names()}
        usage = connection.execute(text('SELECT * FROM mail_usage')).fetchall()
    other_files = files_of(app, keep['user_id'])
    assert files_of(app, victim['user_id'])
    result = app.test_cli_runner().invoke(args=['user', 'delete', '--id', str(victim['user_id']), '--code',
                                                code(app, victim['user_id'])], input=f"{victim['user_id']}\n")
    assert result.exit_code == 0, result.output
    assert 'resume-agent tenant delete --jobtracker-user %d' % victim['user_id'] in result.output
    assert 'The "account deleted" email is queued.' in result.output
    assert not os.path.exists(root(app, victim['user_id']))
    assert files_of(app, keep['user_id']) == other_files
    with engine(app).connect() as connection:
        assert connection.scalar(select(User.id).where(User.id == victim['user_id'])) is None
        assert connection.scalar(select(InviteCode.used_at).where(InviteCode.prefix == 'abc')
                                 .where(InviteCode.used_by.is_(None))) is not None
        assert connection.execute(text('SELECT * FROM mail_usage')).fetchall() == usage
        ledger = connection.execute(select(Deletion.kind, Deletion.user_id)).all()
    assert ledger == [('account', victim['user_id'])]
    taskqueue.drain(app)
    (notice,) = [m for m in mailer.outbox if m['To'] == 'victim@example.com' and 'deleted' in m['Subject']]
    assert 'http' not in notice.get_body(('plain',)).get_content()  # No links that act.
    assert scan(app, 'victim@example.com') == []
    assert scan(app, 'canary-note-victim@example.com') == []
    assert scan(app, f'canary-note-{OWNER_B}') == ['jobs']  # The other account keeps its own.
    with engine(app).connect() as connection:
        for table, rows in before_other.items():
            if table in ('deletions', 'queued_tasks', 'mail_usage', 'invite_codes'):
                continue
            current = connection.execute(text(f'SELECT * FROM "{table}"')).fetchall()
            assert [r for r in rows if r in current] == [r for r in rows if r in current]
    assert agents_live(app, victim['access'], victim['agent']) == (False, False)
    assert not harness_live(app, victim['grant'])
    assert agents_live(app, keep['access'], keep['agent']) == (True, True) and harness_live(app, keep['grant'])
    assert client.get('/api/v1/auth/session').json['authenticated'] is False
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('account deleted')]
    assert len(lines) == 1 and f"user_id={victim['user_id']}" in lines[0] and 'victim' not in lines[0]


def test_deleted_rows_do_not_survive_in_the_raw_database(app, client):
    victim = populate(app, client, 'rawcheck@example.com')
    delete_cli(app, victim['user_id'])
    taskqueue.drain(app)
    with app.app_context():
        deletion.checkpoint()
    data = raw_bytes(app)
    assert b'rawcheck@example.com' not in data and b'canary-note-' not in data


def test_user_delete_guards(app, client):
    runner = app.test_cli_runner()
    owner = delete_cli(app, 1)
    assert owner.exit_code != 0 and 'owner' in owner.output
    victim = populate(app, client, 'guard@example.com')
    assert runner.invoke(args=['user', 'delete', 'guard@example.com', '--yes', '--code',
                               code(app, victim['user_id'])]).exit_code != 0  # --yes needs --id
    wrong = runner.invoke(args=['user', 'delete', 'guard@example.com', '--code', code(app, victim['user_id'])],
                          input='999\n')
    assert wrong.exit_code != 0 and 'nothing was deleted' in wrong.output
    with engine(app).connect() as connection:
        assert connection.scalar(select(User.id).where(User.id == victim['user_id'])) is not None
    assert delete_cli(app, victim['user_id']).exit_code == 0
    again = delete_cli(app, victim['user_id'], with_code=False)
    assert again.exit_code == 0 and 'already deleted' in again.output
    assert delete_cli(app, 1, '--allow-owner').exit_code == 0


def test_support_codes_are_bound_to_one_account_and_purpose_and_used_once(app, client):
    runner = app.test_cli_runner()
    a = add_user(app, 'a@example.com')
    b = add_user(app, 'b@example.com')
    missing = runner.invoke(args=['user', 'delete', '--id', str(b), '--yes'])
    assert missing.exit_code != 0 and '--code' in missing.output  # --yes never waives the code.
    for_a = code(app, a)
    other = runner.invoke(args=['user', 'delete', '--id', str(b), '--yes', '--code', for_a])
    assert other.exit_code != 0 and 'another account' in other.output
    for_export = code(app, b, 'export')
    assert runner.invoke(args=['user', 'delete', '--id', str(b), '--yes', '--code', for_export]).exit_code != 0
    with engine(app).connect() as connection:
        assert connection.scalar(select(func_count(User))) == 3  # The owner, a and b: nothing was deleted.
    check = runner.invoke(args=['support', 'check', for_a])
    assert check.exit_code == 0 and f'delete account {a}' in check.output
    assert runner.invoke(args=['user', 'delete', '--id', str(a), '--yes', '--code', for_a.lower()]).exit_code == 0
    reuse = runner.invoke(args=['support', 'check', for_a])
    assert reuse.exit_code != 0 and 'used' in reuse.output
    with engine(app).begin() as connection:
        from jobtracker.models import SupportCode
        connection.execute(update(SupportCode).values(expires_at=now() - timedelta(seconds=1)))
    assert runner.invoke(args=['user', 'export', '--id', str(b), '--code', for_export]).exit_code != 0  # expired
    assert runner.invoke(args=['support', 'code', '--id', '999', '--for', 'delete']).exit_code != 0
    assert runner.invoke(args=['support', 'check', 'MNM-AAAA-BBBB-CCCC']).exit_code != 0


def func_count(model):
    from sqlalchemy import func
    return func.count(model.id)


def test_unverified_accounts_get_no_deletion_email(app, client):
    user_id = add_user(app, 'legacy@example.com', verified=False)
    result = delete_cli(app, user_id)
    assert 'address unverified' in result.output
    taskqueue.drain(app)
    assert mailer.outbox == []


def test_a_failed_delete_changes_nothing(app, client, monkeypatch):
    victim = populate(app, client, 'atomic@example.com')
    files = files_of(app, victim['user_id'])
    original = deletion._ledger

    def broken(connection, kind, user_id, object_id=None):
        original(connection, kind, user_id, object_id)
        raise RuntimeError('crash before commit')
    monkeypatch.setattr(deletion, '_ledger', broken)
    issued = code(app, victim['user_id'])
    with app.app_context(), pytest.raises(RuntimeError):
        deletion.delete_account(victim['user_id'], code=issued)
    with app.app_context():
        assert deletion.code_status(issued, victim['user_id'], 'delete') == 'ok'  # The rollback kept it usable.
    with engine(app).connect() as connection:
        assert connection.scalar(select(Job.id).where(Job.user_id == victim['user_id'])) is not None
        assert connection.scalar(select(Deletion.id)) is None
    assert files_of(app, victim['user_id']) == files


# Self-service -----------------------------------------------------------------------
def test_role_delete(app, client):
    data = populate(app, client, 'role@example.com')
    job, headers = data['job'], data['headers']
    preview = client.get(f"/api/v1/jobs/{job['id']}/deletion").json
    assert preview == {'contacts': 1, 'activities': preview['activities'], 'resumes': 2, 'drafts': 1}
    assert client.delete(f"/api/v1/jobs/{job['id']}", json={'version': 1}).status_code == 403  # CSRF required
    stale = client.delete(f"/api/v1/jobs/{job['id']}", headers=headers, json={'version': job['version'] + 5})
    assert stale.status_code == 409
    with engine(app).begin() as connection:
        connection.execute(insert(ExtractionTask).values(id='fill-task', user_id=data['user_id'],
                                                         status='running', result={}, created_at=now()))
        taskqueue.enqueue(connection, data['user_id'], 'harness_fill_job',
                          {'task_id': 'fill-task', 'job_id': job['id'], 'url': 'https://x.test'})
    current = client.get(f"/api/v1/jobs/{job['id']}").json['version']
    response = client.delete(f"/api/v1/jobs/{job['id']}", headers=headers, json={'version': current})
    assert response.status_code == 200 and response.json['deleted']['resumes'] == 2
    assert client.get(f"/api/v1/jobs/{job['id']}").status_code == 404
    assert client.get(f"/api/v1/resumes/{data['plain']['id']}/download").status_code == 404
    with engine(app).connect() as connection:
        for model in (Contact, Activity, Resume, TailoredDraft):
            assert connection.scalar(select(model.id).where(model.job_id == job['id'])) is None
        receipt = connection.execute(select(IngestionRequest.url, IngestionRequest.result)).one()
        task = connection.execute(select(QueuedTask.status, QueuedTask.last_error).where(
            QueuedTask.kind == 'harness_fill_job')).one()
        assert connection.execute(select(Deletion.kind, Deletion.object_id)).all() == [('job', job['id'])]
    assert receipt == ('', {'status': 'deleted'}) and task == ('failed', 'Role deleted')
    left = files_of(app, data['user_id'])
    assert not [name for name in left if name.startswith('drafts/')] and len(left) == 3  # The 3 template versions.


def test_role_replay_of_an_ingestion_answers_deleted(app, client, monkeypatch):
    from jobtracker import ingestion
    user_id = add_user(app, 'ingest@example.com')
    with engine(app).begin() as connection:
        token_id = new_id()
        from jobtracker.models import AgentToken
        connection.execute(insert(AgentToken).values(id=token_id, user_id=user_id, name='bot', prefix='p',
                                                     token_hash='f' * 64, permissions=['roles:ingest'],
                                                     created_at=now(), expires_at=now() + timedelta(days=1)))
        job_id = new_id()
        connection.execute(insert(Job).values(id=job_id, user_id=user_id, title='Gone', version=1,
                                              found_date=now().date()))
        connection.execute(insert(IngestionRequest).values(
            id=new_id(), user_id=user_id, idempotency_key='replayed-key-' + 'k' * 16, token_id=token_id,
            url='https://x.test/posting', status='succeeded', result={'job_id': job_id}, created_at=now()))
    with app.app_context():
        deletion.delete_job(user_id, job_id, via='replay')
    from flask import g
    with app.test_request_context():
        g.agent_token = {'id': token_id}
        replayed = ingestion.start_ingestion(user_id, 'https://x.test/posting', 'replayed-key-' + 'k' * 16)
    assert replayed['status'] == 'deleted'
    with engine(app).connect() as connection:
        assert connection.scalar(select(Job.id).where(Job.user_id == user_id)) is None


def test_resume_delete_removes_an_approved_drafts_hidden_copy(app, client):
    data = populate(app, client, 'resume@example.com')
    drafts = os.path.join(root(app, data['user_id']), 'drafts')
    with open(os.path.join(drafts, 'old-approved.docx'), 'wb') as handle:
        handle.write(b'Plain job resume\n')  # An approval from before approvals dropped their copies.
    with engine(app).begin() as connection:
        connection.execute(insert(TailoredDraft).values(
            id='approved-draft', user_id=data['user_id'], job_id=data['job']['id'],
            master_resume_id=data['manager']['id'], request_id='old', revision=1, draft={},
            docx_filename='old-approved.docx', created_at=now(), approved_at=now()))
    response = client.delete(f"/api/v1/resumes/{data['plain']['id']}", headers=data['headers'])
    assert response.json['deleted'] == {'resumes': 1, 'drafts': 1}
    assert not os.path.exists(os.path.join(drafts, 'old-approved.docx'))
    with engine(app).connect() as connection:
        assert connection.scalar(select(TailoredDraft.id).where(TailoredDraft.id == 'approved-draft')) is None
        assert connection.scalar(select(Resume.id).where(Resume.id == data['tailored']['id'])) is not None


def test_version_delete_keeps_tailored_resumes_and_never_renumbers(app, client):
    data = populate(app, client, 'version@example.com')
    template, manager, headers = data['other']['id'], data['manager']['id'], data['headers']
    elsewhere = client.delete(f"/api/v1/templates/{data['main']['template_id']}/versions/{manager}", headers=headers)
    assert elsewhere.status_code == 404  # A version must belong to the template named in the path.
    response = client.delete(f'/api/v1/templates/{template}/versions/{manager}', headers=headers)
    assert response.json['deleted'] == {'versions': 1, 'unlinked': 1, 'drafts': 1}
    tailored = client.get(f"/api/v1/jobs/{data['job']['id']}/resumes").json['resumes']
    assert [r['source'] for r in tailored if r['id'] == data['tailored']['id']] == [None]
    assert [v['version'] for v in client.get(f'/api/v1/templates/{template}').json['versions']] == [2]


def test_template_delete_and_the_default_rules(app, client):
    data = populate(app, client, 'template@example.com')
    headers, main, other = data['headers'], data['main']['template_id'], data['other']['id']
    preview = client.get(f'/api/v1/templates/{main}/deletion').json
    assert preview['is_default'] and not preview['deletable']
    assert client.delete(f'/api/v1/templates/{main}', headers=headers, json={'confirm_name': 'Main resume'}
                         ).status_code == 409
    only = client.get(f'/api/v1/templates/{main}').json['versions'][0]['id']
    assert client.delete(f'/api/v1/templates/{main}/versions/{only}', headers=headers).status_code == 409
    assert client.delete(f'/api/v1/templates/{other}', headers=headers, json={'confirm_name': 'manager'}
                         ).status_code == 400
    deleted = client.delete(f'/api/v1/templates/{other}', headers=headers, json={'confirm_name': 'Manager'})
    assert deleted.status_code == 200 and deleted.json['deleted']['versions'] == 2
    assert client.get(f'/api/v1/templates/{other}').status_code == 404
    last = client.delete(f'/api/v1/templates/{main}', headers=headers, json={'confirm_name': 'Main resume'})
    assert last.status_code == 200
    steps = {step['id']: step['status'] for step in client.get('/api/v1/onboarding').json['steps']}
    assert steps['upload_resume'] != 'done'
    assert [name for name in files_of(app, data['user_id']) if name.startswith('master/')] == []


# Export -----------------------------------------------------------------------------
def test_export_link(app, client, caplog):
    caplog.set_level(logging.INFO, logger='jobtracker.accounts')
    data = populate(app, client, 'export@example.com')
    result = app.test_cli_runner().invoke(args=['user', 'export', '--id', str(data['user_id']), '--code',
                                                code(app, data['user_id'], 'export')])
    assert result.exit_code == 0, result.stderr
    link = result.stdout.strip()
    assert link.startswith('https://example.test/api/v1/exports/nme_') and 'credential' in result.stderr
    path = '/api/v1/exports/' + link.rsplit('/', 1)[1]
    anonymous = app.test_client()
    for _ in range(4):  # Opening the link (or a mail scanner fetching it) uses up nothing.
        page = anonymous.get(path)
        assert page.status_code == 200 and b'Download my data' in page.data and b'3 more times' in page.data
    assert page.headers['Cache-Control'] == 'no-store' and page.headers['Referrer-Policy'] == 'same-origin'
    assert page.headers['X-Robots-Tag'] == 'noindex' and b'<script' not in page.data
    assert anonymous.post(path, headers={'Origin': 'https://evil.test'}).status_code == 403
    response = anonymous.post(path)
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert 'attachment' in response.headers['Content-Disposition']
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    account = json.loads(archive.read('account.json'))
    assert account['account']['email'] == 'export@example.com' and account['roles'][0]['notes'] == 'canary-note-export@example.com'
    assert 'password_hash' not in json.dumps(account) and 'token_hash' not in json.dumps(account)
    assert any(name.startswith('templates/') for name in archive.namelist())
    assert b'2 more times' in anonymous.get(path).data
    assert anonymous.post(path).status_code == 200 and anonymous.post(path).status_code == 200
    assert anonymous.post(path).status_code == 404 and anonymous.get(path).status_code == 404  # 3 downloads at most.
    assert anonymous.get('/api/v1/exports/nme_' + 'x' * 43).status_code == 404
    assert link.rsplit('/', 1)[1] not in caplog.text
    with engine(app).begin() as connection:
        stored = connection.scalar(select(DataExport.stored_name))
        connection.execute(update(DataExport).values(expires_at=now() - timedelta(seconds=1)))
    export_file = os.path.join(app.config['UPLOAD_FOLDER'], '_exports', stored)
    os.utime(export_file, (time.time() - 7200, time.time() - 7200))
    with app.app_context():
        assert deletion.sweep(apply=True)['exports'] == 1
    assert not os.path.exists(export_file)


# Ledger and sweeper -----------------------------------------------------------------
def test_ledger_replay_brings_a_restored_copy_back_to_the_deleted_state(app, client, config, tmp_path):
    data = populate(app, client, 'restore@example.com')
    engine(app).dispose()
    path = database_path(app)
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(tmp_path / 'snapshot.sqlite3')) as copy:
        source.backup(copy)
    job_file = files_of(app, data['user_id'])
    headers = data['headers']
    current = client.get(f"/api/v1/jobs/{data['job']['id']}").json['version']
    client.delete(f"/api/v1/jobs/{data['job']['id']}", headers=headers, json={'version': current})
    client.delete(f"/api/v1/templates/{data['other']['id']}", headers=headers, json={'confirm_name': 'Manager'})
    delete_cli(app, data['user_id'])
    exported = app.test_cli_runner().invoke(args=['deletions', 'export']).output
    assert len(exported.strip().splitlines()) == 3
    (tmp_path / 'ledger.jsonl').write_text(exported)
    taskqueue.drain(app)
    mailer.outbox.clear()
    engine(app).dispose()
    with closing(sqlite3.connect(tmp_path / 'snapshot.sqlite3')) as snapshot, closing(sqlite3.connect(path)) as live:
        snapshot.backup(live)  # "Restore" the older copy, with the account and its rows back.
    restored = create_app(config)
    with engine(restored).connect() as connection:
        assert connection.scalar(select(User.id).where(User.id == data['user_id'])) is not None
    assert job_file  # (files were already removed; replay must still remove the rows and leave no mail)
    result = restored.test_cli_runner().invoke(args=['deletions', 'replay', str(tmp_path / 'ledger.jsonl')])
    assert result.exit_code == 0, result.output
    with engine(restored).connect() as connection:
        assert connection.scalar(select(User.id).where(User.id == data['user_id'])) is None
        assert connection.scalar(select(QueuedTask.id).where(QueuedTask.user_id.is_(None))) is None
        assert len(connection.execute(select(Deletion.id)).all()) == 3
    taskqueue.drain(restored)
    assert mailer.outbox == []  # Replay never sends mail.
    again = restored.test_cli_runner().invoke(args=['deletions', 'replay', str(tmp_path / 'ledger.jsonl')])
    assert again.exit_code == 0 and 'Replayed 0' in again.output


def test_replay_refuses_a_backup_older_than_the_ledger_window(app, client, tmp_path, monkeypatch):
    entry = {'id': new_id(), 'kind': 'account', 'user_id': 99, 'object_id': None, 'created_at': now().isoformat()}
    (tmp_path / 'ledger.jsonl').write_text(json.dumps(entry) + '\n')
    monkeypatch.setattr(deletion, '_newest_write', lambda connection: now() - timedelta(days=36))
    result = app.test_cli_runner().invoke(args=['deletions', 'replay', str(tmp_path / 'ledger.jsonl')])
    assert result.exit_code != 0 and '35-day ledger window' in result.output
    forced = app.test_cli_runner().invoke(args=['deletions', 'replay', '--force', str(tmp_path / 'ledger.jsonl')])
    assert forced.exit_code == 0


def test_ledger_is_pruned_after_35_days(app):
    with engine(app).begin() as connection:
        connection.execute(insert(Deletion).values(id='old', kind='job', user_id=1, object_id='x',
                                                   created_at=now() - timedelta(days=36)))
        connection.execute(insert(Deletion).values(id='new', kind='job', user_id=1, object_id='y', created_at=now()))
        assert deletion.prune_ledger(connection) == 1


def test_sweeper_is_guarded(app, client, tmp_path):
    data = populate(app, client, 'sweep@example.com')
    base = root(app, data['user_id'])
    young, old = os.path.join(base, 'young.txt'), os.path.join(base, 'orphan.txt')
    for name in (young, old):
        with open(name, 'w') as handle:
            handle.write('x')
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    stray = os.path.join(app.config['UPLOAD_FOLDER'], '424242')
    os.makedirs(stray)
    runner = app.test_cli_runner()
    dry = runner.invoke(args=['storage', 'sweep'])
    assert 'Would remove 1 orphan file(s)' in dry.output and '1 unknown folder(s)' in dry.output
    assert os.path.exists(old)
    assert runner.invoke(args=['storage', 'sweep', '--apply']).exit_code == 0
    assert not os.path.exists(old) and os.path.exists(young) and os.path.exists(stray)
    for n in range(deletion.SWEEP_MAX_FILES + 1):
        name = os.path.join(base, f'bulk-{n}.txt')
        with open(name, 'w') as handle:
            handle.write('x')
        os.utime(name, (time.time() - 7200, time.time() - 7200))
    refused = runner.invoke(args=['storage', 'sweep', '--apply'])
    assert refused.exit_code != 0 and '--force' in refused.output
    assert runner.invoke(args=['storage', 'sweep', '--apply', '--force']).exit_code == 0
    with engine(app).begin() as connection:
        connection.execute(text('DELETE FROM schema_version'))
        connection.execute(text('INSERT INTO schema_version (version) VALUES (16)'))
    assert runner.invoke(args=['storage', 'sweep', '--apply']).exit_code != 0


def test_export_entry_names_are_safe(app, client):
    data = populate(app, client, 'zip@example.com')
    with engine(app).begin() as connection:
        connection.execute(update(Resume).where(Resume.id == data['plain']['id']).values(
            original_filename='../../etc/passwd'))
        connection.execute(update(MasterResume).where(MasterResume.id == data['manager']['id']).values(
            original_filename='/abs/..\\..\\evil.txt'))
    with app.app_context():
        raw = deletion.build_export(data['user_id'], code(app, data['user_id'], 'export'))[0]
        stored = engine(app).connect().execute(select(DataExport.stored_name)).scalar()
    archive = zipfile.ZipFile(os.path.join(app.config['UPLOAD_FOLDER'], '_exports', stored))
    for name in archive.namelist():
        assert not name.startswith('/') and '..' not in name and '\\' not in name, name
    assert raw.startswith('nme_')


def test_v16_upgrade_creates_the_deletion_tables(config):
    app = create_app(config)
    engine(app).dispose()
    with closing(sqlite3.connect(database_path(app))) as connection:
        rewind_data_deletion(connection)
        connection.commit()
    upgraded = create_app(config)
    with engine(upgraded).connect() as connection:
        assert connection.scalar(text('SELECT version FROM schema_version')) == migrations.SCHEMA_VERSION
        assert {'deletions', 'data_exports', 'support_codes'} <= set(inspect(connection).get_table_names())


def test_storage_vacuum(app):
    result = app.test_cli_runner().invoke(args=['storage', 'vacuum'])
    assert result.exit_code == 0 and 'Vacuumed' in result.output
