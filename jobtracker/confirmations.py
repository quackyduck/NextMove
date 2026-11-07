"""Email confirmation (schema 15): an account can only be used once its email is verified.

Three kinds of `email_confirmations` row:

* ``signup``: a sign-up that is not an account yet. It holds the chosen
  password hash; confirming creates the user, already verified.
* ``activation``: a messaging account's chosen email and password. The account
  stays messaging-only (no email) until the link is confirmed.
* ``existing``: verifying an account that existed before verification was
  required (it signs in with the right password and gets a link).

Request paths never look the address up in `users`: sign-up, activation and
resend do the same work and give the same 202 for every address. The worker
decides at send time: an address that already has an account gets an "account
exists" notice (no token) and the row is deleted; otherwise it mints the token
(only a SHA-256 persists) and sends the link `/verify-email#token=...`.

Confirming needs the token (the mailbox) and proof of the password: the binding
cookie set in the browser that chose it, or typing it again (5 tries per row).
A victim who opens a link from someone else's sign-up therefore cannot create,
activate or verify an account that someone else controls. Confirming signs the
browser in; checking a link never consumes it.
"""
from datetime import timedelta, timezone
import hashlib
import secrets

from flask import Blueprint, current_app, g, jsonify, make_response, request, session
from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash

from . import credentials, mailer, pow, taskqueue, throttle
from .models import ActivationToken, EmailConfirmation, InviteCode, User, new_id, now

confirmations = Blueprint('confirmations', __name__)

LINK_HOURS = 24
STALE_HOURS = 24
# Hard cap on any confirmation row's life (pending sign-ups hold a password hash): resends never extend it.
MAX_PENDING_HOURS = 48
COOKIE = 'next_move_confirm'
COOKIE_MAX_AGE = 48 * 3600  # Matches the queue's 48 h max_deferral: a deferred link still opens in one click.
MAX_PASSWORD_ATTEMPTS = 5
EXISTING_COOLDOWN_SECONDS = 60
EXISTING_DAILY = 5
GENERIC_GONE = 'This link is invalid, expired, or already used.'
RESEND_MESSAGE = "If that address is waiting for confirmation, we've sent a new link."


class ConfirmationError(Exception):
    def __init__(self, message, status, code=None, field=None, **extra):
        super().__init__(message)
        self.status, self.code, self.field, self.extra = status, code, field, extra


def _engine():
    return current_app.extensions['jobtracker_db']['engine']


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None


def _begin(connection):
    if connection.dialect.name == 'sqlite':
        connection.exec_driver_sql('BEGIN IMMEDIATE')


# Binding cookie ---------------------------------------------------------------
def new_binding():
    """(raw cookie value, its digest) for a fresh confirmation started in this browser."""
    raw = secrets.token_urlsafe(32)
    return raw, _digest(raw)


def set_binding(response, raw):
    response.set_cookie(COOKIE, raw, max_age=COOKIE_MAX_AGE, httponly=True, samesite='Lax',
                        secure=current_app.config['SESSION_COOKIE_SECURE'],
                        path=current_app.config.get('SESSION_COOKIE_PATH') or '/')
    return response


def clear_binding(response):
    response.delete_cookie(COOKIE, path=current_app.config.get('SESSION_COOKIE_PATH') or '/',
                           secure=current_app.config['SESSION_COOKIE_SECURE'], httponly=True, samesite='Lax')
    return response


def _browser_digest():
    raw = request.cookies.get(COOKIE, '')
    return _digest(raw) if isinstance(raw, str) and 16 <= len(raw) <= 128 else None


# Starting confirmations (request paths) -------------------------------------
def _supersede(connection, email, browser_hash, kinds, user_id=None):
    """This browser's earlier live rows for the address (or the account's activation rows) stop working."""
    conditions = [EmailConfirmation.used_at.is_(None), EmailConfirmation.kind.in_(kinds)]
    if user_id is not None:
        conditions.append(EmailConfirmation.user_id == user_id)
    else:
        conditions += [EmailConfirmation.email == email, EmailConfirmation.browser_hash == browser_hash]
    connection.execute(update(EmailConfirmation).where(*conditions).values(used_at=now()))


