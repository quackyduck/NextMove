"""Admin page (schema 18): access rules, step-up, metrics, the no-content guarantee, actions, audit, CLI, migration."""
import ast
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid
import zipfile

import pytest
from sqlalchemy import create_engine, event, insert, inspect, select, text, update
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash

from jobtracker import admin, create_app, credentials, deletion, harness, mailer, migrations, oauth
from jobtracker.models import (OAUTH_SCOPES, AdminAction, AgentToken, Activity, Contact, EmailConfirmation,
                               HarnessGrant, IngestionRequest, Job, MasterResume, OAuthClient, OAuthGrant, OAuthToken,
                               PasswordResetToken, Resume, ResumeTemplate, TailoredDraft, User, now)

from accounts import add_user, sign_in, sign_up
from legacy import ADMIN_ROLLBACK_SQL, database_path, rewind_admin

ROOT = Path(__file__).resolve().parent.parent
OWNER_PASSWORD = 'test-password'
ADMIN_EMAIL, ADMIN_PASSWORD = 'admin@example.com', 'admin-password-1'
ALICE, ALICE_PASSWORD = 'alice@example.com', 'alice-password-1'
BOB = 'bob@example.com'
LEGACY = 'legacy-environment-token-' + 'x' * 16
SECRET = 'harness-secret-' + 'h' * 32
BASE_URL = 'https://example.test'
LINK = re.compile(r'^https://example\.test/reset-password#token=(nmr_[A-Za-z0-9_-]+)$')
# Content that must never appear in any admin response.
MARKERS = ('MARKER-TITLE', 'MARKER-COMPANY', 'https://example.com/marker-posting', 'MARKER-NOTE', 'MARKER-DESC',
           'MARKER-CONTACT', 'marker.contact@example.com', 'MARKER-ACTIVITY', 'marker-resume.docx',
           'marker-template-file.docx', 'MARKER-TEMPLATE-NAME', 'MARKER-TEMPLATE-DESC', 'MARKER-CLIENT',
           'MARKER-AGENT-TOKEN', 'https://example.com/marker-ingest', 'marker-draft.docx')
NOT_FOUND = {'error': 'Not found'}


