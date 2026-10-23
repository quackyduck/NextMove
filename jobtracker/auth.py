"""Single-user browser sessions, password changes and agent authentication."""
from functools import wraps
import hmac
import secrets

from flask import Blueprint, current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from . import credentials, throttle

auth = Blueprint('auth', __name__)


def error(message, status):
    return jsonify(error=message), status


def browser_authenticated():
    if not session.get('authenticated'):
        return False
    credential = credentials.read()
    if session.get('session_version') != credential.session_version:
        session.clear()
        return False
    g.credential = credential
    return True


def issue_session(version):
    session.clear()
    session['authenticated'] = True
    session['session_version'] = version
    session['csrf_token'] = secrets.token_urlsafe(32)
    session.permanent = True


def reserve_password_attempt():
    try:
        allowed = throttle.reserve_attempt()
    except throttle.Unavailable:
        return error('Sign-in temporarily unavailable; try again later', 503)
    if not allowed:
        response, status = error('Too many attempts; try again in five minutes', 429)
        response.headers['Retry-After'] = str(throttle.WINDOW_SECONDS)
        return response, status
    return None


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get('Authorization', '')
        if authorization:
            token = current_app.config.get('AGENT_API_TOKEN')
            if not token or not hmac.compare_digest(authorization.encode('utf-8'), f'Bearer {token}'.encode('utf-8')):
                return error('Authentication required', 401)
            g.auth_type = 'bearer'
        else:
            try:
                valid = browser_authenticated()
            except credentials.Unavailable:
                return error('Sign-in temporarily unavailable; try again later', 503)
            if not valid:
                return error('Authentication required', 401)
            g.auth_type = 'session'
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                supplied = request.headers.get('X-CSRF-Token', '')
                expected = session.get('csrf_token', '')
                if not supplied or not expected or not hmac.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
                    return error('Invalid CSRF token', 403)
        return view(*args, **kwargs)
    return wrapped


@auth.get('/auth/session')
def session_status():
    try:
        valid = browser_authenticated()
    except credentials.Unavailable:
        return error('Sign-in temporarily unavailable; try again later', 503)
    return jsonify(authenticated=valid, csrf_token=session.get('csrf_token') if valid else None)


@auth.post('/auth/login')
def login():
    origin = request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        return error('Invalid request origin', 403)
    rejection = reserve_password_attempt()
    if rejection:
        return rejection
    payload = request.get_json(silent=True)
    password = payload.get('password') if isinstance(payload, dict) else None
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        return error('Invalid password', 401)
    try:
        credential = credentials.read()
    except credentials.Unavailable:
        return error('Sign-in temporarily unavailable; try again later', 503)
    if not check_password_hash(credential.password_hash, password):
        return error('Invalid password', 401)
    # Bind to the generation observed BEFORE hashing. A concurrent password
    # reset must not give this older verification the new session authority.
    issue_session(credential.session_version)
    return jsonify(authenticated=True, csrf_token=session['csrf_token'])


@auth.post('/auth/password')
@require_auth
def change_password():
    if g.auth_type != 'session':
        return error('Browser sign-in required to change the workspace password', 403)
    payload = request.get_json(silent=True)
    required = {'current_password', 'new_password', 'confirm_password'}
    if not isinstance(payload, dict) or set(payload) != required:
        return error('Provide current password, new password, and confirmation', 400)
    old, new, confirmation = (payload[key] for key in ('current_password', 'new_password', 'confirm_password'))
    if not isinstance(old, str) or not 1 <= len(old) <= 128:
        return error('Your current password is incorrect', 400)
    if not isinstance(new, str) or not 8 <= len(new) <= 128:
        return error('Choose a password between 8 and 128 characters', 400)
    if not isinstance(confirmation, str) or new != confirmation:
        return error('New passwords do not match', 400)
    rejection = reserve_password_attempt()
    if rejection:
        return rejection
    observed = g.credential
    if not check_password_hash(observed.password_hash, old):
        return error('Your current password is incorrect', 400)
    try:
        changed = credentials.replace(observed, generate_password_hash(new))
    except credentials.Unavailable:
        return error('Password update temporarily unavailable; try again later', 503)
    if not changed:
        session.clear()
        return error('Your sign-in changed. Sign in again before updating the password', 409)
    issue_session(observed.session_version + 1)
    return jsonify(authenticated=True, csrf_token=session['csrf_token'],
                   message='Password updated. Other sessions have been signed out.')


@auth.post('/auth/logout')
@require_auth
def logout():
    session.clear()
    return jsonify(authenticated=False, csrf_token=None)
