"""Search-only credential and per-account budget, separate from write authority."""
from functools import wraps
import hashlib
import hmac
import re

from flask import current_app, g, request

from . import throttle
from .auth import error, legacy_owner, require_auth, require_agent_token
from .oauth import ACCESS_PREFIX, require_oauth


def budgeted(view):
    """Charge search work to the authenticated account, so tenants never share a budget."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            allowed = throttle.reserve_search(g.user_id)
        except throttle.Unavailable:
            return error('Search temporarily unavailable', 503)
        if not allowed:
            response, status = error('Search rate limit exceeded; retry in one minute', 429)
            response.headers['Retry-After'] = str(throttle.SEARCH_WINDOW_SECONDS)
            return response, status
        return view(*args, **kwargs)
    return wrapped


def require_search_access(view):
    charged = budgeted(view)
    existing_access = require_auth(charged)
    managed_access = require_agent_token(charged, permission='roles:search')
    oauth_access = require_oauth('jobs:read')(charged)

    @wraps(view)
    def wrapped(*args, **kwargs):
        # A per-IP budget first bounds unauthenticated/invalid bearer attempts
        # (and token-hash work) without affecting any other client or account.
        try:
            allowed = throttle.reserve_search()
        except throttle.Unavailable:
            return error('Search temporarily unavailable', 503)
        if not allowed:
            response, status = error('Search rate limit exceeded; retry in one minute', 429)
            response.headers['Retry-After'] = str(throttle.SEARCH_WINDOW_SECONDS)
            return response, status
        authorization = request.headers.get('Authorization', '')
        scheme, _, raw = authorization.partition(' ')
        if scheme.lower() == 'bearer' and raw.startswith('nm_ingest_'):
            return managed_access(*args, **kwargs)
        if scheme.lower() == 'bearer' and raw.startswith(ACCESS_PREFIX):
            return oauth_access(*args, **kwargs)
        verifier = current_app.config.get('ROLE_SEARCH_TOKEN_SHA256')
        if verifier and re.fullmatch(r'[0-9a-f]{64}', verifier) and authorization.startswith('Bearer '):
            token = authorization[7:]
            if 32 <= len(token) <= 512 and hmac.compare_digest(
                    hashlib.sha256(token.encode('utf-8')).hexdigest(), verifier):
                owner = legacy_owner()
                if owner is not None:
                    g.user_id = owner
                    return charged(*args, **kwargs)
        return existing_access(*args, **kwargs)
    return wrapped
