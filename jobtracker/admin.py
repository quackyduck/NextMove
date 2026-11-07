"""The admin page's API (schema 18): usage metrics, account metadata and four support actions.

Who: a browser session of a verified account with `users.is_admin`, granted only from the shell
(`flask user set-admin`). Any Authorization header (legacy token, OAuth/MCP, managed agent tokens, harness grants)
or harness signature header, plain HTTP outside development, no session, or a non-admin all get the same 404 as an
unknown API path. Writes need X-CSRF-Token. Reads need a password checked at some point in this session
(`password_at`); actions need one checked within ADMIN_STEP_UP_SECONDS (10 minutes), renewed by POST /admin/step-up.

What: counts and metadata only, never job, contact, note, resume or template content, and never client or token
names. Actions never target the admin's own account, another admin, or the workspace owner (owns_legacy_tokens):
those stay with the shell. Every action and step-up writes an `admin_actions` row (ids and allowlisted numbers
only) in the action's own transaction, and an audit line on `jobtracker.accounts` (ids only: never an email, link,
token or search text).

Separation: views never assign g.user_id or g.credential (those stay the admin's), never call services.*, and pass
the target id explicitly to the helpers they reuse.
"""
from datetime import datetime, timedelta, timezone
from functools import wraps
import hmac
import logging
import os
import re
import time

import click
from flask import Blueprint, current_app, g, jsonify, request, session
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash

from . import credentials, mailer, throttle
from .models import (ADMIN_ACTIONS, OUTCOMES, STAGES, Activity, AdminAction, AgentToken, Contact, EmailConfirmation,
                     ExtractionTask, HarnessGrant, IngestionRequest, Job, MasterResume, OAuthGrant,
                     PasswordResetRequest, PasswordResetToken, Resume, ResumeTemplate, TailoredDraft, User, now)

admin = Blueprint('admin', __name__)
audit = logging.getLogger('jobtracker.accounts')
log = logging.getLogger('jobtracker.admin')

ADMIN_ACTION_DAYS = 365  # Retention of admin_actions; the privacy page states it (a test pins the two together).
STEP_UP_SECONDS = 600
STEP_UP_RANGE = (60, 1800)
STEP_UP_DAILY = 20
READ_BUDGET = 300
ACTION_BUDGET = 30
PAGE_SIZE = 50
WEB_RESET_MINUTES = (5, 240)  # The shell allows up to 1440; a browser clipboard is a worse place for a long link.
AUDIT_LIMIT = (1, 500)
NOT_FOUND = 'Not found'
# An unknown account id, as distinct from the route itself not existing (only administrators ever see it).
ACCOUNT_NOT_FOUND = ('Account not found', 404, 'account_not_found')
ADMIN_TARGET = 'Use the command line for administrator accounts'
RESET_WARNING = ('This link is a password-reset credential: share it only with the account holder, '
                 'over a channel you trust. Never paste it into a ticket.')

# Audit detail allowlist: action -> {key: type}. record() refuses anything else.
DETAIL_KEYS = {
    'grant_admin': {}, 'revoke_admin': {}, 'step_up': {}, 'step_up_failed': {}, 'view_user': {},
    'reset_link': {'minutes': int, 'expires_at': str},
    'sign_out_everywhere': {},
    'revoke_agents': {'oauth': int, 'agent': int, 'harness': int, 'include_harness': bool},
    'mark_verified': {'revoke_agents': bool, 'include_harness': bool, 'oauth': int, 'agent': int, 'harness': int},
}
assert set(DETAIL_KEYS) == set(ADMIN_ACTIONS)


class AdminError(Exception):
    def __init__(self, message, status, code=None):
        super().__init__(message)
        self.status, self.code = status, code


def _engine():
    return current_app.extensions['jobtracker_db']['engine']


def _begin(connection):
    if connection.dialect.name == 'sqlite':
        connection.exec_driver_sql('BEGIN IMMEDIATE')


def _iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat().replace(
        '+00:00', 'Z')