def _insert(connection, **values):
    identifier = new_id()
    connection.execute(insert(EmailConfirmation).values(
        id=identifier, password_attempts=0, created_at=now(), delivery='queued', **values))
    return identifier


def start_signup(email, password_hash, invite_id, browser_hash):
    """Store a pending sign-up and queue its email; identical work for every address (no users lookup).

    This browser's earlier pending sign-ups for the address (found by the binding cookie it already
    holds) stop working: signing up again replaces the password and the link.
    """
    previous = _browser_digest()
    with _engine().begin() as connection:
        _begin(connection)
        cleanup(connection)
        if previous is not None:
            _supersede(connection, email, previous, ('signup',))
        identifier = _insert(connection, kind='signup', email=email, password_hash=password_hash,
                             invite_id=invite_id, browser_hash=browser_hash)
        taskqueue.enqueue(connection, None, 'send_confirmation', {'confirmation_id': identifier})
    return identifier


def start_activation(token, email, password_hash, browser_hash, allow_send=None):
    """Consume a messaging account's activation link and queue confirming the chosen email.

    The account keeps no email until the link is confirmed. Raises ConfirmationError(410) for a bad link.
    `allow_send()` is the per-address budget, checked only once the link is known to be valid (so bad
    links never spend an address's budget). Over budget the link is still used up and nothing is
    stored or sent; the caller answers with the same 202. Returns the row id, or None.
    """
    digest = credentials._activation_digest(token)
    gone = ConfirmationError('This activation link is invalid, expired, or already used. Ask for a new one in chat.',
                             410)
    if digest is None:
        raise gone
    with _engine().begin() as connection:
        _begin(connection)
        link = connection.execute(select(ActivationToken.id, ActivationToken.user_id).join(
            User, User.id == ActivationToken.user_id).where(
            ActivationToken.token_hash == digest, ActivationToken.used_at.is_(None),
            ActivationToken.expires_at > now(), User.password_hash.is_(None))).first()
        if link is None:
            raise gone
        consumed = connection.execute(update(ActivationToken).where(
            ActivationToken.id == link.id, ActivationToken.used_at.is_(None)).values(used_at=now())).rowcount
        if consumed != 1:
            raise gone
        if allow_send is not None and not allow_send():
            return None
        cleanup(connection)
        _supersede(connection, email, browser_hash, ('activation',), user_id=link.user_id)
        identifier = _insert(connection, kind='activation', email=email, user_id=link.user_id,
                             password_hash=password_hash, browser_hash=browser_hash)
        taskqueue.enqueue(connection, link.user_id, 'send_confirmation', {'confirmation_id': identifier})
    return identifier


def start_existing(credential, browser_hash=None):
    """Queue a verification link for a legacy (unverified) account. False when rate-limited (60 s, 5 a day)."""
    with _engine().begin() as connection:
        _begin(connection)
        return _start_existing(connection, credential.user_id, credential.email, browser_hash) is not None


def _start_existing(connection, user_id, email, browser_hash):
    recent = connection.execute(select(EmailConfirmation.created_at).where(
        EmailConfirmation.user_id == user_id, EmailConfirmation.kind == 'existing',
        EmailConfirmation.created_at > now() - timedelta(days=1))).scalars().all()
    if len(recent) >= EXISTING_DAILY or any(
            _utc(at) > now() - timedelta(seconds=EXISTING_COOLDOWN_SECONDS) for at in recent):
        return None
    connection.execute(update(EmailConfirmation).where(
        EmailConfirmation.user_id == user_id, EmailConfirmation.kind == 'existing',
        EmailConfirmation.used_at.is_(None)).values(used_at=now()))
    identifier = _insert(connection, kind='existing', email=email, user_id=user_id, browser_hash=browser_hash)
    taskqueue.enqueue(connection, user_id, 'send_confirmation', {'confirmation_id': identifier})
    return identifier


