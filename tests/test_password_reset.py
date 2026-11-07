"""Self-service password reset (schema 16): request, worker, link status, completion, revocation, CLI, migration."""
from contextlib import closing
import hashlib
import logging
import re
import sqlite3
import threading
from datetime import timedelta

import pytest
from sqlalchemy import event, insert, inspect, select, text, update
from werkzeug.security import check_password_hash, generate_password_hash

from jobtracker import create_app, credentials, harness, mailer, oauth, password_reset, pow, taskqueue
from jobtracker import agent_tokens
from jobtracker.models import (AgentToken, EmailConfirmation, HarnessGrant, OAuthClient, OAuthGrant, OAuthToken,
                               PasswordResetRequest, PasswordResetToken, QueuedTask, User, new_id, now)

from accounts import add_user, sign_in
from legacy import database_path, rewind_password_reset

PASSWORD = 'test-password'
NEW_PASSWORD = 'a-brand-new-password'
BASE_URL = 'https://example.test/nextmove'
TOKEN = re.compile(r'/nextmove/reset-password#token=(nmr_[A-Za-z0-9_-]+)')


@pytest.fixture
def config(tmp_path, monkeypatch):
    for name in list(mailer.SETTINGS) + ['PASSWORD_RESET_ENABLED', 'PASSWORD_RESET_MINUTES']:
        monkeypatch.delenv(name, raising=False)
    return {
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'reset-test-secret-0123456789abcdef',
        'APP_PASSWORD_HASH': generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "reset.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
        'PUBLIC_BASE_URL': BASE_URL, 'APP_ROOT_PATH': '', 'MAIL_BACKEND': 'memory', 'POW_BITS': 2,
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


def proof(app, purpose='reset'):
    with app.app_context():
        issued = pow.issue(purpose)
    nonce = 0
    while pow.leading_zero_bits(hashlib.sha256(f'{issued["challenge"]}:{nonce}'.encode()).digest()) < issued['bits']:
        nonce += 1
    return {'challenge': issued['challenge'], 'nonce': str(nonce)}


def ip(address):
    return {'environ_base': {'REMOTE_ADDR': address}}


def request_reset(app, client, email, address='192.0.2.10', **kwargs):
    return client.post('/api/v1/auth/password-reset/request', json={'email': email, 'proof': proof(app)},
                       **ip(address), **kwargs)


def drain(app):
    return taskqueue.drain(app)


def mails_to(address):
    return [message for message in mailer.outbox if message['To'] == address]


def body(message):
    return message.get_body(('plain',)).get_content()


def link_token(message):
    match = TOKEN.search(body(message))
    assert match, body(message)
    return match.group(1)


def emailed_token(app, client, email, **kwargs):
    before = len(mails_to(email))
    assert request_reset(app, client, email, **kwargs).status_code == 202
    drain(app)
    messages = mails_to(email)
    assert len(messages) == before + 1
    return link_token(messages[-1])


def operator_token(app, email):
    result = app.test_cli_runner().invoke(args=['user', 'reset-link', email])
    assert result.exit_code == 0, result.output + result.stderr
    match = TOKEN.search(result.stdout)
    assert match, result.stdout
    return match.group(1)


def complete(client, token, password=NEW_PASSWORD, confirmation=None, **kwargs):
    return client.post('/api/v1/auth/password-reset/complete', json={
        'token': token, 'password': password,
        'password_confirmation': password if confirmation is None else confirmation}, **kwargs)


def status(client, token, **kwargs):
    return client.post('/api/v1/auth/password-reset/status', json={'token': token}, **kwargs)


def user_row(app, user_id):
    with engine(app).connect() as connection:
        return connection.execute(select(User.__table__).where(User.id == user_id)).one()


def messaging_account(app, email=None, verified=False):
    """A chat-created account with a live harness grant; with `email`, as a pre-release activation left it."""
    with engine(app).begin() as connection:
        values = dict(session_version=1, created_at=now())
        if email:
            values.update(email=email, password_hash=generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000'),
                          email_verified_at=now() if verified else None)
        user_id = connection.execute(insert(User).values(**values)).inserted_primary_key[0]
        grant = harness.create_grant(connection, user_id)
    return user_id, grant


def connect_agents(app, user_id):
    """One OAuth connection with an access token, and one managed agent token. Returns the raw secrets."""
    access, agent = 'nm_oat_' + 'a' * 43 + str(user_id), 'nm_ingest_' + 'b' * 43 + str(user_id)
    with engine(app).begin() as connection:
        client_id = f'client-{user_id}'
        connection.execute(insert(OAuthClient).values(id=client_id, name='Agent', redirect_uris=['https://a.test/cb'],
                                                      created_at=now()))
        grant_id = new_id()
        connection.execute(insert(OAuthGrant).values(id=grant_id, user_id=user_id, client_id=client_id,
                                                     scopes=['jobs:read'], created_at=now()))
        connection.execute(insert(OAuthToken).values(id=new_id(), grant_id=grant_id, kind='access',
                                                     token_hash=hashlib.sha256(access.encode()).hexdigest(),
                                                     created_at=now(), expires_at=now() + timedelta(hours=1)))
        connection.execute(insert(AgentToken).values(
            id=new_id(), user_id=user_id, name='bot', prefix=agent[:18],
            token_hash=hashlib.sha256(agent.encode()).hexdigest(), permissions=['roles:ingest'], created_at=now(),
            expires_at=now() + timedelta(days=30)))
    return access, agent


def agents_live(app, access, agent):
    with app.test_request_context():
        return oauth.authenticate_access(access) is not None, agent_tokens.authenticate(agent) is not None


def harness_live(app, grant):
    with app.app_context():
        return harness.authenticate_grant(grant, 'resumes:read') is not None


# Request ------------------------------------------------------------------------
def test_request_is_identical_for_every_address_and_reads_no_users(app, client):
    add_user(app, 'verified@example.com')
    add_user(app, 'legacy@example.com', verified=False)
    messaging_account(app)
    activating, _ = messaging_account(app)
    with engine(app).begin() as connection:
        connection.execute(insert(EmailConfirmation).values(
            id=new_id(), kind='signup', email='pending@example.com', password_hash='x', password_attempts=0,
            created_at=now(), delivery='sent'))
        connection.execute(insert(EmailConfirmation).values(
            id=new_id(), kind='activation', email='activating@example.com', user_id=activating, password_hash='x',
            password_attempts=0, created_at=now(), delivery='sent'))
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)
    event.listen(engine(app), 'before_cursor_execute', record)
    responses = [request_reset(app, client, email, address=f'192.0.2.{n}') for n, email in enumerate((
        'verified@example.com', 'legacy@example.com', 'pending@example.com', 'activating@example.com',
        'nobody@example.com', 'owner@localhost'), start=20)]
    event.remove(engine(app), 'before_cursor_execute', record)
    assert {(r.status_code, r.get_data(as_text=True)) for r in responses} == {
        (202, responses[0].get_data(as_text=True))}
    assert responses[0].json == {'message': password_reset.ACCEPTED}
    assert not [s for s in statements if re.search(r'\bFROM users\b|\bJOIN users\b', s)]
    with engine(app).connect() as connection:
        tasks = connection.execute(select(QueuedTask.user_id, QueuedTask.kind)).all()
    assert tasks and all(row == (None, 'password_reset_request') for row in tasks)
    drain(app)
    assert {message['To'] for message in mailer.outbox} == {'verified@example.com', 'legacy@example.com'}
    assert len(mailer.outbox) == 2  # No reset mail and no verification mail for pending addresses.
    with engine(app).connect() as connection:
        assert connection.scalar(select(QueuedTask.id)) is None  # delete_on_success: no address left behind.
        history = connection.execute(select(PasswordResetRequest.outcome)).scalars().all()
    assert sorted(history) == ['sent', 'sent', 'undeliverable']  # owner@localhost: an account, but no mailbox.


