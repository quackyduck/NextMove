"""Browser sign-in lasts a rolling 30 days: every authenticated request re-signs the cookie, so only a
month without any visit signs out. Password changes still sign out every other session at once."""
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import pytest
from itsdangerous import TimestampSigner
from werkzeug.security import generate_password_hash

from jobtracker import create_app

DAY = 24 * 3600


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'session-lifetime-secret',
                       'APP_PASSWORD_HASH': generate_password_hash('original-password'),
                       'DATABASE_URL': f'sqlite:///{tmp_path / "jobs.sqlite3"}',
                       'UPLOAD_FOLDER': str(tmp_path / 'resumes'), 'SESSION_COOKIE_SECURE': False})


@pytest.fixture
def clock(monkeypatch):
    """Moves the session signer's clock (the cookie's signed issue time and its age check) forward."""
    offset = {'seconds': 0}
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self: int(time.time()) + offset['seconds'])
    return offset


def login(client, password='original-password'):
    response = client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': password})
    assert response.status_code == 200
    return response


def session_cookie(response, app):
    name = app.config['SESSION_COOKIE_NAME']
    return next(value for value in response.headers.getlist('Set-Cookie') if value.startswith(f'{name}='))


def signed_in(client):
    return client.get('/api/v1/auth/session').json['authenticated']


def expires_in_days(cookie):
    expires = parsedate_to_datetime(re.search(r'Expires=([^;]+)', cookie).group(1))
    return (expires - datetime.now(timezone.utc)).total_seconds() / DAY


def test_sign_in_cookie_is_persistent_for_thirty_days(app):
    cookie = session_cookie(login(app.test_client()), app)
    assert 29.9 < expires_in_days(cookie) <= 30 and 'HttpOnly' in cookie


def test_session_survives_a_day_or_more_without_activity(app, clock):
    client = app.test_client()
    login(client)
    clock['seconds'] = 13 * 3600  # Overnight: the old 12-hour lifetime signed people out here.
    assert signed_in(client) is True
    clock['seconds'] = 29 * DAY
    assert signed_in(client) is True


def test_use_renews_the_session_and_a_month_idle_signs_out(app, clock):
    client = app.test_client()
    issued = session_cookie(login(client), app)
    clock['seconds'] = 20 * DAY
    response = client.get('/api/v1/auth/session')
    assert response.json['authenticated'] is True
    assert session_cookie(response, app) != issued  # Re-signed: the 30 days count from this visit.
    clock['seconds'] = 45 * DAY  # 45 days after sign-in, 25 after the last visit.
    assert signed_in(client) is True
    assert client.get('/api/v1/jobs').status_code == 200
    clock['seconds'] = 45 * DAY + 31 * DAY
    assert signed_in(client) is False
    assert client.get('/api/v1/jobs').status_code == 401


def test_password_change_still_signs_out_other_sessions_immediately(app):
    first, second = app.test_client(), app.test_client()
    headers = {'X-CSRF-Token': login(first).json['csrf_token']}
    login(second)
    changed = first.post('/api/v1/auth/password', headers=headers, json={
        'current_password': 'original-password', 'new_password': 'updated-password',
        'confirm_password': 'updated-password'})
    assert changed.status_code == 200
    assert signed_in(first) is True and signed_in(second) is False