def queue_resend(email):
    """Resend request path: queue the lookup for the worker (anonymous; the payload is deleted on success)."""
    with _engine().begin() as connection:
        taskqueue.enqueue(connection, None, 'resend_confirmation', {'email': email})


def cleanup(connection):
    """Delete rows that can no longer be used: unsent after 24 h, expired or used over an hour ago, and
    anything created more than MAX_PENDING_HOURS ago (so a pending password hash lives about 2 days at most)."""
    moment = now()
    connection.execute(delete(EmailConfirmation).where(or_(
        EmailConfirmation.created_at < moment - timedelta(hours=MAX_PENDING_HOURS),
        (EmailConfirmation.sent_at.is_(None)) & (EmailConfirmation.created_at < moment - timedelta(hours=STALE_HOURS)),
        EmailConfirmation.expires_at < moment - timedelta(hours=1),
        EmailConfirmation.used_at < moment - timedelta(hours=1))))


def prune(engine):
    """Hourly worker maintenance: delete dead confirmation rows and mail_usage rows older than 400 days."""
    from .models import MailUsage
    with engine.begin() as connection:
        cleanup(connection)
        connection.execute(delete(MailUsage).where(MailUsage.day < (now() - timedelta(days=400)).date()))


# Email templates (one fixed family; only the link and the masked address are interpolated). The plain
# text is built here; the branded HTML part comes from mailer.render (templates/email/, email_content.py).
def _message(kind, link=None, email=''):
    base = mailer.public_url('/')
    signin, forgot = base + '?signin', base + '?forgot'
    if kind == 'signup':
        subject = 'Confirm your email for My Next Moov'
        lines = ['Someone, hopefully you, asked to create a My Next Moov account with this address.',
                 '', f'Confirm it: {link}', '',
                 'The link works once and expires in 24 hours. If this wasn\'t you, ignore this email; '
                 'no account is created.']
    elif kind == 'activation':
        subject = 'Confirm your email to open your My Next Moov dashboard'
        lines = ['Someone, hopefully you, asked to use this address for a My Next Moov account created in chat.',
                 '', f'Confirm it: {link}', '',
                 'The link works once and expires in 24 hours. If this wasn\'t you, ignore this email; '
                 'nothing changes.']
    elif kind == 'existing':
        subject = 'Confirm your email to keep using My Next Moov'
        lines = ['Someone, hopefully you, signed in to My Next Moov and needs to confirm this address.',
                 '', f'Confirm it: {link}', '',
                 'The link works once and expires in 24 hours. If this wasn\'t you, ignore this email and '
                 f'consider resetting your password: {forgot}']
    elif kind == 'account_exists':
        subject = 'You already have a My Next Moov account'
        lines = ['Someone, hopefully you, tried to sign up with this address, but it already has an account.',
                 '', f'Sign in: {signin}', f'Forgot your password? {forgot}', '',
                 'If this wasn\'t you, you can ignore this email.']
    else:  # activation_exists
        subject = 'This address already has a My Next Moov account'
        lines = ['Someone, hopefully you, asked to use this address for a My Next Moov account created in chat,',
                 'but it already belongs to another account. To use that account from chat, contact support',
                 'and ask to link your phone.', '', 'If this wasn\'t you, you can ignore this email.']
    text = '\n'.join(lines) + '\n'
    return mailer.render(kind, subject=subject, text=text, email=email, link=link)


# Worker -------------------------------------------------------------------------
def _set_delivery(engine, identifier, state, reason=None):
    with engine.begin() as connection:
        connection.execute(update(EmailConfirmation).where(EmailConfirmation.id == identifier).values(
            delivery=state, skip_reason=reason))


def _give_up(engine, _user_id, payload):
    if payload.get('confirmation_id'):
        _set_delivery(engine, payload['confirmation_id'], 'failed')