@pytest.fixture
def config(tmp_path, monkeypatch):
    for name in list(mailer.SETTINGS) + ['ADMIN_STEP_UP_SECONDS']:
        monkeypatch.delenv(name, raising=False)
    return {
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'admin-test-secret-0123456789abcdef',
        'APP_PASSWORD_HASH': generate_password_hash(OWNER_PASSWORD, method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "admin.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
        'PUBLIC_BASE_URL': BASE_URL, 'MAIL_BACKEND': 'memory', 'POW_BITS': 2, 'AGENT_API_TOKEN': LEGACY,
        'HARNESS_SERVICE_SECRET': SECRET,
    }


@pytest.fixture
def app(config):
    mailer.outbox.clear()
    yield create_app(config)
    mailer.outbox.clear()


def engine(app):
    return app.extensions['jobtracker_db']['engine']


def cli(app, *args, input=None):
    return app.test_cli_runner().invoke(args=list(args), input=input)


def rows(app, model=AdminAction):
    with engine(app).connect() as connection:
        return connection.execute(select(model.__table__).order_by(model.__table__.c[list(model.__table__.c.keys())[0]])).all()


def actions(app):
    with engine(app).connect() as connection:
        return connection.execute(select(AdminAction.__table__).order_by(AdminAction.id)).all()


def oauth_token(connection, user_id, revoked=False):
    client_id, grant_id = 'nmc_' + secrets.token_hex(8), str(uuid.uuid4())
    raw = oauth.ACCESS_PREFIX + secrets.token_urlsafe(32)
    connection.execute(insert(OAuthClient).values(id=client_id, name='MARKER-CLIENT', redirect_uris=['https://a.example/cb'],
                                                  created_at=now()))
    connection.execute(insert(OAuthGrant).values(id=grant_id, user_id=user_id, client_id=client_id,
                                                 scopes=list(OAUTH_SCOPES), created_at=now(),
                                                 revoked_at=now() if revoked else None))
    connection.execute(insert(OAuthToken).values(id=str(uuid.uuid4()), grant_id=grant_id, kind='access',
                                                 token_hash=hashlib.sha256(raw.encode()).hexdigest(), created_at=now(),
                                                 expires_at=now() + timedelta(hours=1)))
    return {'Authorization': 'Bearer ' + raw}


def agent_token(connection, user_id, expired=False):
    raw = 'nm_ingest_' + secrets.token_urlsafe(32)
    connection.execute(insert(AgentToken).values(
        id=str(uuid.uuid4()), user_id=user_id, name='MARKER-AGENT-TOKEN', prefix=raw[:18],
        token_hash=hashlib.sha256(raw.encode()).hexdigest(), permissions=['roles:ingest', 'roles:search'],
        created_at=now(), expires_at=now() + (timedelta(days=-1) if expired else timedelta(days=30))))
    return {'Authorization': 'Bearer ' + raw}


@pytest.fixture
def world(app):
    """#1 owner (owns_legacy_tokens), #2 admin, #3 Alice (verified, with content), #4 Bob (legacy unverified),
    #5 a messaging-only account. Alice holds one live OAuth grant, agent token and harness grant, plus a revoked
    grant and an expired token."""
    admin_id = add_user(app, ADMIN_EMAIL, ADMIN_PASSWORD)
    alice = add_user(app, ALICE, ALICE_PASSWORD)
    bob = add_user(app, BOB, 'bob-password-12', verified=False)
    with engine(app).begin() as connection:
        messaging = connection.execute(insert(User).values(email=None, password_hash=None, session_version=1,
                                                           created_at=now())).inserted_primary_key[0]
        jobs = []
        for index, (stage, outcome) in enumerate((('applied', 'active'), ('needs_review', 'active'),
                                                  ('offer', 'accepted'))):
            job_id = str(uuid.uuid4())
            jobs.append(job_id)
            connection.execute(insert(Job).values(
                id=job_id, user_id=alice, title=f'MARKER-TITLE {index}', company='MARKER-COMPANY',
                url='https://example.com/marker-posting', description='MARKER-DESC', notes='MARKER-NOTE',
                stage=stage, outcome=outcome, found_date=now().date(), created_at=now(), updated_at=now()))
        connection.execute(insert(Contact).values(id=str(uuid.uuid4()), user_id=alice, job_id=jobs[0],
                                                  name='MARKER-CONTACT', email='marker.contact@example.com',
                                                  notes='MARKER-NOTE'))
        connection.execute(insert(Activity).values(user_id=alice, job_id=jobs[0], kind='note',
                                                   message='MARKER-ACTIVITY', details={}))
        template = str(uuid.uuid4())
        connection.execute(insert(ResumeTemplate).values(id=template, user_id=alice, name='MARKER-TEMPLATE-NAME',
                                                         description='MARKER-TEMPLATE-DESC', is_default=True))
        versions = []
        for version in (1, 2):
            version_id = str(uuid.uuid4())
            versions.append(version_id)
            connection.execute(insert(MasterResume).values(
                id=version_id, user_id=alice, template_id=template, version=version,
                original_filename='marker-template-file.docx', stored_filename=uuid.uuid4().hex + '.docx',
                media_type='application/octet-stream', size=1, sha256='0' * 64))
        for source in (None, versions[1]):
            connection.execute(insert(Resume).values(
                id=str(uuid.uuid4()), user_id=alice, job_id=jobs[0], original_filename='marker-resume.docx',
                stored_filename=uuid.uuid4().hex + '.docx', media_type='application/octet-stream', size=1,
                sha256='0' * 64, source_master_resume_id=source))
        connection.execute(insert(TailoredDraft).values(
            id=str(uuid.uuid4()), user_id=alice, job_id=jobs[0], master_resume_id=versions[0], request_id='rq_1',
            revision=1, draft={'text': 'MARKER-NOTE'}, docx_filename='marker-draft.docx', created_at=now()))
        alice_oauth = oauth_token(connection, alice)
        oauth_token(connection, alice, revoked=True)
        alice_agent = agent_token(connection, alice)
        agent_token(connection, alice, expired=True)
        alice_harness = {'Authorization': 'Bearer ' + harness.create_grant(connection, alice)}
        for days, status in ((1, 'failed'), (10, 'failed'), (1, 'succeeded')):
            connection.execute(insert(IngestionRequest).values(
                id=str(uuid.uuid4()), user_id=alice, idempotency_key=uuid.uuid4().hex,
                url='https://example.com/marker-ingest', status=status, result={},
                created_at=now() - timedelta(days=days)))
        connection.execute(insert(EmailConfirmation).values(
            id=str(uuid.uuid4()), kind='signup', email='pending@example.com', password_hash='x', password_attempts=0,
            created_at=now(), expires_at=now() + timedelta(hours=1), delivery='sent'))
    assert cli(app, 'user', 'set-admin', ADMIN_EMAIL).exit_code == 0
    client = app.test_client()
    headers = sign_in(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    return dict(admin=admin_id, alice=alice, bob=bob, messaging=messaging, client=client, headers=headers,
                alice_oauth=alice_oauth, alice_agent=alice_agent, alice_harness=alice_harness)


def routes(world):
    target = world['alice']
    return [('GET', '/api/v1/admin/overview', None), ('POST', '/api/v1/admin/users/query', {'q': ''}),
            ('GET', f'/api/v1/admin/users/{target}', None), ('POST', '/api/v1/admin/step-up', {'password': 'x'}),
            ('POST', f'/api/v1/admin/users/{target}/reset-link', {}),
            ('POST', f'/api/v1/admin/users/{target}/sign-out', {}),
            ('POST', f'/api/v1/admin/users/{target}/revoke-agents', {'include_harness': True}),
            ('POST', f'/api/v1/admin/users/{target}/verify', {'confirm': 'verify'}),
            ('GET', '/api/v1/admin/audit', None)]


def call(client, method, path, body=None, headers=None):
    return client.open(path, method=method, json=body if method not in ('GET', 'HEAD') else None,
                       headers=headers or {})


ACTION_PATHS = ('reset-link', 'sign-out', 'revoke-agents', 'verify')


# Access -----------------------------------------------------------------------
@pytest.mark.parametrize('mode', ['anonymous', 'non_admin', 'unverified_admin', 'legacy', 'agent', 'oauth',
                                  'harness', 'admin_with_authorization', 'plain_http'])
def test_everyone_but_a_verified_admin_session_gets_the_same_404(app, world, mode):
    client, headers = app.test_client(), {}
    if mode == 'non_admin':
        headers = sign_in(client, ALICE, ALICE_PASSWORD)
    elif mode == 'unverified_admin':
        client, headers = world['client'], world['headers']
        with engine(app).begin() as connection:
            connection.execute(update(User).where(User.id == world['admin']).values(email_verified_at=None))
    elif mode in ('legacy', 'agent', 'oauth', 'harness'):
        headers = {'legacy': {'Authorization': 'Bearer ' + LEGACY}, 'agent': world['alice_agent'],
                   'oauth': world['alice_oauth'], 'harness': {**world['alice_harness'], 'X-RA-Signature': 'x',
                                                              'X-RA-Timestamp': '1', 'X-RA-Nonce': 'n'}}[mode]
        if mode == 'legacy':  # The legacy token acts for the owner; make the owner an admin to prove it still fails.
            with engine(app).begin() as connection:
                connection.execute(update(User).where(User.id == 1).values(is_admin=True))
    elif mode == 'admin_with_authorization':
        client = world['client']
        headers = {**world['headers'], 'Authorization': 'Bearer ' + LEGACY}
    elif mode == 'plain_http':
        client, headers = world['client'], world['headers']
        app.config['APP_ENV'] = 'production'
    unknown = app.test_client().get('/api/v1/no-such-route')
    assert unknown.status_code == 404 and unknown.json == NOT_FOUND
    before = actions(app)
    for method, path, body in routes(world):
        for verb in ((method, 'HEAD') if method == 'GET' else (method,)):
            response = call(client, verb, path, body, headers)
            assert response.status_code == 404, (mode, verb, path, response.status_code)
            if verb != 'HEAD':
                assert response.json == NOT_FOUND
    assert actions(app) == before


def test_options_never_reaches_a_view(app, world):
    for method, path, _ in routes(world):
        response = world['client'].open(path, method='OPTIONS', headers=world['headers'])
        assert response.status_code == 404 and 'Allow' not in response.headers


def test_csrf_and_step_up_apply_only_after_the_admin_check(app, world):
    client, headers = world['client'], world['headers']
    for method, path, body in routes(world):
        if method == 'POST':
            assert call(client, method, path, body).json == {'error': 'Invalid CSRF token'}
    with client.session_transaction() as session:
        session['password_at'] = int(time.time()) - 601
    for method, path, body in routes(world):
        response = call(client, method, path, body, headers)
        if path.endswith(ACTION_PATHS):
            assert response.status_code == 403 and response.json['code'] == 'step_up_required'
        else:
            assert response.status_code != 403, path
    for value in (None, 'yesterday', int(time.time()) + 3600):
        with client.session_transaction() as session:
            session.pop('password_at', None)
            if value is not None:
                session['password_at'] = value
        assert client.get('/api/v1/admin/overview').json['code'] == 'step_up_required'
        assert call(client, 'POST', f"/api/v1/admin/users/{world['alice']}/sign-out", {}, headers).status_code == 403


def test_revoking_admin_takes_effect_on_the_next_request(app, world):
    assert world['client'].get('/api/v1/admin/overview').status_code == 200
    assert cli(app, 'user', 'set-admin', ADMIN_EMAIL, '--revoke').exit_code == 0
    assert world['client'].get('/api/v1/admin/overview').json == NOT_FOUND


def test_a_refused_signed_in_non_admin_is_logged_once_an_hour_by_id(app, world, caplog):
    client = app.test_client()
    sign_in(client, ALICE, ALICE_PASSWORD)
    with caplog.at_level(logging.INFO, logger='jobtracker.admin'):
        client.get('/api/v1/admin/overview')
        client.get('/api/v1/admin/audit')
        app.test_client().get('/api/v1/admin/overview')  # anonymous: not logged
    lines = [record.getMessage() for record in caplog.records if record.name == 'jobtracker.admin']
    assert lines == [f"admin route refused user_id={world['alice']}"]


def test_every_admin_rule_is_guarded_and_absent_from_agent_surfaces(app):
    rules = [rule for rule in app.url_map.iter_rules() if rule.rule.startswith('/api/v1/admin')]
    assert len(rules) == 9
    for rule in rules:
        assert getattr(app.view_functions[rule.endpoint], 'admin_route', None) in ('read', 'action', 'step_up')
    assert 'admin' not in (ROOT / 'jobtracker/openapi.json').read_text().lower()
    from jobtracker import mcp
    assert not [name for name in mcp.TOOLS if 'admin' in name]
    assert not [scope for scope in OAUTH_SCOPES if 'admin' in scope]


def test_admin_views_keep_the_admin_identity_and_only_the_cli_writes_the_flag():
    source = (ROOT / 'jobtracker/admin.py').read_text()
    assert not re.search(r'g\.(user_id|credential)\s*=', source)
    assert 'services' not in re.findall(r'from \.(\w+) import|import (\w+)', source).__repr__()
    allowed = {('admin.py', 'user_set_admin'), ('credentials.py', 'clear_admin')}
    found = set()

    def own_nodes(function):  # The function's nodes, without those of functions nested in it.
        stack = list(ast.iter_child_nodes(function))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            yield node
            stack.extend(ast.iter_child_nodes(node))
    for path in (ROOT / 'jobtracker').glob('*.py'):
        tree = ast.parse(path.read_text())
        for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in own_nodes(function):
                # .values(is_admin=...) on insert/update, `row.is_admin = ...`, setattr, or SQL text that writes it.
                writes = (isinstance(node, ast.Call) and getattr(node.func, 'attr', '') in ('values', 'insert', 'update')
                          and any(k.arg == 'is_admin' or k.arg is None for k in node.keywords)
                          and 'is_admin' in ast.unparse(node)) or (
                    isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                    and any(getattr(t, 'attr', None) == 'is_admin'
                            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))) or (
                    isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'setattr'
                    and 'is_admin' in ast.unparse(node)) or (
                    isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and re.search(r'(SET|INSERT|ALTER)[^;]*is_admin', node.value, re.IGNORECASE)
                    and (path.name, function.name) != ('migrations.py', 'upgrade_v17_to_v18'))
                if writes:
                    found.add((path.name, function.name))
    # admin.py's set-admin also filters on the flag; credentials.clear_admin only ever writes False.
    assert found <= allowed, found
    assert ('admin.py', 'user_set_admin') in found and ('credentials.py', 'clear_admin') in found


def test_only_login_and_password_change_count_as_a_password_check():
    calls = {}
    for path in (ROOT / 'jobtracker').glob('*.py'):
        tree = ast.parse(path.read_text())
        for function in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]:
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, 'id', getattr(node.func, 'attr', '')) == 'issue_session':
                    verified = any(k.arg == 'password_verified' and getattr(k.value, 'value', False) for k in node.keywords)
                    calls.setdefault(verified, set()).add((path.name, function.name))
    assert calls[True] == {('auth.py', 'login'), ('auth.py', 'change_password')}
    assert ('confirmations.py', 'confirm_route') in calls[False]


