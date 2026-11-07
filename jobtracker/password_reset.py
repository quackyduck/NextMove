"""Self-service password reset (schema 16).

Request path (`POST /auth/password-reset/request`): same-origin, proof-of-work,
per-IP and per-address throttles, then one anonymous queue task holding only the
submitted address. It never reads `users`, so the work and the 202 are the same
for every address. The worker resolves the account (`account_state` must be
`verified` or `legacy_unverified`), checks deliverability, mints the token (only
its SHA-256 persists) and sends `/reset-password#token=...`. The fragment never
reaches a server; the SPA captures it in authLink.js before anything else runs.

A token is single use, expires after PASSWORD_RESET_MINUTES, and is bound to
the account's session generation and email: any password change, email change
or newer link kills it. Checking a link (`/status`) never consumes it.

Completing a reset (`/complete`) in one transaction: sets the password, bumps
the session generation (every browser session ends), revokes OAuth connections
and agent tokens through credentials.revoke_agents (and harness grants when the
account was still unverified: B1), marks an emailed reset's address verified,
and queues the "password changed" notice. It never signs the browser in.

Operators: `flask user reset-link` prints a link (never emails it, never
verifies), and `flask user reset-password` uses the same revocation rules.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
import secrets

import click
from flask import Blueprint, current_app, jsonify, request, session
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash

from . import credentials, mailer, taskqueue, throttle
from .models import PasswordResetRequest, PasswordResetToken, User, new_id, now

password_reset = Blueprint('password_reset', __name__)
audit = logging.getLogger('jobtracker.accounts')
log = logging.getLogger('jobtracker.password_reset')

TOKEN_PREFIX = 'nmr_'
ELIGIBLE = ('verified', 'legacy_unverified')
DEFAULT_MINUTES = 60
OPERATOR_MINUTES = (5, 1440)
DEDUPE_SECONDS = 60
TOKEN_RETENTION = timedelta(days=7)
REQUEST_RETENTION = timedelta(days=30)
# Throttles (keys are HMACs; IPv6 clients are grouped by /64 in throttle.client_address).
REQUEST_IP = ((5, throttle.WINDOW_SECONDS), (8, throttle.DAY_SECONDS))
# Per canonical address: (limit, clean_limit, window). Past `limit`, a client that has made fewer than
# ADDRESS_CLEAN_TRIES requests for the address in that window still gets through, up to `clean_limit`, so a
# stranger who spends an address's budget can't silently block its owner (the same rule as sign-up).
REQUEST_ADDRESS = ((3, 6, 3600), (5, 6, throttle.DAY_SECONDS))
ADDRESS_CLEAN_TRIES = 2
CHECK_IP = 30
COMPLETE_IP = 10

ACCEPTED = "If an account uses that email, we've sent a link to reset its password. It expires in 60 minutes."
INVALID = 'This link is invalid, expired or already used. Request a new one.'
UNAVAILABLE = 'Password reset is temporarily unavailable; try again later'
DONE = 'Password updated. Sign in with your new password.'


class _Conflict(Exception):
    """Rolls the reset transaction back: a concurrent change or submission won."""


class Rejected(Exception):
    def __init__(self, message, status, code=None):
        super().__init__(message)
        self.status, self.code = status, code


# Configuration -----------------------------------------------------------------
def init_app(app):
    minutes = app.config.setdefault('PASSWORD_RESET_MINUTES', int(os.getenv('PASSWORD_RESET_MINUTES', DEFAULT_MINUTES)))
    if not 15 <= minutes <= 60:
        raise RuntimeError('PASSWORD_RESET_MINUTES must be between 15 and 60')
    flag = app.config.setdefault('PASSWORD_RESET_ENABLED', os.getenv('PASSWORD_RESET_ENABLED'))
    app.config['PASSWORD_RESET_ENABLED'] = flag if isinstance(flag, bool) else str(flag or '1').strip() != '0'
    app.register_blueprint(password_reset, url_prefix='/api/v1')
    _register_cli(app)


def enabled():
    """Self-service reset is on: not switched off, mail is on, and links can be built.

    Links come from PUBLIC_BASE_URL (the mailer's MAIL_LINK_BASE falls back to the loopback dev server only for
    local backends), never from the Host header.
    """
    base = current_app.config.get('PUBLIC_BASE_URL') or current_app.config.get('MAIL_LINK_BASE')
    return bool(current_app.config.get('PASSWORD_RESET_ENABLED') and mailer.enabled() and base)


def settings():
    """For /auth/session: whether the UI should offer "Forgot password?"."""
    return {'enabled': enabled()}


def _engine():
    return current_app.extensions['jobtracker_db']['engine']


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None


def _begin(connection):
    if connection.dialect.name == 'sqlite':
        connection.exec_driver_sql('BEGIN IMMEDIATE')


def _token_digest(token):
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or not 20 <= len(token) <= 128:
        return None
    return _digest(token)


# Tokens ------------------------------------------------------------------------
def revoke_reset_tokens(connection, user_id):
    """Kill every live reset link of the account, inside the caller's transaction."""
    return connection.execute(update(PasswordResetToken).where(
        PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None),
        PasswordResetToken.revoked_at.is_(None)).values(revoked_at=now())).rowcount