# Audit ---------------------------------------------------------------------------
def record(connection, action, *, actor='web', admin_id=None, target=None, **detail):
    """Insert one admin_actions row inside the caller's transaction. Detail keys and types come from DETAIL_KEYS."""
    allowed = DETAIL_KEYS.get(action)
    if allowed is None or actor not in ('web', 'cli') or (actor == 'web') != (admin_id is not None):
        raise ValueError('Unknown admin action')
    for name, value in detail.items():
        kind = allowed.get(name)
        if kind is None or type(value) is not kind or (kind is str and len(value) > 40):
            raise ValueError(f'Detail {name!r} is not allowed for {action}')
    connection.execute(AdminAction.__table__.insert().values(
        created_at=now(), actor=actor, admin_user_id=admin_id, action=action, target_user_id=target, detail=detail))


def prune(connection):
    """Worker maintenance: drop audit rows older than ADMIN_ACTION_DAYS."""
    return connection.execute(AdminAction.__table__.delete().where(
        AdminAction.created_at < now() - timedelta(days=ADMIN_ACTION_DAYS))).rowcount


# Access --------------------------------------------------------------------------
def _not_found():
    return jsonify(error=NOT_FOUND), 404


def _error(message, status, code=None):
    body = {'error': message}
    if code:
        body['code'] = code
    response = jsonify(body)
    response.status_code = status
    if status == 429:
        response.headers['Retry-After'] = str(throttle.WINDOW_SECONDS)
    return response


def password_age():
    """Seconds since this session last checked the password, or None (missing, malformed or in the future)."""
    checked = session.get('password_at')
    if type(checked) is not int:
        return None
    moment = int(time.time())
    if checked > moment + 60:
        return None
    return max(0, moment - checked)


def step_up_seconds():
    return current_app.config['ADMIN_STEP_UP_SECONDS']


def _note_refused(user_id):
    """A signed-in non-admin reached an admin route: one INFO line per account per hour (ids only)."""
    try:
        if throttle.reserve([(throttle.key('admin-refused-log', user_id), 1, 3600)]):
            log.info('admin route refused user_id=%d', user_id)
    except throttle.Unavailable:
        pass


