"""Test helpers for accounts: create users and sign in as them."""
from sqlalchemy import insert
from werkzeug.security import generate_password_hash

from jobtracker.models import User, now

OWNER = 'owner@localhost'


def add_user(app, email, password='other-password-123', user_id=None, verified=True):
    """Insert an account directly (bypassing sign-up); verified unless `verified=False` (a legacy account)."""
    values = dict(email=email, password_hash=generate_password_hash(password, method='pbkdf2:sha256:1000'),
                  session_version=1, created_at=now(), email_verified_at=now() if verified else None)
    if user_id is not None:
        values['id'] = user_id
    with app.extensions['jobtracker_db']['engine'].begin() as connection:
        return connection.execute(insert(User).values(**values)).inserted_primary_key[0]


def sign_in(client, email=OWNER, password='test-password', **kwargs):
    response = client.post('/api/v1/auth/login', json={'email': email, 'password': password}, **kwargs)
    assert response.status_code == 200, response.json
    return {'X-CSRF-Token': response.json['csrf_token']}


def verify_all(app):
    """Mark every account with an email verified, as an operator would with `flask user verify` after an upgrade."""
    from sqlalchemy import update
    with app.extensions['jobtracker_db']['engine'].begin() as connection:
        connection.execute(update(User).where(User.email.is_not(None), User.email_verified_at.is_(None))
                           .values(email_verified_at=now()))


def solve_proof(client, purpose):
    """Fetch a proof-of-work challenge and solve it (tests run at a trivial difficulty)."""
    import hashlib
    from jobtracker.pow import leading_zero_bits
    issued = client.get(f'/api/v1/auth/challenge?purpose={purpose}').json
    nonce = 0
    while leading_zero_bits(hashlib.sha256(f'{issued["challenge"]}:{nonce}'.encode()).digest()) < issued['bits']:
        nonce += 1
    return {'challenge': issued['challenge'], 'nonce': str(nonce)}


def last_link(email=None):
    """The token from the newest confirmation email in the memory outbox (optionally for one address)."""
    import re
    from jobtracker import mailer
    for message in reversed(mailer.outbox):
        if email is None or message['To'] == email:
            found = re.search(r'#token=([A-Za-z0-9_-]+)', message.get_body(('plain',)).get_content())
            if found:
                return found.group(1)
    raise AssertionError('No confirmation link was sent')


def sign_up(app, client, email, password='new-password-123', invite_code=None, **extra):
    """The full sign-up: proof-of-work, 202, the worker sends the link, confirm in the same browser.

    Returns the CSRF headers of the new session.
    """
    from jobtracker import taskqueue
    body = {'email': email, 'password': password, 'password_confirmation': password,
            'proof': solve_proof(client, 'signup'), **extra}
    if invite_code is not None:
        body['invite_code'] = invite_code
    response = client.post('/api/v1/auth/signup', json=body)
    assert response.status_code == 202, response.json
    taskqueue.drain(app)
    confirmed = client.post('/api/v1/auth/email-verification/confirm',
                            json={'token': last_link(email.strip().lower())})
    assert confirmed.status_code == 200, confirmed.json
    return {'X-CSRF-Token': confirmed.json['csrf_token']}