def deliver(identifier):
    """Send (or skip) one confirmation row. Raises taskqueue.Retry for transient problems."""
    engine = _engine()
    with engine.connect() as connection:
        row = connection.execute(select(EmailConfirmation.__table__).where(
            EmailConfirmation.id == identifier)).mappings().first()
        if row is None or row['used_at'] is not None:
            return 'gone'
        created = _utc(row['created_at'])
        if (row['sent_at'] is None and created < now() - timedelta(hours=STALE_HOURS)) or \
                created < now() - timedelta(hours=MAX_PENDING_HOURS):
            _set_delivery(engine, identifier, 'skipped', 'stale')
            return 'stale'
        owner = connection.execute(select(User.id, User.email, User.email_verified_at).where(
            User.email == row['email'])).first()
        account = (connection.execute(select(User.email, User.email_verified_at, User.password_hash).where(
            User.id == row['user_id'])).first() if row['user_id'] is not None else None)
    kind = row['kind']
    if kind == 'existing' and (account is None or account.email != row['email'] or account.email_verified_at):
        _set_delivery(engine, identifier, 'skipped', 'stale')
        return 'stale'
    if kind == 'activation' and (account is None or account.password_hash is not None):
        _set_delivery(engine, identifier, 'skipped', 'stale')
        return 'stale'
    notice = kind in ('signup', 'activation') and owner is not None
    try:
        recipient = mailer.recipient_ok(row['email'], 'signup')
    except mailer.TransientMailError as exc:
        raise taskqueue.Retry('DNS lookup failed', delay=60) from exc
    if recipient.status != 'ok':
        _set_delivery(engine, identifier, 'skipped', recipient.status)
        return recipient.status
    mail_kind = 'existing' if kind == 'existing' else None
    if not mailer.has_room('signup', mail_kind):
        raise taskqueue.Retry('Mail quota spent', delay=3600, count_attempt=False)
    if notice:
        subject, text, html = _message('account_exists' if kind == 'signup' else 'activation_exists',
                                       email=row['email'])
        try:
            mailer.send_email(row['email'], subject, text, html, category='signup', recipient=recipient)
        except mailer.MailQuotaExceeded as exc:
            raise taskqueue.Retry('Mail quota spent', delay=3600, count_attempt=False) from exc
        except mailer.TransientMailError as exc:
            raise taskqueue.Retry('Mail provider unavailable', delay=60) from exc
        except mailer.PermanentMailError:
            pass
        with engine.begin() as connection:  # The address has an account: forget the pending row (and its hash).
            connection.execute(delete(EmailConfirmation).where(EmailConfirmation.id == identifier))
        return 'account_exists'
    raw = secrets.token_urlsafe(32)
    with engine.begin() as connection:
        minted = connection.execute(update(EmailConfirmation).where(
            EmailConfirmation.id == identifier, EmailConfirmation.used_at.is_(None)).values(
            token_hash=_digest(raw), sent_at=now(), expires_at=now() + timedelta(hours=LINK_HOURS))).rowcount
    if not minted:
        return 'gone'
    subject, text, html = _message(kind, mailer.public_url('/verify-email', {'token': raw}), email=row['email'])
    try:
        mailer.send_email(row['email'], subject, text, html, category='signup', kind=mail_kind, recipient=recipient)
    except mailer.MailQuotaExceeded as exc:
        raise taskqueue.Retry('Mail quota spent', delay=3600, count_attempt=False) from exc
    except mailer.TransientMailError as exc:
        raise taskqueue.Retry('Mail provider unavailable', delay=60) from exc
    except mailer.PermanentMailError:
        _set_delivery(engine, identifier, 'failed')
        return 'failed'
    _set_delivery(engine, identifier, 'sent')
    return 'sent'


@taskqueue.handler('send_confirmation', mail=True, anonymous=True, max_attempts=5, lease_seconds=60, give_up=_give_up)
def run_send(app, task):
    deliver(task.payload['confirmation_id'])


