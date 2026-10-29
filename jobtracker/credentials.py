"""Accounts: password verifiers, atomic session-generation changes and admin CLI.

Schema 9 replaced the singleton workspace credential with a `users` row per
account; the original workspace is user #1. Nothing here sends email.
"""
from dataclasses import dataclass
from datetime import timedelta, timezone
import hashlib
import re
import secrets

import click
from flask import current_app
from flask.cli import AppGroup
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import generate_password_hash

from .models import ActivationToken, InviteCode, User, now

PASSWORD_MIN, PASSWORD_MAX = 8, 128
EMAIL_MAX = 254
INVITE_DAYS = 7
# Deliberately simple: one @, no whitespace, a dotted domain (or localhost for
# the migrated placeholder). Deliverability is never checked; no email is sent.
EMAIL_PATTERN = re.compile(r'[^\s@]{1,64}@(?:[^\s@.]+\.)+[^\s@.]{2,}|[^\s@]{1,64}@localhost')
_dummy_hash = None


def dummy_hash():
    """Verified against when an email is unknown, so timing does not reveal accounts.

    Built on first use: scrypt needs ~32 MiB, which the worker should never pay.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = generate_password_hash(secrets.token_urlsafe(16))
    return _dummy_hash


class Unavailable(Exception):
    pass


@dataclass(frozen=True)
class Credential:
    user_id: int
    email: str
    password_hash: str
    session_version: int
    created_at: object = None


def normalize_email(value):
    """Trim + lowercase; None when the value is not a plausible address."""
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if len(email) > EMAIL_MAX or not EMAIL_PATTERN.fullmatch(email):
        return None
    return email


def password_problem(password):
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return f'Choose a password between {PASSWORD_MIN} and {PASSWORD_MAX} characters'
    return None


def bootstrap(connection, password_hash, email):
    """Fresh-schema only: the configured verifier becomes user #1, the workspace owner."""
    connection.execute(insert(User).values(id=1, email=email, password_hash=password_hash, session_version=1,
                                           owns_legacy_tokens=True, created_at=now()))


def legacy_owner_id():
    """The account the deprecated environment tokens act for, or None.

    Only the bootstrapped/migrated workspace owner carries the flag; a signed-up
    account can never acquire it, even if IDs were ever reused.
    """
    try:
        with engine().connect() as connection:
            # `= 1` (not `IS 1`) so SQLite uses the partial unique index.
            return connection.scalar(select(User.id).where(User.owns_legacy_tokens == True))  # noqa: E712
    except SQLAlchemyError:
        return None


def engine():
    return current_app.extensions['jobtracker_db']['engine']


def _credential(row):
    return Credential(row.id, row.email, row.password_hash, row.session_version, row.created_at) if row else None


def read(user_id):
    """The account's current verifier; None when the account no longer exists."""
    try:
        with engine().connect() as connection:
            return _credential(connection.execute(select(User.__table__).where(User.id == user_id)).first())
    except SQLAlchemyError:
        raise Unavailable() from None


def find(email):
    try:
        with engine().connect() as connection:
            return _credential(connection.execute(select(User.__table__).where(User.email == email)).first())
    except SQLAlchemyError:
        raise Unavailable() from None


def replace(expected, password_hash):
    # Hash outside the transaction, then compare both captured values at write
    # time. A concurrent change must never be overwritten by an older request.
    try:
        with engine().begin() as connection:
            changed = connection.execute(update(User).where(
                User.id == expected.user_id, User.password_hash == expected.password_hash,
                User.session_version == expected.session_version,
            ).values(password_hash=password_hash, session_version=User.session_version + 1)).rowcount
        return changed == 1
    except SQLAlchemyError:
        raise Unavailable() from None


class SignupError(Exception):
    def __init__(self, message, status, field):
        super().__init__(message)
        self.status, self.field = status, field


def invite_digest(code):
    return hashlib.sha256(code.strip().encode()).hexdigest()


def create_user(email, password_hash, invite_code=None):
    """Consume the invite (when given) and insert the user in one transaction."""
    try:
        with engine().begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            invite_id = None
            if invite_code is not None:
                invite_id = connection.scalar(select(InviteCode.id).where(
                    InviteCode.code_hash == invite_digest(invite_code), InviteCode.used_at.is_(None),
                    InviteCode.expires_at > now()))
                if invite_id is None:
                    raise SignupError('This invite code is invalid, expired, or already used', 403, 'invite_code')
            if connection.scalar(select(User.id).where(User.email == email)) is not None:
                # Checked after the invite so that, without one, emails cannot be probed;
                # and a duplicate uses the invite up, so one code answers one probe.
                duplicate = True
            else:
                duplicate = False
            if duplicate:
                if invite_id is not None:
                    connection.execute(update(InviteCode).where(InviteCode.id == invite_id).values(used_at=now()))
                user_id = None
            else:
                user_id = connection.execute(insert(User).values(
                    email=email, password_hash=password_hash, session_version=1, created_at=now())).inserted_primary_key[0]
                from .onboarding import start as start_onboarding
                start_onboarding(connection, user_id)  # Marks the account for guided setup.
            if invite_id is not None and user_id is not None:
                consumed = connection.execute(update(InviteCode).where(
                    InviteCode.id == invite_id, InviteCode.used_at.is_(None)
                ).values(used_at=now(), used_by=user_id)).rowcount
                if consumed != 1:
                    raise SignupError('This invite code is invalid, expired, or already used', 403, 'invite_code')
        if user_id is None:
            raise SignupError('An account with this email already exists', 409, 'email')
        return read(user_id)
    except IntegrityError:
        raise SignupError('An account with this email already exists', 409, 'email') from None
    except SQLAlchemyError:
        raise Unavailable() from None


