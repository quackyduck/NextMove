"""Persistent, atomic attempt budgets keyed per account and per client IP.

Reserve before password hashing, including successful/malformed attempts. Do not
clear on success: concurrent successes must not reset another worker's budget.
No password, token, email or client address is stored: budget keys are HMACs
under SECRET_KEY. A separate SQLite file survives application restarts and is
shared by every web worker.

There is deliberately no global budget: in a multi-tenant app it would let a
few addresses lock every account out. Login budgets per five minutes:

* 30 per client IP (bounds one client's hashing work and password spraying);
* 10 per account (email, known or not, so 429 never reveals an account) from
  any address; once spent, a client that has made fewer than 3 attempts on
  that account in the window may still try, up to 50 per account in total. An
  attacker hammering a known email therefore cannot lock its owner out from
  their own network, while a distributed guesser needs a fresh address per 3
  guesses and stops at 50 per window.

Signed-in re-verification (password change, token creation) uses a per-user
budget only, so login abuse against an email cannot block its owner's session.
"""
from contextlib import closing
import hashlib
import hmac
from pathlib import Path
import sqlite3
import time

from flask import current_app, request

WINDOW_SECONDS = 300
MAX_ATTEMPTS = 10
MAX_CLEAN_CLIENT_ATTEMPTS = 50
CLEAN_CLIENT_TRIES = 3
MAX_IP_ATTEMPTS = 30
SIGNUP_IP_ATTEMPTS = 10
SEARCH_WINDOW_SECONDS = 60
SEARCH_ATTEMPTS = 120


class Unavailable(Exception):
    pass


def init_app(app):
    configured = app.config.get('LOGIN_THROTTLE_DATABASE')
    path = Path(configured) if configured else Path(app.config['UPLOAD_FOLDER']).parent / 'login-throttle.sqlite3'
    if str(path) == ':memory:':
        raise RuntimeError('Login throttle requires a persistent SQLite file')
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation gives a new file private permissions before SQLite opens
    # it. Existing files are retained, including their active login budget.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        # attempts/search_attempts are pre-schema-9 tables, kept so older builds still start.
        connection.execute('CREATE TABLE IF NOT EXISTS attempts (timestamp REAL NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS search_attempts (timestamp REAL NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS keyed_attempts (key TEXT NOT NULL, timestamp REAL NOT NULL)')
        connection.execute('CREATE INDEX IF NOT EXISTS keyed_attempts_key ON keyed_attempts (key, timestamp)')
        connection.commit()
    app.extensions['login_throttle_path'] = path


def key(kind, value):
    secret = current_app.config['SECRET_KEY'].encode()
    return kind + ':' + hmac.new(secret, f'{kind}\0{value}'.encode(), hashlib.sha256).hexdigest()[:32]


def client_address():
    # With TRUST_PROXY, ProxyFix sets remote_addr from the single trusted proxy.
    return request.remote_addr or 'unknown'


def _transaction(decide):
    """Run decide(count, record) atomically; count(key, window) and record(keys)."""
    try:
        with closing(sqlite3.connect(current_app.extensions['login_throttle_path'], timeout=5)) as connection:
            # Serialize pruning, counting, and reservation across processes. The
            # password hash runs AFTER commit, so slow hashes never hold a lock.
            connection.execute('BEGIN IMMEDIATE')
            now = time.time()
            # Future timestamps also count: clock rollback must not reset budget.
            connection.execute('DELETE FROM keyed_attempts WHERE timestamp <= ?', (now - WINDOW_SECONDS,))

            def count(name, window=WINDOW_SECONDS):
                return connection.execute('SELECT COUNT(*) FROM keyed_attempts WHERE key = ? AND timestamp > ?',
                                          (name, now - window)).fetchone()[0]

            def record(names):
                connection.executemany('INSERT INTO keyed_attempts (key, timestamp) VALUES (?, ?)',
                                       [(name, now) for name in names])
            allowed = decide(count, record)
            connection.commit()
            return allowed
    except sqlite3.Error as exc:
        # Fail closed; do not include paths or database diagnostics in responses.
        raise Unavailable() from exc


def reserve(budgets, window=WINDOW_SECONDS):
    """Reserve one attempt in every (key, limit) budget atomically, or none."""
    def decide(count, record):
        allowed = all(count(name, window) < limit for name, limit in budgets)
        if allowed:
            record([name for name, _ in budgets])
        return allowed
    return _transaction(decide)


def reserve_login(email):
    """Login budget for a normalized email (None when malformed) from this client."""
    ip = key('ip', client_address())
    account = key('account', email) if email is not None else None
    pair = key('account-ip', f'{email}\0{client_address()}') if email is not None else None

    def decide(count, record):
        if count(ip) >= MAX_IP_ATTEMPTS:
            return False
        if account is not None:
            used = count(account)
            clean_client = count(pair) < CLEAN_CLIENT_TRIES
            if used >= MAX_CLEAN_CLIENT_ATTEMPTS or (used >= MAX_ATTEMPTS and not clean_client):
                return False
        record([name for name in (ip, account, pair) if name])
        return True
    return _transaction(decide)


def reserve_verification(user_id):
    """Re-verifying a signed-in user's password (password change, token creation)."""
    return reserve([(key('verify', user_id), MAX_ATTEMPTS)])


def reserve_signup():
    return reserve([(key('signup-ip', client_address()), SIGNUP_IP_ATTEMPTS),
                    (key('ip', client_address()), MAX_IP_ATTEMPTS)])


def reserve_search(user_id=None):
    """Search work per account after authentication, or per IP before it."""
    name = key('search-user', user_id) if user_id is not None else key('search-ip', client_address())
    return reserve([(name, SEARCH_ATTEMPTS)], SEARCH_WINDOW_SECONDS)