def test_prefix_mount(config, tmp_path):
    app = create_app({**config, 'APP_ROOT_PATH': '/nextmove'})
    add_user(app, ADMIN_EMAIL, ADMIN_PASSWORD)
    add_user(app, ALICE, ALICE_PASSWORD)
    cli(app, 'user', 'set-admin', ADMIN_EMAIL)
    client, other = app.test_client(), app.test_client()
    assert client.post('/nextmove/api/v1/auth/login', json={'email': ADMIN_EMAIL, 'password': ADMIN_PASSWORD}).status_code == 200
    assert client.get('/nextmove/api/v1/admin/overview').status_code == 200
    other.post('/nextmove/api/v1/auth/login', json={'email': ALICE, 'password': ALICE_PASSWORD})
    assert other.get('/nextmove/api/v1/admin/overview').json == NOT_FOUND


# Step-up ----------------------------------------------------------------------
def test_step_up_checks_the_password_and_records_both_outcomes(app, world):
    client, headers = world['client'], world['headers']
    with client.session_transaction() as session:
        session['password_at'] = int(time.time()) - 3600
    wrong = client.post('/api/v1/admin/step-up', json={'password': 'nope-nope'}, headers=headers)
    assert wrong.status_code == 400 and wrong.json['code'] == 'step_up_failed'
    ok = client.post('/api/v1/admin/step-up', json={'password': ADMIN_PASSWORD}, headers=headers)
    assert ok.status_code == 200 and ok.json == {'ok': True, 'valid_for': 600}
    assert client.post(f"/api/v1/admin/users/{world['alice']}/sign-out", json={}, headers=headers).status_code == 200
    kinds = [row.action for row in actions(app) if row.action.startswith('step_up')]
    assert kinds == ['step_up_failed', 'step_up']
    with client.session_transaction() as session:
        assert abs(session['password_at'] - time.time()) < 5


def test_step_up_shares_the_password_recheck_budget(app, world):
    client, headers = world['client'], world['headers']
    for _ in range(10):
        client.post('/api/v1/auth/password', headers=headers, json={
            'current_password': 'wrong-password', 'new_password': 'whatever-123', 'confirm_password': 'whatever-123'})
    limited = client.post('/api/v1/admin/step-up', json={'password': ADMIN_PASSWORD}, headers=headers)
    assert limited.status_code == 429 and limited.headers['Retry-After']
    other = app.test_client()
    add_user(app, 'second@example.com', 'second-password-1')
    cli(app, 'user', 'set-admin', 'second@example.com')
    second = sign_in(other, 'second@example.com', 'second-password-1')
    for _ in range(10):
        assert other.post('/api/v1/admin/step-up', json={'password': 'bad-password'}, headers=second).status_code == 400
    assert other.post('/api/v1/auth/password', headers=second, json={
        'current_password': 'second-password-1', 'new_password': 'whatever-123',
        'confirm_password': 'whatever-123'}).status_code == 429


