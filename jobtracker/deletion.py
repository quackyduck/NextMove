"""Real deletion and export (schema 17).

Self-service (browser session plus CSRF only, see `destructive`): a role with its contacts, notes, activity,
resumes and drafts; one job resume; one template version; a whole template. Agents of every kind (OAuth/MCP,
managed tokens, the legacy token, harness grants) can add and edit but never delete.

Operators: `flask user delete` removes an account and everything in it; `flask user export` builds a ZIP behind
a short-lived download link; `flask support code` issues the one-time code the identity procedure uses.

Every deletion writes a ledger row (`deletions`, ids only) inside its own transaction, so `flask deletions
replay` can bring a restored backup back to the deleted state. Rows go first, in one transaction; files are
unlinked after commit, and the guarded sweeper removes anything left behind.
"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import time
import zipfile

import click
from flask import Blueprint, current_app, g, jsonify, request, send_file
from sqlalchemy import delete, func, insert, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError

from . import credentials, mailer, services, storage, taskqueue, throttle
from .auth import require_browser_auth
from .models import (Activity, ActivationToken, AgentToken, Contact, DataExport, Deletion, EmailConfirmation,
                     ExtractionTask, HarnessGrant, IngestionRequest, InviteCode, Job, MasterResume, OAuthCode,
                     OAuthGrant, OAuthToken, OnboardingStep, PasswordResetRequest, PasswordResetToken, QueuedTask, Resume,
                     ResumeTemplate, SupportCode, TailoredDraft, User, new_id, now)

deletion = Blueprint('deletion', __name__)
audit = logging.getLogger('jobtracker.accounts')
log = logging.getLogger('jobtracker.deletion')

LEDGER_DAYS = 35
EXPORT_HOURS = 48
EXPORT_DOWNLOADS = 3
EXPORT_PREFIX = 'nme_'
EXPORTS_DIR = '_exports'
SUPPORT_CODE_HOURS = 72
SWEEP_AGE = timedelta(hours=1)
SWEEP_MAX_FILES = 50
SWEEP_MAX_DIRECTORIES = 1
# Kinds whose anonymous payload carries an address (account deletion removes them by address).
ADDRESS_TASK_KINDS = ('password_reset_request', 'resend_confirmation', 'account_deleted_email')
# Every table holding an account's rows, in the order account deletion empties them. A guard test fails when a
# table that references users (directly or through jobs, grants, templates or versions) is missing here.
ACCOUNT_TABLES = ('oauth_tokens', 'oauth_codes', 'oauth_grants', 'ingestion_requests', 'activities', 'contacts',
                  'tailored_drafts', 'resumes', 'master_resumes', 'resume_templates', 'jobs', 'extraction_tasks',
                  'agent_tokens', 'harness_grants', 'activation_tokens', 'onboarding_steps', 'email_confirmations',
                  'password_reset_tokens', 'password_reset_requests', 'data_exports', 'queued_tasks',
                  'invite_codes', 'users')
# Columns that name stored files, and where those files live. A guard test fails when one is missing.
FILE_COLUMNS = {('resumes', 'stored_filename'): '', ('master_resumes', 'stored_filename'): 'master',
                ('tailored_drafts', 'docx_filename'): 'drafts', ('tailored_drafts', 'pdf_filename'): 'drafts',
                ('data_exports', 'stored_name'): None}


def _engine():
    return current_app.extensions['jobtracker_db']['engine']


def _begin(connection):
    if connection.dialect.name == 'sqlite':
        connection.exec_driver_sql('BEGIN IMMEDIATE')


def _root():
    return Path(current_app.config['UPLOAD_FOLDER']).resolve()


def _file(user_id, folder, name):
    """The path of a stored file inside the owner's directory, or None for anything that would escape it."""
    if not name or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name) or name in ('.', '..'):
        return None
    directory = storage.user_directory(_root(), user_id) / folder if folder else storage.user_directory(_root(), user_id)
    target = (directory / name).resolve()
    return target if target.parent == directory.resolve() else None


def _unlink(paths):
    """Remove files after commit. A failure is counted, never named; the sweeper finishes the job."""
    removed = failed = 0
    for path in paths:
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            failed += 1
    if failed:
        log.warning('deletion left files for the sweeper count=%d', failed)
    return removed


def _ledger(connection, kind, user_id, object_id=None):
    connection.execute(insert(Deletion).values(id=new_id(), kind=kind, user_id=user_id, object_id=object_id,
                                               created_at=now()))


def checkpoint():
    """Fold the WAL into the database and truncate it, so deleted rows don't linger in -wal (with secure_delete)."""
    with _engine().connect() as connection:
        connection.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)')


def destructive(view):
    """Browser session plus CSRF only. Marked so the route-classification guard can find every destructive route."""
    wrapped = require_browser_auth(view)
    wrapped.destructive = True
    return wrapped


# Self-service: roles ------------------------------------------------------------
def _job_rows(connection, user_id, job_id):
    """Delete a role's rows inside the caller's transaction. Returns (counts, files to unlink after commit)."""
    files = [_file(user_id, '', name) for name in connection.execute(select(Resume.stored_filename).where(
        Resume.job_id == job_id, Resume.user_id == user_id)).scalars()]
    drafts = connection.execute(select(TailoredDraft.docx_filename, TailoredDraft.pdf_filename).where(
        TailoredDraft.job_id == job_id, TailoredDraft.user_id == user_id)).all()
    files += [_file(user_id, 'drafts', name) for row in drafts for name in row if name]
    counts = {}
    for name, model in (('activities', Activity), ('contacts', Contact), ('drafts', TailoredDraft),
                        ('resumes', Resume)):
        counts[name] = connection.execute(delete(model).where(model.job_id == job_id,
                                                              model.user_id == user_id)).rowcount
    connection.execute(update(QueuedTask).where(
        QueuedTask.user_id == user_id, QueuedTask.status == 'queued', QueuedTask.kind == 'harness_fill_job',
        func.json_extract(QueuedTask.payload, '$.job_id') == job_id).values(
        status='failed', last_error='Role deleted', updated_at=now()))
    # Keep the receipt so a replayed ingestion doesn't re-create the role, but forget the posting it named.
    connection.execute(update(IngestionRequest).where(
        IngestionRequest.user_id == user_id, func.json_extract(IngestionRequest.result, '$.job_id') == job_id).values(
        url='', result={'status': 'deleted'}))
    counts['jobs'] = connection.execute(delete(Job).where(Job.id == job_id, Job.user_id == user_id)).rowcount
    counts['files'] = sum(1 for path in files if path is not None)
    return counts, files