def test_over_budget_address_is_silent_and_shares_canonical_variants(app, client):
    add_user(app, 'jane.doe@gmail.com')
    first = request_reset(app, client, 'jane.doe@gmail.com', address='192.0.2.30')
    for variant in ('janedoe@gmail.com', 'JaneDoe+x@googlemail.com', 'j.a.n.e.doe+y@gmail.com'):
        response = request_reset(app, client, variant, address='192.0.2.30')
        assert (response.status_code, response.json) == (first.status_code, first.json)
    drain(app)
    with engine(app).connect() as connection:
        queued = connection.scalar(text("SELECT count(*) FROM queued_tasks"))
    assert len(mailer.outbox) == 1 and queued == 0  # 3 per hour per canonical address; the 4th enqueued nothing.
    unknown = [request_reset(app, client, 'ghost@example.com', address='192.0.2.40') for n in range(4)]
    assert {(r.status_code, r.get_data(as_text=True)) for r in unknown} == {(202, first.get_data(as_text=True))}


def queued_requests(app):
    with engine(app).connect() as connection:
        return connection.scalar(text("SELECT count(*) FROM queued_tasks WHERE kind='password_reset_request'"))


def test_a_stranger_cannot_spend_the_owners_reset_budget(app, client, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr('jobtracker.throttle.time.time', lambda: clock[0])
    for n in range(3):  # The attacker spends the hourly budget from one network...
        request_reset(app, client, 'owner@example.com', address='203.0.113.5')
    assert queued_requests(app) == 3
    request_reset(app, client, 'owner@example.com', address='203.0.113.5')
    assert queued_requests(app) == 3  # ...and is then refused (silently: same 202).
    request_reset(app, client, 'owner@example.com', address='198.51.100.20')
    assert queued_requests(app) == 4  # The owner, who barely touched the address, still gets a link.
    for hour in range(1, 3):  # Over the day, the attacker returns hourly, from fresh networks too.
        clock[0] += 3601
        request_reset(app, client, 'owner@example.com', address=f'203.0.113.{10 + hour}')
    assert queued_requests(app) == 6
    clock[0] += 3601
    for address in ('198.51.100.21', '198.51.100.22'):
        request_reset(app, client, 'owner@example.com', address=address)
    assert queued_requests(app) == 6  # Capped at 6 a day per address, however many clients ask.


def test_ip_budget_is_five_per_five_minutes_and_ipv6_is_grouped_by_64(app, client):
    for n in range(5):
        assert request_reset(app, client, f'u{n}@example.com', address='2001:db8:1:2::10').status_code == 202
    blocked = request_reset(app, client, 'other@example.com', address='2001:db8:1:2:ffff::1')
    assert blocked.status_code == 429 and blocked.headers['Retry-After'] == '300'
    assert request_reset(app, client, 'other@example.com', address='2001:db8:1:3::10').status_code == 202


def test_ip_budget_is_eight_per_day(app, client, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr('jobtracker.throttle.time.time', lambda: clock[0])
    for n in range(8):
        clock[0] += 301 if n % 5 == 0 and n else 1
        assert request_reset(app, client, f'd{n}@example.com', address='198.51.100.7').status_code == 202
    clock[0] += 400
    assert request_reset(app, client, 'd9@example.com', address='198.51.100.7').status_code == 429


@pytest.mark.parametrize('bad', [None, {}, {'challenge': 'x', 'nonce': '1'}, 'reuse', 'signup'])
def test_proof_of_work_is_checked_before_any_throttle(app, client, bad):
    if bad == 'reuse':
        spent = proof(app)
        client.post('/api/v1/auth/password-reset/request', json={'email': 'a@example.com', 'proof': spent})
        bad = spent
    elif bad == 'signup':
        bad = proof(app, 'signup')
    with closing(sqlite3.connect(app.extensions['login_throttle_path'])) as connection:
        before = connection.execute("SELECT count(*) FROM keyed_attempts").fetchone()[0]
    response = client.post('/api/v1/auth/password-reset/request', json={'email': 'a@example.com', 'proof': bad})
    assert response.status_code == 400 and response.json['code'] == 'proof_invalid'
    with closing(sqlite3.connect(app.extensions['login_throttle_path'])) as connection:
        after = connection.execute("SELECT count(*) FROM keyed_attempts").fetchone()[0]
    assert after == before


def test_request_validation_origin_and_disabled_states(app, client, config):
    assert client.post('/api/v1/auth/password-reset/request', json={'email': 'a@example.com', 'proof': proof(app)},
                       headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.post('/api/v1/auth/password-reset/request', data='email=a',
                       content_type='application/x-www-form-urlencoded').status_code == 400
    bad = client.post('/api/v1/auth/password-reset/request', json={'email': 'not an email', 'proof': proof(app)})
    assert bad.status_code == 400 and bad.json['field'] == 'email'
    assert client.get('/api/v1/auth/session').json['password_reset'] == {'enabled': True}
    for overrides in ({'PASSWORD_RESET_ENABLED': False}, {'MAIL_BACKEND': 'none'}, {'PUBLIC_BASE_URL': ''}):
        other_app = create_app({**config, **overrides})
        if 'PUBLIC_BASE_URL' in overrides:
            other_app.config['MAIL_LINK_BASE'] = ''  # As with a real backend: no loopback fallback.
        other = other_app.test_client()
        assert other.get('/api/v1/auth/session').json['password_reset'] == {'enabled': False}
        response = other.post('/api/v1/auth/password-reset/request', json={'email': 'a@example.com'})
        assert response.status_code == 503 and response.json['code'] == 'reset_unavailable'


# Worker -------------------------------------------------------------------------
def test_link_uses_public_base_url_once_and_ignores_the_host_header(app, client):
    add_user(app, 'user@example.com')
    request_reset(app, client, 'user@example.com', headers={'Host': 'evil.test', 'X-Forwarded-Host': 'evil.test'})
    drain(app)
    (message,) = mailer.outbox
    assert message['Subject'] == 'Reset your My Next Moov password'
    text_body = body(message)
    assert f'{BASE_URL}/reset-password#token=nmr_' in text_body and 'evil.test' not in text_body
    assert text_body.count('/nextmove') == 1 and 'expires in 60 minutes' in text_body


def test_raw_token_and_address_are_never_stored_or_logged(app, client, caplog):
    caplog.set_level(logging.DEBUG)
    add_user(app, 'secret@example.com')
    token = emailed_token(app, client, 'secret@example.com')
    with engine(app).connect() as connection:
        for table in inspect(connection).get_table_names():
            for row in connection.execute(text(f'SELECT * FROM "{table}"')):
                assert token not in repr(row), table
        assert connection.scalar(text("SELECT count(*) FROM queued_tasks WHERE payload LIKE '%secret@%'")) == 0
    with closing(sqlite3.connect(app.extensions['login_throttle_path'])) as connection:
        assert token not in repr(connection.execute('SELECT * FROM keyed_attempts').fetchall())
    logged = '\n'.join(record.getMessage() for record in caplog.records)
    assert token not in logged and 'secret@example.com' not in logged


def test_worker_skips_undeliverable_addresses_and_dns_failures(app, client, monkeypatch):
    add_user(app, 'owner2@example.com')
    request_reset(app, client, 'owner@localhost')
    app.config['MAIL_CHECK_RECIPIENT_DNS'] = True
    monkeypatch.setattr(mailer, '_resolve', lambda domain: ('dns', None))
    request_reset(app, client, 'owner2@example.com', address='192.0.2.50')
    drain(app)
    assert mailer.outbox == []
    with engine(app).connect() as connection:
        assert connection.execute(select(PasswordResetRequest.outcome)).scalars().all() == ['undeliverable'] * 2
        assert connection.scalar(select(PasswordResetToken.id)) is None


def test_mail_errors_retry_supersede_and_quota_defers_without_using_attempts(app, client, monkeypatch):
    add_user(app, 'retry@example.com')
    calls = []

    def flaky(to, subject, text_, html=None, **kwargs):
        calls.append(TOKEN.search(text_).group(1))
        if len(calls) == 1:
            raise mailer.TransientMailError('timeout')
        mailer.outbox.append(mailer.build_message(to, subject, text_, html))
    monkeypatch.setattr(mailer, 'send_email', flaky)
    request_reset(app, client, 'retry@example.com')
    taskqueue.run_once(app)
    with engine(app).begin() as connection:
        connection.execute(update(QueuedTask).values(run_after=now()))
    taskqueue.run_once(app)
    assert len(calls) == 2 and calls[0] != calls[1]
    with app.test_request_context():
        assert password_reset.link_status(calls[0]) == (False, None)
        assert password_reset.link_status(calls[1])[0]

    def quota(*args, **kwargs):
        raise mailer.MailQuotaExceeded('spent')
    monkeypatch.setattr(mailer, 'send_email', quota)
    with engine(app).begin() as connection:
        connection.execute(update(PasswordResetRequest).values(created_at=now() - timedelta(minutes=5)))
    request_reset(app, client, 'retry@example.com', address='192.0.2.60')
    for _ in range(4):
        assert taskqueue.run_once(app) == 'deferred'
        with engine(app).begin() as connection:
            connection.execute(update(QueuedTask).values(run_after=now()))
    with engine(app).connect() as connection:
        assert connection.execute(select(QueuedTask.status, QueuedTask.attempts)).one() == ('queued', 0)
        connection.execute(update(QueuedTask).values(created_at=now() - timedelta(hours=49)))
        connection.commit()
    assert taskqueue.run_once(app) == 'failed'  # The queue's 48 h max_deferral; no deadline of our own.


def test_permanent_mail_error_is_final_and_dedupe_holds_for_a_minute(app, client, monkeypatch):
    add_user(app, 'perm@example.com')
    emailed_token(app, client, 'perm@example.com')
    request_reset(app, client, 'perm@example.com', address='192.0.2.61')
    drain(app)
    assert len(mails_to('perm@example.com')) == 1

    def refused(*args, **kwargs):
        raise mailer.PermanentMailError('refused')
    monkeypatch.setattr(mailer, 'send_email', refused)
    with engine(app).begin() as connection:
        connection.execute(update(PasswordResetRequest).values(created_at=now() - timedelta(minutes=5)))
    request_reset(app, client, 'perm@example.com', address='192.0.2.62')
    assert taskqueue.run_once(app) == 'succeeded'
    with engine(app).connect() as connection:
        assert connection.execute(select(PasswordResetRequest.outcome)
                                  .order_by(PasswordResetRequest.created_at)).scalars().all() == [
            'sent', 'deduped', 'undeliverable']


def test_reset_share_is_capped_and_the_notice_still_sends(app, client):
    app.config.update(MAIL_DAILY_LIMIT=10, MAIL_LIMIT_SIGNUP=6, MAIL_LIMIT_RESET=2)
    for n in range(3):
        add_user(app, f'share{n}@example.com')
        request_reset(app, client, f'share{n}@example.com', address=f'192.0.2.{70 + n}')
    outcomes = drain(app)
    assert len(mailer.outbox) == 2 and 'deferred' in outcomes
    with app.app_context():
        assert not mailer.has_room('reset') and mailer.has_room('notice')
    token = link_token(mailer.outbox[0])
    assert complete(client, token).status_code == 200
    drain(app)
    assert mailer.outbox[-1]['Subject'] == 'Your My Next Moov password was changed'


# Status and completion --------------------------------------------------------
def test_status_never_consumes_and_rejects_bad_shapes(app, client):
    add_user(app, 'status@example.com')
    token = emailed_token(app, client, 'status@example.com')
    for _ in range(3):
        assert status(client, token).json == {'valid': True, 'email': 'status@example.com'}
    for bad in (None, '', 'nmr_short', token[:-1] + 'x', 'x' * 60, 123, token.replace('nmr_', 'abc_')):
        assert status(client, bad).json == {'valid': False}
    assert status(client, token, headers={'Origin': 'https://evil.test'}).status_code == 403


def test_complete_resets_revokes_and_notifies(app, client, caplog):
    caplog.set_level(logging.INFO, logger='jobtracker.accounts')
    user_id = add_user(app, 'full@example.com', password=PASSWORD)
    other = app.test_client()
    sign_in(other, 'full@example.com', PASSWORD)
    access, agent = connect_agents(app, user_id)
    _, grant = None, None
    with engine(app).begin() as connection:
        grant = harness.create_grant(connection, user_id)
    assert agents_live(app, access, agent) == (True, True)
    token = emailed_token(app, client, 'full@example.com')
    sign_in(client, 'full@example.com', PASSWORD)
    response = complete(client, token)
    assert response.status_code == 200
    assert response.json == {'message': password_reset.DONE, 'email': 'full@example.com',
                             'disconnected': {'sessions': True, 'agents': 2, 'chat': False}, 'review_data': False}
    assert client.get('/api/v1/auth/session').json['authenticated'] is False  # Never signs in; this session ended.
    assert other.get('/api/v1/auth/session').json['authenticated'] is False
    row = user_row(app, user_id)
    assert check_password_hash(row.password_hash, NEW_PASSWORD) and row.session_version == 2
    assert agents_live(app, access, agent) == (False, False)
    assert harness_live(app, grant)  # Verified account: the chat link stays.
    assert 'revoked user_id=%d oauth=1 agent=1 harness=0 via=reset' % user_id in caplog.text
    sign_in(app.test_client(), 'full@example.com', NEW_PASSWORD)
    drain(app)
    notice = mails_to('full@example.com')[-1]
    assert notice['Subject'] == 'Your My Next Moov password was changed' and 'nmr_' not in body(notice)
    with engine(app).connect() as connection:
        payload = connection.execute(text("SELECT payload FROM queued_tasks WHERE kind='password_changed_email'")).scalar()
    assert 'full@example.com' not in payload


def test_emailed_reset_verifies_a_legacy_account_and_an_operator_link_does_not(app, client):
    emailed = add_user(app, 'legacy1@example.com', password=PASSWORD, verified=False)
    operated = add_user(app, 'legacy2@example.com', password=PASSWORD, verified=False)
    with engine(app).begin() as connection:
        connection.execute(insert(EmailConfirmation).values(
            id=new_id(), kind='existing', email='legacy1@example.com', user_id=emailed, password_attempts=0,
            created_at=now(), delivery='sent'))
    assert complete(client, emailed_token(app, client, 'legacy1@example.com')).json['review_data'] is True
    assert user_row(app, emailed).email_verified_at is not None
    with engine(app).connect() as connection:
        assert connection.scalar(select(EmailConfirmation.id).where(EmailConfirmation.user_id == emailed)) is None
    assert app.test_client().post('/api/v1/auth/login', json={
        'email': 'legacy1@example.com', 'password': NEW_PASSWORD}).status_code == 200
    assert complete(client, operator_token(app, 'legacy2@example.com')).status_code == 200
    assert user_row(app, operated).email_verified_at is None
    login = app.test_client().post('/api/v1/auth/login', json={'email': 'legacy2@example.com',
                                                                'password': NEW_PASSWORD})
    assert login.status_code == 403 and login.json['code'] == 'verification_required'


def test_b1_legacy_messaging_account_loses_its_harness_grant(app, client):
    user_id, grant = messaging_account(app, 'victim@example.com', verified=False)
    assert harness_live(app, grant)
    response = complete(client, emailed_token(app, client, 'victim@example.com'))
    assert response.json['disconnected']['chat'] is True and response.json['review_data'] is True
    assert not harness_live(app, grant)
    assert user_row(app, user_id).email_verified_at is not None  # Decided before mark_verified, then verified.


def test_b1_verified_messaging_account_keeps_its_harness_grant(app, client):
    _, grant = messaging_account(app, 'phone@example.com', verified=True)
    response = complete(client, emailed_token(app, client, 'phone@example.com'))
    assert response.json['disconnected']['chat'] is False and response.json['review_data'] is False
    assert harness_live(app, grant)


def test_b1_after_operator_email_change_the_grant_is_revoked(app, client):
    user_id, grant = messaging_account(app, 'before@example.com', verified=True)
    with app.app_context():
        credentials.set_email(user_id, 'after@example.com')
    response = complete(client, emailed_token(app, client, 'after@example.com'))
    assert response.json['disconnected']['chat'] is True and not harness_live(app, grant)
    assert user_row(app, user_id).email_verified_at is not None


def test_single_use_and_a_race_has_one_winner(app, client, monkeypatch):
    add_user(app, 'once@example.com')
    token = emailed_token(app, client, 'once@example.com')
    assert complete(client, token).status_code == 200
    gone = complete(client, token, password='yet-another-password')
    assert gone.status_code == 410 and gone.json['error'] == password_reset.INVALID
    add_user(app, 'race@example.com')
    token = emailed_token(app, client, 'race@example.com', address='192.0.2.90')
    barrier, results = threading.Barrier(2), []
    original = password_reset.reset_with_token

    def slow(*args):
        barrier.wait(timeout=5)
        return original(*args)
    monkeypatch.setattr(password_reset, 'reset_with_token', slow)

    def go(password):
        results.append(complete(app.test_client(), token, password=password).status_code)
    threads = [threading.Thread(target=go, args=(p,)) for p in ('first-password-1', 'second-password-2')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [200, 410]


def test_expiry_supersede_and_password_or_email_changes_kill_links(app, client):
    user_id = add_user(app, 'kill@example.com', password=PASSWORD)
    expired = emailed_token(app, client, 'kill@example.com')
    with engine(app).begin() as connection:
        connection.execute(update(PasswordResetToken).values(expires_at=now() - timedelta(seconds=1)))
    assert complete(client, expired).status_code == 410
    first = operator_token(app, 'kill@example.com')
    second = operator_token(app, 'kill@example.com')
    assert status(client, first).json == {'valid': False} and status(client, second).json['valid']
    headers = sign_in(client, 'kill@example.com', PASSWORD)
    assert client.post('/api/v1/auth/password', headers=headers, json={
        'current_password': PASSWORD, 'new_password': NEW_PASSWORD, 'confirm_password': NEW_PASSWORD}).status_code == 200
    assert status(client, second).json == {'valid': False}
    with engine(app).connect() as connection:
        assert connection.scalar(select(PasswordResetToken.revoked_at).where(
            PasswordResetToken.token_hash == hashlib.sha256(second.encode()).hexdigest())) is not None
    drain(app)
    assert mails_to('kill@example.com')[-1]['Subject'] == 'Your My Next Moov password was changed'
    third = operator_token(app, 'kill@example.com')
    with app.app_context():
        credentials.set_email(user_id, 'moved@example.com', verified=True)
    assert status(client, third).json == {'valid': False}
    fourth = operator_token(app, 'moved@example.com')
    assert complete(client, fourth).status_code == 200
    assert status(client, operator_token(app, 'moved@example.com')).json['valid']


def test_cli_reset_password_kills_links_and_follows_the_rules(app, client, caplog):
    caplog.set_level(logging.INFO, logger='jobtracker.accounts')
    user_id, grant = messaging_account(app, 'cli@example.com', verified=False)
    access, agent = connect_agents(app, user_id)
    link = operator_token(app, 'cli@example.com')
    runner = app.test_cli_runner()
    result = runner.invoke(args=['user', 'reset-password', 'cli@example.com'], input=f'{NEW_PASSWORD}\n{NEW_PASSWORD}\n')
    assert result.exit_code == 0, result.stderr
    assert 'revoked 1 agent connection(s), 1 agent token(s) and 1 chat link(s)' in result.output
    assert status(client, link).json == {'valid': False}
    assert agents_live(app, access, agent) == (False, False) and not harness_live(app, grant)
    assert f'revoked user_id={user_id} oauth=1 agent=1 harness=1 via=operator' in caplog.text
    assert f'operator password reset user_id={user_id} operator=cli' in caplog.text
    drain(app)
    assert mails_to('cli@example.com')[-1]['Subject'] == 'Your My Next Moov password was changed'
    kept, kept_grant = messaging_account(app, 'keep@example.com', verified=False)
    assert runner.invoke(args=['user', 'reset-password', 'keep@example.com', '--keep-harness'],
                         input=f'{NEW_PASSWORD}\n{NEW_PASSWORD}\n').exit_code == 0
    assert harness_live(app, kept_grant)
    verified, verified_grant = messaging_account(app, 'verified-cli@example.com', verified=True)
    assert runner.invoke(args=['user', 'reset-password', 'verified-cli@example.com'],
                         input=f'{NEW_PASSWORD}\n{NEW_PASSWORD}\n').exit_code == 0
    assert harness_live(app, verified_grant)
    assert runner.invoke(args=['user', 'reset-password', 'verified-cli@example.com', '--revoke-harness'],
                         input=f'{PASSWORD}\n{PASSWORD}\n').exit_code == 0
    assert not harness_live(app, verified_grant)


def test_complete_validates_before_consuming_and_throttles_before_hashing(app, client, monkeypatch):
    add_user(app, 'valid@example.com')
    token = emailed_token(app, client, 'valid@example.com')
    short = complete(client, token, password='short')
    assert short.status_code == 400 and short.json['field'] == 'password'
    mismatch = complete(client, token, confirmation='something-else-123')
    assert mismatch.status_code == 400 and mismatch.json['field'] == 'password_confirmation'
    assert status(client, token).json['valid']
    hashes = []
    monkeypatch.setattr(password_reset, 'generate_password_hash', lambda value: hashes.append(value) or 'x')
    for n in range(10):
        assert complete(client, 'nmr_' + 'z' * 43, **ip('192.0.2.99')).status_code == 410
    assert complete(client, 'nmr_' + 'z' * 43, **ip('192.0.2.99')).status_code == 429
    assert hashes == []  # Garbage links never reach scrypt; the 11th try never got past the throttle.
    assert complete(client, token, headers={'Origin': 'https://evil.test'}).status_code == 403


def test_reset_and_login_budgets_are_independent(app, client):
    add_user(app, 'indep@example.com', password=PASSWORD)
    for n in range(5):
        request_reset(app, client, f'x{n}@example.com', address='192.0.2.120')
    assert request_reset(app, client, 'y@example.com', address='192.0.2.120').status_code == 429
    login = client.post('/api/v1/auth/login', json={'email': 'indep@example.com', 'password': PASSWORD},
                        **ip('192.0.2.120'))
    assert login.status_code == 200


# Operator link ------------------------------------------------------------------
def test_reset_link_cli(app, caplog):
    caplog.set_level(logging.INFO, logger='jobtracker.accounts')
    runner = app.test_cli_runner()
    owner = runner.invoke(args=['user', 'reset-link', '--id', '1', '--minutes', '30'])
    assert owner.exit_code == 0 and TOKEN.search(owner.stdout)  # owner@localhost: works for placeholders.
    assert 'password-reset credential' in owner.stderr and 'nmr_' not in owner.stderr
    token = TOKEN.search(owner.stdout).group(1)
    assert mailer.outbox == []
    records = [r.getMessage() for r in caplog.records if 'operator reset link minted' in r.getMessage()]
    assert len(records) == 1 and 'user_id=1' in records[0] and 'minutes=30 operator=cli' in records[0]
    assert token not in caplog.text and 'owner@localhost' not in records[0]
    with engine(app).connect() as connection:
        row = connection.execute(select(PasswordResetToken.issued_by, PasswordResetToken.created_at,
                                        PasswordResetToken.expires_at)).one()
    assert row.issued_by == 'operator' and timedelta(minutes=29) < row.expires_at - row.created_at <= timedelta(minutes=30)
    messaging_id, _ = messaging_account(app)
    with engine(app).begin() as connection:
        connection.execute(insert(EmailConfirmation).values(
            id=new_id(), kind='signup', email='pending@example.com', password_hash='x', password_attempts=0,
            created_at=now(), delivery='sent'))
    for args in (['--id', str(messaging_id)], ['pending@example.com'], ['nobody@example.com'], [],
                 ['owner@localhost', '--id', '1'], ['--id', '1', '--minutes', '2'], ['--id', '1', '--minutes', '1441']):
        assert runner.invoke(args=['user', 'reset-link', *args]).exit_code != 0, args
    app.config['PUBLIC_BASE_URL'] = app.config['MAIL_LINK_BASE'] = ''
    missing = runner.invoke(args=['user', 'reset-link', '--id', '1'])
    assert missing.exit_code != 0 and 'PUBLIC_BASE_URL' in missing.stderr


def test_agents_cli_is_evs_revoke_agents_only(app):
    commands = app.cli.commands['user'].commands
    assert 'revoke-agents' in commands and 'revoke-harness' not in commands and 'reset-link' in commands


# Schema -------------------------------------------------------------------------
def test_v15_upgrade_creates_the_reset_tables(config, tmp_path):
    app = create_app(config)
    path = database_path(app)
    with closing(sqlite3.connect(path)) as connection:
        rewind_password_reset(connection)
        connection.commit()
        assert connection.execute('SELECT version FROM schema_version').fetchone() == (15,)
    upgraded = create_app(config)
    with engine(upgraded).connect() as connection:
        assert connection.scalar(text('SELECT version FROM schema_version')) >= 16  # 17: data deletion.
        names = set(inspect(connection).get_table_names())
        assert {'password_reset_tokens', 'password_reset_requests'} <= names
        indexes = {index['name'] for index in inspect(connection).get_indexes('password_reset_requests')}
        assert 'ix_password_reset_requests_user_created' in indexes


def test_merge_messaging_still_works_with_the_reset_tables(app):
    source, _ = messaging_account(app)
    target = add_user(app, 'target@example.com')
    result = app.test_cli_runner().invoke(args=['user', 'merge-messaging', str(source), str(target)])
    assert result.exit_code == 0, result.output