def test_step_up_has_a_daily_cap(app, world, monkeypatch):
    monkeypatch.setattr(admin, 'STEP_UP_DAILY', 3)
    client, headers = world['client'], world['headers']
    statuses = [client.post('/api/v1/admin/step-up', json={'password': 'bad-password'}, headers=headers).status_code
                for _ in range(4)]
    assert statuses == [400, 400, 400, 429]
    assert sum(1 for row in actions(app) if row.action == 'step_up_failed') == 3


def test_an_email_link_session_must_step_up_before_reading(app, world):
    with app.app_context():
        [code] = credentials.create_invites(1, 7)
    client = app.test_client()
    headers = sign_up(app, client, 'linked@example.com', invite_code=code)
    with client.session_transaction() as session:
        assert 'password_at' not in session
    cli(app, 'user', 'set-admin', 'linked@example.com')
    assert client.get('/api/v1/admin/overview').json['code'] == 'step_up_required'
    assert client.post('/api/v1/admin/step-up', json={'password': 'new-password-123'}, headers=headers).status_code == 200
    assert client.get('/api/v1/admin/overview').status_code == 200


def test_a_password_reset_ends_the_stepped_up_session(app, world):
    client = world['client']
    result = cli(app, 'user', 'reset-link', ADMIN_EMAIL)
    token = re.search(r'#token=(nmr_[A-Za-z0-9_-]+)', result.stdout).group(1)
    done = client.post('/api/v1/auth/password-reset/complete', json={
        'token': token, 'password': 'another-password-1', 'password_confirmation': 'another-password-1'})
    assert done.status_code == 200
    with client.session_transaction() as session:
        assert 'password_at' not in session
    assert client.get('/api/v1/admin/overview').json == NOT_FOUND


def test_a_replayed_cookie_dies_with_an_operator_password_reset(app, world):
    with world['client'].session_transaction() as session:
        copied = dict(session)
    assert cli(app, 'user', 'reset-password', ADMIN_EMAIL, input='new-admin-pass-1\nnew-admin-pass-1\n').exit_code == 0
    replay = app.test_client()
    with replay.session_transaction() as session:
        session.update(copied)
    assert replay.get('/api/v1/admin/overview').json == NOT_FOUND


def test_an_admin_sign_out_ends_copies_of_the_cookie(app, world):
    with world['client'].session_transaction() as session:
        copied = dict(session)
    world['client'].post('/api/v1/auth/logout', headers=world['headers'])
    replay = app.test_client()
    with replay.session_transaction() as session:
        session.update(copied)
    assert replay.get('/api/v1/admin/overview').json == NOT_FOUND


# Metrics ----------------------------------------------------------------------
def test_overview_counts_the_fixture(app, world):
    moment = now()
    with engine(app).begin() as connection:
        for user_id, days in ((world['alice'], 0), (world['bob'], 3), (world['messaging'], 40)):
            connection.execute(update(User).where(User.id == user_id).values(created_at=moment - timedelta(days=days)))
        connection.execute(update(User).values(last_seen_at=None))
        connection.execute(update(User).where(User.id == world['bob']).values(last_seen_at=moment - timedelta(days=3)))
    data = world['client'].get('/api/v1/admin/overview').json
    assert data['accounts'] == {'total': 5, 'verified': 3, 'legacy_unverified': 1, 'messaging': 1, 'admins': 1}
    daily = {item['date']: item['count'] for item in data['signups']['daily']}
    assert len(daily) == 30 and list(daily)[-1] == moment.date().isoformat()
    assert daily[moment.date().isoformat()] >= 1 and daily[(moment - timedelta(days=3)).date().isoformat()] >= 1
    assert sum(daily.values()) == 4  # owner, admin and Alice today; Bob three days ago; messaging 40 days ago is out
    weekly = data['signups']['weekly']
    assert len(weekly) == 12 and sum(item['count'] for item in weekly) == 5
    assert datetime.fromisoformat(weekly[-1]['week_start']).weekday() == 0
    # The admin's own requests on this page set last_seen_at (after the reset above).
    assert data['active'] == {'day': 1, 'week': 2, 'month': 2, 'never': 3}
    roles = data['roles']
    assert roles['total'] == 3 and roles['by_stage']['applied'] == 1 and roles['by_stage']['offer'] == 1
    assert roles['by_stage']['interesting'] == 0 and roles['by_outcome'] == {
        'active': 2, 'rejected': 0, 'withdrawn': 0, 'closed': 0, 'accepted': 1, 'not_interested': 0}
    assert roles['per_account'] == {'0': 4, '1-5': 1, '6-20': 0, '21-50': 0, '51+': 0}
    assert roles['max_per_account'] == 3 and roles['median_per_account'] == 0
    assert data['resumes'] == {'templates': 1, 'template_versions': 2, 'job_resumes': 2, 'tailored': 1,
                               'accounts_with_template': 1, 'drafts': 1}
    assert data['agents'] == {'oauth_grants': 1, 'oauth_accounts': 1, 'agent_tokens': 1, 'agent_token_accounts': 1,
                              'harness_grants': 1, 'harness_accounts': 1}
    assert data['imports_failed_7d'] == {'ingestion': 1, 'extraction': 0}
    assert data['pending_confirmations'] == {'signup': 1, 'activation': 0, 'existing': 0}
    with app.app_context():
        assert data['mail'] == json.loads(json.dumps(mailer.usage_report()))
    assert data['mail']['backend'] == 'memory' and data['mail']['last_24h']['limit'] > 0


def test_the_overview_fires_no_mail_alerts(app, world, monkeypatch):
    fired = []
    monkeypatch.setattr(mailer, '_alert', lambda *args, **kwargs: fired.append(args))
    monkeypatch.setattr(mailer, '_seen', lambda name, window: 5 if name == 'fail' else 0)
    data = world['client'].get('/api/v1/admin/overview').json
    assert data['mail']['status'] == 'degraded' and fired == []


def test_no_admin_response_contains_content(app, world):
    client, headers = world['client'], world['headers']
    bodies = [client.get('/api/v1/admin/overview').data,
              client.post('/api/v1/admin/users/query', json={}, headers=headers).data,
              client.get('/api/v1/admin/audit').data]
    for user_id in (world['alice'], world['bob'], world['messaging'], world['admin'], 1):
        bodies.append(client.get(f'/api/v1/admin/users/{user_id}').data)
    client.post(f"/api/v1/admin/users/{world['alice']}/revoke-agents", json={'include_harness': True}, headers=headers)
    bodies.append(client.get('/api/v1/admin/audit').data)
    blob = b'\n'.join(bodies).decode()
    leaked = [marker for marker in MARKERS if marker in blob]
    assert not leaked, leaked
    assert ALICE in blob  # emails are the point of the page; content is not


