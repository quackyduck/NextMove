"""Schema 15 account helpers (mark_verified, account_state, set_email, revoke_agents) and the v14 -> v15 upgrade."""
from contextlib import closing
import logging
import sqlite3

import pytest
from sqlalchemy import insert, inspect, select
from werkzeug.security import generate_password_hash

from jobtracker import create_app, credentials
from jobtracker.models import (AgentToken, EmailConfirmation, HarnessGrant, OAuthClient, OAuthGrant, QueuedTask, User,
                               new_id, now)

from accounts import add_user, sign_in
from legacy import database_path, rewind_verification

PASSWORD = 'test-password'


@pytest.fixture
def config(tmp_path):
    return {
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'helpers-test-secret-0123456789abcdef',
        'APP_PASSWORD_HASH': generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "helpers.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
        'BOOTSTRAP_ADMIN_EMAIL': 'owner@example.com',
    }


@pytest.fixture
def app(config):
    return create_app(config)


def engine(app):
    return app.extensions['jobtracker_db']['engine']


def user_row(app, user_id):
    with engine(app).connect() as connection:
        return connection.execute(select(User.__table__).where(User.id == user_id)).one()


def add_confirmation(app, user_id, email):
    with engine(app).begin() as connection:
        connection.execute(insert(EmailConfirmation).values(
            id=new_id(), kind='existing', email=email, user_id=user_id, password_attempts=0, created_at=now(),
            delivery='queued'))


def test_fresh_bootstrap_owner_is_verified_and_states(app):
    with app.app_context():
        owner = credentials.read(1)
        assert owner.verified and credentials.account_state(owner) == 'verified'
        legacy = credentials.read(add_user(app, 'legacy@example.com', verified=False))
        assert credentials.account_state(legacy) == 'legacy_unverified'
    with engine(app).begin() as connection:
        messaging = connection.execute(insert(User).values(session_version=1, created_at=now())).inserted_primary_key[0]
    with app.app_context():
        assert credentials.account_state(credentials.read(messaging)) == 'messaging'


def test_mark_verified_matches_the_email_keeps_the_first_time_and_clears_confirmations(app, caplog):
    user_id = add_user(app, 'legacy@example.com', verified=False)
    add_confirmation(app, user_id, 'legacy@example.com')
    caplog.set_level(logging.INFO, logger='jobtracker.accounts')
    with app.app_context():
        with engine(app).begin() as connection:
            assert not credentials.mark_verified(connection, user_id, 'other@example.com', via='link')
        assert user_row(app, user_id).email_verified_at is None
        with engine(app).begin() as connection:
            assert credentials.mark_verified(connection, user_id, 'legacy@example.com', via='reset')
        first = user_row(app, user_id).email_verified_at
        with engine(app).begin() as connection:
            assert credentials.mark_verified(connection, user_id, 'legacy@example.com', via='cli')
        assert user_row(app, user_id).email_verified_at == first
        with engine(app).connect() as connection:
            assert connection.scalar(select(EmailConfirmation.id)) is None
    assert f'user verified user_id={user_id} via=reset' in caplog.text


def test_set_email_is_one_transaction_that_signs_out_and_runs_hooks(app):
    client = app.test_client()
    user_id = add_user(app, 'person@example.com')
    sign_in(client, 'person@example.com', 'other-password-123')
    add_confirmation(app, user_id, 'person@example.com')
    seen = []
    hook = credentials.email_change_hook(lambda connection, changed: seen.append((changed, connection.in_transaction())))
    try:
        with app.app_context():
            assert credentials.set_email(user_id, ' New@Example.com ') == 'new@example.com'
            with pytest.raises(ValueError):
                credentials.set_email(user_id, 'owner@localhost')
            with pytest.raises(LookupError):
                credentials.set_email(9999, 'x@example.com')
    finally:
        credentials.EMAIL_CHANGE_HOOKS.remove(hook)
    row = user_row(app, user_id)
    assert (row.email, row.email_verified_at, row.session_version) == ('new@example.com', None, 2)
    assert seen == [(user_id, True)]
    assert client.get('/api/v1/auth/session').json['authenticated'] is False
    with engine(app).connect() as connection:
        assert connection.scalar(select(EmailConfirmation.id)) is None


def test_set_email_cli_flags(app):
    user_id = add_user(app, 'person@example.com')
    runner = app.test_cli_runner()
    result = runner.invoke(args=['user', 'set-email', str(user_id), 'kept@example.com', '--verified'])
    assert result.exit_code == 0 and '(verified)' in result.output
    row = user_row(app, user_id)
    assert row.email_verified_at is not None and row.session_version == 2
    assert runner.invoke(args=['user', 'set-email', str(user_id), 'owner@localhost']).exit_code != 0
    assert 'already uses' in runner.invoke(args=['user', 'set-email', str(user_id), 'owner@example.com']).output