def admin_route(kind):
    """kind: 'read' (any password check this session), 'action' (a fresh one) or 'step_up' (renews it)."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            from .auth import browser_authenticated
            if request.method == 'OPTIONS' or 'Authorization' in request.headers or any(
                    name.lower().startswith('x-ra-') for name in request.headers.keys()):
                return _not_found()
            if current_app.config['APP_ENV'] != 'development' and not request.is_secure:
                return _not_found()
            try:
                valid = browser_authenticated()
            except credentials.Unavailable:
                return _error('Temporarily unavailable; try again later', 503)
            if not valid:
                return _not_found()
            credential = g.credential
            if not credential.is_admin or not credential.verified:
                _note_refused(credential.user_id)
                return _not_found()
            if request.method not in ('GET', 'HEAD'):
                supplied = request.headers.get('X-CSRF-Token', '')
                expected = session.get('csrf_token', '')
                if not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                    return _error('Invalid CSRF token', 403)
            g.auth_type = 'session'
            g.admin_id = credential.user_id
            age = password_age()
            if kind == 'read' and age is None or kind == 'action' and (age is None or age > step_up_seconds()):
                return _error('Confirm your password to continue', 403, 'step_up_required')
            if kind != 'step_up':
                budget = ('admin-read', READ_BUDGET) if kind == 'read' else ('admin-action', ACTION_BUDGET)
                try:
                    allowed = throttle.reserve([(throttle.key(budget[0], credential.user_id), budget[1])])
                except throttle.Unavailable:
                    return _error('Temporarily unavailable; try again later', 503)
                if not allowed:
                    return _error('Too many requests; try again in a few minutes', 429)
            try:
                return view(*args, **kwargs)
            except AdminError as exc:
                return _error(str(exc), exc.status, exc.code)
            except (credentials.Unavailable, throttle.Unavailable):
                return _error('Temporarily unavailable; try again later', 503)
        wrapped.admin_route = kind
        return wrapped
    return decorator


def _route(path, methods, kind):
    def decorator(view):
        wrapped = admin_route(kind)(view)
        admin.add_url_rule(path, view_func=wrapped, methods=[*methods, 'OPTIONS'], provide_automatic_options=False)
        return wrapped
    return decorator


def _body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise AdminError('A JSON object is required', 400)
    return payload


# Step-up -------------------------------------------------------------------------
@_route('/admin/step-up', ['POST'], 'step_up')
def step_up():
    password = _body().get('password')
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        raise AdminError('Enter your password', 400, 'step_up_failed')
    admin_id = g.admin_id
    # One budget with POST /auth/password (re-checking a signed-in password), plus a daily cap. Before hashing.
    allowed = throttle.reserve([(throttle.key('verify', admin_id), throttle.MAX_ATTEMPTS),
                                (throttle.key('admin-stepup-day', admin_id), STEP_UP_DAILY, throttle.DAY_SECONDS)])
    if not allowed:
        raise AdminError('Too many attempts; try again later', 429)
    ok = check_password_hash(g.credential.password_hash, password)
    with _engine().begin() as connection:
        record(connection, 'step_up' if ok else 'step_up_failed', admin_id=admin_id)
    if not ok:
        audit.info('admin step-up failed admin_id=%d', admin_id)
        raise AdminError('Incorrect password', 400, 'step_up_failed')
    session['password_at'] = int(time.time())
    audit.info('admin step-up admin_id=%d', admin_id)
    return jsonify(ok=True, valid_for=step_up_seconds())


# Metrics -------------------------------------------------------------------------
def _count(connection, model, *where):
    return connection.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _by(connection, column, *where):
    return {key: count for key, count in connection.execute(
        select(column, func.count()).where(*where).group_by(column))}


def _active_agents(moment):
    """Active (unrevoked, unexpired) connections: {name: (model, where clauses)}."""
    return {
        'oauth': (OAuthGrant, (OAuthGrant.revoked_at.is_(None),)),
        'agent': (AgentToken, (AgentToken.revoked_at.is_(None), AgentToken.expires_at > moment)),
        'harness': (HarnessGrant, (HarnessGrant.revoked_at.is_(None),)),
    }


def _bucket(count):
    for label, high in (('0', 0), ('1-5', 5), ('6-20', 20), ('21-50', 50)):
        if count <= high:
            return label
    return '51+'


def _median(values):
    if not values:
        return 0
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def overview(moment=None):
    moment = moment or now()
    today = moment.date()
    with _engine().connect() as connection:
        rows = connection.execute(select(User.email, User.password_hash, User.email_verified_at, User.is_admin)).all()
        states = {state: 0 for state in credentials.ACCOUNT_STATES}
        for row in rows:
            states[credentials.account_state(row)] += 1
        accounts = {'total': len(rows), 'verified': states['verified'],
                    'legacy_unverified': states['legacy_unverified'], 'messaging': states['messaging'],
                    'admins': sum(1 for row in rows if row.is_admin)}

        first_day = today - timedelta(days=29)
        first_week = today - timedelta(days=today.weekday()) - timedelta(weeks=11)
        since = datetime.combine(min(first_day, first_week), datetime.min.time(), tzinfo=timezone.utc)
        per_day = {}
        for day, count in connection.execute(select(func.date(User.created_at), func.count()).where(
                User.created_at >= since).group_by(func.date(User.created_at))):
            per_day[datetime.strptime(day, '%Y-%m-%d').date()] = count
        daily = [{'date': (first_day + timedelta(days=offset)).isoformat(),
                  'count': per_day.get(first_day + timedelta(days=offset), 0)} for offset in range(30)]
        weekly = []
        for offset in range(12):
            start = first_week + timedelta(weeks=offset)
            weekly.append({'week_start': start.isoformat(),
                           'count': sum(count for day, count in per_day.items()
                                        if start <= day < start + timedelta(weeks=1))})

        active = {name: _count(connection, User, User.last_seen_at >= moment - timedelta(days=days))
                  for name, days in (('day', 1), ('week', 7), ('month', 30))}
        active['never'] = _count(connection, User, User.last_seen_at.is_(None))

        per_account = [count for _, count in connection.execute(
            select(User.id, func.count(Job.id)).select_from(User).outerjoin(Job, Job.user_id == User.id)
            .group_by(User.id))]
        distribution = {label: 0 for label in ('0', '1-5', '6-20', '21-50', '51+')}
        for count in per_account:
            distribution[_bucket(count)] += 1
        by_stage, by_outcome = _by(connection, Job.stage), _by(connection, Job.outcome)
        roles = {'total': sum(per_account), 'by_stage': {stage: by_stage.get(stage, 0) for stage in STAGES},
                 'by_outcome': {outcome: by_outcome.get(outcome, 0) for outcome in OUTCOMES},
                 'per_account': distribution, 'max_per_account': max(per_account, default=0),
                 'median_per_account': _median(per_account)}

        resumes = {
            'templates': _count(connection, ResumeTemplate),
            'template_versions': _count(connection, MasterResume),
            'job_resumes': _count(connection, Resume),
            'tailored': _count(connection, Resume, Resume.source_master_resume_id.is_not(None)),
            'accounts_with_template': connection.scalar(
                select(func.count(func.distinct(ResumeTemplate.user_id)))) or 0,
            'drafts': _count(connection, TailoredDraft),
        }

        agents = {}
        labels = {'oauth': ('oauth_grants', 'oauth_accounts'), 'agent': ('agent_tokens', 'agent_token_accounts'),
                  'harness': ('harness_grants', 'harness_accounts')}
        for name, (model, where) in _active_agents(moment).items():
            total, accounts_label = labels[name]
            agents[total] = _count(connection, model, *where)
            agents[accounts_label] = connection.scalar(
                select(func.count(func.distinct(model.user_id))).where(*where)) or 0

        week_ago = moment - timedelta(days=7)
        imports = {'ingestion': _count(connection, IngestionRequest, IngestionRequest.status == 'failed',
                                       IngestionRequest.created_at >= week_ago),
                   'extraction': _count(connection, ExtractionTask, ExtractionTask.status == 'failed',
                                        ExtractionTask.created_at >= week_ago)}
        pending = _by(connection, EmailConfirmation.kind, EmailConfirmation.used_at.is_(None),
                      (EmailConfirmation.expires_at.is_(None)) | (EmailConfirmation.expires_at > moment))
    return {
        'generated_at': _iso(moment), 'accounts': accounts, 'signups': {'daily': daily, 'weekly': weekly},
        'active': active, 'roles': roles, 'resumes': resumes, 'agents': agents, 'imports_failed_7d': imports,
        'pending_confirmations': {kind: pending.get(kind, 0) for kind in ('signup', 'activation', 'existing')},
        'mail': mailer.usage_report(alerts=False),
    }


@_route('/admin/overview', ['GET'], 'read')
def overview_route():
    return jsonify(overview())


# Users ---------------------------------------------------------------------------
def _counted(model, *where):
    return select(func.count()).select_from(model).where(model.user_id == User.id, *where).scalar_subquery()


def query_users(text='', page=1, moment=None):
    moment = moment or now()
    oauth, agent, harness = (_counted(model, *where) for model, where in _active_agents(moment).values())
    agents = oauth + agent + harness
    where = []
    if text:
        where.append(func.instr(func.lower(User.email), text.lower()) > 0)
    with _engine().connect() as connection:
        total = connection.scalar(select(func.count()).select_from(User).where(*where))
        rows = connection.execute(select(
            User.id, User.email, User.password_hash, User.email_verified_at, User.is_admin, User.created_at,
            User.last_seen_at, _counted(Job).label('roles'), _counted(Resume).label('job_resumes'),
            _counted(ResumeTemplate).label('templates'), agents.label('agents'),
        ).where(*where).order_by(User.id.desc()).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    return {'users': [{
        'id': row.id, 'email': row.email, 'state': credentials.account_state(row), 'is_admin': bool(row.is_admin),
        'created_at': _iso(row.created_at), 'last_seen_at': _iso(row.last_seen_at),
        'counts': {'roles': row.roles, 'job_resumes': row.job_resumes, 'templates': row.templates,
                   'agents': row.agents}} for row in rows], 'page': page, 'page_size': PAGE_SIZE, 'total': total}


@_route('/admin/users/query', ['POST'], 'read')
def users_route():
    # A POST so search text never sits in a URL (history, proxy logs, telemetry). Never logged.
    payload = _body()
    text, page = payload.get('q', ''), payload.get('page', 1)
    if not isinstance(text, str) or len(text) > 254:
        raise AdminError('Search text must be at most 254 characters', 400)
    if type(page) is not int or not 1 <= page <= 10000:
        raise AdminError('page must be a positive integer', 400)
    return jsonify(query_users(text.strip(), page))


def protected(target, admin_id):
    """Targets the web never acts on: yourself, other administrators, and the workspace owner."""
    return target.id == admin_id or bool(target.is_admin) or bool(target.owns_legacy_tokens)


def _actions_for(connection, user_id, limit=20):
    return [_action_row(row) for row in connection.execute(select(AdminAction.__table__).where(
        AdminAction.target_user_id == user_id).order_by(AdminAction.id.desc()).limit(limit))]


def _action_row(row):
    return {'id': row.id, 'created_at': _iso(row.created_at), 'actor': row.actor, 'admin_user_id': row.admin_user_id,
            'action': row.action, 'target_user_id': row.target_user_id, 'detail': row.detail or {}}


def _record_view(admin_id, user_id, moment):
    """One view row per (admin, account, UTC hour). Checked on a read connection first, so repeat views within the
    hour never take the SQLite write lock. A cross-site top-level GET (SameSite=Lax sends the cookie) can at most
    add that one deduplicated row."""
    hour = moment.replace(minute=0, second=0, microsecond=0)
    seen = select(AdminAction.id).where(
        AdminAction.action == 'view_user', AdminAction.admin_user_id == admin_id,
        AdminAction.target_user_id == user_id, AdminAction.created_at >= hour).limit(1)
    with _engine().connect() as connection:
        if connection.scalar(seen) is not None:
            return
    with _engine().begin() as connection:
        _begin(connection)
        if connection.scalar(seen) is None:
            record(connection, 'view_user', admin_id=admin_id, target=user_id)


def user_detail(user_id, admin_id, moment=None):
    moment = moment or now()
    with _engine().connect() as connection:
        if connection.scalar(select(User.id).where(User.id == user_id)) is None:
            return None
    _record_view(admin_id, user_id, moment)
    with _engine().connect() as connection:
        user = connection.execute(select(User.__table__).where(User.id == user_id)).first()
        if user is None:
            return None
        mine = lambda model, *where: _count(connection, model, model.user_id == user_id, *where)  # noqa: E731
        by_stage = _by(connection, Job.stage, Job.user_id == user_id)
        by_outcome = _by(connection, Job.outcome, Job.user_id == user_id)
        counts = {
            'roles': mine(Job), 'roles_by_stage': {stage: by_stage.get(stage, 0) for stage in STAGES},
            'roles_by_outcome': {outcome: by_outcome.get(outcome, 0) for outcome in OUTCOMES},
            'contacts': mine(Contact), 'history_entries': mine(Activity), 'job_resumes': mine(Resume),
            'tailored': mine(Resume, Resume.source_master_resume_id.is_not(None)),
            'templates': mine(ResumeTemplate), 'template_versions': mine(MasterResume), 'drafts': mine(TailoredDraft),
        }
        agents = {}
        for name, (model, where) in _active_agents(moment).items():
            agents[name] = {'active': mine(model, *where), 'last_used_at': _iso(connection.scalar(
                select(func.max(model.last_used_at)).where(model.user_id == user_id)))}
        pending = {
            'confirmations': _count(connection, EmailConfirmation, EmailConfirmation.user_id == user_id,
                                    EmailConfirmation.used_at.is_(None)),
            'reset_links_live': mine(PasswordResetToken, PasswordResetToken.used_at.is_(None),
                                     PasswordResetToken.revoked_at.is_(None), PasswordResetToken.expires_at > moment,
                                     # A session-generation bump (sign-out everywhere) kills a link too (_live).
                                     PasswordResetToken.session_version == user.session_version),
            'reset_requests_30d': _by(connection, PasswordResetRequest.outcome,
                                      PasswordResetRequest.user_id == user_id,
                                      PasswordResetRequest.created_at >= moment - timedelta(days=30)),
        }
        actions = _actions_for(connection, user_id)
    state = credentials.account_state(user)
    deliverable = bool(user.email) and mailer.deliverable(user.email) is not None
    blocked = protected(user, admin_id)
    return {
        'user': {'id': user.id, 'email': user.email, 'state': state, 'is_admin': bool(user.is_admin),
                 'owns_legacy_tokens': bool(user.owns_legacy_tokens), 'created_at': _iso(user.created_at),
                 'email_verified_at': _iso(user.email_verified_at), 'last_seen_at': _iso(user.last_seen_at),
                 'deliverable': deliverable},
        'counts': counts, 'agents': agents, 'pending': pending, 'actions': actions,
        'protected': blocked, 'self': user.id == admin_id,
        'can': {'reset_link': not blocked and state != 'messaging', 'sign_out': not blocked and state != 'messaging',
                'revoke_agents': not blocked,
                'verify': not blocked and state == 'legacy_unverified' and deliverable},
    }


@_route('/admin/users/<int:user_id>', ['GET'], 'read')
def user_route(user_id):
    detail = user_detail(user_id, g.admin_id)
    if detail is None:
        raise AdminError(*ACCOUNT_NOT_FOUND)
    return jsonify(detail)


# Actions -------------------------------------------------------------------------
def _act(user_id, perform):
    """Run perform(connection, target) in one BEGIN IMMEDIATE transaction, after re-reading the target in it."""
    admin_id = g.admin_id
    with _engine().begin() as connection:
        _begin(connection)
        target = connection.execute(select(User.__table__).where(User.id == user_id)).first()
        if target is None:
            raise AdminError(*ACCOUNT_NOT_FOUND)
        if protected(target, admin_id):
            raise AdminError(ADMIN_TARGET, 409, 'admin_target')
        return perform(connection, target, admin_id)


@_route('/admin/users/<int:user_id>/reset-link', ['POST'], 'action')
def reset_link_route(user_id):
    from .password_reset import mint_reset_token
    minutes = _body().get('minutes', 60)
    if type(minutes) is not int or not WEB_RESET_MINUTES[0] <= minutes <= WEB_RESET_MINUTES[1]:
        raise AdminError(f'minutes must be between {WEB_RESET_MINUTES[0]} and {WEB_RESET_MINUTES[1]}', 400)
    try:
        mailer.public_url('/')  # Links come from PUBLIC_BASE_URL only, never the Host header.
    except mailer.MailNotConfigured:
        raise AdminError('PUBLIC_BASE_URL is not configured', 503) from None

    def perform(connection, target, admin_id):
        if credentials.account_state(target) == 'messaging':
            raise AdminError('That account has no web sign-in yet; send an activation link from chat', 409)
        minted = mint_reset_token(connection, target.id, minutes, 'operator')
        if minted is None:
            raise AdminError('That account cannot reset its password', 409)
        expiry = _iso(minted[1])
        record(connection, 'reset_link', admin_id=admin_id, target=target.id, minutes=minutes, expires_at=expiry)
        return minted[0], expiry, admin_id
    raw, expiry, admin_id = _act(user_id, perform)
    audit.info('admin reset link minted user_id=%d admin_id=%d minutes=%d expires=%s', user_id, admin_id, minutes,
               expiry)
    response = jsonify(link=mailer.public_url('/reset-password', {'token': raw}), expires_at=expiry,
                       warning=RESET_WARNING)
    response.headers['Cache-Control'] = 'no-store'
    return response


@_route('/admin/users/<int:user_id>/sign-out', ['POST'], 'action')
def sign_out_route(user_id):
    _body()

    def perform(connection, target, admin_id):
        if credentials.account_state(target) == 'messaging':
            raise AdminError('That account has no browser sign-in', 409)
        # Also ends live reset links: they are bound to the session generation.
        connection.execute(update(User).where(User.id == target.id).values(session_version=User.session_version + 1))
        record(connection, 'sign_out_everywhere', admin_id=admin_id, target=target.id)
        return admin_id
    admin_id = _act(user_id, perform)
    audit.info('admin signed out user_id=%d admin_id=%d', user_id, admin_id)
    return jsonify(signed_out=True)


@_route('/admin/users/<int:user_id>/revoke-agents', ['POST'], 'action')
def revoke_agents_route(user_id):
    include_harness = _body().get('include_harness', False)
    if type(include_harness) is not bool:
        raise AdminError('include_harness must be true or false', 400)

    def perform(connection, target, admin_id):
        counts = credentials.revoke_agents(connection, target.id, harness=include_harness, via='admin')
        record(connection, 'revoke_agents', admin_id=admin_id, target=target.id, include_harness=include_harness,
               **counts)
        return counts, admin_id
    counts, admin_id = _act(user_id, perform)
    audit.info('admin revoked agents user_id=%d admin_id=%d', user_id, admin_id)
    return jsonify(counts)


@_route('/admin/users/<int:user_id>/verify', ['POST'], 'action')
def verify_route(user_id):
    payload = _body()
    if payload.get('confirm') != 'verify':
        raise AdminError('Confirm with {"confirm": "verify"}', 400)
    revoke, include_harness = payload.get('revoke_agents', False), payload.get('include_harness', False)
    if type(revoke) is not bool or type(include_harness) is not bool:
        raise AdminError('revoke_agents and include_harness must be true or false', 400)

    def perform(connection, target, admin_id):
        if credentials.account_state(target) != 'legacy_unverified' or mailer.deliverable(target.email) is None:
            raise AdminError('Only an unverified account with a deliverable address can be marked verified', 409)
        if not credentials.mark_verified(connection, target.id, target.email, via='admin'):
            raise AdminError('The account changed; reload and try again', 409)
        # After the verification can no longer fail, so the revocation's audit line never describes a rollback.
        counts = credentials.revoke_agents(connection, target.id, harness=include_harness, via='admin') if revoke else {}
        record(connection, 'mark_verified', admin_id=admin_id, target=target.id, revoke_agents=revoke,
               include_harness=revoke and include_harness, **counts)
        return counts, admin_id
    counts, admin_id = _act(user_id, perform)
    audit.info('admin marked verified user_id=%d admin_id=%d', user_id, admin_id)
    return jsonify(verified=True, revoked=counts or None)


@_route('/admin/audit', ['GET'], 'read')
def audit_route():
    raw = request.args.get('limit', '100')
    if not re.fullmatch(r'[0-9]{1,4}', raw) or not AUDIT_LIMIT[0] <= int(raw) <= AUDIT_LIMIT[1]:
        raise AdminError(f'limit must be between {AUDIT_LIMIT[0]} and {AUDIT_LIMIT[1]}', 400)
    with _engine().connect() as connection:
        rows = connection.execute(select(AdminAction.__table__).order_by(AdminAction.id.desc()).limit(int(raw)))
        return jsonify(actions=[_action_row(row) for row in rows])


# Setup and CLI -------------------------------------------------------------------
def init_app(app):
    seconds = app.config.setdefault('ADMIN_STEP_UP_SECONDS', int(os.getenv('ADMIN_STEP_UP_SECONDS', STEP_UP_SECONDS)))
    if type(seconds) is not int or not STEP_UP_RANGE[0] <= seconds <= STEP_UP_RANGE[1]:
        raise RuntimeError(f'ADMIN_STEP_UP_SECONDS must be between {STEP_UP_RANGE[0]} and {STEP_UP_RANGE[1]}')
    app.register_blueprint(admin, url_prefix='/api/v1')
    users = app.cli.commands.get('user')
    if users is None or 'set-admin' in users.commands:
        return

    @users.command('set-admin')
    @click.argument('email', required=False)
    @click.option('--id', 'user_id', type=int, help='The account ID, instead of its email.')
    @click.option('--revoke', is_flag=True, help='Remove administrator access instead.')
    def user_set_admin(email, user_id, revoke):
        """Grant (or --revoke) access to the admin page. Only verified accounts can be administrators."""
        if bool(email) == (user_id is not None):
            raise click.ClickException('Give an EMAIL or --id')
        credential = credentials.read(user_id) if user_id is not None else credentials.find(
            credentials.normalize_email(email) or '')
        if credential is None:
            raise click.ClickException('No account with that email or ID')
        if not revoke and credentials.account_state(credential) != 'verified':
            raise click.ClickException('Only a verified account can be an administrator')
        try:
            with _engine().begin() as connection:
                _begin(connection)
                where = [User.id == credential.user_id, User.is_admin == revoke]
                if not revoke:
                    where.append(User.email_verified_at.is_not(None))
                changed = connection.execute(update(User).where(*where).values(is_admin=not revoke)).rowcount
                if changed:
                    record(connection, 'revoke_admin' if revoke else 'grant_admin', actor='cli',
                           target=credential.user_id)
        except SQLAlchemyError:
            raise click.ClickException('Credential store unavailable') from None
        if not changed:
            click.echo(f'Account {credential.user_id} already {"has no" if revoke else "has"} administrator access.')
            return
        audit.info('admin %s user_id=%d operator=cli', 'revoked' if revoke else 'granted', credential.user_id)
        click.echo(f'Account {credential.user_id} {"no longer has" if revoke else "now has"} administrator access.')
