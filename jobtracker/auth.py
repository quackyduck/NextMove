"""Per-user browser sessions, signup, password changes and agent authentication.

Every authenticated request sets `g.user_id`; services scope all reads and
writes by it. Browser sessions carry the user id and that account's session
generation, so a password change signs out only that account's other sessions.
"""
from datetime import timezone
from functools import wraps
import hmac
import secrets

from flask import Blueprint, current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from . import credentials, throttle

auth = Blueprint('auth', __name__)
# The deprecated environment tokens (AGENT_API_TOKEN, ROLE_SEARCH_TOKEN_SHA256)
# predate accounts and act only for the account flagged owns_legacy_tokens (the
# migrated/bootstrapped workspace owner, user #1); see credentials.legacy_owner_id.


def error(message, status, field=None):
    body = {'error': message}
    if field is not None:
        body['field'] = field
    return jsonify(body), status


def user_summary(credential):
    created = credential.created_at
    if created is not None:
        created = created.replace(tzinfo=created.tzinfo or timezone.utc).isoformat().replace('+00:00', 'Z')
    return {'id': credential.user_id, 'email': credential.email, 'created_at': created}


def browser_authenticated():
    user_id = session.get('user_id')
    if not session.get('authenticated') or type(user_id) is not int:
        if session:
            session.clear()  # Pre-schema-9 cookies carry no account and sign in again.
        return False
    credential = credentials.read(user_id)
    if credential is None or session.get('session_version') != credential.session_version:
        session.clear()
        return False
    g.credential = credential
    g.user_id = credential.user_id
    return True


def issue_session(credential):
    session.clear()
    session['authenticated'] = True
    session['user_id'] = credential.user_id
    session['session_version'] = credential.session_version
    session['csrf_token'] = secrets.token_urlsafe(32)
    session.permanent = True


def throttled(reservation):
    try:
        allowed = reservation()
    except throttle.Unavailable:
        return error('Sign-in temporarily unavailable; try again later', 503)
    if not allowed:
        response, status = error('Too many attempts; try again in five minutes', 429)
        response.headers['Retry-After'] = str(throttle.WINDOW_SECONDS)
        return response, status
    return None




def legacy_owner():
    return credentials.legacy_owner_id()


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get('Authorization', '')
        if authorization:
            token = current_app.config.get('AGENT_API_TOKEN')
            if not token or not hmac.compare_digest(authorization.encode('utf-8'), f'Bearer {token}'.encode('utf-8')):
                return error('Authentication required', 401)
            owner = legacy_owner()
            if owner is None:
                return error('Authentication required', 401)
            g.auth_type = 'bearer'
            g.user_id = owner
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


def require_browser_auth(view):
    @require_auth
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_app.config['APP_ENV'] != 'development' and not request.is_secure:
            return error('HTTPS required', 403)
        if g.auth_type != 'session':
            return error('Browser sign-in required', 403)
        return view(*args, **kwargs)
    return wrapped


def require_agent_token(view=None, *, permission='roles:ingest'):
    if view is None:
        return lambda target: require_agent_token(target, permission=permission)
    @wraps(view)
    def wrapped(*args, **kwargs):
        from .agent_tokens import authenticate
        if current_app.config['APP_ENV'] != 'development' and not request.is_secure:
            return error('HTTPS required', 403)
        scheme, _, raw = request.headers.get('Authorization', '').partition(' ')
        authenticated = authenticate(raw, permission) if scheme.lower() == 'bearer' else None
        if authenticated is None:
            response, status = error('Authentication required', 401)
            response.headers['WWW-Authenticate'] = 'Bearer realm="NextMove agents"'
            return response, status
        g.agent_token, g.user_id = authenticated
        g.auth_type = 'agent'
        return view(*args, **kwargs)
    return wrapped


def require_resume_upload_access(view):
    """Append attachments with ingestion authority; keep all read routes private."""
    existing_access = require_auth(view)
    managed_access = require_agent_token(view, permission='roles:ingest')

    @wraps(view)
    def wrapped(*args, **kwargs):
        scheme, _, raw = request.headers.get('Authorization', '').partition(' ')
        if scheme.lower() == 'bearer' and raw.startswith('nm_ingest_'):
            return managed_access(*args, **kwargs)
        return existing_access(*args, **kwargs)
    return wrapped


def same_origin():
    origin = request.headers.get('Origin')
    return not origin or origin.rstrip('/') == request.host_url.rstrip('/')


def signup_settings():
    return {'enabled': bool(current_app.config.get('SIGNUP_ENABLED', True)),
            'invite_required': bool(current_app.config.get('SIGNUP_REQUIRES_INVITE', True))}


def signed_in(status=200):
    return jsonify(authenticated=True, csrf_token=session['csrf_token'], user=user_summary(g.credential)), status


@auth.get('/auth/session')
def session_status():
    try:
        valid = browser_authenticated()
    except credentials.Unavailable:
        return error('Sign-in temporarily unavailable; try again later', 503)
    return jsonify(authenticated=valid, csrf_token=session.get('csrf_token') if valid else None,
                   user=user_summary(g.credential) if valid else None, signup=signup_settings())


