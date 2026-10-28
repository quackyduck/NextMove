"""Test helpers for accounts: create users and sign in as them."""
from sqlalchemy import insert
from werkzeug.security import generate_password_hash

from jobtracker.models import User, now

OWNER = 'owner@localhost'


def add_user(app, email, password='other-password-123', user_id=None):
    """Insert an account directly (bypassing invites); returns its id."""
    values = dict(email=email, password_hash=generate_password_hash(password, method='pbkdf2:sha256:1000'),
                  session_version=1, created_at=now())
    if user_id is not None:
        values['id'] = user_id
    with app.extensions['jobtracker_db']['engine'].begin() as connection:
        return connection.execute(insert(User).values(**values)).inserted_primary_key[0]


def sign_in(client, email=OWNER, password='test-password', **kwargs):
    response = client.post('/api/v1/auth/login', json={'email': email, 'password': password}, **kwargs)
    assert response.status_code == 200, response.json
    return {'X-CSRF-Token': response.json['csrf_token']}