def test_users_query_searches_pages_and_counts(app, world, monkeypatch):
    client, headers = world['client'], world['headers']
    found = client.post('/api/v1/admin/users/query', json={'q': ' ALICE '}, headers=headers).json
    assert found['total'] == 1 and found['users'][0]['counts'] == {'roles': 3, 'job_resumes': 2, 'templates': 1,
                                                                   'agents': 3}
    assert found['users'][0]['state'] == 'verified' and found['users'][0]['last_seen_at'] is None
    for literal in ('%', '_', 'a%e'):
        assert client.post('/api/v1/admin/users/query', json={'q': literal}, headers=headers).json['total'] == 0
    everyone = client.post('/api/v1/admin/users/query', json={}, headers=headers).json
    assert [user['id'] for user in everyone['users']] == [5, 4, 3, 2, 1]
    assert everyone['users'][0]['email'] is None and everyone['users'][0]['state'] == 'messaging'
    monkeypatch.setattr(admin, 'PAGE_SIZE', 2)
    pages = [client.post('/api/v1/admin/users/query', json={'page': page}, headers=headers).json for page in (1, 3)]
    assert [len(page['users']) for page in pages] == [2, 1] and pages[0]['total'] == 5
    for bad in ({'q': 5}, {'q': 'x' * 255}, {'page': 0}, {'page': '2'}):
        assert client.post('/api/v1/admin/users/query', json=bad, headers=headers).status_code == 400


def test_user_detail_and_view_rows_are_deduplicated_per_hour(app, world):
    client = world['client']
    detail = client.get(f"/api/v1/admin/users/{world['alice']}").json
    assert detail['counts']['roles'] == 3 and detail['counts']['roles_by_stage']['applied'] == 1
    assert detail['counts']['tailored'] == 1 and detail['counts']['contacts'] == 1
    assert {name: value['active'] for name, value in detail['agents'].items()} == {'oauth': 1, 'agent': 1, 'harness': 1}
    assert detail['can'] == {'reset_link': True, 'sign_out': True, 'revoke_agents': True, 'verify': False}
    assert client.get(f"/api/v1/admin/users/{world['bob']}").json['can']['verify'] is True
    client.get(f"/api/v1/admin/users/{world['alice']}")
    views = [row for row in actions(app) if row.action == 'view_user']
    assert [(row.admin_user_id, row.target_user_id) for row in views] == [(world['admin'], world['alice']),
                                                                         (world['admin'], world['bob'])]
    with engine(app).begin() as connection:
        connection.execute(update(AdminAction).values(created_at=now() - timedelta(hours=2)))
    client.get(f"/api/v1/admin/users/{world['alice']}")
    assert sum(1 for row in actions(app) if row.action == 'view_user') == 3
    missing = client.get('/api/v1/admin/users/999')
    assert missing.status_code == 404 and missing.json == {'error': 'Account not found', 'code': 'account_not_found'}
    for target in (1, world['admin']):
        protected = client.get(f'/api/v1/admin/users/{target}').json
        assert protected['protected'] is True and not any(protected['can'].values())


# Actions ----------------------------------------------------------------------
def test_reset_link_uses_the_operator_path_and_is_shown_only_in_the_response(app, world, caplog):
    client, headers = world['client'], world['headers']
    path = f"/api/v1/admin/users/{world['bob']}/reset-link"
    with caplog.at_level(logging.DEBUG):
        client.set_cookie('next_move_session', client.get_cookie('next_move_session').value, domain='evil.example')
        first = client.post(path, json={}, headers=headers, base_url='http://evil.example')
        second = client.post(path, json={'minutes': 30}, headers=headers)
    assert first.status_code == 200 and first.headers['Cache-Control'] == 'no-store'
    assert first.json['warning'].startswith('This link is a password-reset credential')
    token1, token2 = (LINK.match(response.json['link']).group(1) for response in (first, second))
    log_text = '\n'.join(record.getMessage() for record in caplog.records)
    assert token1 not in log_text and token2 not in log_text and BOB not in log_text and 'reset-password' not in log_text
    stored = json.dumps([dict(row._mapping) for row in actions(app)], default=str)
    assert token1 not in stored and 'reset-password' not in stored
    row = [row for row in actions(app) if row.action == 'reset_link'][-1]
    assert row.detail['minutes'] == 30 and set(row.detail) == {'minutes', 'expires_at'}
    with engine(app).connect() as connection:
        issued = connection.execute(select(PasswordResetToken.issued_by)).scalars().all()
    assert set(issued) == {'operator'}
    dead = client.post('/api/v1/auth/password-reset/complete', json={
        'token': token1, 'password': 'bob-new-password', 'password_confirmation': 'bob-new-password'})
    assert dead.status_code == 410
    done = app.test_client().post('/api/v1/auth/password-reset/complete', json={
        'token': token2, 'password': 'bob-new-password', 'password_confirmation': 'bob-new-password'})
    assert done.status_code == 200
    with app.app_context():
        assert credentials.read(world['bob']).verified is False  # An operator link never verifies the address.
    for minutes in (4, 241, '60'):
        assert client.post(path, json={'minutes': minutes}, headers=headers).status_code == 400
    messaging = client.post(f"/api/v1/admin/users/{world['messaging']}/reset-link", json={}, headers=headers)
    assert messaging.status_code == 409
    app.config['PUBLIC_BASE_URL'] = ''
    app.config['MAIL_LINK_BASE'] = None
    assert client.post(path, json={}, headers=headers).status_code == 503


def test_sign_out_everywhere_ends_the_targets_sessions(app, world):
    alice = app.test_client()
    sign_in(alice, ALICE, ALICE_PASSWORD)
    assert alice.get('/api/v1/auth/session').json['authenticated'] is True
    response = world['client'].post(f"/api/v1/admin/users/{world['alice']}/sign-out", json={},
                                    headers=world['headers'])
    assert response.json == {'signed_out': True}
    assert alice.get('/api/v1/auth/session').json['authenticated'] is False
    # A live reset link dies with the sign-out, and the detail count says so.
    with app.app_context(), engine(app).begin() as connection:
        from jobtracker.password_reset import mint_reset_token
        mint_reset_token(connection, world['alice'], 60, 'operator')
    path = f"/api/v1/admin/users/{world['alice']}"
    assert world['client'].get(path).json['pending']['reset_links_live'] == 1
    world['client'].post(f'{path}/sign-out', json={}, headers=world['headers'])
    assert world['client'].get(path).json['pending']['reset_links_live'] == 0
    assert world['client'].post(f"/api/v1/admin/users/{world['messaging']}/sign-out", json={},
                                headers=world['headers']).status_code == 409