@taskqueue.handler('resend_confirmation', mail=True, anonymous=True, delete_on_success=True, max_attempts=5, lease_seconds=60)
def run_resend(app, task):
    """Find what is waiting for this address and send it again; send nothing to anyone else."""
    email = task.payload.get('email')
    engine = _engine()
    with engine.begin() as connection:
        _begin(connection)
        pending = connection.execute(select(EmailConfirmation.id).where(
            EmailConfirmation.email == email, EmailConfirmation.kind.in_(('signup', 'activation')),
            EmailConfirmation.used_at.is_(None),
            EmailConfirmation.created_at > now() - timedelta(hours=MAX_PENDING_HOURS)
        ).order_by(EmailConfirmation.created_at.desc()).limit(1)).scalar()
        target = None
        if pending is not None:
            # Re-mint: the newest pending row gets a fresh link (its browser binding is kept). Its creation
            # time is kept, so resending never extends how long the pending password hash is stored.
            connection.execute(update(EmailConfirmation).where(EmailConfirmation.id == pending).values(
                token_hash=None, expires_at=None, delivery='queued', skip_reason=None))
            target = pending
        else:
            user = connection.execute(select(User.id, User.email, User.password_hash, User.email_verified_at).where(
                User.email == email)).first()
            if user is not None and credentials.account_state(user) == 'legacy_unverified':
                live = connection.scalar(select(EmailConfirmation.id).where(
                    EmailConfirmation.user_id == user.id, EmailConfirmation.kind == 'existing',
                    EmailConfirmation.used_at.is_(None)))
                if live is not None:
                    connection.execute(update(EmailConfirmation).where(EmailConfirmation.id == live).values(
                        token_hash=None, expires_at=None, delivery='queued', skip_reason=None))
                    target = live
                else:
                    recent = connection.scalar(select(EmailConfirmation.id).where(
                        EmailConfirmation.user_id == user.id, EmailConfirmation.kind == 'existing',
                        EmailConfirmation.created_at > now() - timedelta(seconds=EXISTING_COOLDOWN_SECONDS)))
                    if recent is None:
                        target = _insert(connection, kind='existing', email=user.email, user_id=user.id,
                                         browser_hash=None)
    if target is not None:
        deliver(target)


# Confirming (web) ---------------------------------------------------------------
def _find(token):
    digest = _digest(token) if isinstance(token, str) and 16 <= len(token) <= 128 else None
    if digest is None:
        return None
    with _engine().connect() as connection:
        row = connection.execute(select(EmailConfirmation.__table__).where(
            EmailConfirmation.token_hash == digest, EmailConfirmation.used_at.is_(None),
            EmailConfirmation.expires_at > now())).mappings().first()
    if row is None or row['password_attempts'] >= MAX_PASSWORD_ATTEMPTS:
        return None
    return row


def _session_user():
    from .auth import browser_authenticated
    try:
        return g.credential.user_id if browser_authenticated() else None
    except credentials.Unavailable:
        return None


def check(token):
    row = _find(token)
    if row is None:
        return {'valid': False}
    signed_in = _session_user()
    return {'valid': True, 'kind': row['kind'], 'email_hint': mailer.mask_address(row['email']),
            'needs_password': _browser_digest() != row['browser_hash'] or row['browser_hash'] is None,
            'signed_in_as_other': signed_in is not None and signed_in != row['user_id']}


