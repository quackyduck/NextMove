"""Email confirmation (schema 15): pending sign-ups, cookie-or-password proof, legacy verification and resend."""
from datetime import timedelta
import logging
import sqlite3

import pytest
from sqlalchemy import event, select, update
from werkzeug.security import generate_password_hash

from jobtracker import confirmations, create_app, mailer, taskqueue
from jobtracker.models import EmailConfirmation, QueuedTask, User, now

from accounts import add_user, last_link, sign_in, sign_up

PASSWORD = 'confirm-password-1'


@pytest.fixture
def config(tmp_path):
    return {
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'confirm-test-secret-0123456789abcdef',
        'APP_PASSWORD_HASH': generate_password_hash('test-password', method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "confirm.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
        # The app itself is served at the root here; links still come from PUBLIC_BASE_URL (with its mount path).
        'SIGNUP_REQUIRES_INVITE': False, 'PUBLIC_BASE_URL': 'https://example.test/nextmove',
    }


@pytest.fixture
def app(config):
    mailer.outbox.clear()
    return create_app(config)


def engine(app):
    return app.extensions['jobtracker_db']['engine']


def start(client, email='new@example.com', password=PASSWORD, **kwargs):
    return client.post('/api/v1/auth/signup', json={
        'email': email, 'password': password, 'password_confirmation': password,
        'proof': solve_proof_at(client, 'signup')}, **kwargs)


def solve_proof_at(client, purpose):
    import hashlib
    from jobtracker.pow import leading_zero_bits
    issued = client.get(f'/api/v1/auth/challenge?purpose={purpose}').json
    nonce = 0
    while leading_zero_bits(hashlib.sha256(f'{issued["challenge"]}:{nonce}'.encode()).digest()) < issued['bits']:
        nonce += 1
    return {'challenge': issued['challenge'], 'nonce': str(nonce)}


def check(client, token):
    return client.post('/api/v1/auth/email-verification/check', json={'token': token})


def confirm(client, token, password=None):
    body = {'token': token}
    if password is not None:
        body['password'] = password
    return client.post('/api/v1/auth/email-verification/confirm', json=body)


def resend(client, email, **kwargs):
    return client.post('/api/v1/auth/email-verification/resend',
                       json={'email': email, 'proof': solve_proof_at(client, 'resend')}, **kwargs)


def rows(app):
    with engine(app).connect() as connection:
        return connection.execute(select(EmailConfirmation.__table__)).mappings().all()


def users(app):
    with engine(app).connect() as connection:
        return [row.email for row in connection.execute(select(User.email).order_by(User.id))]


# The link and the token ---------------------------------------------------------------
def test_the_link_comes_from_public_base_url_and_only_a_digest_is_stored(app, tmp_path):
    client = app.test_client()
    assert start(client, headers={'Host': 'evil.example'}).status_code == 202
    taskqueue.drain(app)
    message = mailer.outbox[-1]
    body = message.get_body(('plain',)).get_content()
    token = last_link('new@example.com')
    assert f'https://example.test/nextmove/verify-email#token={token}' in body
    assert 'evil.example' not in body and message['Subject'] == 'Confirm your email for My Next Moov'
    engine(app).dispose()
    for database in (tmp_path / 'confirm.sqlite3', tmp_path / 'throttle.sqlite3'):
        with sqlite3.connect(database) as connection:
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                for row in connection.execute(f'SELECT * FROM "{table}"'):
                    assert token not in repr(row), table


def test_checking_never_consumes_and_confirming_in_the_same_browser_takes_one_click(app):
    client = app.test_client()
    start(client)
    taskqueue.drain(app)
    token = last_link()
    for _ in range(3):
        assert check(client, token).json == {'valid': True, 'kind': 'signup', 'email_hint': 'n•••@example.com',
                                             'needs_password': False, 'signed_in_as_other': False}
    confirmed = confirm(client, token)
    assert confirmed.status_code == 200 and confirmed.json['user']['email'] == 'new@example.com'
    assert 'next_move_confirm=;' in confirmed.headers['Set-Cookie']  # The binding cookie is cleared.
    assert client.get('/api/v1/auth/session').json['authenticated'] is True
    assert check(client, token).json == {'valid': False}
    assert confirm(client, token).status_code == 410
    with engine(app).connect() as connection:
        verified = connection.scalar(select(User.email_verified_at).where(User.email == 'new@example.com'))
    assert verified is not None and rows(app) == []


def test_another_browser_needs_the_chosen_password_and_dies_after_five_wrong_tries(app):
    start(app.test_client())
    taskqueue.drain(app)
    token, other = last_link(), app.test_client()
    assert check(other, token).json['needs_password'] is True
    assert confirm(other, token).json['code'] == 'password_required'
    assert confirm(other, token, 'not-it-123').json['code'] == 'wrong_password'
    good = confirm(other, token, PASSWORD)
    assert good.status_code == 200 and other.get('/api/v1/auth/session').json['authenticated'] is True
    start(app.test_client(), 'second@example.com')
    taskqueue.drain(app)
    token = last_link('second@example.com')
    for _ in range(5):
        assert confirm(app.test_client(), token, 'wrong-password').json['code'] == 'wrong_password'
    assert confirm(app.test_client(), token, PASSWORD).status_code == 410
    assert users(app) == ['owner@localhost', 'new@example.com']


def test_a_victim_cannot_create_an_account_someone_else_started_with_their_address(app):
    attacker = app.test_client()
    start(attacker, 'victim@example.com', 'attacker-password')
    taskqueue.drain(app)
    victim = app.test_client()
    token = last_link('victim@example.com')
    assert confirm(victim, token).status_code == 400
    assert users(app) == ['owner@localhost']
    # The real owner signs up too; whichever link is confirmed from the mailbox wins, and the other dies.
    start(victim, 'victim@example.com', 'victim-password-1')
    taskqueue.drain(app)
    mine = last_link('victim@example.com')
    assert confirm(victim, mine).status_code == 200
    assert confirm(attacker, token).status_code == 410
    sign_in(app.test_client(), 'victim@example.com', 'victim-password-1')


def test_signing_up_again_from_the_same_browser_replaces_the_pending_password(app):
    client = app.test_client()
    start(client, password='first-password-1')
    taskqueue.drain(app)
    first = last_link()
    start(client, password='second-password-1')
    taskqueue.drain(app)
    second = last_link()
    assert first != second and check(client, first).json == {'valid': False}
    assert confirm(client, second).status_code == 200
    sign_in(app.test_client(), 'new@example.com', 'second-password-1')


def test_confirming_while_signed_in_as_someone_else_switches_after_check_warns(app):
    client = app.test_client()
    sign_in(client)
    start(client)
    taskqueue.drain(app)
    token = last_link()
    assert check(client, token).json['signed_in_as_other'] is True
    assert confirm(client, token).status_code == 200
    assert client.get('/api/v1/auth/session').json['user']['email'] == 'new@example.com'


def test_an_address_taken_in_the_meantime_gets_account_exists(app):
    client = app.test_client()
    start(client)
    taskqueue.drain(app)
    token = last_link()
    add_user(app, 'new@example.com')
    response = confirm(client, token)
    assert response.status_code == 409 and response.json['code'] == 'account_exists'
    assert rows(app) == []


def test_links_expire_after_a_day_and_unsent_rows_are_skipped_then_cleaned_up(app):
    client = app.test_client()
    start(client)
    taskqueue.drain(app)
    token = last_link()
    with engine(app).begin() as connection:
        connection.execute(update(EmailConfirmation).values(expires_at=now() - timedelta(seconds=1)))
    assert check(client, token).json == {'valid': False} and confirm(client, token).status_code == 410
    start(client, 'stale@example.com')
    with engine(app).begin() as connection:
        connection.execute(update(EmailConfirmation).where(EmailConfirmation.email == 'stale@example.com')
                           .values(created_at=now() - timedelta(hours=25)))
    sent = len(mailer.outbox)
    taskqueue.drain(app)
    assert len(mailer.outbox) == sent
    with engine(app).begin() as connection:
        connection.execute(update(EmailConfirmation).values(expires_at=now() - timedelta(hours=2)))
        confirmations.cleanup(connection)
    assert rows(app) == []


def test_a_mailbox_that_already_has_an_account_gets_a_notice_and_nothing_is_kept(app):
    add_user(app, 'taken@example.com')
    start(app.test_client(), 'taken@example.com')
    taskqueue.drain(app)
    notice = mailer.outbox[-1]
    body = notice.get_body(('plain',)).get_content()
    assert notice['Subject'] == 'You already have a My Next Moov account'
    assert 'https://example.test/nextmove/?signin' in body and 'https://example.test/nextmove/?forgot' in body
    assert '#token=' not in body and rows(app) == []


def test_signup_never_reads_users_on_the_request_path(app):
    seen = []

    def watch(connection, cursor, statement, parameters, context, executemany):
        seen.append(statement)
    event.listen(engine(app), 'before_cursor_execute', watch)
    try:
        assert start(app.test_client()).status_code == 202
    finally:
        event.remove(engine(app), 'before_cursor_execute', watch)
    assert seen and not any('FROM users' in statement for statement in seen)


# Legacy accounts ---------------------------------------------------------------------
def test_a_legacy_account_verifies_at_sign_in_and_keeps_its_credentials(app):
    add_user(app, 'legacy@example.com', 'legacy-password-1', verified=False)
    client = app.test_client()
    wrong = client.post('/api/v1/auth/login', json={'email': 'legacy@example.com', 'password': 'nope'})
    assert wrong.status_code == 401 and 'code' not in wrong.json
    response = client.post('/api/v1/auth/login', json={'email': 'legacy@example.com',
                                                                'password': 'legacy-password-1'})
    assert response.status_code == 403 and response.json['code'] == 'verification_required'
    assert response.json['email_hint'] == 'l•••@example.com' and 'next_move_confirm=' in response.headers['Set-Cookie']
    assert client.get('/api/v1/auth/session').json['authenticated'] is False
    # A second sign-in within a minute does not send another link.
    client.post('/api/v1/auth/login', json={'email': 'legacy@example.com', 'password': 'legacy-password-1'})
    taskqueue.drain(app)
    assert sum(message['To'] == 'legacy@example.com' for message in mailer.outbox) == 1
    token = last_link('legacy@example.com')
    assert check(client, token).json['kind'] == 'existing'
    assert confirm(client, token).status_code == 200
    sign_in(app.test_client(), 'legacy@example.com', 'legacy-password-1')


def test_a_legacy_link_opened_elsewhere_needs_the_account_password(app):
    add_user(app, 'legacy@example.com', 'legacy-password-1', verified=False)
    app.test_client().post('/api/v1/auth/login', json={'email': 'legacy@example.com',
                                                                 'password': 'legacy-password-1'})
    taskqueue.drain(app)
    other = app.test_client()
    token = last_link('legacy@example.com')
    assert confirm(other, token, 'guess-password').json['code'] == 'wrong_password'
    assert confirm(other, token, 'legacy-password-1').status_code == 200


def test_existing_sessions_of_unverified_accounts_end(app):
    user_id = add_user(app, 'legacy@example.com', 'legacy-password-1')
    client = app.test_client()
    sign_in(client, 'legacy@example.com', 'legacy-password-1')
    with engine(app).begin() as connection:
        connection.execute(update(User).where(User.id == user_id).values(email_verified_at=None))
    assert client.get('/api/v1/jobs').status_code == 401


def test_undeliverable_legacy_addresses_and_mail_off_say_contact_support(app, config):
    add_user(app, 'placeholder@localhost', 'legacy-password-1', verified=False)
    response = app.test_client().post('/api/v1/auth/login', json={'email': 'placeholder@localhost',
                                                                            'password': 'legacy-password-1'})
    assert response.status_code == 403 and response.json['code'] == 'verification_unavailable'
    off = create_app({**config, 'MAIL_BACKEND': 'none'})
    add_user(off, 'legacy@example.com', 'legacy-password-1', verified=False)
    response = off.test_client().post('/api/v1/auth/login', json={'email': 'legacy@example.com',
                                                                            'password': 'legacy-password-1'})
    assert response.json['code'] == 'verification_unavailable'


# Resend --------------------------------------------------------------------------------
def test_resend_is_always_202_and_only_mails_what_is_waiting(app):
    client = app.test_client()
    start(client)
    taskqueue.drain(app)
    old = last_link()
    add_user(app, 'verified@example.com')
    add_user(app, 'legacy@example.com', verified=False)
    responses = [resend(app.test_client(), email, environ_base={'REMOTE_ADDR': f'198.51.100.{index}'})
                 for index, email in enumerate(('new@example.com', 'verified@example.com', 'nobody@example.com',
                                                'not an email', 'legacy@example.com'))]
    assert {(response.status_code, response.data) for response in responses} == {(202, responses[0].data)}
    before = len(mailer.outbox)
    taskqueue.drain(app)
    sent = [message['To'] for message in mailer.outbox[before:]]
    assert sorted(sent) == ['legacy@example.com', 'new@example.com']
    # The new link replaces the old one and keeps the browser binding.
    assert check(client, old).json == {'valid': False}
    assert confirm(client, last_link('new@example.com')).status_code == 200
    with engine(app).connect() as connection:  # Resend payloads hold an address: deleted once done.
        assert connection.scalar(select(QueuedTask.id).where(QueuedTask.kind == 'resend_confirmation')) is None


def test_resend_needs_a_proof_and_shares_the_strict_per_ip_budget(app):
    client = app.test_client()
    assert client.post('/api/v1/auth/email-verification/resend',
                       json={'email': 'a@example.com'}).json['code'] == 'proof_invalid'
    for index in range(5):
        assert resend(client, f'a{index}@example.com').status_code == 202
    assert resend(client, 'more@example.com').status_code == 429
    assert start(client, 'blocked@example.com').status_code == 429  # Signup shares the same budget.


def test_check_and_confirm_refuse_other_origins(app):
    for path in ('check', 'confirm', 'resend'):
        response = app.test_client().post(f'/api/v1/auth/email-verification/{path}', json={'token': 'x' * 40},
                                          headers={'Origin': 'https://evil.example'})
        assert response.status_code == 403


# Quota and logging ---------------------------------------------------------------------
def test_quota_deferral_keeps_the_row_and_does_not_burn_attempts(app, config):
    tight = create_app({**config, 'MAIL_DAILY_LIMIT': 20, 'MAIL_LIMIT_SIGNUP': 7, 'MAIL_LIMIT_RESET': 4})
    with tight.app_context():
        mailer.send_email('a@example.com', 'Hi', 'Body', category='signup', kind='existing')
        mailer.send_email('a@example.com', 'Hi', 'Body', category='signup', kind='existing')
    client = tight.test_client()
    assert start(client).status_code == 202  # One new sign-up's worth of room is left (7 - 5 reserve - 0 used).
    with tight.app_context():
        mailer.send_email('b@example.com', 'Hi', 'Body', category='signup')
        mailer.send_email('b@example.com', 'Hi', 'Body', category='signup')
    assert taskqueue.drain(tight) == ['deferred']
    with engine(tight).connect() as connection:
        task = connection.execute(select(QueuedTask.attempts, QueuedTask.status).where(
            QueuedTask.kind == 'send_confirmation')).one()
    assert tuple(task) == (0, 'queued') and rows(tight)[0]['delivery'] == 'queued'


def test_no_token_link_or_address_reaches_the_logs(app, caplog):
    caplog.set_level(logging.DEBUG)
    client = app.test_client()
    start(client, 'private.person@example.com')
    taskqueue.drain(app)
    token = last_link('private.person@example.com')
    confirm(client, token)
    resend(app.test_client(), 'private.person@example.com')
    taskqueue.drain(app)
    assert token not in caplog.text and 'private.person@example.com' not in caplog.text


def test_merging_a_messaging_account_deletes_its_pending_activation(app):
    from sqlalchemy import insert
    from jobtracker.models import new_id
    target = add_user(app, 'web@example.com')
    with engine(app).begin() as connection:
        source = connection.execute(insert(User).values(session_version=1, created_at=now())).inserted_primary_key[0]
        connection.execute(insert(EmailConfirmation).values(
            id=new_id(), kind='activation', email='chosen@example.com', user_id=source, password_hash='h',
            password_attempts=0, created_at=now(), delivery='queued'))
    result = app.test_cli_runner().invoke(args=['user', 'merge-messaging', str(source), str(target)])
    assert result.exit_code == 0, result.output
    assert rows(app) == []
    assert sign_up(app, app.test_client(), 'helper@example.com')['X-CSRF-Token']


# Review fixes ------------------------------------------------------------------------------
def test_resend_keeps_the_creation_time_and_rows_never_outlive_two_days(app):
    client = app.test_client()
    start(client)
    taskqueue.drain(app)
    created = rows(app)[0]['created_at']
    resend(app.test_client(), 'new@example.com', environ_base={'REMOTE_ADDR': '198.51.100.40'})
    taskqueue.drain(app)
    assert rows(app)[0]['created_at'] == created  # Resending never extends the pending password hash's life.
    with engine(app).begin() as connection:
        connection.execute(update(EmailConfirmation).values(
            created_at=now() - timedelta(hours=confirmations.MAX_PENDING_HOURS, minutes=1),
            expires_at=now() + timedelta(hours=10)))
    assert check(client, last_link()).json == {'valid': True, 'kind': 'signup', 'email_hint': 'n•••@example.com',
                                               'needs_password': False, 'signed_in_as_other': False}
    with engine(app).begin() as connection:
        confirmations.cleanup(connection)
    assert rows(app) == []  # The hard cap wins even over a link that has not expired yet.


def test_concurrent_wrong_passwords_get_at_most_five_evaluations(app, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    start(app.test_client())
    taskqueue.drain(app)
    token, evaluated = last_link(), []
    real = confirmations.check_password_hash

    def counting(stored, password):
        evaluated.append(password)
        return real(stored, password)
    monkeypatch.setattr(confirmations, 'check_password_hash', counting)

    def guess(index):
        return confirm(app.test_client(), token, f'wrong-guess-{index}').status_code
    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(guess, range(12)))
    assert len(evaluated) <= confirmations.MAX_PASSWORD_ATTEMPTS
    assert statuses.count(400) <= confirmations.MAX_PASSWORD_ATTEMPTS and set(statuses) <= {400, 410, 429}
    assert rows(app)[0]['password_attempts'] == confirmations.MAX_PASSWORD_ATTEMPTS
    assert confirm(app.test_client(), token, PASSWORD).status_code == 410


def test_the_right_password_on_the_fifth_try_still_confirms(app):
    start(app.test_client())
    taskqueue.drain(app)
    token = last_link()
    for _ in range(4):
        assert confirm(app.test_client(), token, 'wrong-guess').json['code'] == 'wrong_password'
    assert confirm(app.test_client(), token, PASSWORD).status_code == 200


def test_session_reads_the_pause_state_from_a_short_cache(app, config, monkeypatch):
    cached = create_app({**config, 'MAIL_STATUS_CACHE_SECONDS': 30})
    calls = []
    real = mailer._pause_reason
    monkeypatch.setattr(mailer, '_pause_reason', lambda: calls.append(1) or real())
    client = cached.test_client()
    for _ in range(5):
        client.get('/api/v1/auth/session')
    assert len(calls) == 2  # Once for the pause state and once for the status, then cached.
    start(client)  # The signup POST itself always reads the live state.
    assert len(calls) == 3
