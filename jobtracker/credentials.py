"""Accounts: password verifiers, atomic session-generation changes and admin CLI.

Schema 9 replaced the singleton workspace credential with a `users` row per
account; the original workspace is user #1. Schema 15 requires a verified
email: every account created since is verified at creation (a pending sign-up
is not a user, see confirmations.py), and accounts from before verify at their
next sign-in (`email_verified_at` NULL, "legacy_unverified").

Helpers other features rely on: `account_state`, `mark_verified`,
`on_email_change` (hooks run when an operator changes an email),
`revoke_agents` (the single revocation helper) and `normalize_new_email`.
"""
from dataclasses import dataclass
from datetime import timedelta, timezone
import hashlib
import logging
import re
import secrets

import click
from flask import current_app
from flask.cli import AppGroup
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import generate_password_hash

from .db import Base
from .models import (ActivationToken, AgentToken, EmailConfirmation, HarnessGrant, InviteCode, OAuthGrant, User,
                     now)

audit = logging.getLogger('jobtracker.accounts')

PASSWORD_MIN, PASSWORD_MAX = 8, 128
EMAIL_MAX = 254
INVITE_DAYS = 7
# Deliberately lenient, for looking accounts up (login, CLI): one @, no whitespace,
# a dotted domain, or localhost for the migrated placeholder. New addresses must
# also pass mailer.deliverable (see normalize_new_email).
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
    email_verified_at: object = None

    @property
    def verified(self):
        return self.email_verified_at is not None


def normalize_email(value):
    """Trim + lowercase; None when the value is not a plausible address."""
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if len(email) > EMAIL_MAX or not EMAIL_PATTERN.fullmatch(email):
        return None
    return email


def normalize_new_email(value):
    """normalize_email plus mailer.deliverable: the check for every address being newly written."""
    from .mailer import deliverable
    email = normalize_email(value)
    return deliverable(email) if email else None


ACCOUNT_STATES = ('messaging', 'legacy_unverified', 'verified')


def account_state(row):
    """'messaging' (no email/password), 'legacy_unverified' or 'verified' for a users row or Credential.

    A pending sign-up or activation is not a users row at all (email_confirmations).
    """
    email = getattr(row, 'email', None)
    password = getattr(row, 'password_hash', None)
    if not email or not password:
        return 'messaging'
    return 'verified' if getattr(row, 'email_verified_at', None) is not None else 'legacy_unverified'


def mark_verified(connection, user_id, email, *, via):
    """Record that `email` is proven for this account, inside the caller's transaction.

    Conditional on the account's current email; keeps the first verification time;
    deletes the account's outstanding email confirmations. Returns True when the
    account's email matched. via: 'link', 'reset' or 'cli' (audit only).
    """
    matched = connection.execute(update(User).where(User.id == user_id, User.email == email).values(
        email_verified_at=func.coalesce(User.email_verified_at, now()))).rowcount == 1
    if matched:
        connection.execute(delete(EmailConfirmation).where(EmailConfirmation.user_id == user_id))
        audit.info('user verified user_id=%d via=%s', user_id, via)
    return matched


EMAIL_CHANGE_HOOKS = []


def email_change_hook(function):
    """Register function(connection, user_id), run inside every operator email change (e.g. revoke reset tokens)."""
    EMAIL_CHANGE_HOOKS.append(function)
    return function


def on_email_change(connection, user_id):
    """Run the registered email-change hooks inside the caller's transaction."""
    for hook in EMAIL_CHANGE_HOOKS:
        hook(connection, user_id)