def confirm(token, password):
    """Consume a confirmation; returns the account's user id. Raises ConfirmationError."""
    row = _find(token)
    if row is None:
        raise ConfirmationError(GENERIC_GONE, 410)
    bound = row['browser_hash'] is not None and _browser_digest() == row['browser_hash']
    observed_hash = None
    if row['kind'] == 'existing':
        with _engine().connect() as connection:
            observed_hash = connection.scalar(select(User.password_hash).where(User.id == row['user_id']))
    if not bound:
        if not isinstance(password, str) or not password:
            raise ConfirmationError('Enter the password you chose to confirm.', 400, 'password_required', 'password')
        if not 1 <= len(password) <= 128:
            password = ''
        if not throttle.reserve([(throttle.key('confirm-pw-ip', throttle.client_address()), 10)]):
            raise ConfirmationError('Too many attempts; try again in five minutes', 429)
        # Count the attempt first, atomically, and only then hash: concurrent guesses can never get
        # more than MAX_PASSWORD_ATTEMPTS evaluations between them, whatever their timing.
        with _engine().begin() as connection:
            counted = connection.execute(update(EmailConfirmation).where(
                EmailConfirmation.id == row['id'], EmailConfirmation.used_at.is_(None),
                EmailConfirmation.password_attempts < MAX_PASSWORD_ATTEMPTS).values(
                password_attempts=EmailConfirmation.password_attempts + 1)).rowcount
        if counted != 1:
            raise ConfirmationError(GENERIC_GONE, 410)
        stored = row['password_hash'] if row['kind'] != 'existing' else observed_hash
        if not stored or not check_password_hash(stored, password):
            raise ConfirmationError('That password is not the one chosen for this link.', 400, 'wrong_password',
                                    'password')
    try:
        with _engine().begin() as connection:
            _begin(connection)
            # <= : the attempt that just proved the password was counted too (it may be the fifth).
            live = connection.execute(select(EmailConfirmation.__table__).where(
                EmailConfirmation.id == row['id'], EmailConfirmation.token_hash == row['token_hash'],
                EmailConfirmation.used_at.is_(None), EmailConfirmation.expires_at > now(),
                EmailConfirmation.password_attempts <= MAX_PASSWORD_ATTEMPTS)).mappings().first()
            if live is None:
                raise ConfirmationError(GENERIC_GONE, 410)
            user_id = _apply(connection, live, observed_hash)
    except IntegrityError:
        _forget_address(row['email'])
        raise ConfirmationError('This address already has an account. Sign in instead.', 409,
                                'account_exists') from None
    except SQLAlchemyError:
        raise ConfirmationError('Confirmation temporarily unavailable; try again later', 503) from None
    return user_id


def _forget_address(email):
    with _engine().begin() as connection:
        connection.execute(delete(EmailConfirmation).where(
            EmailConfirmation.email == email, EmailConfirmation.kind.in_(('signup', 'activation'))))


def _apply(connection, row, observed_hash):
    kind, email = row['kind'], row['email']
    if kind == 'signup':
        if connection.scalar(select(User.id).where(User.email == email)) is not None:
            raise IntegrityError('email taken', None, Exception())
        user_id = connection.execute(insert(User).values(
            email=email, password_hash=row['password_hash'], session_version=1, created_at=now(),
            email_verified_at=now())).inserted_primary_key[0]
        if row['invite_id'] is not None:
            consumed = connection.execute(update(InviteCode).where(
                InviteCode.id == row['invite_id'], InviteCode.used_at.is_(None)).values(
                used_at=now(), used_by=user_id)).rowcount
            if consumed != 1:
                raise ConfirmationError('This invite code was used in the meantime. Ask for a new one.', 403,
                                        'invite_used')
        from .onboarding import start as start_onboarding
        start_onboarding(connection, user_id)
        credentials.audit.info('user verified user_id=%d via=%s', user_id, 'link')
    elif kind == 'activation':
        user_id = row['user_id']
        if connection.scalar(select(User.id).where(User.email == email, User.id != user_id)) is not None:
            raise IntegrityError('email taken', None, Exception())
        activated = connection.execute(update(User).where(
            User.id == user_id, User.email.is_(None), User.password_hash.is_(None)).values(
            email=email, password_hash=row['password_hash'], email_verified_at=now(),
            session_version=User.session_version + 1)).rowcount
        if activated != 1:
            raise ConfirmationError(GENERIC_GONE, 410)
        credentials.audit.info('user verified user_id=%d via=%s', user_id, 'link')
    else:
        user_id = row['user_id']
        if observed_hash is not None and connection.scalar(
                select(User.password_hash).where(User.id == user_id)) != observed_hash:
            raise ConfirmationError(GENERIC_GONE, 410)  # The password changed after this link was checked.
        if not credentials.mark_verified(connection, user_id, email, via='link'):
            raise ConfirmationError(GENERIC_GONE, 410)
    connection.execute(delete(EmailConfirmation).where(or_(
        EmailConfirmation.user_id == user_id,
        (EmailConfirmation.email == email) & EmailConfirmation.kind.in_(('signup', 'activation')))))
    return user_id


