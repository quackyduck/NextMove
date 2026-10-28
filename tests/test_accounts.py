"""Accounts: signup (invites, validation, races, throttling), login and the admin CLI.

No email verification exists and nothing sends email, so recovery is the
`user reset-password` / `reset-workspace-password` admin commands.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from werkzeug.security import generate_password_hash

from jobtracker import create_app, credentials
from jobtracker.models import InviteCode, User, now

from accounts import OWNER, add_user, sign_in

PASSWORD = 'signup-password-1'


@pytest.fixture
def config(tmp_path):
    return {
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'accounts-test-secret',
        'APP_PASSWORD_HASH': generate_password_hash('test-password', method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "accounts.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
    }


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def client(app):
    return app.test_client()


def invite(app, days=7):
    with app.app_context():
        [code] = credentials.create_invites(1, days)
    return code


def signup_body(email='new@example.com', password=PASSWORD, confirmation=None, code=None):
    body = {'email': email, 'password': password,
            'password_confirmation': password if confirmation is None else confirmation}
    if code is not None:
        body['invite_code'] = code
    return body


def unused_invites(app):
    with app.extensions['jobtracker_db']['engine'].connect() as connection:
        return connection.scalar(select(InviteCode.id).where(InviteCode.used_at.is_(None)).limit(1)) is not None


def users(app):
    with app.extensions['jobtracker_db']['engine'].connect() as connection:
        return connection.execute(select(User.id, User.email).order_by(User.id)).all()


def test_signup_starts_a_session_with_working_csrf(app, client):
    response = client.post('/api/v1/auth/signup', json=signup_body(code=invite(app)))
    assert response.status_code == 201
    assert response.json['authenticated'] is True and response.json['user']['email'] == 'new@example.com'
    session = client.get('/api/v1/auth/session').json
    assert set(session) == {'authenticated', 'csrf_token', 'user', 'signup'}
    assert session['authenticated'] is True and session['csrf_token'] == response.json['csrf_token']
    assert set(session['user']) == {'id', 'email', 'created_at'}
    assert session['user']['id'] == 2 and session['user']['created_at'].endswith('Z')
    assert session['signup'] == {'enabled': True, 'invite_required': True}
    assert client.post('/api/v1/jobs', json={'title': 'Mine'}).status_code == 403
    created = client.post('/api/v1/jobs', json={'title': 'Mine'}, headers={'X-CSRF-Token': session['csrf_token']})
    assert created.status_code == 201
    assert [job['title'] for job in client.get('/api/v1/jobs').json['jobs']] == ['Mine']


def test_anonymous_session_reports_signup_settings(app, client):
    assert client.get('/api/v1/auth/session').json == {
        'authenticated': False, 'csrf_token': None, 'user': None,
        'signup': {'enabled': True, 'invite_required': True}}


def test_email_is_trimmed_and_lowercased(app, client):
    assert client.post('/api/v1/auth/signup', json=signup_body(' A@X.COM ', code=invite(app))).status_code == 201
    assert users(app)[-1].email == 'a@x.com'
    sign_in(app.test_client(), 'a@x.com', PASSWORD)
    sign_in(app.test_client(), '  A@x.Com', PASSWORD)


@pytest.mark.parametrize('body, field', [
    (signup_body('not-an-email'), 'email'),
    (signup_body('a@b'), 'email'),
    (signup_body(''), 'email'),
    (signup_body(password='short'), 'password'),
    (signup_body(password='x' * 129), 'password'),
    (signup_body(confirmation='different-password'), 'password_confirmation'),
])
def test_invalid_signups_name_the_field_and_keep_the_invite(app, client, body, field):
    code = invite(app)
    response = client.post('/api/v1/auth/signup', json={**body, 'invite_code': code})
    assert response.status_code == 400 and response.json['field'] == field
    assert unused_invites(app) and len(users(app)) == 1
    assert client.get('/api/v1/auth/session').json['authenticated'] is False


def test_missing_unknown_used_and_expired_invites_are_refused(app, client):
    for code in (None, '', 'short', 'definitely-not-a-real-invite'):
        response = client.post('/api/v1/auth/signup', json=signup_body(code=code))
        assert response.status_code == 403 and response.json['field'] == 'invite_code'
    used = invite(app)
    assert app.test_client().post('/api/v1/auth/signup', json=signup_body('first@example.com', code=used)).status_code == 201
    again = client.post('/api/v1/auth/signup', json=signup_body('second@example.com', code=used))
    assert again.status_code == 403 and again.json['field'] == 'invite_code'
    expired = invite(app)
    with app.extensions['jobtracker_db']['engine'].begin() as connection:
        connection.execute(update(InviteCode).where(InviteCode.code_hash == credentials.invite_digest(expired))
                           .values(expires_at=now() - timedelta(seconds=1)))
    response = client.post('/api/v1/auth/signup', json=signup_body('third@example.com', code=expired))
    assert response.status_code == 403 and response.json['field'] == 'invite_code'
    assert [row.email for row in users(app)] == [OWNER, 'first@example.com']


def test_duplicate_email_conflicts_and_uses_up_the_invite(app, client):
    """One invite answers at most one "is this email registered?" question."""
    add_user(app, 'taken@example.com')
    code = invite(app)
    response = client.post('/api/v1/auth/signup', json=signup_body(' TAKEN@example.com', code=code))
    assert response.status_code == 409 and response.json['field'] == 'email'
    assert not unused_invites(app)
    reused = client.post('/api/v1/auth/signup', json=signup_body('fresh@example.com', code=code))
    assert reused.status_code == 403 and reused.json['field'] == 'invite_code'


def test_an_invite_is_single_use_under_concurrency(app):
    code = invite(app)

    def attempt(index):
        return app.test_client().post('/api/v1/auth/signup', json=signup_body(f'racer{index}@example.com', code=code)).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(attempt, range(2)))
    assert statuses == [201, 403]
    assert len(users(app)) == 2


def test_open_signup_needs_no_invite(config):
    app = create_app({**config, 'SIGNUP_REQUIRES_INVITE': False})
    client = app.test_client()
    assert client.get('/api/v1/auth/session').json['signup'] == {'enabled': True, 'invite_required': False}
    # An invite supplied anyway is ignored rather than validated.
    response = client.post('/api/v1/auth/signup', json=signup_body(code='ignored-invite-code'))
    assert response.status_code == 201
    assert client.post('/api/v1/auth/signup', json=signup_body()).status_code == 409


def test_closed_signup_is_refused(config):
    app = create_app({**config, 'SIGNUP_ENABLED': False})
    client = app.test_client()
    assert client.get('/api/v1/auth/session').json['signup']['enabled'] is False
    assert client.post('/api/v1/auth/signup', json=signup_body(code=invite(app))).status_code == 403
    assert len(users(app)) == 1


def test_cross_origin_signup_is_refused(app, client):
    response = client.post('/api/v1/auth/signup', json=signup_body(code=invite(app)),
                           headers={'Origin': 'https://evil.example'})
    assert response.status_code == 403 and unused_invites(app) and len(users(app)) == 1


def test_signup_is_throttled_per_ip(config):
    app = create_app({**config, 'SIGNUP_REQUIRES_INVITE': False})
    client = app.test_client()
    for index in range(10):
        assert client.post('/api/v1/auth/signup', json=signup_body(f'user{index}@example.com')).status_code == 201
    denied = client.post('/api/v1/auth/signup', json=signup_body('one-more@example.com'))
    assert denied.status_code == 429 and denied.headers['Retry-After'] == '300'
    other_ip = client.post('/api/v1/auth/signup', json=signup_body('elsewhere@example.com'),
                           environ_base={'REMOTE_ADDR': '198.51.100.7'})
    assert other_ip.status_code == 201


def test_login_does_not_reveal_whether_an_account_exists(app, client):
    add_user(app, 'known@example.com', 'right-password-1')
    wrong = client.post('/api/v1/auth/login', json={'email': 'known@example.com', 'password': 'wrong-password'})
    unknown = client.post('/api/v1/auth/login', json={'email': 'nobody@example.com', 'password': 'wrong-password'})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.data == unknown.data == b'{"error":"Invalid email or password"}\n'


def test_login_requires_an_email(client):
    for body in ({'password': 'test-password'}, {'email': '', 'password': 'test-password'},
                 {'email': 'owner', 'password': 'test-password'}):
        response = client.post('/api/v1/auth/login', json=body)
        assert response.status_code == 401 and response.json == {'error': 'Invalid email or password'}
    sign_in(client)


def cli(app, *args, **kwargs):
    return app.test_cli_runner().invoke(args=list(args), **kwargs)


def test_invite_create_prints_working_single_use_codes(app):
    result = cli(app, 'invite', 'create', '--count', '3', '--expires-days', '2')
    assert result.exit_code == 0, result.output
    codes = result.output.split()
    assert len(codes) == len(set(codes)) == 3
    with app.extensions['jobtracker_db']['engine'].connect() as connection:
        rows = connection.execute(select(InviteCode.__table__)).all()
    assert len(rows) == 3
    for row in rows:
        assert timedelta(days=2) - timedelta(minutes=1) < row.expires_at - row.created_at <= timedelta(days=2, seconds=1)
    for index, code in enumerate(codes):
        assert app.test_client().post('/api/v1/auth/signup',
                                      json=signup_body(f'invited{index}@example.com', code=code)).status_code == 201
        assert app.test_client().post('/api/v1/auth/signup',
                                      json=signup_body(f'reuse{index}@example.com', code=code)).status_code == 403


def test_invite_list_shows_prefix_and_status_never_the_code(app):
    used, unused, expired = cli(app, 'invite', 'create', '--count', '3').output.split()
    assert app.test_client().post('/api/v1/auth/signup', json=signup_body(code=used)).status_code == 201
    with app.extensions['jobtracker_db']['engine'].begin() as connection:
        connection.execute(update(InviteCode).where(InviteCode.code_hash == credentials.invite_digest(expired))
                           .values(expires_at=now() - timedelta(minutes=1)))
    result = cli(app, 'invite', 'list')
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 3
    for line, code, status in zip(lines, (used, unused, expired), ('used by user 2', 'unused', 'expired')):
        assert code[:6] + '…' in line and status in line
        assert code not in result.output


def test_reset_password_changes_it_and_revokes_only_that_users_sessions(app):
    add_user(app, 'target@example.com', 'old-password-1')
    add_user(app, 'bystander@example.com', 'other-password-1')
    target, bystander = app.test_client(), app.test_client()
    sign_in(target, 'target@example.com', 'old-password-1')
    sign_in(bystander, 'bystander@example.com', 'other-password-1')
    result = cli(app, 'user', 'reset-password', ' Target@Example.com', input='newpass123\nnewpass123\n')
    assert result.exit_code == 0, result.output
    assert 'Password reset for target@example.com' in result.output and 'newpass123' not in result.output
    assert target.get('/api/v1/jobs').status_code == 401
    assert bystander.get('/api/v1/jobs').status_code == 200
    assert app.test_client().post('/api/v1/auth/login', json={
        'email': 'target@example.com', 'password': 'old-password-1'}).status_code == 401
    sign_in(app.test_client(), 'target@example.com', 'newpass123')


def test_reset_password_rejects_unknown_email_and_short_password(app):
    result = cli(app, 'user', 'reset-password', 'nobody@example.com', input='newpass123\nnewpass123\n')
    assert result.exit_code != 0 and 'No account with that email' in result.output
    result = cli(app, 'user', 'reset-password', OWNER, input='short\nshort\n')
    assert result.exit_code != 0 and 'between 8 and 128' in result.output
    sign_in(app.test_client())


def test_set_email_replaces_the_sign_in_address(app):
    add_user(app, 'other@example.com')
    result = cli(app, 'user', 'set-email', '1', ' New@Example.com ')
    assert result.exit_code == 0 and 'new@example.com' in result.output
    sign_in(app.test_client(), 'new@example.com', 'test-password')
    for args, message in ((('1', 'not-an-email'), 'valid email'), (('1', 'other@example.com'), 'already uses'),
                          (('99', 'free@example.com'), 'No account with that ID')):
        result = cli(app, 'user', 'set-email', *args)
        assert result.exit_code != 0 and message in result.output
    assert [row.email for row in users(app)] == ['new@example.com', 'other@example.com']


def test_user_list_shows_every_account(app):
    add_user(app, 'second@example.com')
    result = cli(app, 'user', 'list')
    assert result.exit_code == 0
    rows = [line.split('\t') for line in result.output.splitlines()]
    assert [(row[0], row[1]) for row in rows] == [('1', OWNER), ('2', 'second@example.com')]
    assert all('pbkdf2' not in line and 'scrypt' not in line for line in result.output.splitlines())


def test_legacy_reset_workspace_password_resets_user_one(app):
    add_user(app, 'second@example.com', 'second-password')
    owner, second = app.test_client(), app.test_client()
    sign_in(owner)
    sign_in(second, 'second@example.com', 'second-password')
    result = cli(app, 'reset-workspace-password', input='newpass123\nnewpass123\n')
    assert result.exit_code == 0, result.output
    assert owner.get('/api/v1/jobs').status_code == 401 and second.get('/api/v1/jobs').status_code == 200
    sign_in(app.test_client(), OWNER, 'newpass123')