def job_preview(user_id, job_id):
    with _engine().connect() as connection:
        if connection.scalar(select(Job.id).where(Job.id == job_id, Job.user_id == user_id)) is None:
            raise services.ServiceError('Job not found', 404)
        count = lambda model: connection.scalar(select(func.count()).select_from(model).where(  # noqa: E731
            model.job_id == job_id, model.user_id == user_id))
        return {'contacts': count(Contact), 'activities': count(Activity), 'resumes': count(Resume),
                'drafts': count(TailoredDraft)}


def delete_job(user_id, job_id, version=None, *, via='ui'):
    """Delete a role and everything attached to it. `version` (optimistic) is required from the UI."""
    with _engine().begin() as connection:
        _begin(connection)
        current = connection.scalar(select(Job.version).where(Job.id == job_id, Job.user_id == user_id))
        if current is None:
            raise services.ServiceError('Job not found', 404)
        if via == 'ui':
            if type(version) is not int or version < 1:
                raise services.ServiceError('version must be a positive integer')
            if version != current:
                raise services.ServiceError('This job changed. Reload it before deleting.', 409)
        counts, files = _job_rows(connection, user_id, job_id)
        if via != 'replay':  # Replay re-inserts the exported rows themselves.
            _ledger(connection, 'job', user_id, job_id)
    _unlink(files)
    audit.info('job deleted user_id=%d job_id=%s resumes=%d drafts=%d contacts=%d activities=%d files=%d via=%s',
               user_id, job_id, counts['resumes'], counts['drafts'], counts['contacts'], counts['activities'],
               counts['files'], via)
    return counts