def revoke_agents(connection, user_id, *, harness, via):
    """Revoke the account's OAuth connections (and so their tokens) and managed agent tokens; harness grants too
    when `harness`. The single revocation helper: reset, operator recovery and the CLI all use it. Writes one
    audit line. Returns the counts."""
    moment = now()
    counts = {
        'oauth': connection.execute(update(OAuthGrant).where(
            OAuthGrant.user_id == user_id, OAuthGrant.revoked_at.is_(None)).values(revoked_at=moment)).rowcount,
        'agent': connection.execute(update(AgentToken).where(
            AgentToken.user_id == user_id, AgentToken.revoked_at.is_(None)).values(revoked_at=moment)).rowcount,
        'harness': connection.execute(update(HarnessGrant).where(
            HarnessGrant.user_id == user_id, HarnessGrant.revoked_at.is_(None)).values(revoked_at=moment)).rowcount
        if harness else 0,
    }
    audit.info('revoked user_id=%d oauth=%d agent=%d harness=%d via=%s',
               user_id, counts['oauth'], counts['agent'], counts['harness'], via)
    return counts


def set_email(user_id, email, *, verified=False):
    """The one operator email change, in one transaction: deliverable check, update, clear verification
    (unless `verified`), delete confirmations, run the email-change hooks, bump session_version."""
    normalized = normalize_new_email(email)
    if normalized is None:
        raise ValueError('Enter a deliverable email address')
    with engine().begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        changed = connection.execute(update(User).where(User.id == user_id).values(
            email=normalized, email_verified_at=now() if verified else None,
            session_version=User.session_version + 1)).rowcount
        if not changed:
            raise LookupError('No account with that ID')
        connection.execute(delete(EmailConfirmation).where(EmailConfirmation.user_id == user_id))
        on_email_change(connection, user_id)
        audit.info('email changed user_id=%d verified=%s', user_id, 'yes' if verified else 'no')
    return normalized


def password_problem(password):
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        return f'Choose a password between {PASSWORD_MIN} and {PASSWORD_MAX} characters'
    return None


def bootstrap(connection, password_hash, email):
    """Fresh-schema only: the configured verifier becomes user #1, the workspace owner."""
    # Operator-configured identity: verified from the start.
    connection.execute(insert(User).values(id=1, email=email, password_hash=password_hash, session_version=1,
                                           owns_legacy_tokens=True, created_at=now(), email_verified_at=now()))


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
    return Credential(row.id, row.email, row.password_hash, row.session_version, row.created_at,
                      row.email_verified_at) if row else None


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


