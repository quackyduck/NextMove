"""Persistent, atomic login budget for the single workspace, independent of IP.

Reserve before password hashing, including successful/malformed attempts. Do not
clear on success: concurrent successes must not reset another worker's budget.
No password, token, or client address is stored. A separate SQLite file avoids
changing the version-1 personal-data schema and survives application restarts.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import time

from flask import current_app

WINDOW_SECONDS = 300
MAX_ATTEMPTS = 10


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
        connection.commit()
    app.extensions['login_throttle_path'] = path


def reserve_attempt():
    try:
        with closing(sqlite3.connect(current_app.extensions['login_throttle_path'], timeout=5)) as connection:
            # Serialize pruning, counting, and reservation across processes. The
            # password hash runs AFTER commit, so slow hashes never hold a lock.
            connection.execute('BEGIN IMMEDIATE')
            now = time.time()
            # Future timestamps also count: clock rollback must not reset budget.
            connection.execute('DELETE FROM attempts WHERE timestamp <= ?', (now - WINDOW_SECONDS,))
            count = connection.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
            allowed = count < MAX_ATTEMPTS
            if allowed:
                connection.execute('INSERT INTO attempts (timestamp) VALUES (?)', (now,))
            connection.commit()
            return allowed
    except sqlite3.Error as exc:
        # Fail closed; do not include paths or database diagnostics in responses.
        raise Unavailable() from exc