# Routes -------------------------------------------------------------------------
def _error(exc):
    body = {'error': str(exc)}
    if exc.code:
        body['code'] = exc.code
    if exc.field:
        body['field'] = exc.field
    body.update(exc.extra)
    return jsonify(body), exc.status


def _rejected(message, status, code=None):
    body = {'error': message}
    if code:
        body['code'] = code
    response = jsonify(body)
    response.status_code = status
    if status == 429:
        response.headers['Retry-After'] = str(throttle.WINDOW_SECONDS)
    return response


def guard_mail_request(purpose, proof):
    """Shared checks for unauthenticated requests that send mail: proof-of-work first, then nothing else here.

    Returns a response to send back, or None. (Throttles come after, in each route.)
    """
    try:
        if not pow.verify(purpose, proof):
            return _rejected('Your request expired. Please try again.', 400, 'proof_invalid')
    except throttle.Unavailable:
        return _rejected('Temporarily unavailable; try again later', 503)
    return None


@confirmations.get('/auth/challenge')
def challenge():
    purpose = request.args.get('purpose', '')
    if purpose not in pow.PURPOSES:
        return _rejected('Unknown purpose', 400)
    try:
        if not throttle.reserve([(throttle.key('challenge-ip', throttle.client_address()), 30)]):
            return _rejected('Too many requests from your network. Try again later.', 429)
    except throttle.Unavailable:
        return _rejected('Temporarily unavailable; try again later', 503)
    return jsonify(pow.issue(purpose))


def _confirm_budget():
    try:
        return throttle.reserve([(throttle.key('confirm-ip', throttle.client_address()), 30)])
    except throttle.Unavailable:
        return None


@confirmations.post('/auth/email-verification/check')
def check_route():
    from .auth import same_origin
    if not same_origin():
        return _rejected('Invalid request origin', 403)
    allowed = _confirm_budget()
    if allowed is None:
        return _rejected('Temporarily unavailable; try again later', 503)
    if not allowed:
        return _rejected('Too many attempts; try again in five minutes', 429)
    payload = request.get_json(silent=True)
    token = payload.get('token') if isinstance(payload, dict) else None
    try:
        return jsonify(check(token))
    except SQLAlchemyError:
        return _rejected('Confirmation temporarily unavailable; try again later', 503)


@confirmations.post('/auth/email-verification/confirm')
def confirm_route():
    from .auth import issue_session, same_origin, user_summary
    if not same_origin():
        return _rejected('Invalid request origin', 403)
    allowed = _confirm_budget()
    if allowed is None:
        return _rejected('Temporarily unavailable; try again later', 503)
    if not allowed:
        return _rejected('Too many attempts; try again in five minutes', 429)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    try:
        user_id = confirm(payload.get('token'), payload.get('password'))
    except ConfirmationError as exc:
        return _error(exc)
    except throttle.Unavailable:
        return _rejected('Temporarily unavailable; try again later', 503)
    credential = credentials.read(user_id)
    issue_session(credential)
    g.credential = credential
    response = make_response(jsonify(authenticated=True, csrf_token=session['csrf_token'],
                                     user=user_summary(credential)))
    return clear_binding(response)


@confirmations.post('/auth/email-verification/resend')
def resend_route():
    from .auth import same_origin
    if not same_origin():
        return _rejected('Invalid request origin', 403)
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    rejection = guard_mail_request('resend', payload.get('proof'))
    if rejection is not None:
        return rejection
    try:
        if not throttle.reserve_mail_trigger():
            return _rejected('Too many requests from your network. Try again later.', 429)
        email = credentials.normalize_new_email(payload.get('email'))
        # Per address, silently: the response is the same whether or not anything is sent.
        if email is not None and mailer.enabled() and throttle.reserve_per_address('confirm-addr', email):
            queue_resend(email)
    except throttle.Unavailable:
        return _rejected('Temporarily unavailable; try again later', 503)
    except SQLAlchemyError:
        return _rejected('Temporarily unavailable; try again later', 503)
    return jsonify(message=RESEND_MESSAGE), 202