def live_invite(code):
    """The id of an unused, unexpired invite code (consumed only when the sign-up is confirmed), else None."""
    try:
        with engine().connect() as connection:
            return connection.scalar(select(InviteCode.id).where(
                InviteCode.code_hash == invite_digest(code), InviteCode.used_at.is_(None),
                InviteCode.expires_at > now()))
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
    @click.option('--unverified', is_flag=True, help='Only accounts that still have to verify their email.')
    @click.option('--undeliverable', is_flag=True, help='Only accounts whose address cannot receive mail.')
    def user_list(unverified, undeliverable):
        from .mailer import deliverable
        with engine().connect() as connection:
            rows = connection.execute(select(User.id, User.email, User.password_hash, User.email_verified_at,
                                             User.created_at).order_by(User.id)).all()
        for row in rows:
            state = account_state(row)
            reachable = bool(row.email) and deliverable(row.email) is not None
            if (unverified and state != 'legacy_unverified') or (undeliverable and (reachable or not row.email)):
                continue
            label = {'messaging': 'messaging account, not activated', 'legacy_unverified': 'unverified',
                     'verified': 'verified'}[state]
            click.echo(f'{row.id}\t{row.email or "-"}\t{label}\t{"deliverable" if reachable else "undeliverable"}'
                       f'\t{_as_utc(row.created_at).isoformat()}')

    def account_by_email(email):
        credential = find(normalize_email(email) or '')
        if credential is None:
            raise click.ClickException('No account with that email')
        return credential

    @users.command('create')
    @click.argument('email')
    @click.option('--unverified', is_flag=True, help='Create it unverified (it verifies at first sign-in).')
    @click.option('--allow-placeholder', is_flag=True, help='Allow an address that cannot receive mail.')
    def user_create(email, unverified, allow_placeholder):
        """Create an account from the shell (e.g. the test server, where sign-up is off). Prompts for the password."""
        normalized = normalize_new_email(email) or (normalize_email(email) if allow_placeholder else None)
        if normalized is None:
            raise click.ClickException('Enter a deliverable email address (or pass --allow-placeholder)')
        password = click.prompt('Password', hide_input=True, confirmation_prompt=True)
        problem = password_problem(password)
        if problem:
            raise click.ClickException(problem)
        try:
            with engine().begin() as connection:
                user_id = connection.execute(insert(User).values(
                    email=normalized, password_hash=generate_password_hash(password), session_version=1,
                    created_at=now(), email_verified_at=None if unverified else now())).inserted_primary_key[0]
                from .onboarding import start as start_onboarding
                start_onboarding(connection, user_id)
        except IntegrityError:
            raise click.ClickException('Another account already uses that email') from None
        click.echo(f'Created account {user_id} for {normalized} ({"unverified" if unverified else "verified"}).')

    @users.command('verify')
    @click.argument('email')
    def user_verify(email):
        """Mark an account's email as verified (support confirmed it another way)."""
        from .mailer import deliverable
        credential = account_by_email(email)
        if deliverable(credential.email) is None:
            raise click.ClickException('That address cannot receive mail; change it with set-email first')
        with engine().begin() as connection:
            mark_verified(connection, credential.user_id, credential.email, via='cli')
        click.echo(f'{credential.email} is verified.')

    @users.command('unverify')
    @click.argument('email')
    def user_unverify(email):
        """Clear verification and sign the account out: its next sign-in must verify the email."""
        credential = account_by_email(email)
        with engine().begin() as connection:
            connection.execute(update(User).where(User.id == credential.user_id).values(
                email_verified_at=None, session_version=User.session_version + 1))
        click.echo(f'{credential.email} must verify at next sign-in; its browser sessions were signed out.')

    @users.command('send-verification')
    @click.argument('email', required=False)
    @click.option('--all-legacy', is_flag=True, help='Every unverified account with a deliverable address.')
    def user_send_verification(email, all_legacy):
        """Queue a verification link (confirming it needs the account's password). Never prints a link."""
        from .confirmations import start_existing
        from .mailer import deliverable
        if bool(email) == bool(all_legacy):
            raise click.ClickException('Give an EMAIL or --all-legacy')
        if all_legacy:
            with engine().connect() as connection:
                rows = connection.execute(select(User.__table__).where(
                    User.email.is_not(None), User.password_hash.is_not(None), User.email_verified_at.is_(None))).all()
            targets = [_credential(row) for row in rows if deliverable(row.email)]
        else:
            targets = [account_by_email(email)]
        queued = 0
        for credential in targets:
            if credential.verified:
                raise click.ClickException(f'{credential.email} is already verified')
            if deliverable(credential.email) is None:
                raise click.ClickException('That address cannot receive mail; change it with set-email first')
            queued += bool(start_existing(credential))
        click.echo(f'Queued {queued} verification link(s).')

    @users.command('revoke-agents')
    @click.argument('user_id', type=int)
    @click.option('--harness', is_flag=True, help='Also disconnect the chat (Resume Agent harness) grants.')
    def user_revoke_agents(user_id, harness):
        """Revoke an account's OAuth connections and agent tokens (support tool; confirm identity first)."""
        if read(user_id) is None:
            raise click.ClickException('No account with that ID')
        with engine().begin() as connection:
            counts = revoke_agents(connection, user_id, harness=harness, via='cli')
        click.echo(f'Revoked {counts["oauth"]} OAuth connection(s), {counts["agent"]} agent token(s) and '
                   f'{counts["harness"]} harness grant(s) for account {user_id}.')

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
    @click.option('--verified', is_flag=True, help='Support confirmed the new address another way.')
    @click.option('--send-verification', is_flag=True, help='Queue a verification link to the new address.')
    def user_set_email(user_id, email, verified, send_verification):
        """Replace an account's sign-in email; clears verification (unless --verified) and signs it out everywhere."""
        try:
            normalized = set_email(user_id, email, verified=verified)
        except ValueError:
            raise click.ClickException('Enter a deliverable email address') from None
        except LookupError:
            raise click.ClickException('No account with that ID') from None
        except IntegrityError:
            raise click.ClickException('Another account already uses that email') from None
        click.echo(f'User {user_id} now signs in as {normalized} ({"verified" if verified else "unverified"}); '
                   'their browser sessions were signed out.')
        if send_verification and not verified:
            from .confirmations import start_existing
            start_existing(read(user_id))
            click.echo('Queued a verification link.')

    @users.command('merge-messaging')
    @click.argument('source', type=int)
    @click.argument('target', type=int)
    def user_merge_messaging(source, target):
        """Give an empty messaging account's harness access to an existing account, then delete it.

        For linking someone's phone to the account they already use on the web: they text the agent once,
        which creates the messaging account, and this moves that account's grants to the existing one.
        """
        if source == target:
            raise click.ClickException('Choose two different accounts')
        with engine().begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            found = {row.id: row for row in connection.execute(
                select(User.id, User.email, User.password_hash).where(User.id.in_((source, target))))}
            for user_id in (source, target):
                if user_id not in found:
                    raise click.ClickException(f'No account with ID {user_id}')
            if found[source].email or found[source].password_hash:
                raise click.ClickException(f'Account {source} has web sign-in; only a messaging account can be merged')
            for table in Base.metadata.sorted_tables:
                if table in (HarnessGrant.__table__, ActivationToken.__table__, EmailConfirmation.__table__,
                             User.__table__):
                    continue
                for key in table.foreign_keys:
                    if key.column.table is User.__table__ and connection.scalar(
                            select(func.count()).select_from(table).where(key.parent == source)):
                        raise click.ClickException(f'Account {source} already has data in {table.name}; '
                                                   'merge only an empty messaging account')
            moved = connection.execute(update(HarnessGrant).where(HarnessGrant.user_id == source)
                                       .values(user_id=target)).rowcount
            connection.execute(delete(ActivationToken).where(ActivationToken.user_id == source))
            connection.execute(delete(EmailConfirmation).where(EmailConfirmation.user_id == source))
            connection.execute(delete(User).where(User.id == source))
        click.echo(f'Moved {moved} harness grant(s) from account {source} to account {target}, '
                   f'and deleted account {source}.')

    signups = AppGroup('signup', help='Pending sign-ups (admin).')
    app.cli.add_command(signups)

    @signups.command('pending')
    @click.option('--email', default=None, help='Only rows for this address.')
    def signup_pending(email):
        """Counts of live confirmation rows by kind and delivery state; addresses are masked."""
        from .mailer import mask_address
        query = select(EmailConfirmation.kind, EmailConfirmation.delivery, EmailConfirmation.email).where(
            EmailConfirmation.used_at.is_(None))
        if email:
            query = query.where(EmailConfirmation.email == (normalize_email(email) or ''))
        with engine().connect() as connection:
            rows = connection.execute(query).all()
        counts = {}
        for row in rows:
            counts[(row.kind, row.delivery)] = counts.get((row.kind, row.delivery), 0) + 1
        for (kind, delivery), count in sorted(counts.items()):
            click.echo(f'{kind}\t{delivery}\t{count}')
        if email and rows:
            click.echo(f'address: {mask_address(rows[0].email)}')

    @app.cli.command('reset-workspace-password')
    def reset_workspace_password():
        """Legacy alias: reset user #1 (the migrated workspace owner)."""
        credential = read(1)
        if credential is None:
            raise click.ClickException('User #1 does not exist')
        reset(credential)