# Self-service: one job resume ---------------------------------------------------
def _sha256(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None
    except OSError:
        return None


def delete_resume(user_id, resume_id, *, via='ui'):
    with _engine().begin() as connection:
        _begin(connection)
        row = connection.execute(select(Resume.__table__).where(Resume.id == resume_id, Resume.user_id == user_id)
                                 ).first()
        if row is None:
            raise services.ServiceError('Resume not found', 404)
        files = [_file(user_id, '', row.stored_filename)]
        # Drafts approved before approval dropped its copies still hold the same bytes in drafts/: remove that copy.
        drafts = 0
        for draft in connection.execute(select(TailoredDraft.__table__).where(
                TailoredDraft.job_id == row.job_id, TailoredDraft.user_id == user_id,
                TailoredDraft.approved_at.is_not(None))).all():
            copies = [_file(user_id, 'drafts', name) for name in (draft.docx_filename, draft.pdf_filename) if name]
            if row.sha256 in {_sha256(path) for path in copies}:
                connection.execute(delete(TailoredDraft).where(TailoredDraft.id == draft.id))
                files += copies
                drafts += 1
        connection.execute(delete(Resume).where(Resume.id == resume_id, Resume.user_id == user_id))
        if via != 'replay':
            _ledger(connection, 'resume', user_id, resume_id)
    _unlink(files)
    audit.info('resume deleted user_id=%d resume_id=%s drafts=%d files=%d via=%s', user_id, resume_id, drafts,
               sum(1 for path in files if path), via)
    return {'resumes': 1, 'drafts': drafts}


# Self-service: template versions and templates ----------------------------------
def _version_rows(connection, user_id, version_ids):
    """Unlink tailored resumes, delete drafts sourced from these versions, delete the versions. (counts, files)."""
    if not version_ids:
        return {'versions': 0, 'unlinked': 0, 'drafts': 0}, []
    jobs = connection.execute(select(func.distinct(Resume.job_id)).where(
        Resume.user_id == user_id, Resume.source_master_resume_id.in_(version_ids))).scalars().all()
    unlinked = connection.execute(update(Resume).where(
        Resume.user_id == user_id, Resume.source_master_resume_id.in_(version_ids)).values(
        source_master_resume_id=None)).rowcount
    for job_id in jobs:  # The resume stays; its history says why it no longer names a template version.
        connection.execute(insert(Activity).values(
            id=new_id(), user_id=user_id, job_id=job_id, kind='template_version_deleted',
            message='A template version a resume here was tailored from was deleted; the resume is unchanged.',
            details={}, created_at=now()))
    drafts = connection.execute(select(TailoredDraft.id, TailoredDraft.docx_filename, TailoredDraft.pdf_filename)
                                .where(TailoredDraft.user_id == user_id,
                                       TailoredDraft.master_resume_id.in_(version_ids))).all()
    files = [_file(user_id, 'drafts', name) for row in drafts for name in (row.docx_filename, row.pdf_filename)
             if name]
    if drafts:
        connection.execute(delete(TailoredDraft).where(TailoredDraft.id.in_([row.id for row in drafts])))
    files += [_file(user_id, 'master', name) for name in connection.execute(select(MasterResume.stored_filename)
              .where(MasterResume.user_id == user_id, MasterResume.id.in_(version_ids))).scalars()]
    versions = connection.execute(delete(MasterResume).where(
        MasterResume.user_id == user_id, MasterResume.id.in_(version_ids))).rowcount
    return {'versions': versions, 'unlinked': unlinked, 'drafts': len(drafts)}, files


def _template(connection, user_id, template_id):
    row = connection.execute(select(ResumeTemplate.__table__).where(
        ResumeTemplate.id == template_id, ResumeTemplate.user_id == user_id)).first() \
        if isinstance(template_id, str) else None
    if row is None:
        raise services.ServiceError('Template not found', 404)
    return row


def _others_with_versions(connection, user_id, template_id):
    return connection.scalar(select(func.count(func.distinct(MasterResume.template_id))).where(
        MasterResume.user_id == user_id, MasterResume.template_id != template_id))


def template_preview(user_id, template_id):
    with _engine().connect() as connection:
        template = _template(connection, user_id, template_id)
        ids = connection.execute(select(MasterResume.id).where(MasterResume.template_id == template_id,
                                                                MasterResume.user_id == user_id)).scalars().all()
        others = connection.scalar(select(func.count()).select_from(ResumeTemplate).where(
            ResumeTemplate.user_id == user_id, ResumeTemplate.id != template_id))
        unlinked = connection.scalar(select(func.count()).select_from(Resume).where(
            Resume.user_id == user_id, Resume.source_master_resume_id.in_(ids))) if ids else 0
        drafts = connection.scalar(select(func.count()).select_from(TailoredDraft).where(
            TailoredDraft.user_id == user_id, TailoredDraft.master_resume_id.in_(ids))) if ids else 0
    return {'name': template.name, 'versions': len(ids), 'tailored_resumes_unlinked': unlinked, 'drafts': drafts,
            'is_default': bool(template.is_default), 'other_templates': others,
            'deletable': not (template.is_default and others)}


def delete_version(user_id, template_id, version_id, *, via='ui'):
    with _engine().begin() as connection:
        _begin(connection)
        template = _template(connection, user_id, template_id)
        # The version must belong to this template of this account: anything else is "not found".
        version = connection.scalar(select(MasterResume.id).where(
            MasterResume.id == version_id, MasterResume.template_id == template_id,
            MasterResume.user_id == user_id)) if isinstance(version_id, str) else None
        if version is None:
            raise services.ServiceError('Version not found', 404)
        remaining = connection.scalar(select(func.count()).select_from(MasterResume).where(
            MasterResume.template_id == template_id, MasterResume.user_id == user_id))
        if template.is_default and remaining == 1 and _others_with_versions(connection, user_id, template_id):
            raise services.ServiceError('This is the only version of your default template. Make another template '
                                        'the default first.', 409)
        counts, files = _version_rows(connection, user_id, [version_id])
        connection.execute(update(ResumeTemplate).where(ResumeTemplate.id == template_id).values(updated_at=now()))
        _ledger(connection, 'master_resume', user_id, version_id)
    _unlink(files)
    audit.info('template version deleted user_id=%d template_id=%s version_id=%s unlinked=%d drafts=%d files=%d '
               'via=%s', user_id, template_id, version_id, counts['unlinked'], counts['drafts'],
               sum(1 for path in files if path), via)
    return counts


def delete_template(user_id, template_id, confirm_name=None, *, via='ui'):
    with _engine().begin() as connection:
        _begin(connection)
        template = _template(connection, user_id, template_id)
        if via == 'ui' and (not isinstance(confirm_name, str) or confirm_name.strip() != template.name):
            raise services.ServiceError('Type the template name to confirm')
        others = connection.scalar(select(func.count()).select_from(ResumeTemplate).where(
            ResumeTemplate.user_id == user_id, ResumeTemplate.id != template_id))
        if via == 'ui' and template.is_default and others:
            raise services.ServiceError('Make another template the default first.', 409)
        ids = connection.execute(select(MasterResume.id).where(MasterResume.template_id == template_id,
                                                                MasterResume.user_id == user_id)).scalars().all()
        counts, files = _version_rows(connection, user_id, ids)
        connection.execute(delete(ResumeTemplate).where(ResumeTemplate.id == template_id,
                                                        ResumeTemplate.user_id == user_id))
        if via != 'replay':
            _ledger(connection, 'template', user_id, template_id)
    _unlink(files)
    audit.info('template deleted user_id=%d template_id=%s versions=%d unlinked=%d drafts=%d files=%d via=%s',
               user_id, template_id, counts['versions'], counts['unlinked'], counts['drafts'],
               sum(1 for path in files if path), via)
    return counts


# Accounts -----------------------------------------------------------------------
def _account_filters(user_id, email, grants):
    """{table: where-clause} for every row of the account, in ACCOUNT_TABLES order."""
    by_user = lambda model: model.user_id == user_id  # noqa: E731
    filters = {
        'oauth_tokens': OAuthToken.grant_id.in_(grants), 'oauth_codes': OAuthCode.grant_id.in_(grants),
        'oauth_grants': by_user(OAuthGrant), 'ingestion_requests': by_user(IngestionRequest),
        'activities': by_user(Activity), 'contacts': by_user(Contact), 'tailored_drafts': by_user(TailoredDraft),
        'resumes': by_user(Resume), 'master_resumes': by_user(MasterResume),
        'resume_templates': by_user(ResumeTemplate), 'jobs': by_user(Job), 'extraction_tasks': by_user(ExtractionTask),
        'agent_tokens': by_user(AgentToken), 'harness_grants': by_user(HarnessGrant),
        'activation_tokens': by_user(ActivationToken),
        'email_confirmations': or_(EmailConfirmation.user_id == user_id, EmailConfirmation.email == email)
        if email else EmailConfirmation.user_id == user_id,
        'password_reset_tokens': by_user(PasswordResetToken), 'password_reset_requests': by_user(PasswordResetRequest),
        'data_exports': by_user(DataExport),
        'queued_tasks': or_(QueuedTask.user_id == user_id, (QueuedTask.user_id.is_(None) & QueuedTask.kind.in_(
            ADDRESS_TASK_KINDS) & (func.json_extract(QueuedTask.payload, '$.email') == email)))
        if email else QueuedTask.user_id == user_id,
    }
    filters['onboarding_steps'] = OnboardingStep.user_id == user_id
    return filters


def _models():
    from .db import Base
    return {table.name: table for table in Base.metadata.sorted_tables}


def account_summary(user_id):
    """Counts per table for the operator's confirmation screen (no content)."""
    with _engine().connect() as connection:
        user = connection.execute(select(User.__table__).where(User.id == user_id)).first()
        if user is None:
            return None
        grants = connection.execute(select(OAuthGrant.id).where(OAuthGrant.user_id == user_id)).scalars().all()
        tables = _models()
        counts = {name: connection.scalar(select(func.count()).select_from(tables[name]).where(clause))
                  for name, clause in _account_filters(user_id, user.email, grants).items()}
    files = sum(1 for path in storage.user_directory(_root(), user_id).rglob('*') if path.is_file()) \
        if storage.user_directory(_root(), user_id).exists() else 0
    return {'user': user, 'state': credentials.account_state(user), 'counts': counts, 'files': files}


class CodeRejected(Exception):
    """The support code doesn't authorize this action for this account (unknown, used, expired, other account)."""


def delete_account(user_id, *, code=None, notify=True, via='cli'):
    """Delete the account and everything in it, in one transaction; files after commit. Returns a summary or None.

    code: the support code issued for (user_id, 'delete'); required unless replaying, and consumed in this same
    transaction, so a failed deletion leaves it usable and a code can never authorize two deletions.
    notify: queue the "account deleted" email (verified accounts only; never during a ledger replay).
    """
    with _engine().begin() as connection:
        _begin(connection)
        user = connection.execute(select(User.__table__).where(User.id == user_id)).first()
        if user is None:
            return None
        if via != 'replay' and not _consume_code(connection, code, user_id, 'delete'):
            raise CodeRejected()
        email = user.email
        grants = connection.execute(select(OAuthGrant.id).where(OAuthGrant.user_id == user_id)).scalars().all()
        exports = [name for name in connection.execute(select(DataExport.stored_name).where(
            DataExport.user_id == user_id)).scalars()]
        tables = _models()
        rows = 0
        for name, clause in _account_filters(user_id, email, grants).items():
            rows += connection.execute(delete(tables[name]).where(clause)).rowcount
        rows += connection.execute(update(InviteCode).where(InviteCode.used_by == user_id).values(used_by=None)).rowcount
        rows += connection.execute(delete(User).where(User.id == user_id)).rowcount
        connection.execute(delete(SupportCode).where(SupportCode.user_id == user_id, SupportCode.used_at.is_(None)))
        if via != 'replay':
            _ledger(connection, 'account', user_id)
        verified = credentials.account_state(user) == 'verified'
        sent = bool(notify and verified and email and mailer.enabled() and mailer.deliverable(email))
        if sent:
            taskqueue.enqueue(connection, None, 'account_deleted_email', {'email': email})
    directory = storage.user_directory(_root(), user_id)
    files = sum(1 for path in directory.rglob('*') if path.is_file()) if directory.exists() else 0
    shutil.rmtree(directory, ignore_errors=True)
    _unlink([_export_path(name) for name in exports])
    try:
        checkpoint()
    except SQLAlchemyError:
        log.warning('wal checkpoint after account deletion failed')
    audit.info('account deleted user_id=%d rows=%d files=%d operator=%s', user_id, rows, files, via)
    return {'rows': rows, 'files': files, 'email_sent': sent, 'verified': verified}


@taskqueue.handler('account_deleted_email', anonymous=True, delete_on_success=True, max_attempts=5, lease_seconds=60)
def run_account_deleted_email(app, task):
    """A pure send: no account lookups, nothing that could create rows for the address, no links that act."""
    email = task.payload.get('email')
    if not email or mailer.deliverable(email) is None:
        return 'skipped'
    try:
        if mailer.recipient_ok(email, 'notice').status != 'ok':
            return 'skipped'
    except mailer.TransientMailError as exc:
        raise taskqueue.Retry('DNS lookup failed', delay=60) from exc
    support = (current_app.config.get('MAIL_REPLY_TO') or '').strip()
    lines = [f'Your My Next Moov account and everything in it were deleted on {now().strftime("%Y-%m-%d")} (UTC).',
             'Backups are replaced within 4 weeks; until then, a backup can still contain this data.', '',
             f'Questions? Reply to this email{" or write to " + support if support else ""}.']
    text_body = '\n'.join(lines) + '\n'
    try:
        mailer.send_email(email, 'Your My Next Moov account was deleted', text_body, category='notice')
    except mailer.MailQuotaExceeded as exc:
        raise taskqueue.Retry('Mail quota spent', delay=exc.delay, count_attempt=False) from exc
    except mailer.TransientMailError as exc:
        raise taskqueue.Retry('Mail provider unavailable', delay=60) from exc
    except mailer.PermanentMailError:
        return 'failed'
    return 'sent'


# Export ---------------------------------------------------------------------------
def _export_path(stored_name):
    if not stored_name or not re.fullmatch(r'[a-f0-9]{32}\.zip', stored_name):
        return None
    return _root() / EXPORTS_DIR / stored_name


def _value(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat().replace('+00:00', 'Z')
    return value.isoformat() if isinstance(value, date) else value


def _rows(connection, model, *clauses):
    columns = [column for column in model.__table__.columns if column.name != 'user_id']
    return [{column.name: _value(getattr(row, column.name)) for column in columns}
            for row in connection.execute(select(model.__table__).where(*clauses)).all()]


def _entry(original):
    """A safe archive entry name from a user-controlled file name: no directories, no '..', nothing absolute."""
    name = Path(str(original or 'file').replace('\\', '/')).name
    name = re.sub(r'[^\w. -]', '_', name).replace('..', '_').strip(' .') or 'file'
    return name[:120]


def build_export(user_id, code):
    """Write the account's ZIP and return (raw token, expires_at, files, bytes). Hashes and secrets are left out.

    code: the support code issued for (user_id, 'export'), consumed together with the export it authorizes.
    """
    if code_status(code, user_id, 'export') != 'ok':
        raise CodeRejected()
    secret_columns = {'password_hash', 'token_hash', 'code_hash', 'prefix'}
    with _engine().connect() as connection:
        user = connection.execute(select(User.__table__).where(User.id == user_id)).first()
        if user is None:
            return None
        data = {
            'account': {'id': user.id, 'email': user.email, 'created_at': _value(user.created_at),
                        'email_verified_at': _value(user.email_verified_at)},
            'roles': _rows(connection, Job, Job.user_id == user_id),
            'contacts': _rows(connection, Contact, Contact.user_id == user_id),
            'activities': _rows(connection, Activity, Activity.user_id == user_id),
            'resumes': _rows(connection, Resume, Resume.user_id == user_id),
            'templates': _rows(connection, ResumeTemplate, ResumeTemplate.user_id == user_id),
            'template_versions': _rows(connection, MasterResume, MasterResume.user_id == user_id),
            'tailored_drafts': _rows(connection, TailoredDraft, TailoredDraft.user_id == user_id),
            'agent_tokens': [{k: v for k, v in row.items() if k not in secret_columns}
                             for row in _rows(connection, AgentToken, AgentToken.user_id == user_id)],
        }
        files = [(f'resumes/{r["id"]}-{_entry(r["original_filename"])}', _file(user_id, '', r['stored_filename']))
                 for r in data['resumes']]
        files += [(f'templates/{r["template_id"]}/v{r["version"]}-{_entry(r["original_filename"])}',
                   _file(user_id, 'master', r['stored_filename'])) for r in data['template_versions']]
    folder = _root() / EXPORTS_DIR
    folder.mkdir(mode=0o700, exist_ok=True)
    stored = secrets.token_hex(16) + '.zip'
    target = folder / stored
    count = 0
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('account.json', json.dumps(data, indent=2, ensure_ascii=False))
        for name, path in files:
            if path is not None and path.is_file():
                archive.write(path, name)
                count += 1
    raw = EXPORT_PREFIX + secrets.token_urlsafe(32)
    expires = now() + timedelta(hours=EXPORT_HOURS)
    size = target.stat().st_size
    with _engine().begin() as connection:
        _begin(connection)
        if not _consume_code(connection, code, user_id, 'export'):
            target.unlink(missing_ok=True)
            raise CodeRejected()
        connection.execute(insert(DataExport).values(
            id=new_id(), user_id=user_id, token_hash=hashlib.sha256(raw.encode()).hexdigest(), stored_name=stored,
            size=size, created_at=now(), expires_at=expires, downloads=0, last_download_at=None))
    audit.info('export created user_id=%d files=%d bytes=%d operator=cli', user_id, count, size)
    return raw, expires, count, size


EXPORT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Your My Next Moov data</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 16px;line-height:1.5;color:#1f2328;background:#fff}}
button{{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:8px;background:#1f6feb;color:#fff;cursor:pointer}}
@media (prefers-color-scheme: dark){{body{{background:#0d1117;color:#e6edf3}}}}</style></head>
<body><h1>Your My Next Moov data</h1>{body}</body></html>"""
EXPORT_GONE = ('<p>This download link has expired or was already used. Reply to the support email thread to ask '
               'for a new one.</p>')


def _export_page(body, status=200):
    response = current_app.response_class(EXPORT_PAGE.format(body=body), status=status, mimetype='text/html')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Robots-Tag'] = 'noindex'  # Referrer-Policy: same-origin (app-wide); the page loads nothing else.
    return response


def _export_budget():
    try:
        return throttle.reserve([(throttle.key('export-ip', throttle.client_address()), 20)])
    except throttle.Unavailable:
        return None


def _export_digest(token):
    if not isinstance(token, str) or not token.startswith(EXPORT_PREFIX) or not 20 <= len(token) <= 128:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def _live_export(connection, digest):
    return connection.execute(select(DataExport.__table__).where(
        DataExport.token_hash == digest, DataExport.expires_at > now(),
        DataExport.downloads < EXPORT_DOWNLOADS)).first()


@deletion.get('/exports/<token>')
def export_page(token):
    """The emailed link: a page with a Download button. Opening it (or a mail scanner fetching it) uses up nothing."""
    allowed = _export_budget()
    if allowed is None:
        return _export_page('<p>Temporarily unavailable; try again later.</p>', 503)
    if not allowed:
        return _export_page('<p>Too many attempts; try again later.</p>', 429)
    digest = _export_digest(token)
    with _engine().connect() as connection:
        row = _live_export(connection, digest) if digest else None
    if row is None:
        return _export_page(EXPORT_GONE, 404)
    left = EXPORT_DOWNLOADS - row.downloads
    expires = row.expires_at.replace(tzinfo=row.expires_at.tzinfo or timezone.utc).strftime('%Y-%m-%d %H:%M')
    return _export_page(
        '<p>This ZIP file holds everything in your account: roles, notes, contacts, history, resumes and templates. '
        f'The link works {left} more time{"s" if left != 1 else ""}, until {expires} UTC.</p>'
        '<form method="post"><button type="submit">Download my data (ZIP)</button></form>')


@deletion.post('/exports/<token>')
def export_download(token):
    """The download itself: same-origin POST from the page above; each one counts toward the limit."""
    from .auth import same_origin
    if not same_origin():
        return _export_page('<p>Open the link from your email and use its Download button.</p>', 403)
    allowed = _export_budget()
    if allowed is None:
        return _export_page('<p>Temporarily unavailable; try again later.</p>', 503)
    if not allowed:
        return _export_page('<p>Too many attempts; try again later.</p>', 429)
    digest = _export_digest(token)
    if digest is None:
        return _export_page(EXPORT_GONE, 404)
    with _engine().begin() as connection:
        _begin(connection)
        row = _live_export(connection, digest)
        if row is None:
            return _export_page(EXPORT_GONE, 404)
        connection.execute(update(DataExport).where(DataExport.id == row.id).values(
            downloads=DataExport.downloads + 1, last_download_at=now()))
    path = _export_path(row.stored_name)
    if path is None or not path.is_file():
        return _export_page(EXPORT_GONE, 404)
    audit.info('export downloaded user_id=%d n=%d', row.user_id, row.downloads + 1)
    response = send_file(path, mimetype='application/zip', as_attachment=True,
                         download_name='my-next-moov-export.zip', max_age=0)
    response.headers['Cache-Control'] = 'no-store'
    return response


# Ledger -----------------------------------------------------------------------------
def prune_ledger(connection):
    return connection.execute(delete(Deletion).where(Deletion.created_at < now() - timedelta(days=LEDGER_DAYS))).rowcount


def export_ledger():
    with _engine().connect() as connection:
        return [{'id': row.id, 'kind': row.kind, 'user_id': row.user_id, 'object_id': row.object_id,
                 'created_at': _value(row.created_at)}
                for row in connection.execute(select(Deletion.__table__).order_by(Deletion.created_at))]


def _newest_write(connection):
    values = [connection.scalar(select(func.max(column))) for column in
              (User.created_at, Job.updated_at, Activity.created_at, Deletion.created_at)]
    values = [value.replace(tzinfo=value.tzinfo or timezone.utc) for value in values if value is not None]
    return max(values) if values else None


def _parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def replay(entries, *, force=False):
    """Re-apply ledger entries to a restored database. Never sends mail. Returns (applied, skipped)."""
    if not entries:
        return 0, 0
    with _engine().connect() as connection:
        newest = _newest_write(connection)
    # A backup older than the ledger window may predate deletions the ledger has already pruned: refuse it.
    if newest is not None and newest < now() - timedelta(days=LEDGER_DAYS) and not force:
        raise click.ClickException(f'The restored database is older than the {LEDGER_DAYS}-day ledger window, so '
                                   'some deletions can no longer be replayed. Do not restore backups that old '
                                   '(use --force only if you are sure).')
    applied = skipped = 0
    actions = {
        'account': lambda e: delete_account(e['user_id'], notify=False, via='replay'),
        'job': lambda e: delete_job(e['user_id'], e['object_id'], via='replay'),
        'resume': lambda e: delete_resume(e['user_id'], e['object_id'], via='replay'),
        'master_resume': lambda e: _replay_version(e['user_id'], e['object_id']),
        'template': lambda e: delete_template(e['user_id'], e['object_id'], via='replay'),
    }
    for entry in sorted(entries, key=lambda item: item['created_at']):
        try:
            result = actions[entry['kind']](entry)
            applied += result is not None
            skipped += result is None
        except services.ServiceError:
            skipped += 1  # Already gone from the restored copy.
    with _engine().begin() as connection:
        known = set(connection.execute(select(Deletion.id)).scalars())
        for entry in entries:
            if entry['id'] not in known:
                connection.execute(insert(Deletion).values(
                    id=entry['id'], kind=entry['kind'], user_id=entry['user_id'], object_id=entry['object_id'],
                    created_at=_parse_time(entry['created_at'])))
    audit.info('deletions replay applied=%d skipped=%d', applied, skipped)
    return applied, skipped


def _replay_version(user_id, version_id):
    with _engine().connect() as connection:
        template_id = connection.scalar(select(MasterResume.template_id).where(
            MasterResume.id == version_id, MasterResume.user_id == user_id))
    if template_id is None:
        return None
    with _engine().begin() as connection:
        _begin(connection)
        counts, files = _version_rows(connection, user_id, [version_id])
    _unlink(files)
    return counts


# Sweeper ------------------------------------------------------------------------------
class SweepRefused(Exception):
    pass


def sweep(*, apply=False, force=False):
    """Find (and with apply, remove) orphan files and the directories of accounts the ledger says were deleted.

    Refuses on an unhealthy database or an unusually large pass unless `force`. Never removes a directory the
    ledger doesn't name, and never a file younger than an hour (an upload writes its file before its row).
    """
    from .migrations import SCHEMA_VERSION
    root = _root()
    with _engine().connect() as connection:
        version = connection.scalar(text('SELECT version FROM schema_version'))
        users = set(connection.execute(select(User.id)).scalars())
        deleted = set(connection.execute(select(Deletion.user_id).where(Deletion.kind == 'account')).scalars())
        referenced = {}
        for (table, column), folder in FILE_COLUMNS.items():
            if folder is None:
                continue
            model = _models()[table]
            for owner, name in connection.execute(select(model.c.user_id, model.c[column])):
                if name:
                    referenced.setdefault(owner, set()).add((folder, name))
        exports = connection.execute(select(DataExport.id, DataExport.stored_name).where(
            DataExport.expires_at <= now())).all()
        live_exports = set(connection.execute(select(DataExport.stored_name).where(
            DataExport.expires_at > now())).scalars())
    if version != SCHEMA_VERSION or not users:
        log.error('storage sweep refused: database not ready (schema=%s users=%d)', version, len(users))
        raise SweepRefused('The database is not current or has no accounts')
    cutoff = time.time() - SWEEP_AGE.total_seconds()
    orphans, directories, unknown = [], [], 0
    for directory in (path for path in root.iterdir() if path.is_dir() and path.name.isdigit()):
        owner = int(directory.name)
        if owner not in users:
            if owner in deleted:
                directories.append(directory)
            else:
                unknown += 1
            continue
        names = referenced.get(owner, set())
        for folder in ('', 'master', 'drafts'):
            base = directory / folder if folder else directory
            if not base.is_dir():
                continue
            for path in base.iterdir():
                if path.is_file() and (folder, path.name) not in names and path.stat().st_mtime < cutoff:
                    orphans.append(path)
    export_dir = root / EXPORTS_DIR
    stale_exports = [path for path in export_dir.iterdir() if path.is_file() and path.name not in live_exports
                     and path.stat().st_mtime < cutoff] if export_dir.is_dir() else []
    files = len(orphans) + len(stale_exports)  # Expired exports count toward the same per-pass bound.
    if (files > SWEEP_MAX_FILES or len(directories) > SWEEP_MAX_DIRECTORIES) and not force:
        log.error('storage sweep refused: too large files=%d directories=%d', files, len(directories))
        raise SweepRefused(f'{files} files and {len(directories)} directories: run with --force if expected')
    if apply:
        _unlink(orphans + stale_exports)
        for directory in directories:
            shutil.rmtree(directory, ignore_errors=True)
        if exports:
            with _engine().begin() as connection:
                connection.execute(delete(DataExport).where(DataExport.id.in_([row.id for row in exports])))
    log.info('storage sweep orphans=%d directories=%d unknown_directories=%d exports=%d applied=%s', len(orphans),
             len(directories), unknown, len(stale_exports), apply)
    return {'orphans': len(orphans), 'directories': len(directories), 'unknown_directories': unknown,
            'exports': len(stale_exports)}


def maintenance(app):
    """Worker maintenance: prune the ledger and support codes, sweep (bounded), checkpoint the WAL."""
    with app.app_context():
        with _engine().begin() as connection:
            prune_ledger(connection)
            connection.execute(delete(SupportCode).where(SupportCode.expires_at < now() - timedelta(days=1)))
        try:
            sweep(apply=True)
        except SweepRefused:
            pass
        checkpoint()


# Support codes ------------------------------------------------------------------------
def _code_digest(code):
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def issue_support_code(user_id, purpose):
    """A one-time code for one account and one purpose ('delete' or 'export'), valid 72 hours."""
    if purpose not in ('delete', 'export'):
        raise ValueError('purpose must be delete or export')
    alphabet = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
    code = 'MNM-' + '-'.join(''.join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
    with _engine().begin() as connection:
        connection.execute(insert(SupportCode).values(
            id=new_id(), code_hash=_code_digest(code), user_id=user_id, purpose=purpose, created_at=now(),
            expires_at=now() + timedelta(hours=SUPPORT_CODE_HOURS)))
    return code


def _well_formed(code):
    return isinstance(code, str) and re.fullmatch(r'MNM(-[A-Z0-9]{4}){3}', code.strip().upper()) is not None


def code_status(code, user_id=None, purpose=None):
    """'ok', 'used', 'expired', 'mismatch' (another account or purpose) or 'unknown'. Never consumes."""
    if not _well_formed(code):
        return 'unknown'
    with _engine().connect() as connection:
        row = connection.execute(select(SupportCode.__table__).where(
            SupportCode.code_hash == _code_digest(code))).first()
    if row is None:
        return 'unknown'
    if row.used_at is not None:
        return 'used'
    if row.expires_at.replace(tzinfo=row.expires_at.tzinfo or timezone.utc) <= now():
        return 'expired'
    if (user_id is not None and row.user_id != user_id) or (purpose is not None and row.purpose != purpose):
        return 'mismatch'
    return 'ok'


def _consume_code(connection, code, user_id, purpose):
    """Use up a live code bound to (user_id, purpose), inside the caller's transaction. True when it did."""
    if not _well_formed(code):
        return False
    return connection.execute(update(SupportCode).where(
        SupportCode.code_hash == _code_digest(code), SupportCode.user_id == user_id, SupportCode.purpose == purpose,
        SupportCode.used_at.is_(None), SupportCode.expires_at > now()).values(used_at=now())).rowcount == 1


# Routes -------------------------------------------------------------------------------
@deletion.errorhandler(services.ServiceError)
def service_error(exc):
    return jsonify(error=str(exc)), exc.status_code


def _body():
    value = request.get_json(silent=True)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise services.ServiceError('A JSON object is required', 400)
    return value


@deletion.get('/jobs/<job_id>/deletion')
@destructive
def job_deletion_preview(job_id):
    return jsonify(job_preview(g.user_id, job_id))


@deletion.delete('/jobs/<job_id>')
@destructive
def job_delete(job_id):
    return jsonify(deleted=delete_job(g.user_id, job_id, _body().get('version')))


@deletion.delete('/resumes/<resume_id>')
@destructive
def resume_delete(resume_id):
    return jsonify(deleted=delete_resume(g.user_id, resume_id))


@deletion.get('/templates/<template_id>/deletion')
@destructive
def template_deletion_preview(template_id):
    return jsonify(template_preview(g.user_id, template_id))


@deletion.delete('/templates/<template_id>')
@destructive
def template_delete(template_id):
    return jsonify(deleted=delete_template(g.user_id, template_id, _body().get('confirm_name')))


@deletion.delete('/templates/<template_id>/versions/<version_id>')
@destructive
def template_version_delete(template_id, version_id):
    return jsonify(deleted=delete_version(g.user_id, template_id, version_id))


# CLI ----------------------------------------------------------------------------------
def init_app(app):
    app.register_blueprint(deletion, url_prefix='/api/v1')
    users = app.cli.commands.get('user')
    if users is None or 'delete' in users.commands:
        return

    def resolve(email, user_id, yes):
        if bool(email) == (user_id is not None):
            raise click.ClickException('Give an EMAIL or --id')
        if yes and user_id is None:
            raise click.ClickException('--yes needs --id, so a typo in an email can never pick another account')
        credential = credentials.read(user_id) if user_id is not None else credentials.find(
            credentials.normalize_email(email) or '')
        return credential

    def require_code(code, user_id, purpose):
        if not code:
            raise click.ClickException(f'Give --code: the support code issued with `flask support code --id {user_id} '
                                       f'--for {purpose}` and returned by the requester')
        state = code_status(code, user_id, purpose)
        if state != 'ok':
            reason = {'used': 'already used', 'expired': 'expired', 'unknown': 'unknown',
                      'mismatch': f'issued for another account or purpose, not {purpose} of account {user_id}'}[state]
            raise click.ClickException(f'Support code {reason}; nothing was done')

    def owner_guard(credential, allow_owner):
        with _engine().connect() as connection:
            legacy = connection.scalar(select(User.owns_legacy_tokens).where(User.id == credential.user_id))
        bootstrap = credentials.normalize_email(current_app.config.get('BOOTSTRAP_ADMIN_EMAIL') or '')
        if (legacy or (bootstrap and credential.email == bootstrap)) and not allow_owner:
            raise click.ClickException('That is the workspace owner account; add --allow-owner if you really mean it')

    @users.command('delete')
    @click.argument('email', required=False)
    @click.option('--id', 'user_id', type=int)
    @click.option('--yes', is_flag=True, help='Skip the typed confirmation (needs --id).')
    @click.option('--allow-owner', is_flag=True, help='Allow deleting the workspace owner account.')
    @click.option('--pending-only', is_flag=True, help='Only delete pending sign-ups for EMAIL (not an account).')
    @click.option('--code', help='The support code issued for this account and "delete" (`flask support code`).')
    def user_delete(email, user_id, yes, allow_owner, pending_only, code):
        """Delete an account and everything in it (after the identity check in OPERATIONS.md)."""
        if pending_only:
            normalized = credentials.normalize_email(email or '')
            if not normalized:
                raise click.ClickException('Give the EMAIL of the pending sign-up')
            with _engine().begin() as connection:
                removed = connection.execute(delete(EmailConfirmation).where(
                    EmailConfirmation.email == normalized, EmailConfirmation.user_id.is_(None))).rowcount
            click.echo(f'Deleted {removed} pending sign-up(s).')
            return
        credential = resolve(email, user_id, yes)
        if credential is None:
            target = user_id
            with _engine().connect() as connection:
                done = target is not None and connection.scalar(select(Deletion.id).where(
                    Deletion.kind == 'account', Deletion.user_id == target)) is not None
            if done:
                click.echo(f'Account {target} was already deleted.')
                return
            raise click.ClickException('No account with that email or ID (a pending sign-up is not an account; '
                                       'see --pending-only)')
        owner_guard(credential, allow_owner)
        require_code(code, credential.user_id, 'delete')  # --yes never waives it.
        summary = account_summary(credential.user_id)
        click.echo(f'Account {credential.user_id} ({mailer.mask_address(credential.email or "") or "no email"}), '
                   f'{summary["state"]}, created {credential.created_at}:')
        for name, count in summary['counts'].items():
            if count:
                click.echo(f'  {name}: {count}')
        click.echo(f'  files: {summary["files"]}')
        if not yes:
            typed = click.prompt('Type the account ID to delete it permanently', default='', show_default=False)
            if typed.strip() != str(credential.user_id):
                raise click.ClickException('Not confirmed; nothing was deleted')
        try:
            result = delete_account(credential.user_id, code=code)
        except CodeRejected:
            raise click.ClickException('The support code no longer authorizes this; nothing was deleted') from None
        click.echo(f'Deleted account {credential.user_id}: {result["rows"]} rows and {result["files"]} files.')
        if result['email_sent']:
            click.echo('The "account deleted" email is queued.')
        else:
            click.echo('No email sent' + (' (address unverified): reply from the request thread instead.'
                                          if not result['verified'] else '.'))
        click.echo('Now remove its chat data, in the gateway container:\n'
                   f'  resume-agent tenant delete --jobtracker-user {credential.user_id}\n'
                   'and the conversation in Messages on the bridge Mac (OPERATIONS.md, "Deletion and export requests").')

    @users.command('export')
    @click.argument('email', required=False)
    @click.option('--id', 'user_id', type=int)
    @click.option('--code', help='The support code issued for this account and "export" (`flask support code`).')
    def user_export(email, user_id, code):
        """Build the account's data export and print a 48-hour download link (after the identity check)."""
        credential = resolve(email, user_id, False)
        if credential is None:
            raise click.ClickException('No account with that email or ID')
        try:
            base = mailer.public_url('/')
        except mailer.MailNotConfigured:
            raise click.ClickException('PUBLIC_BASE_URL is not configured') from None
        require_code(code, credential.user_id, 'export')
        try:
            raw, expires, count, size = build_export(credential.user_id, code)
        except CodeRejected:
            raise click.ClickException('The support code no longer authorizes this; no export was made') from None
        click.echo('This link is a credential for all of this account\'s data: send it only in the verified '
                   'request thread. It works 3 times within 48 hours.', err=True)
        click.echo(f'{base.rstrip("/")}/api/v1/exports/{raw}')
        click.echo(f'{count} files, {size} bytes; expires {expires.isoformat().replace("+00:00", "Z")}.', err=True)

    support = app.cli.commands.get('support')
    if support is None:
        from flask.cli import AppGroup
        support = AppGroup('support', help='Support identity checks (admin).')
        app.cli.add_command(support)

    @support.command('code')
    @click.option('--id', 'user_id', type=int, required=True, help='The account the request is about.')
    @click.option('--for', 'purpose', type=click.Choice(['delete', 'export']), required=True)
    def support_code(user_id, purpose):
        """Print a one-time code for one account and one action (72 hours; only its hash is stored)."""
        if credentials.read(user_id) is None:
            raise click.ClickException('No account with that ID')
        click.echo(issue_support_code(user_id, purpose))

    @support.command('check')
    @click.argument('code')
    def support_check(code):
        """Show what a returned code authorizes (it is used up only by the delete or export itself)."""
        if not _well_formed(code):
            raise click.ClickException('Unknown code')
        with _engine().connect() as connection:
            row = connection.execute(select(SupportCode.user_id, SupportCode.purpose).where(
                SupportCode.code_hash == _code_digest(code))).first()
        state = code_status(code)
        if row is None or state != 'ok':
            raise click.ClickException(f'Code {state}')
        click.echo(f'Code is live: {row.purpose} account {row.user_id}. Also confirm SPF and DKIM pass in the '
                   'reply\'s raw headers, then run the command with --code.')

    ledger = app.cli.commands.get('deletions')
    if ledger is None:
        from flask.cli import AppGroup
        ledger = AppGroup('deletions', help='The deletion ledger, for backup restores (admin).')
        app.cli.add_command(ledger)

    @ledger.command('export')
    def deletions_export():
        """Print the ledger as JSON lines. Run BEFORE restoring a backup; keep the file until the replay is done."""
        for entry in export_ledger():
            click.echo(json.dumps(entry))

    @ledger.command('replay')
    @click.argument('path', type=click.Path(exists=True, dir_okay=False))
    @click.option('--force', is_flag=True, help='Replay even into a database older than the ledger export.')
    def deletions_replay(path, force):
        """Re-apply an exported ledger to a restored database, with web and worker stopped. Sends no mail."""
        with open(path) as source:
            entries = [json.loads(line) for line in source if line.strip()]
        applied, skipped = replay(entries, force=force)
        click.echo(f'Replayed {applied} deletion(s); {skipped} were already gone. Delete {path} now.')

    storage_group = app.cli.commands.get('storage')
    if storage_group is None:
        from flask.cli import AppGroup
        storage_group = AppGroup('storage', help='Uploaded file storage (admin).')
        app.cli.add_command(storage_group)

    @storage_group.command('vacuum')
    def storage_vacuum():
        """Rewrite both databases so no freed page keeps old data (with web and worker stopped)."""
        with _engine().connect() as connection:
            connection.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)')
            connection.exec_driver_sql('VACUUM')
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(current_app.extensions['login_throttle_path'], timeout=5)) as connection:
            connection.execute('VACUUM')
        click.echo('Vacuumed the database and the throttle database.')

    @storage_group.command('sweep')
    @click.option('--apply', is_flag=True, help='Delete what the dry run lists.')
    @click.option('--force', is_flag=True, help='Allow an unusually large pass.')
    def storage_sweep(apply, force):
        """Report (or with --apply remove) orphan files and deleted accounts' directories."""
        try:
            result = sweep(apply=apply, force=force)
        except SweepRefused as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(('Removed' if apply else 'Would remove') + f' {result["orphans"]} orphan file(s), '
                   f'{result["directories"]} deleted account folder(s) and {result["exports"]} expired export(s); '
                   f'{result["unknown_directories"]} unknown folder(s) left alone.')