@auth.post('/auth/signup')
def signup():
    if not same_origin():
        return error('Invalid request origin', 403)
    settings = signup_settings()
    if not settings['enabled']:
        return error('Sign-up is closed', 403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error('A JSON object is required', 400)
    email = credentials.normalize_email(payload.get('email'))
    if email is None:
        return error('Enter a valid email address', 400, 'email')
    password = payload.get('password')
    problem = credentials.password_problem(password)
    if problem:
        return error(problem, 400, 'password')
    if payload.get('password_confirmation') != password:
        return error('Passwords do not match', 400, 'password_confirmation')
    invite = payload.get('invite_code')
    if settings['invite_required']:
        if not isinstance(invite, str) or not 8 <= len(invite.strip()) <= 128:
            return error('Enter the invite code you were given', 403, 'invite_code')
    else:
        invite = None
    rejection = throttled(throttle.reserve_signup)
    if rejection:
        return rejection
    try:
        g.credential = credentials.create_user(email, generate_password_hash(password), invite)
    except credentials.SignupError as exc:
        return error(str(exc), exc.status, exc.field)
    except credentials.Unavailable:
        return error('Sign-up temporarily unavailable; try again later', 503)
    issue_session(g.credential)
    return signed_in(201)


@auth.post('/auth/login')
def login():
    if not same_origin():
        return error('Invalid request origin', 403)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    email = credentials.normalize_email(payload.get('email'))
    # Reserve before validation and hashing: unknown and malformed attempts count.
    rejection = throttled(lambda: throttle.reserve_login(email))
    if rejection:
        return rejection
    password = payload.get('password')
    if email is None or not isinstance(password, str) or not 1 <= len(password) <= 128:
        return error('Invalid email or password', 401)
    try:
        credential = credentials.find(email)
    except credentials.Unavailable:
        return error('Sign-in temporarily unavailable; try again later', 503)
    # Unknown emails still pay for one hash so response time does not reveal accounts.
    verified = check_password_hash(credential.password_hash if credential else credentials.dummy_hash(), password)
    if credential is None or not verified:
        return error('Invalid email or password', 401)
    # Bind to the generation observed BEFORE hashing. A concurrent password
    # reset must not give this older verification the new session authority.
    issue_session(credential)
    g.credential = credential
    return signed_in()


@auth.post('/auth/password')
@require_auth
def change_password():
    if g.auth_type != 'session':
        return error('Browser sign-in required to change the password', 403)
    payload = request.get_json(silent=True)
    required = {'current_password', 'new_password', 'confirm_password'}
    if not isinstance(payload, dict) or set(payload) != required:
        return error('Provide current password, new password, and confirmation', 400)
    old, new, confirmation = (payload[key] for key in ('current_password', 'new_password', 'confirm_password'))
    if not isinstance(old, str) or not 1 <= len(old) <= 128:
        return error('Your current password is incorrect', 400)
    problem = credentials.password_problem(new)
    if problem:
        return error(problem, 400)
    if not isinstance(confirmation, str) or new != confirmation:
        return error('New passwords do not match', 400)
    observed = g.credential
    rejection = throttled(lambda: throttle.reserve_verification(observed.user_id))
    if rejection:
        return rejection
    if not check_password_hash(observed.password_hash, old):
        return error('Your current password is incorrect', 400)
    try:
        changed = credentials.replace(observed, generate_password_hash(new))
    except credentials.Unavailable:
        return error('Password update temporarily unavailable; try again later', 503)
    if not changed:
        session.clear()
        return error('Your sign-in changed. Sign in again before updating the password', 409)
    issue_session(credentials.Credential(observed.user_id, observed.email, '', observed.session_version + 1))
    return jsonify(authenticated=True, csrf_token=session['csrf_token'],
                   message='Password updated. Other sessions have been signed out.')


@auth.post('/auth/logout')
@require_auth
def logout():
    session.clear()
    return jsonify(authenticated=False, csrf_token=None)


@auth.get('/auth/activation')
def activation_status():
    try:
        return jsonify(valid=credentials.activation_valid(request.args.get('token', '')))
    except credentials.Unavailable:
        return error('Activation temporarily unavailable; try again later', 503)


@auth.post('/auth/activate')
def activate():
    """Set the email and password of an account created from chat, then sign it in."""
    if not same_origin():
        return error('Invalid request origin', 403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error('A JSON object is required', 400)
    email = credentials.normalize_email(payload.get('email'))
    if email is None:
        return error('Enter a valid email address', 400, 'email')
    password = payload.get('password')
    problem = credentials.password_problem(password)
    if problem:
        return error(problem, 400, 'password')
    if payload.get('password_confirmation') != password:
        return error('Passwords do not match', 400, 'password_confirmation')
    # Shares the signup budget: each guess at a link costs the same as a signup attempt.
    rejection = throttled(throttle.reserve_signup)
    if rejection:
        return rejection
    try:
        g.credential = credentials.activate(payload.get('token'), email, generate_password_hash(password))
    except credentials.SignupError as exc:
        return error(str(exc), exc.status, exc.field)
    except credentials.Unavailable:
        return error('Activation temporarily unavailable; try again later', 503)
    issue_session(g.credential)
    return signed_in()
