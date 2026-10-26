"""Search-only credential and cross-worker budget, separate from write authority."""
from contextlib import closing
from functools import wraps
import hashlib
import hmac
import re
import sqlite3
import time

from flask import current_app, request

from .auth import error, require_auth, require_agent_token


def require_search_access(view):
    existing_access = require_auth(view)
    managed_access = require_agent_token(view, permission='roles:search')

    @wraps(view)
    def wrapped(*args, **kwargs):
        # One global budget bounds search work and invalid bearer attempts across
        # workers/restarts. Does not consume the browser password-login budget.
        try:
            with closing(sqlite3.connect(current_app.extensions['login_throttle_path'], timeout=5)) as connection:
                connection.execute('BEGIN IMMEDIATE')
                now = time.time()
                connection.execute('DELETE FROM search_attempts WHERE timestamp <= ?', (now - 60,))
                count = connection.execute('SELECT COUNT(*) FROM search_attempts').fetchone()[0]
                if count < 120:
                    connection.execute('INSERT INTO search_attempts (timestamp) VALUES (?)', (now,))
                connection.commit()
        except sqlite3.Error:
            return error('Search temporarily unavailable', 503)
        if count >= 120:
            response, status = error('Search rate limit exceeded; retry in one minute', 429)
            response.headers['Retry-After'] = '60'
            return response, status
        authorization = request.headers.get('Authorization', '')
        scheme, _, raw = authorization.partition(' ')
        if scheme.lower() == 'bearer' and raw.startswith('nm_ingest_'):
            return managed_access(*args, **kwargs)
        verifier = current_app.config.get('ROLE_SEARCH_TOKEN_SHA256')
        if verifier and re.fullmatch(r'[0-9a-f]{64}', verifier) and authorization.startswith('Bearer '):
            token = authorization[7:]
            if 32 <= len(token) <= 512 and hmac.compare_digest(
                    hashlib.sha256(token.encode('utf-8')).hexdigest(), verifier):
                return view(*args, **kwargs)
        return existing_access(*args, **kwargs)
    return wrapped
