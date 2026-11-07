"""Proof-of-work challenges: signing, difficulty, expiry, single spend, and the challenge route."""
import base64
import hashlib
import json
import time

import pytest
from werkzeug.security import generate_password_hash

from jobtracker import create_app, pow


@pytest.fixture
def app(tmp_path):
    return create_app({
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'pow-test-secret-0123456789abcdef',
        'APP_PASSWORD_HASH': generate_password_hash('test-password', method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "pow.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 't.sqlite3'), 'SESSION_COOKIE_SECURE': False, 'POW_BITS': 6,
    })


def solve(challenge, bits):
    nonce = 0
    while pow.leading_zero_bits(hashlib.sha256(f'{challenge}:{nonce}'.encode()).digest()) < bits:
        nonce += 1
    return {'challenge': challenge, 'nonce': str(nonce)}


def test_leading_zero_bits():
    assert pow.leading_zero_bits(b'\x00\x00\x80') == 16
    assert pow.leading_zero_bits(b'\x00\x01') == 15
    assert pow.leading_zero_bits(b'\xff') == 0
    assert pow.leading_zero_bits(b'\x00\x00') == 16


def test_a_solution_verifies_once_for_its_purpose_only(app):
    with app.test_request_context('/'):
        issued = pow.issue('signup')
        assert issued['bits'] == 6
        proof = solve(issued['challenge'], issued['bits'])
        assert not pow.verify('resend', proof)  # Per-purpose key: no cross-form reuse.
        assert pow.verify('signup', proof)
        assert not pow.verify('signup', proof)  # Spent.


def test_tampering_and_weak_or_malformed_proofs_fail(app):
    with app.test_request_context('/'):
        issued = pow.issue('signup')
        body, signature = issued['challenge'].split('.')
        payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        easier = dict(payload, b=1)
        forged = base64.urlsafe_b64encode(json.dumps(easier, separators=(',', ':'), sort_keys=True).encode()
                                          ).rstrip(b'=').decode() + '.' + signature
        assert not pow.verify('signup', solve(forged, 1))
        weak = {'challenge': issued['challenge'], 'nonce': '0'}
        while pow.leading_zero_bits(hashlib.sha256(f'{issued["challenge"]}:{weak["nonce"]}'.encode()).digest()) >= 6:
            weak['nonce'] = str(int(weak['nonce']) + 1)
        assert not pow.verify('signup', weak)
        for proof in (None, 'x', {}, {'challenge': issued['challenge']}, {'challenge': issued['challenge'], 'nonce': '-1'},
                      {'challenge': issued['challenge'], 'nonce': '1' * 21}, {'challenge': 'x' * 600, 'nonce': '1'},
                      {'challenge': 7, 'nonce': '1'}):
            assert not pow.verify('signup', proof)
        with pytest.raises(ValueError):
            pow.issue('login')


def test_expired_challenges_fail(app, monkeypatch):
    with app.test_request_context('/'):
        issued = pow.issue('reset')
        proof = solve(issued['challenge'], issued['bits'])
        real = time.time
        monkeypatch.setattr(pow.time, 'time', lambda: real() + pow.LIFETIME_SECONDS + 1)
        assert not pow.verify('reset', proof)


def test_difficulty_rises_while_mail_is_degraded(app, monkeypatch):
    from jobtracker import mailer
    with app.test_request_context('/'):
        assert pow.issue('signup')['bits'] == 6
        monkeypatch.setattr(mailer, 'status', lambda cached=False: 'degraded')
        assert pow.issue('signup')['bits'] == 8


def test_challenge_route_and_its_budget(app):
    client = app.test_client()
    response = client.get('/api/v1/auth/challenge?purpose=activate')
    assert response.status_code == 200 and set(response.json) == {'challenge', 'bits', 'expires_at'}
    assert client.get('/api/v1/auth/challenge?purpose=login').status_code == 400
    for _ in range(29):
        client.get('/api/v1/auth/challenge?purpose=signup')
    assert client.get('/api/v1/auth/challenge?purpose=signup').status_code == 429


def test_configuration_bounds(tmp_path):
    with pytest.raises(RuntimeError, match='POW_BITS'):
        create_app({'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'k' * 32, 'POW_BITS': 40,
                    'DATABASE_URL': f'sqlite:///{tmp_path / "b.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'u'),
                    'LOGIN_THROTTLE_DATABASE': str(tmp_path / 't.sqlite3')})
