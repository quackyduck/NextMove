"""Per-user browser sessions, signup, password changes and agent authentication.

Every authenticated request sets `g.user_id`; services scope all reads and
writes by it. Browser sessions carry the user id and that account's session
generation, so a password change signs out only that account's other sessions.

Schema 15: a browser session exists only for a verified account. Sign-up and
activation return 202 and create no session; the emailed link signs in (see
confirmations.py). A legacy account that signs in with the right password but
has not verified gets 403 verification_required and a link.
"""
from datetime import timezone
from functools import wraps
import hmac
import logging
import secrets
import time

from flask import Blueprint, current_app, g, jsonify, make_response, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from . import credentials, throttle

auth = Blueprint('auth', __name__)
log = logging.getLogger('jobtracker.auth')
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
    # is_admin is only ever the caller's own flag (schema 18): it shows the Admin link.
    return {'id': credential.user_id, 'email': credential.email, 'created_at': created,
            'is_admin': bool(getattr(credential, 'is_admin', False))}


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
    if not credential.verified:
        session.clear()  # Schema 15: unverified (legacy) accounts sign in again and verify first.
        return False
    g.credential = credential
    g.user_id = credential.user_id
    if request.method not in ('HEAD', 'OPTIONS'):
        touch_last_seen(credential)
    return True


def touch_last_seen(credential):
    """Schema 18: record the hour of this browser request, at most one write per account per hour.

    Only browser_authenticated calls this (never bearer, OAuth, agent-token, harness or link requests). A conditional
    UPDATE on its own short connection, with a short busy timeout; any database error is skipped, never retried,
    and never fails the request.
    """
    from datetime import datetime
    from sqlalchemy import or_, update
    from sqlalchemy.exc import SQLAlchemyError
    from .models import User
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    seen = credential.last_seen_at
    if seen is not None and seen.replace(tzinfo=seen.tzinfo or timezone.utc) >= hour:
        return
    try:
        with credentials.engine().begin() as connection:
            sqlite = connection.dialect.name == 'sqlite'
            if sqlite:
                connection.exec_driver_sql('PRAGMA busy_timeout=250')
            try:
                connection.execute(update(User).where(
                    User.id == credential.user_id,
                    or_(User.last_seen_at.is_(None), User.last_seen_at < hour)).values(last_seen_at=hour))
            finally:
                if sqlite:
                    connection.exec_driver_sql('PRAGMA busy_timeout=5000')
    except SQLAlchemyError:
        log.debug('last seen not recorded')


def issue_session(credential, *, password_verified=False):
    """A fresh session for the account. password_verified: this request just checked the account's password
    (login, password change). It records `password_at`, which the admin step-up reads (schema 18)."""
    session.clear()
    session['authenticated'] = True
    session['user_id'] = credential.user_id
    session['session_version'] = credential.session_version
    session['csrf_token'] = secrets.token_urlsafe(32)
    if password_verified:
        session['password_at'] = int(time.time())
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


def can_read_resumes():
    """Whether this request's credential may read resumes: not managed agent tokens, nor OAuth connections or
    upload links without resumes:read. Sessions, the legacy token (job resumes) and harness grants may."""
    if g.get('auth_type') == 'oauth':
        return 'resumes:read' in g.oauth.scopes
    if 'link_scopes' in g:
        return 'resumes:read' in g.link_scopes
    return g.get('auth_type') in ('session', 'bearer', 'harness')


def require_scope(scope, fallback=require_auth):
    """An OAuth access token carrying `scope`, or whatever `fallback` already accepts.

    OAuth tokens (nm_oat_) are recognized by prefix and never reach the fallback,
    so they cannot satisfy browser-only routes; other credentials keep their rules.
    """
    def decorator(view):
        from .oauth import ACCESS_PREFIX, require_oauth
        delegated, existing_access = require_oauth(scope)(view), fallback(view)

        @wraps(view)
        def wrapped(*args, **kwargs):
            scheme, _, raw = request.headers.get('Authorization', '').partition(' ')
            if scheme.lower() == 'bearer' and raw.startswith(ACCESS_PREFIX):
                return delegated(*args, **kwargs)
            return existing_access(*args, **kwargs)
        return wrapped
    return decorator


def same_origin():
    origin = request.headers.get('Origin')
    return not origin or origin.rstrip('/') == request.host_url.rstrip('/')


PAUSED_MESSAGE = 'Sign-up is paused for today. Try again tomorrow.'


def signup_settings(cached=True):
    from . import mailer
    enabled = bool(current_app.config.get('SIGNUP_ENABLED', True)) and mailer.enabled()
    paused = enabled and mailer.signup_paused(cached=cached)
    return {'enabled': enabled and not paused,
            'invite_required': bool(current_app.config.get('SIGNUP_REQUIRES_INVITE', True)),
            'paused': paused, 'paused_message': PAUSED_MESSAGE if paused else None}


def signed_in(status=200):
    return jsonify(authenticated=True, csrf_token=session['csrf_token'], user=user_summary(g.credential)), status


@auth.get('/auth/session')
def session_status():
    try:
        valid = browser_authenticated()
    except credentials.Unavailable:
        return error('Sign-in temporarily unavailable; try again later', 503)
    from . import mailer
    return jsonify(authenticated=valid, csrf_token=session.get('csrf_token') if valid else None,
                   user=user_summary(g.credential) if valid else None, signup=signup_settings(),
                   mail_status=mailer.status(cached=True), password_reset=_password_reset_settings())


def _password_reset_settings():
    from .password_reset import settings
    return settings()


CHECK_EMAIL = 'Check your email to finish creating your account.'
CHECK_EMAIL_ACTIVATION = 'Check your email to finish opening your dashboard.'