def test_revoke_agents_with_and_without_chat(app, world):
    path = f"/api/v1/admin/users/{world['alice']}/revoke-agents"
    first = world['client'].post(path, json={}, headers=world['headers'])
    assert first.json == {'oauth': 1, 'agent': 2, 'harness': 0}  # the expired token is revoked too
    second = world['client'].post(path, json={'include_harness': True}, headers=world['headers'])
    assert second.json == {'oauth': 0, 'agent': 0, 'harness': 1}
    assert app.test_client().get('/api/v1/jobs', headers=world['alice_oauth']).status_code == 401
    assert world['client'].post(path, json={'include_harness': 'yes'}, headers=world['headers']).status_code == 400
    rows_ = [row for row in actions(app) if row.action == 'revoke_agents']
    assert [row.detail for row in rows_] == [{'oauth': 1, 'agent': 2, 'harness': 0, 'include_harness': False},
                                             {'oauth': 0, 'agent': 0, 'harness': 1, 'include_harness': True}]


def test_mark_verified_only_for_unverified_accounts(app, world):
    client, headers = world['client'], world['headers']
    path = f"/api/v1/admin/users/{world['bob']}/verify"
    assert client.post(path, json={}, headers=headers).status_code == 400
    response = client.post(path, json={'confirm': 'verify'}, headers=headers)
    assert response.json == {'verified': True, 'revoked': None}
    with app.app_context():
        assert credentials.read(world['bob']).verified
    count = len(actions(app))
    again = client.post(path, json={'confirm': 'verify'}, headers=headers)  # now verified: the in-transaction check
    assert again.status_code == 409 and len(actions(app)) == count
    for target in (world['alice'], world['messaging']):
        assert client.post(f'/api/v1/admin/users/{target}/verify', json={'confirm': 'verify'},
                           headers=headers).status_code == 409


def test_mark_verified_can_also_revoke_agents(app, world):
    with engine(app).begin() as connection:
        connection.execute(update(User).where(User.id == world['alice']).values(email_verified_at=None))
    response = world['client'].post(f"/api/v1/admin/users/{world['alice']}/verify", headers=world['headers'],
                                    json={'confirm': 'verify', 'revoke_agents': True, 'include_harness': True})
    assert response.json == {'verified': True, 'revoked': {'oauth': 1, 'agent': 2, 'harness': 1}}
    assert actions(app)[-1].detail == {'revoke_agents': True, 'include_harness': True, 'oauth': 1, 'agent': 2,
                                       'harness': 1}


def test_mark_verified_records_whether_revocation_was_asked(app, world):
    response = world['client'].post(f"/api/v1/admin/users/{world['bob']}/verify", headers=world['headers'],
                                    json={'confirm': 'verify'})
    assert response.status_code == 200
    assert actions(app)[-1].detail == {'revoke_agents': False, 'include_harness': False}


def test_a_failed_verify_logs_no_revocation(app, world, caplog, monkeypatch):
    with engine(app).begin() as connection:
        connection.execute(update(User).where(User.id == world['alice']).values(email_verified_at=None))
    monkeypatch.setattr(credentials, 'mark_verified', lambda *args, **kwargs: False)
    with caplog.at_level(logging.INFO, logger='jobtracker.accounts'):
        response = world['client'].post(f"/api/v1/admin/users/{world['alice']}/verify", headers=world['headers'],
                                        json={'confirm': 'verify', 'revoke_agents': True, 'include_harness': True})
    assert response.status_code == 409
    assert not [record for record in caplog.records if record.getMessage().startswith('revoked ')]
    with engine(app).connect() as connection:
        live = connection.scalar(text('SELECT COUNT(*) FROM oauth_grants WHERE user_id=:u AND revoked_at IS NULL'),
                                 {'u': world['alice']})
    assert live == 1


def test_actions_on_a_deleted_account_say_account_not_found(app, world):
    client, headers = world['client'], world['headers']
    for name, body in (('reset-link', {}), ('sign-out', {}), ('revoke-agents', {}), ('verify', {'confirm': 'verify'})):
        response = client.post(f'/api/v1/admin/users/999/{name}', json=body, headers=headers)
        assert response.status_code == 404 and response.json['code'] == 'account_not_found', name


def test_repeat_views_within_the_hour_take_no_write_lock(app, world):
    client = world['client']
    client.get(f"/api/v1/admin/users/{world['alice']}")
    locks = []

    @event.listens_for(engine(app), 'before_cursor_execute')
    def count(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('BEGIN IMMEDIATE') or statement.startswith('INSERT INTO admin_actions'):
            locks.append(statement)
    try:
        assert client.get(f"/api/v1/admin/users/{world['alice']}").status_code == 200
    finally:
        event.remove(engine(app), 'before_cursor_execute', count)
    assert locks == []


def test_each_action_writes_one_allowlisted_row_and_logs_no_identity(app, world, caplog):
    client, headers = world['client'], world['headers']
    with caplog.at_level(logging.DEBUG):
        for name, target, body in (('reset-link', 'alice', {}), ('sign-out', 'alice', {}),
                                   ('revoke-agents', 'alice', {}), ('verify', 'bob', {'confirm': 'verify'})):
            before = len(actions(app))
            assert client.post(f"/api/v1/admin/users/{world[target]}/{name}", json=body, headers=headers).status_code == 200
            new = actions(app)[before:]
            assert len(new) == 1 and new[0].actor == 'web' and new[0].admin_user_id == world['admin']
            assert new[0].target_user_id == world[target]
            assert set(new[0].detail) <= set(admin.DETAIL_KEYS[new[0].action])
        client.post('/api/v1/admin/users/query', json={'q': 'searchtext-canary'}, headers=headers)
    text_ = '\n'.join(record.getMessage() for record in caplog.records)
    for secret in (ALICE, BOB, ADMIN_EMAIL, 'nmr_', 'searchtext-canary'):
        assert secret not in text_
    assert f"admin signed out user_id={world['alice']} admin_id={world['admin']}" in text_


@pytest.mark.parametrize('target', ['self', 'other_admin', 'owner'])
def test_actions_never_target_admins_or_the_owner(app, world, target):
    add_user(app, 'second@example.com', 'second-password-1')
    assert cli(app, 'user', 'set-admin', 'second@example.com').exit_code == 0
    with app.app_context():
        second = credentials.find('second@example.com').user_id
    client = app.test_client()
    headers = sign_in(client, 'second@example.com', 'second-password-1')
    target_id = {'self': second, 'other_admin': world['admin'], 'owner': 1}[target]
    if target == 'owner':
        with engine(app).begin() as connection:  # The owner may be unverified and not an admin: still refused.
            connection.execute(update(User).where(User.id == 1).values(email_verified_at=None))
    with engine(app).connect() as connection:
        version = connection.scalar(select(User.session_version).where(User.id == target_id))
    before = len(actions(app))
    for name, body in (('reset-link', {}), ('sign-out', {}), ('revoke-agents', {'include_harness': True}),
                       ('verify', {'confirm': 'verify'})):
        response = client.post(f'/api/v1/admin/users/{target_id}/{name}', json=body, headers=headers)
        assert response.status_code == 409 and response.json['code'] == 'admin_target', name
    with engine(app).connect() as connection:
        assert connection.scalar(select(User.session_version).where(User.id == target_id)) == version
        assert connection.scalar(select(PasswordResetToken.id)) is None
    assert len(actions(app)) == before


def test_audit_view_lists_newest_first(app, world):
    client, headers = world['client'], world['headers']
    client.post(f"/api/v1/admin/users/{world['alice']}/sign-out", json={}, headers=headers)
    listed = client.get('/api/v1/admin/audit?limit=2').json['actions']
    assert [item['action'] for item in listed] == ['sign_out_everywhere', 'grant_admin']
    assert listed[1]['actor'] == 'cli' and listed[1]['admin_user_id'] is None
    for bad in ('0', '501', 'x', '²', '١٠', '+5', ' 5', '10000'):
        assert client.get('/api/v1/admin/audit', query_string={'limit': bad}).status_code == 400, bad


def test_record_refuses_unknown_details_and_anonymous_web_rows(app):
    with app.app_context(), engine(app).begin() as connection:
        with pytest.raises(ValueError):
            admin.record(connection, 'reset_link', admin_id=1, target=2, link='https://x')
        with pytest.raises(ValueError):
            admin.record(connection, 'reset_link', admin_id=1, target=2, minutes='60')
        with pytest.raises(ValueError):
            admin.record(connection, 'sign_out_everywhere', target=2)  # web without an admin
        with pytest.raises(ValueError):
            admin.record(connection, 'grant_admin', actor='cli', admin_id=1, target=2)
    with closing(sqlite3.connect(database_path(app))) as raw:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO admin_actions (created_at, actor, admin_user_id, action, detail) "
                        "VALUES ('2026-01-01', 'web', NULL, 'step_up', '{}')")