@credentials.email_change_hook
def _on_email_change(connection, user_id):
    revoke_reset_tokens(connection, user_id)


def mint_reset_token(connection, user_id, minutes, issued_by):
    """Supersede the account's live links and insert a new one; returns (raw, expires_at) or None.

    Call inside a write transaction. The raw token exists only in the caller's memory.
    """
    user = connection.execute(select(User.__table__).where(User.id == user_id)).first()
    if user is None or credentials.account_state(user) not in ELIGIBLE:
        return None
    moment = now()
    connection.execute(delete(PasswordResetToken).where(PasswordResetToken.expires_at < moment - TOKEN_RETENTION))
    revoke_reset_tokens(connection, user_id)
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires = moment + timedelta(minutes=minutes)
    connection.execute(insert(PasswordResetToken).values(
        id=new_id(), user_id=user_id, token_hash=_digest(raw), email_digest=_digest(user.email),
        session_version=user.session_version, issued_by=issued_by, created_at=moment, expires_at=expires,
        used_at=None, revoked_at=None))
    return raw, expires


def _live(connection, token):
    """The token row joined to its account when the link may still be used, else None."""
    digest = _token_digest(token)
    if digest is None:
        return None
    tokens = PasswordResetToken.__table__
    row = connection.execute(
        select(tokens, User.email, User.password_hash, User.email_verified_at,
               User.session_version.label('current_version'))
        .join(User, User.id == tokens.c.user_id)
        .where(tokens.c.token_hash == digest, tokens.c.used_at.is_(None), tokens.c.revoked_at.is_(None),
               tokens.c.expires_at > now())).first()
    if row is None or row.current_version != row.session_version or not row.email:
        return None
    if _digest(row.email) != row.email_digest or credentials.account_state(row) not in ELIGIBLE:
        return None
    return row


def link_status(token):
    """(valid, email). Reading never consumes the link (previews and scanners are harmless)."""
    try:
        with _engine().connect() as connection:
            row = _live(connection, token)
    except SQLAlchemyError:
        raise credentials.Unavailable() from None
    return (True, row.email) if row else (False, None)


def _notice(connection, user_id, via):
    if mailer.enabled():
        mailer.enqueue_notice(connection, user_id, 'password_changed_email',
                              {'via': via, 'at': now().isoformat().replace('+00:00', 'Z')})


def reset_with_token(token, password_hash):
    """Use the link: set the password and revoke everything, all or nothing. None when the link is not usable."""
    try:
        with _engine().begin() as connection:
            _begin(connection)
            row = _live(connection, token)
            if row is None:
                return None
            # Decide before anything changes: mark_verified below turns a legacy account verified.
            was_verified = credentials.account_state(row) == 'verified'
            changed = connection.execute(update(User).where(
                User.id == row.user_id, User.session_version == row.session_version, User.email == row.email,
            ).values(password_hash=password_hash, session_version=User.session_version + 1)).rowcount
            used = connection.execute(update(PasswordResetToken).where(
                PasswordResetToken.id == row.id, PasswordResetToken.used_at.is_(None),
                PasswordResetToken.revoked_at.is_(None)).values(used_at=now())).rowcount
            if changed != 1 or used != 1:
                raise _Conflict()
            revoke_reset_tokens(connection, row.user_id)
            # B1: an unverified account's harness grant may belong to whoever attached this email; drop it.
            counts = credentials.revoke_agents(connection, row.user_id, harness=not was_verified, via='reset')
            if row.issued_by == 'email':
                credentials.mark_verified(connection, row.user_id, row.email, via='reset')
            _notice(connection, row.user_id, 'reset')
        return {'email': row.email, 'counts': counts, 'was_verified': was_verified}
    except _Conflict:
        return None
    except SQLAlchemyError:
        raise credentials.Unavailable() from None