def test_revoke_agents_revokes_oauth_and_tokens_and_optionally_harness(app, caplog):
    user_id = add_user(app, 'person@example.com')
    with engine(app).begin() as connection:
        connection.execute(insert(OAuthClient).values(id='client', name='Agent', redirect_uris=['https://x'],
                                                      created_at=now()))
        connection.execute(insert(OAuthGrant).values(id=new_id(), user_id=user_id, client_id='client', scopes=['jobs:read'],
                                                     created_at=now()))
        connection.execute(insert(AgentToken).values(id=new_id(), user_id=user_id, name='t', prefix='nm_ingest_x',
                                                     token_hash='h' * 64, permissions=['roles:ingest'], created_at=now(),
                                                     expires_at=now()))
        connection.execute(insert(HarnessGrant).values(id=new_id(), user_id=user_id, prefix='nm_harness_x',
                                                       token_hash='g' * 64, scopes=['jobs:read'], created_at=now()))
    caplog.set_level(logging.INFO, logger='jobtracker.accounts')
    with app.app_context():
        with engine(app).begin() as connection:
            assert credentials.revoke_agents(connection, user_id, harness=False, via='reset') == {
                'oauth': 1, 'agent': 1, 'harness': 0}
        runner = app.test_cli_runner()
        output = runner.invoke(args=['user', 'revoke-agents', str(user_id), '--harness']).output
    assert 'Revoked 0 OAuth connection(s), 0 agent token(s) and 1 harness grant(s)' in output
    assert f'revoked user_id={user_id} oauth=1 agent=1 harness=0 via=reset' in caplog.text
    assert f'revoked user_id={user_id} oauth=0 agent=0 harness=1 via=cli' in caplog.text


def test_normalize_new_email_is_strict_but_lookup_stays_lenient():
    assert credentials.normalize_new_email(' A@Example.com ') == 'a@example.com'
    assert credentials.normalize_new_email('owner@localhost') is None
    assert credentials.normalize_email('owner@localhost') == 'owner@localhost'


def test_user_create_verify_unverify_and_list(app):
    runner = app.test_cli_runner()
    created = runner.invoke(args=['user', 'create', 'new@example.com'], input='a-good-password\na-good-password\n')
    assert created.exit_code == 0 and '(verified)' in created.output
    assert runner.invoke(args=['user', 'create', 'x@localhost'], input='a-good-password\na-good-password\n').exit_code != 0
    assert runner.invoke(args=['user', 'unverify', 'new@example.com']).exit_code == 0
    listed = runner.invoke(args=['user', 'list', '--unverified']).output
    assert 'new@example.com\tunverified\tdeliverable' in listed and 'owner@example.com' not in listed
    assert runner.invoke(args=['user', 'verify', 'new@example.com']).exit_code == 0
    assert 'new@example.com' not in runner.invoke(args=['user', 'list', '--unverified']).output
    add_user(app, 'owner@localhost', verified=False)
    assert 'owner@localhost' in runner.invoke(args=['user', 'list', '--undeliverable']).output
    assert runner.invoke(args=['user', 'verify', 'owner@localhost']).exit_code != 0


def test_schema_14_databases_upgrade_to_15(app, config):
    """Every row survives; users gain a NULL email_verified_at; queued_tasks accepts NULL owners afterwards."""
    legacy = add_user(app, 'legacy@example.com')
    with engine(app).begin() as connection:
        connection.execute(insert(QueuedTask).values(id=new_id(), user_id=legacy, kind='extract_preview', payload={'a': 1},
                                                     status='succeeded', attempts=1, max_attempts=2, run_after=now(),
                                                     last_error='', created_at=now(), updated_at=now()))
    engine(app).dispose()
    with closing(sqlite3.connect(database_path(app))) as connection:
        rewind_verification(connection)
        before_users = connection.execute('SELECT id, email, password_hash, session_version FROM users ORDER BY id').fetchall()
        before_tasks = connection.execute('SELECT * FROM queued_tasks').fetchall()
        sequence = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='users'").fetchone()
        assert connection.execute('SELECT version FROM schema_version').fetchone() == (14,)
    upgraded = create_app(config)
    with closing(sqlite3.connect(database_path(upgraded))) as connection:
        assert connection.execute('SELECT version FROM schema_version').fetchone()[0] >= 15  # 16: password reset.
        assert connection.execute('SELECT id, email, password_hash, session_version FROM users ORDER BY id'
                                  ).fetchall() == before_users
        assert connection.execute('SELECT COUNT(*) FROM users WHERE email_verified_at IS NOT NULL').fetchone() == (0,)
        assert connection.execute('SELECT * FROM queued_tasks').fetchall() == before_tasks
        assert connection.execute("SELECT seq FROM sqlite_sequence WHERE name='users'").fetchone() == sequence
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    with engine(upgraded).begin() as connection:
        connection.execute(insert(QueuedTask).values(id=new_id(), user_id=None, kind='send_confirmation', payload={},
                                                     status='queued', attempts=0, max_attempts=5, run_after=now(),
                                                     last_error='', created_at=now(), updated_at=now()))
    # The upgraded schema matches a fresh one for the new and rebuilt tables.
    fresh = create_app({**config, 'DATABASE_URL': config['DATABASE_URL'] + '.fresh'})

    def shape(target, table):
        inspector = inspect(engine(target))
        return ({column['name']: (str(column['type']), column['nullable']) for column in inspector.get_columns(table)},
                sorted(index['name'] for index in inspector.get_indexes(table)),
                sorted(constraint['name'] or '' for constraint in inspector.get_check_constraints(table)))
    for table in ('email_confirmations', 'mail_usage', 'queued_tasks', 'users'):
        assert shape(upgraded, table) == shape(fresh, table), table
    # Legacy accounts must verify at next sign-in (the verification flow is tested elsewhere).
    response = upgraded.test_client().post('/api/v1/auth/login', json={'email': 'legacy@example.com',
                                                                          'password': 'other-password-123'})
    assert response.status_code == 403 and response.json['code'] == 'verification_required'