def test_maintenance_prunes_old_audit_rows(app, world):
    with engine(app).begin() as connection:
        connection.execute(update(AdminAction).values(created_at=now() - timedelta(days=admin.ADMIN_ACTION_DAYS + 1)))
    world['client'].get(f"/api/v1/admin/users/{world['alice']}")
    deletion.maintenance(app)
    assert [row.action for row in actions(app)] == ['view_user']


def test_sentry_sees_the_admin_never_the_target_or_any_email(tmp_path, monkeypatch):
    from telemetry_helpers import CaptureTransport, sentry_app, shutdown_sentry
    transport = CaptureTransport()
    try:
        app = sentry_app(tmp_path, transport, MAIL_BACKEND='memory', PUBLIC_BASE_URL=BASE_URL)
        admin_id = add_user(app, ADMIN_EMAIL, ADMIN_PASSWORD)
        alice = add_user(app, ALICE, ALICE_PASSWORD)
        cli(app, 'user', 'set-admin', ADMIN_EMAIL)
        client = app.test_client()
        headers = sign_in(client, ADMIN_EMAIL, ADMIN_PASSWORD)

        secret = 'nmr' + '_' + 'leak' * 6

        def boom(*args, **kwargs):
            raise RuntimeError(' '.join(('failure for', credentials.read(alice).email, secret)))
        monkeypatch.setattr(admin, 'mint_reset_token', boom, raising=False)
        from jobtracker import password_reset
        monkeypatch.setattr(password_reset, 'mint_reset_token', boom)
        assert client.post(f'/api/v1/admin/users/{alice}/reset-link', json={}, headers=headers).status_code == 500
        [event_] = transport.events()
        raw = transport.raw().decode()
        assert ALICE not in raw and ADMIN_EMAIL not in raw and secret not in raw
        assert event_['user'] == {'id': str(admin_id)}
        assert event_['transaction'] == '/api/v1/admin/users/<int:user_id>/reset-link'
    finally:
        shutdown_sentry()