def operator_reset(credential, password_hash, harness=None):
    """`flask user reset-password`: the reset revocation set without a link. None on a concurrent change.

    harness: None applies the state rule (revoke when unverified); True/False override it.
    """
    with _engine().begin() as connection:
        _begin(connection)
        changed = connection.execute(update(User).where(
            User.id == credential.user_id, User.password_hash == credential.password_hash,
            User.session_version == credential.session_version,
        ).values(password_hash=password_hash, session_version=User.session_version + 1)).rowcount
        if changed != 1:
            return None
        revoke_reset_tokens(connection, credential.user_id)
        revoke_harness = credentials.account_state(credential) != 'verified' if harness is None else harness
        counts = credentials.revoke_agents(connection, credential.user_id, harness=revoke_harness, via='operator')
        if mailer.deliverable(credential.email):
            _notice(connection, credential.user_id, 'operator')
    audit.info('operator password reset user_id=%d operator=cli', credential.user_id)
    return counts


def notify_password_changed(user_id, via='change'):
    """Queue the notice after a self-service password change (best effort: the change already happened)."""
    try:
        with _engine().begin() as connection:
            _notice(connection, user_id, via)
    except SQLAlchemyError:
        log.warning('password changed notice not queued user_id=%d', user_id)


# Mail --------------------------------------------------------------------------
def _messages(kind, link=None, minutes=DEFAULT_MINUTES, at=None, email=''):
    """Subject and plain text built here; the branded HTML part comes from mailer.render."""
    forgot = mailer.public_url('/?forgot')
    if kind == 'reset':
        subject = 'Reset your My Next Moov password'
        lines = ['Someone, hopefully you, asked to reset the password of the My Next Moov account for this address.',
                 '', f'Choose a new password: {link}', '',
                 f'The link works once and expires in {minutes} minutes. If you didn\'t ask for this, ignore this '
                 'email; your password hasn\'t changed.']
    else:
        subject = 'Your My Next Moov password was changed'
        lines = [f'The password of your My Next Moov account was changed at {at} (UTC).',
                 'Every browser was signed out.', '',
                 f'If this wasn\'t you, reset your password now: {forgot}']
    text = '\n'.join(lines) + '\n'
    if kind == 'reset':
        return mailer.render('password_reset', subject=subject, text=text, email=email, link=link, minutes=minutes)
    return mailer.render('password_changed', subject=subject, text=text, email=email, at=at)


def _record(user_id, outcome):
    with _engine().begin() as connection:
        connection.execute(delete(PasswordResetRequest).where(
            PasswordResetRequest.created_at < now() - REQUEST_RETENTION))
        connection.execute(insert(PasswordResetRequest).values(
            id=new_id(), user_id=user_id, outcome=outcome, created_at=now()))
    return outcome


def _send(to, subject, text, html, category, recipient=None):
    try:
        mailer.send_email(to, subject, text, html, category=category, recipient=recipient)
    except mailer.MailQuotaExceeded as exc:
        raise taskqueue.Retry('Mail quota spent', delay=exc.delay, count_attempt=False) from exc
    except mailer.TransientMailError as exc:
        raise taskqueue.Retry('Mail provider unavailable', delay=60) from exc