def _paused():
    return jsonify(error=PAUSED_MESSAGE, code='signup_paused'), 503


def _accepted(message, binding):
    from .confirmations import set_binding
    response = make_response(jsonify(message=message), 202)
    return set_binding(response, binding)


@auth.post('/auth/signup')
def signup():
    """Store a pending sign-up and email a link; the same 202 whether or not the address has an account."""
    from . import mailer
    from .confirmations import guard_mail_request, new_binding, start_signup
    if not same_origin():
        return error('Invalid request origin', 403)
    settings = signup_settings(cached=False)
    if not current_app.config.get('SIGNUP_ENABLED', True) or not mailer.enabled():
        return error('Sign-up is closed', 403)
    if settings['paused']:
        return _paused()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error('A JSON object is required', 400)
    rejection = guard_mail_request('signup', payload.get('proof'))
    if rejection is not None:
        return rejection
    email = credentials.normalize_new_email(payload.get('email'))
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
    # Hash for every request, before knowing anything about the address: same cost and timing.
    password_hash = generate_password_hash(password)
    try:
        invite_id = credentials.live_invite(invite) if invite is not None else None
        if invite is not None and invite_id is None:
            return error('This invite code is invalid, expired, or already used', 403, 'invite_code')
        raw, digest = new_binding()
        # Per address (canonical form), silently: over budget, nothing is stored or sent, same 202.
        if throttle.reserve_per_address('confirm-addr', email):
            start_signup(email, password_hash, invite_id, digest)
    except (credentials.Unavailable, throttle.Unavailable):
        return error('Sign-up temporarily unavailable; try again later', 503)
    except Exception as exc:  # noqa: BLE001 - storage errors must not leak details
        from sqlalchemy.exc import SQLAlchemyError
        if isinstance(exc, SQLAlchemyError):
            return error('Sign-up temporarily unavailable; try again later', 503)
        raise
    return _accepted(CHECK_EMAIL, raw)


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
    if not credential.verified:
        return verification_required(credential)
    # Bind to the generation observed BEFORE hashing. A concurrent password
    # reset must not give this older verification the new session authority.
    issue_session(credential, password_verified=True)
    g.credential = credential
    return signed_in()


def verification_required(credential):
    """A legacy account proved its password but not its email: no session; email a link (rate-limited)."""
    from . import mailer
    from .confirmations import new_binding, set_binding, start_existing
    if not mailer.enabled() or mailer.deliverable(credential.email) is None:
        return jsonify(error="This account's email can't receive mail. Contact support.",
                       code='verification_unavailable'), 403
    raw, digest = new_binding()
    try:
        queued = start_existing(credential, digest)
    except Exception as exc:  # noqa: BLE001
        from sqlalchemy.exc import SQLAlchemyError
        if isinstance(exc, SQLAlchemyError):
            return error('Sign-in temporarily unavailable; try again later', 503)
        raise
    hint = mailer.mask_address(credential.email)
    response = make_response(jsonify(
        error=f'Confirm your email to continue. We sent a link to {hint}.', code='verification_required',
        email_hint=hint), 403)
    # Within the resend cooldown nothing new was queued: keep the cookie that matches the earlier link.
    return set_binding(response, raw) if queued else response


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
    issue_session(credentials.Credential(observed.user_id, observed.email, '', observed.session_version + 1),
                  password_verified=True)
    from .password_reset import notify_password_changed
    notify_password_changed(observed.user_id)  # Password reset (schema 16): the "password changed" notice.
    return jsonify(authenticated=True, csrf_token=session['csrf_token'],
                   message='Password updated. Other sessions have been signed out.')


@auth.post('/auth/logout')
@require_auth
def logout():
    if g.auth_type == 'session' and g.credential.is_admin:
        # Schema 18: an administrator's sign-out also ends copies of this cookie (and their other browsers).
        from sqlalchemy import update
        from sqlalchemy.exc import SQLAlchemyError
        from .models import User
        try:
            with credentials.engine().begin() as connection:
                connection.execute(update(User).where(
                    User.id == g.credential.user_id, User.session_version == g.credential.session_version,
                ).values(session_version=User.session_version + 1))
        except SQLAlchemyError:
            log.warning('admin sign-out could not end other sessions user_id=%d', g.credential.user_id)
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
    """Choose the email and password of an account created from chat. The email is bound only once confirmed."""
    from . import mailer
    from .confirmations import ConfirmationError, guard_mail_request, new_binding, start_activation
    if not same_origin():
        return error('Invalid request origin', 403)
    if not mailer.enabled():
        return error('Activation is unavailable right now; keep using chat.', 503)
    if mailer.signup_paused():
        return _paused()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error('A JSON object is required', 400)
    rejection = guard_mail_request('activate', payload.get('proof'))
    if rejection is not None:
        return rejection
    email = credentials.normalize_new_email(payload.get('email'))
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
    password_hash = generate_password_hash(password)
    raw, digest = new_binding()
    try:
        # Per address (canonical form), silently, as for sign-up and resend: a chat user minting activation
        # links cannot mail an address more than the shared per-address budget allows.
        start_activation(payload.get('token'), email, password_hash, digest,
                         allow_send=lambda: throttle.reserve_per_address('confirm-addr', email))
    except throttle.Unavailable:
        return error('Activation temporarily unavailable; try again later', 503)
    except ConfirmationError as exc:
        return error(str(exc), exc.status)
    except Exception as exc:  # noqa: BLE001
        from sqlalchemy.exc import SQLAlchemyError
        if isinstance(exc, SQLAlchemyError):
            return error('Activation temporarily unavailable; try again later', 503)
        raise
    return _accepted(CHECK_EMAIL_ACTIVATION, raw)
