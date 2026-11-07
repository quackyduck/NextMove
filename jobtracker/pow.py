"""Self-hosted proof-of-work for unauthenticated requests that send mail.

Sign-up, activation, resend and the password-reset request each cost the client
about a second of hashing, so a script cannot spend the mail quota for free and
the single web thread is spared scrypt work. No third party, no CSP change: the
browser solves in a same-origin Web Worker (frontend/src/pow.worker.js).

Challenge (stateless): `b64url(json({p, s, b, e})) + "." + b64url(HMAC)`, where
p is the purpose, s a random salt, b the required leading zero bits and e the
expiry (10 minutes). The HMAC key is derived per purpose from SECRET_KEY, so a
challenge for one form never works on another. A solution is a decimal nonce
such that SHA-256(challenge + ":" + nonce) starts with b zero bits. Each
challenge can be spent once (its digest is recorded in the throttle database
for its lifetime). Difficulty rises by POW_DEGRADED_EXTRA_BITS while mail is
degraded or the sign-up share is nearly spent.

This slows scripts; it does not stop a botnet (Turnstile is the next step).
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from flask import current_app

from . import throttle

PURPOSES = ('signup', 'activate', 'resend', 'reset')
LIFETIME_SECONDS = 600
NONCE = re.compile(r'[0-9]{1,20}')
MAX_CHALLENGE = 512


def init_app(app):
    # Tests default to a trivial difficulty unless they set POW_BITS themselves.
    app.config.setdefault('POW_BITS', int(os.getenv('POW_BITS', '4' if app.testing else '18')))
    app.config.setdefault('POW_DEGRADED_EXTRA_BITS', int(os.getenv('POW_DEGRADED_EXTRA_BITS', '2')))
    if not 1 <= app.config['POW_BITS'] <= 28 or not 0 <= app.config['POW_DEGRADED_EXTRA_BITS'] <= 6:
        raise RuntimeError('POW_BITS must be 1-28 and POW_DEGRADED_EXTRA_BITS 0-6')


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _key(purpose):
    return hmac.new(current_app.config['SECRET_KEY'].encode(), f'pow:{purpose}'.encode(), hashlib.sha256).digest()


def difficulty():
    """Bits required now: POW_BITS, plus the extra bits while mail is degraded or sign-up is nearly full."""
    bits = current_app.config['POW_BITS']
    from . import mailer
    try:
        busy = mailer.status(cached=True) == 'degraded' or (
            mailer.enabled() and mailer.sent_today('signup-new') >= 0.8 * mailer._category_limit('signup'))
    except Exception:  # noqa: BLE001 - difficulty must never fail the request
        busy = False
    return bits + (current_app.config['POW_DEGRADED_EXTRA_BITS'] if busy else 0)


def issue(purpose):
    """A new signed challenge: {challenge, bits, expires_at}."""
    if purpose not in PURPOSES:
        raise ValueError('Unknown proof-of-work purpose')
    bits = difficulty()
    expires = int(time.time()) + LIFETIME_SECONDS
    body = _b64(json.dumps({'p': purpose, 's': _b64(secrets.token_bytes(16)), 'b': bits, 'e': expires},
                           separators=(',', ':'), sort_keys=True).encode())
    signature = _b64(hmac.new(_key(purpose), body.encode(), hashlib.sha256).digest())
    return {'challenge': f'{body}.{signature}', 'bits': bits, 'expires_at': expires}


def leading_zero_bits(digest):
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        return bits + 8 - byte.bit_length()
    return bits


def _valid(purpose, proof):
    if purpose not in PURPOSES or not isinstance(proof, dict):
        return None
    challenge, nonce = proof.get('challenge'), proof.get('nonce')
    if not isinstance(challenge, str) or not isinstance(nonce, str) or len(challenge) > MAX_CHALLENGE:
        return None
    if not NONCE.fullmatch(nonce):
        return None
    body, _, signature = challenge.partition('.')
    expected = _b64(hmac.new(_key(purpose), body.encode(), hashlib.sha256).digest())
    if not signature or not hmac.compare_digest(signature.encode(), expected.encode()):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get('p') != purpose:
        return None
    bits, expires = payload.get('b'), payload.get('e')
    if type(bits) is not int or type(expires) is not int or not 1 <= bits <= 34 or expires < time.time():
        return None
    digest = hashlib.sha256(f'{challenge}:{nonce}'.encode()).digest()
    if leading_zero_bits(digest) < bits:
        return None
    return challenge


def verify(purpose, proof):
    """True when `proof` ({challenge, nonce}) solves an unexpired challenge for `purpose`, spending it once.

    Call first, before throttles or password hashing. Raises throttle.Unavailable
    when the spent-challenge store is unavailable (callers answer 503).
    """
    challenge = _valid(purpose, proof)
    if challenge is None:
        return False
    spent = throttle.key('pow', hashlib.sha256(challenge.encode()).hexdigest())
    return throttle.reserve([(spent, 1, LIFETIME_SECONDS + 60)])