def deliver_request(email):
    """Worker side of a reset request: find the account, then mint and send, or do nothing. Never logs the address."""
    engine = _engine()
    normalized = credentials.normalize_email(email)
    if normalized is None:
        return 'none'
    with engine.connect() as connection:
        user = connection.execute(select(User.__table__).where(User.email == normalized)).first()
    if user is None or credentials.account_state(user) not in ELIGIBLE:
        return 'none'  # Unknown, messaging-only, or a pending sign-up (which is not a user): nothing to do.
    if not enabled():
        return _record(user.id, 'disabled')
    if mailer.deliverable(user.email) is None:
        return _record(user.id, 'undeliverable')
    try:
        recipient = mailer.recipient_ok(user.email, 'reset')
    except mailer.TransientMailError as exc:
        raise taskqueue.Retry('DNS lookup failed', delay=60) from exc
    if recipient.status != 'ok':
        return _record(user.id, 'undeliverable')
    with engine.connect() as connection:
        # Only a link actually sent counts: a retry after a failed send must still deliver one.
        recent = connection.scalar(select(PasswordResetRequest.id).where(
            PasswordResetRequest.user_id == user.id, PasswordResetRequest.outcome == 'sent',
            PasswordResetRequest.created_at > now() - timedelta(seconds=DEDUPE_SECONDS)).limit(1))
    if recent is not None:
        return _record(user.id, 'deduped')
    if not mailer.has_room('reset'):
        raise taskqueue.Retry('Mail quota spent', delay=3600, count_attempt=False)
    minutes = current_app.config['PASSWORD_RESET_MINUTES']
    with engine.begin() as connection:
        _begin(connection)
        current = connection.execute(select(User.email).where(User.id == user.id)).scalar()
        minted = mint_reset_token(connection, user.id, minutes, 'email') if current == user.email else None
    if minted is None:
        return 'none'
    subject, text, html = _messages('reset', mailer.public_url('/reset-password', {'token': minted[0]}), minutes,
                                    email=user.email)
    try:
        _send(user.email, subject, text, html, 'reset', recipient)
    except mailer.PermanentMailError:
        return _record(user.id, 'undeliverable')
    return _record(user.id, 'sent')


@taskqueue.handler('password_reset_request', mail=True, anonymous=True, delete_on_success=True, max_attempts=3,
                   lease_seconds=60)
def run_request(app, task):
    return deliver_request(task.payload.get('email'))


