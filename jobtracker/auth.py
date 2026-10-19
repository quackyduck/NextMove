"""Single-user browser sessions and agent bearer authentication."""
from functools import wraps
import hmac
import secrets

from flask import Blueprint, current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash
from . import throttle

auth = Blueprint('auth', __name__)


def error(message, status):
    return jsonify(error=message), status


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get('Authorization', '')
        if authorization:
            token = current_app.config.get('AGENT_API_TOKEN')
            if not token or not hmac.compare_digest(authorization.encode('utf-8'), f'Bearer {token}'.encode('utf-8')):
                return error('Authentication required', 401)
            g.auth_type = 'bearer'
        elif session.get('authenticated'):
            g.auth_type = 'session'
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                supplied = request.headers.get('X-CSRF-Token', '')
                expected = session.get('csrf_token', '')
                if not supplied or not expected or not hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
                    return error('Invalid CSRF token', 403)
        else:
            return error('Authentication required', 401)
        return view(*args, **kwargs)
    return wrapped


@auth.get('/auth/session')
def session_status():
    return jsonify(authenticated=bool(session.get('authenticated')),
                   csrf_token=session.get('csrf_token') if session.get('authenticated') else None)


@auth.post('/auth/login')
def login():
    # Reject cross-origin browser login requests; no cross-origin API is needed.
    origin = request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        return error('Invalid request origin', 403)
    try:
        allowed = throttle.reserve_attempt()
    except throttle.Unavailable:
        return error('Login temporarily unavailable; try again later', 503)
    if not allowed:
        response, status = error('Too many login attempts; try again in five minutes', 429)
        response.headers['Retry-After'] = str(throttle.WINDOW_SECONDS)
        return response, status
    payload = request.get_json(silent=True)
    password = payload.get('password') if isinstance(payload, dict) else None
    password_hash = current_app.config['APP_PASSWORD_HASH']
    if not isinstance(password, str) or not check_password_hash(password_hash, password):
        return error('Invalid password', 401)
    session.clear()
    session['authenticated'] = True
    session['csrf_token'] = secrets.token_urlsafe(32)
    session.permanent = True
    return jsonify(authenticated=True, csrf_token=session['csrf_token'])


@auth.post('/auth/logout')
@require_auth
def logout():
    session.clear()
    return jsonify(authenticated=False, csrf_token=None)
