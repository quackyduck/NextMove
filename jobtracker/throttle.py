"""Persistent, atomic attempt budgets per account, per client IP and overall.

Reserve before password hashing, including successful/malformed attempts. Do not
clear on success: concurrent successes must not reset another worker's budget.
No password, token, email or client address is stored: budget keys are HMACs
under SECRET_KEY. A separate SQLite file survives application restarts and is
shared by every web worker.

Limits per five-minute window: 10 per account (email), 30 per client IP, and
200 overall so slow password hashes cannot exhaust the small host's CPU.
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
MAX_IP_ATTEMPTS = 30
MAX_GLOBAL_ATTEMPTS = 200
SIGNUP_IP_ATTEMPTS = 10


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
        connection.execute('CREATE TABLE IF NOT EXISTS attempts (timestamp REAL NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS search_attempts (timestamp REAL NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS keyed_attempts (key TEXT NOT NULL, timestamp REAL NOT NULL)')
        connection.execute('CREATE INDEX IF NOT EXISTS keyed_attempts_key ON keyed_attempts (key, timestamp)')
        connection.commit()
    app.extensions['login_throttle_path'] = path


def key(kind, value):
    secret = current_app.config['SECRET_KEY'].encode()
    return kind + ':' + hmac.new(secret, f'{kind}\0{value}'.encode(), hashlib.sha256).hexdigest()[:32]


def client_key():
    # With TRUST_PROXY, ProxyFix sets remote_addr from the single trusted proxy.
    return key('ip', request.remote_addr or 'unknown')


def account_key(identity):
    return key('account', identity)


def reserve(budgets):
    """Atomically reserve one attempt in every (key, limit) budget, or none.

    Returns True when all budgets had room. The global budget always applies.
    """
    budgets = [('global', MAX_GLOBAL_ATTEMPTS), *budgets]
    try:
        with closing(sqlite3.connect(current_app.extensions['login_throttle_path'], timeout=5)) as connection:
            # Serialize pruning, counting, and reservation across processes. The
            # password hash runs AFTER commit, so slow hashes never hold a lock.
            connection.execute('BEGIN IMMEDIATE')
            now = time.time()
            # Future timestamps also count: clock rollback must not reset budget.
            connection.execute('DELETE FROM keyed_attempts WHERE timestamp <= ?', (now - WINDOW_SECONDS,))
            allowed = all(connection.execute('SELECT COUNT(*) FROM keyed_attempts WHERE key = ?', (name,)).fetchone()[0] < limit
                          for name, limit in budgets)
            if allowed:
                connection.executemany('INSERT INTO keyed_attempts (key, timestamp) VALUES (?, ?)',
                                       [(name, now) for name, _ in budgets])
            connection.commit()
            return allowed
    except sqlite3.Error as exc:
        # Fail closed; do not include paths or database diagnostics in responses.
        raise Unavailable() from exc


def reserve_attempt(identity=None):
    """Password verification budget for one account (email or user id) and IP."""
    budgets = [(client_key(), MAX_IP_ATTEMPTS)]
    if identity is not None:
        budgets.append((account_key(identity), MAX_ATTEMPTS))
    return reserve(budgets)


def reserve_signup():
    return reserve([(key('signup-ip', request.remote_addr or 'unknown'), SIGNUP_IP_ATTEMPTS),
                    (client_key(), MAX_IP_ATTEMPTS)])