def _activation_digest(token):
    return hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) and 16 <= len(token) <= 128 else None


def activation_valid(token):
    """Whether a link can still activate its account. Reading never consumes it (link previews do this)."""
    digest = _activation_digest(token)
    if digest is None:
        return False
    try:
        with engine().connect() as connection:
            return connection.scalar(select(ActivationToken.id).join(User, User.id == ActivationToken.user_id).where(
                ActivationToken.token_hash == digest, ActivationToken.used_at.is_(None),
                ActivationToken.expires_at > now(), User.password_hash.is_(None))) is not None
    except SQLAlchemyError:
        raise Unavailable() from None


def activate(token, email, password_hash):
    """Give a messaging-first account its email and password, consuming the link, all or nothing."""
    digest = _activation_digest(token)
    invalid = SignupError('This activation link is invalid, expired, or already used. Ask for a new one in chat.',
                          410, None)
    if digest is None:
        raise invalid
    try:
        with engine().begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            link = connection.execute(select(ActivationToken.id, ActivationToken.user_id).where(
                ActivationToken.token_hash == digest, ActivationToken.used_at.is_(None),
                ActivationToken.expires_at > now())).first()
            if link is None:
                raise invalid
            if connection.scalar(select(User.id).where(User.email == email, User.id != link.user_id)) is not None:
                raise SignupError('An account with this email already exists', 409, 'email')
            activated = connection.execute(update(User).where(User.id == link.user_id, User.password_hash.is_(None))
                                           .values(email=email, password_hash=password_hash,
                                                   session_version=User.session_version + 1)).rowcount
            consumed = connection.execute(update(ActivationToken).where(
                ActivationToken.id == link.id, ActivationToken.used_at.is_(None)).values(used_at=now())).rowcount
            if activated != 1 or consumed != 1:
                raise invalid  # Already activated, or a simultaneous submission won.
        return read(link.user_id)
    except IntegrityError:
        raise SignupError('An account with this email already exists', 409, 'email') from None
    except SQLAlchemyError:
        raise Unavailable() from None


def create_invites(count, days):
    codes = []
    with engine().begin() as connection:
        for _ in range(count):
            code = secrets.token_urlsafe(18)
            connection.execute(insert(InviteCode).values(
                code_hash=invite_digest(code), prefix=code[:6], created_at=now(),
                expires_at=now() + timedelta(days=days)))
            codes.append(code)
    return codes


def _as_utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None


def init_app(app):
    users = AppGroup('user', help='Manage accounts (admin).')
    invites = AppGroup('invite', help='Create and list single-use signup invites (admin).')
    app.cli.add_command(users)
    app.cli.add_command(invites)

    @invites.command('create')
    @click.option('--count', default=1, type=click.IntRange(1, 50), show_default=True)
    @click.option('--expires-days', default=INVITE_DAYS, type=click.IntRange(1, 90), show_default=True)
    def invite_create(count, expires_days):
        """Print new codes once; only their hashes are stored. Share them yourself."""
        for code in create_invites(count, expires_days):
            click.echo(code)

    @invites.command('list')
    def invite_list():
        with engine().connect() as connection:
            rows = connection.execute(select(InviteCode.__table__).order_by(InviteCode.id)).all()
        for row in rows:
            status = ('used by user ' + str(row.used_by)) if row.used_at else (
                'expired' if _as_utc(row.expires_at) <= now() else 'unused')
            click.echo(f'{row.id}\t{row.prefix}…\t{status}\texpires {_as_utc(row.expires_at).isoformat()}')

    @users.command('list')
    def user_list():
        with engine().connect() as connection:
            for row in connection.execute(select(User.id, User.email, User.created_at).order_by(User.id)):
                click.echo(f'{row.id}\t{row.email or "(messaging account, not activated)"}\t'
                           f'{_as_utc(row.created_at).isoformat()}')

    def reset(credential):
        password = click.prompt('New password', hide_input=True, confirmation_prompt=True)
        problem = password_problem(password)
        if problem:
            raise click.ClickException(problem)
        try:
            changed = replace(credential, generate_password_hash(password))
        except Unavailable:
            raise click.ClickException('Credential store unavailable') from None
        if not changed:
            raise click.ClickException('Password changed concurrently; retry recovery')
        click.echo(f'Password reset for {credential.email}. Their browser sessions have been signed out.')

    @users.command('reset-password')
    @click.argument('email')
    def user_reset_password(email):
        """Administrator recovery; never accepts the password as a shell argument."""
        credential = find(normalize_email(email) or '')
        if credential is None:
            raise click.ClickException('No account with that email')
        reset(credential)

    @users.command('set-email')
    @click.argument('user_id', type=int)
    @click.argument('email')
    def user_set_email(user_id, email):
        """Replace an account's sign-in email (e.g. user #1's migration placeholder)."""
        normalized = normalize_email(email)
        if normalized is None:
            raise click.ClickException('Enter a valid email address')
        try:
            with engine().begin() as connection:
                changed = connection.execute(update(User).where(User.id == user_id).values(email=normalized)).rowcount
        except IntegrityError:
            raise click.ClickException('Another account already uses that email') from None
        if not changed:
            raise click.ClickException('No account with that ID')
        click.echo(f'User {user_id} now signs in as {normalized}.')

    @app.cli.command('reset-workspace-password')
    def reset_workspace_password():
        """Legacy alias: reset user #1 (the migrated workspace owner)."""
        credential = read(1)
        if credential is None:
            raise click.ClickException('User #1 does not exist')
        reset(credential)