# last_seen_at -------------------------------------------------------------------
def test_last_seen_is_hourly_and_only_from_browser_sessions(app, world):
    with engine(app).begin() as connection:
        connection.execute(update(User).values(last_seen_at=None))
    writes = []

    @event.listens_for(engine(app), 'before_cursor_execute')
    def count(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('UPDATE users SET last_seen_at') and 'IS NULL' in statement:
            writes.append(statement)
    try:
        for headers in (world['alice_oauth'], world['alice_agent'], world['alice_harness'],
                        {'Authorization': 'Bearer ' + LEGACY}):
            app.test_client().get('/api/v1/jobs', headers=headers)
            app.test_client().get('/api/v1/jobs/search', headers=headers)
        assert writes == []
        alice = app.test_client()
        sign_in(alice, ALICE, ALICE_PASSWORD)  # login itself is not session-authenticated
        for _ in range(3):
            alice.get('/api/v1/auth/session')
            alice.get('/api/v1/jobs')
        alice.head('/api/v1/jobs')
        assert len(writes) == 1
        with app.app_context():
            seen = credentials.read(world['alice']).last_seen_at
        assert (seen.minute, seen.second, seen.microsecond) == (0, 0, 0)
        with engine(app).begin() as connection:
            connection.execute(update(User).where(User.id == world['alice']).values(
                last_seen_at=now() - timedelta(hours=2)))
        alice.get('/api/v1/jobs')
        assert len(writes) == 2
    finally:
        event.remove(engine(app), 'before_cursor_execute', count)
    with app.app_context():
        assert credentials.read(1).last_seen_at is None


def test_a_failed_last_seen_write_never_fails_the_request(app, world):
    with engine(app).begin() as connection:
        connection.execute(update(User).values(last_seen_at=None))

    @event.listens_for(engine(app), 'before_cursor_execute')
    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('UPDATE users SET last_seen_at'):
            raise OperationalError(statement, parameters, Exception('database is locked'))
    try:
        alice = app.test_client()
        sign_in(alice, ALICE, ALICE_PASSWORD)
        assert alice.get('/api/v1/jobs').status_code == 200
    finally:
        event.remove(engine(app), 'before_cursor_execute', fail)


# CLI ----------------------------------------------------------------------------
def test_set_admin_cli(app, world, caplog):
    with caplog.at_level(logging.INFO, logger='jobtracker.accounts'):
        assert cli(app, 'user', 'set-admin', ALICE).exit_code == 0
        assert 'already has' in cli(app, 'user', 'set-admin', ALICE).stdout
        assert cli(app, 'user', 'set-admin', '--id', str(world['alice']), '--revoke').exit_code == 0
    for args in ((BOB,), ('--id', str(world['messaging'])), ('nobody@example.com',), ()):
        result = cli(app, 'user', 'set-admin', *args)
        assert result.exit_code != 0
    assert [(row.actor, row.action, row.target_user_id) for row in actions(app)][-2:] == [
        ('cli', 'grant_admin', world['alice']), ('cli', 'revoke_admin', world['alice'])]
    lines = [record.getMessage() for record in caplog.records]
    assert f"admin granted user_id={world['alice']} operator=cli" in lines and ALICE not in '\n'.join(lines)
    listing = cli(app, 'user', 'list').stdout.splitlines()
    assert [line for line in listing if line.endswith('\tadmin')] == [line for line in listing if ADMIN_EMAIL in line]


@pytest.mark.parametrize('change', ['set-email', 'set-email-verified', 'unverify'])
def test_identity_changes_remove_admin_access(app, world, change):
    args = {'set-email': ('set-email', str(world['admin']), 'moved@example.com'),
            'set-email-verified': ('set-email', str(world['admin']), 'moved@example.com', '--verified'),
            'unverify': ('unverify', ADMIN_EMAIL)}[change]
    result = cli(app, 'user', *args)
    assert result.exit_code == 0 and 'Administrator access removed' in result.stdout
    with app.app_context():
        assert credentials.read(world['admin']).is_admin is False
    assert (actions(app)[-1].actor, actions(app)[-1].action) == ('cli', 'revoke_admin')


def test_mail_status_output_is_unchanged(app):
    lines = cli(app, 'mail', 'status').stdout.splitlines()
    with app.app_context():
        day, month = mailer._config('MAIL_DAILY_LIMIT'), mailer._config('MAIL_MONTHLY_LIMIT')
        signup, reset = mailer._config('MAIL_LIMIT_SIGNUP'), mailer._config('MAIL_LIMIT_RESET')
        new = mailer._category_limit('signup')
    assert lines == ['backend: memory', 'status: ok', 'signup paused: no', f'last 24 h: 0 of {day}',
                     f'  signup: 0 of {signup} (new sign-ups 0 of {new})', f'  reset: 0 of {reset}',
                     f'this month (UTC): 0 of {month}']


# Export, privacy ----------------------------------------------------------------
def test_export_includes_last_seen_admin_flag_and_support_actions(app, world):
    world['client'].post(f"/api/v1/admin/users/{world['alice']}/sign-out", json={}, headers=world['headers'])
    with app.app_context():
        code = deletion.issue_support_code(world['alice'], 'export')
        deletion.build_export(world['alice'], code)
        with engine(app).connect() as connection:
            stored = connection.execute(text('SELECT stored_name FROM data_exports')).scalar()
    archive = zipfile.ZipFile(os.path.join(app.config['UPLOAD_FOLDER'], '_exports', stored))
    account = json.loads(archive.read('account.json'))
    assert set(account['account']) >= {'last_seen_at', 'is_admin'} and account['account']['is_admin'] is False
    assert [item['action'] for item in account['support_actions']] == ['sign_out_everywhere']
    assert set(account['support_actions'][0]) == {'created_at', 'action'}


def test_privacy_text_matches_retention_and_precision():
    privacy = (ROOT / 'frontend/src/Privacy.jsx').read_text()
    assert admin.ADMIN_ACTION_DAYS == 365 and 'Records of support actions:</strong> 1 year' in privacy
    assert 'rounded to the hour' in privacy


# Migration ----------------------------------------------------------------------
def test_17_to_18_preserves_rows_makes_nobody_admin_and_matches_a_fresh_schema(app, world, config, tmp_path):
    with engine(app).connect() as connection:
        before = {table: connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar()
                  for table in inspect(connection).get_table_names() if table != 'admin_actions'}
        users_before = connection.execute(text(
            'SELECT id, email, password_hash, session_version, created_at, email_verified_at FROM users ORDER BY id')).all()
    engine(app).dispose()
    with closing(sqlite3.connect(database_path(app))) as raw:
        assert raw.execute('SELECT sqlite_version()').fetchone()[0] >= '3.35'
        rewind_admin(raw)
        assert 'is_admin' not in [row[1] for row in raw.execute('PRAGMA table_info(users)')]
    upgraded = create_app(config)
    with engine(upgraded).connect() as connection:
        assert connection.scalar(text('SELECT version FROM schema_version')) == 18
        after = {table: connection.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() for table in before}
        assert after == before
        assert connection.execute(text(
            'SELECT id, email, password_hash, session_version, created_at, email_verified_at FROM users ORDER BY id')).all() == users_before
        assert connection.scalar(text('SELECT COUNT(*) FROM users WHERE is_admin OR last_seen_at IS NOT NULL')) == 0
        assert connection.scalar(text('SELECT COUNT(*) FROM admin_actions')) == 0
    fresh = create_app({**config, 'DATABASE_URL': f'sqlite:///{tmp_path / "fresh.sqlite3"}'})
    migrated_inspector, fresh_inspector = inspect(engine(upgraded)), inspect(engine(fresh))
    for table in ('users', 'admin_actions'):
        shape = lambda inspector: sorted((c['name'], str(c['type']), c['nullable'], str(c.get('default')))
                                         for c in inspector.get_columns(table))
        assert shape(migrated_inspector) == shape(fresh_inspector), table
        indexes = lambda inspector: sorted((i['name'], tuple(i['column_names']), bool(i['unique']))
                                           for i in inspector.get_indexes(table))
        assert indexes(migrated_inspector) == indexes(fresh_inspector), table
        checks = lambda inspector: sorted(c['name'] or '' for c in inspector.get_check_constraints(table))
        assert checks(migrated_inspector) == checks(fresh_inspector), table


def test_the_documented_rollback_runs_on_a_fresh_database_too(config, tmp_path):
    app = create_app({**config, 'DATABASE_URL': f'sqlite:///{tmp_path / "fresh.sqlite3"}'})
    add_user(app, ADMIN_EMAIL, ADMIN_PASSWORD)
    cli(app, 'user', 'set-admin', ADMIN_EMAIL)
    engine(app).dispose()
    with closing(sqlite3.connect(database_path(app))) as raw:
        raw.executescript(ADMIN_ROLLBACK_SQL)
        assert raw.execute('SELECT version FROM schema_version').fetchone()[0] == 17
        assert raw.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 2
    docs = (ROOT / 'docs/OPERATIONS.md').read_text()
    for statement in ADMIN_ROLLBACK_SQL.split(';'):
        if statement.strip():
            assert statement.strip() in docs, statement


def test_a_newer_schema_is_refused():
    with pytest.raises(RuntimeError, match='Unsupported database schema'):
        migrations.migrate(None, [19])