@taskqueue.handler('password_changed_email', mail=True, max_attempts=5, lease_seconds=60)
def run_notice(app, task):
    """The takeover alarm. Reads the address at send time; the payload holds none."""
    with _engine().connect() as connection:
        email = connection.execute(select(User.email).where(User.id == task.user_id)).scalar()
    if not email or mailer.deliverable(email) is None or not mailer.enabled():
        return 'skipped'
    try:
        at = datetime.fromisoformat(task.payload.get('at', '').replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        at = 'an unknown time'
    try:
        subject, text, html = _messages('notice', at=at, email=email)
        _send(email, subject, text, html, 'notice')
    except mailer.PermanentMailError:
        return 'failed'
    return 'sent'


# Routes ------------------------------------------------------------------------
def _error(message, status, code=None, field=None):
    body = {'error': message}
    if code:
        body['code'] = code
    if field:
        body['field'] = field
    response = jsonify(body)
    response.status_code = status
    if status == 429:
        response.headers['Retry-After'] = str(throttle.WINDOW_SECONDS)
    return response


def _guard():
    """Checks every reset route shares: same origin, HTTPS outside development, a JSON object."""
    from .auth import same_origin
    if not same_origin():
        raise Rejected('Invalid request origin', 403)
    if current_app.config['APP_ENV'] != 'development' and not request.is_secure:
        raise Rejected('HTTPS required', 403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise Rejected('A JSON object is required', 400)
    return payload


def _reserve(budgets):
    try:
        allowed = throttle.reserve(budgets)
    except throttle.Unavailable:
        raise Rejected(UNAVAILABLE, 503) from None
    if not allowed:
        raise Rejected('Too many attempts; try again in a few minutes', 429)


def _reserve_address(email, client):
    """Both per-address windows in one atomic step, each with the clean-client allowance. True when allowed."""
    canonical = throttle.address_key(email)
    budgets = [(throttle.key(name, canonical), throttle.key(name + '-client', f'{canonical}\0{client}'),
                limit, clean_limit, window)
               for name, (limit, clean_limit, window) in zip(('reset-addr', 'reset-addr-day'), REQUEST_ADDRESS)]

    def decide(count, record):
        for address, pair, limit, clean_limit, window in budgets:
            used, clean = count(address, window), count(pair, window) < ADDRESS_CLEAN_TRIES
            if used >= clean_limit or (used >= limit and not clean):
                return False
        record([name for budget in budgets for name in budget[:2]])
        return True
    return throttle.transaction(decide)


@password_reset.errorhandler(Rejected)
def rejected(exc):
    return _error(str(exc), exc.status, exc.code)


@password_reset.post('/auth/password-reset/request')
def request_reset():
    from .confirmations import guard_mail_request
    payload = _guard()
    if not enabled():
        raise Rejected("Password reset isn't available right now. Contact support.", 503, 'reset_unavailable')
    rejection = guard_mail_request('reset', payload.get('proof'))
    if rejection is not None:
        return rejection
    email = credentials.normalize_email(payload.get('email'))
    if email is None:
        return _error('Enter a valid email address', 400, field='email')
    client = throttle.client_address()
    _reserve([(throttle.key('reset-ip', client), REQUEST_IP[0][0], REQUEST_IP[0][1]),
              (throttle.key('reset-ip-day', client), REQUEST_IP[1][0], REQUEST_IP[1][1])])
    try:
        # A separate step on purpose: the IP attempt above counts even when this address is over budget.
        allowed = _reserve_address(email, client)
    except throttle.Unavailable:
        raise Rejected(UNAVAILABLE, 503) from None
    if allowed:
        try:
            with _engine().begin() as connection:
                taskqueue.enqueue(connection, None, 'password_reset_request', {'email': email})
        except SQLAlchemyError:
            raise Rejected(UNAVAILABLE, 503) from None
    return jsonify(message=ACCEPTED), 202


@password_reset.post('/auth/password-reset/status')
def link_status_route():
    payload = _guard()
    _reserve([(throttle.key('reset-check-ip', throttle.client_address()), CHECK_IP)])
    try:
        valid, email = link_status(payload.get('token'))
    except credentials.Unavailable:
        raise Rejected(UNAVAILABLE, 503) from None
    return jsonify(valid=True, email=email) if valid else jsonify(valid=False)


@password_reset.post('/auth/password-reset/complete')
def complete_reset():
    payload = _guard()
    client = throttle.client_address()
    # Before any hashing: scrypt is the expensive part of this route.
    _reserve([(throttle.key('reset-complete-ip', client), COMPLETE_IP),
              (throttle.key('ip', client), throttle.MAX_IP_ATTEMPTS)])
    password = payload.get('password')
    problem = credentials.password_problem(password)
    if problem:
        return _error(problem, 400, field='password')
    if payload.get('password_confirmation') != password:
        return _error('Passwords do not match', 400, field='password_confirmation')
    token = payload.get('token')
    try:
        if not link_status(token)[0]:
            return _error(INVALID, 410)
        result = reset_with_token(token, generate_password_hash(password))
    except credentials.Unavailable:
        raise Rejected(UNAVAILABLE, 503) from None
    if result is None:
        return _error(INVALID, 410)
    session.clear()
    counts = result['counts']
    return jsonify(message=DONE, email=result['email'],
                   disconnected={'sessions': True, 'agents': counts['oauth'] + counts['agent'],
                                 'chat': counts['harness'] > 0},
                   review_data=not result['was_verified'])


# CLI ---------------------------------------------------------------------------
def _register_cli(app):
    users = app.cli.commands.get('user')
    if users is None or 'reset-link' in users.commands:
        return

    @users.command('reset-link')
    @click.argument('email', required=False)
    @click.option('--id', 'user_id', type=int, help='The account ID, instead of its email.')
    @click.option('--minutes', default=DEFAULT_MINUTES, show_default=True, type=click.IntRange(*OPERATOR_MINUTES))
    def user_reset_link(email, user_id, minutes):
        """Print a one-time password reset link for an account (sends no email, never verifies the address)."""
        if bool(email) == (user_id is not None):
            raise click.ClickException('Give an EMAIL or --id')
        credential = credentials.read(user_id) if user_id is not None else credentials.find(
            credentials.normalize_email(email) or '')
        if credential is None:
            raise click.ClickException('No account with that email or ID (a pending sign-up is not an account yet)')
        if credentials.account_state(credential) == 'messaging':
            raise click.ClickException('That account has no web sign-in yet; send an activation link from chat')
        try:
            mailer.public_url('/')
        except mailer.MailNotConfigured:
            raise click.ClickException('PUBLIC_BASE_URL is not configured') from None
        with _engine().begin() as connection:
            _begin(connection)
            minted = mint_reset_token(connection, credential.user_id, minutes, 'operator')
        if minted is None:
            raise click.ClickException('That account cannot reset its password')
        raw, expires = minted
        expiry = expires.isoformat().replace('+00:00', 'Z')
        audit.info('operator reset link minted user_id=%d expires=%s minutes=%d operator=cli',
                   credential.user_id, expiry, minutes)
        click.echo('This link is a password-reset credential: share it only with the account holder, '
                   'over a channel you trust. Never paste it into a ticket.', err=True)
        click.echo(mailer.public_url('/reset-password', {'token': raw}))
        click.echo(f'Expires {expiry}.', err=True)
