"""Resume Agent harness API: service-signed account creation and grant-scoped `/me` routes.

The harness acts strictly as one user. `POST /harness/accounts` (signed with
HARNESS_SERVICE_SECRET) creates a messaging-first account, which has no email
or password until dashboard activation, and returns that account's grant. Every
`/me` route takes `Authorization: Bearer nm_harness_...` and touches only the
grant owner's rows, exactly like the browser routes: another account's IDs are
404. Only a SHA-256 of each grant persists.

These routes are internal. The public proxy refuses /api/v1/harness/ and
/api/v1/me/, and the harness reaches JobTracker over the private container
network, which is why HARNESS_ALLOW_HTTP may waive the HTTPS check here.

Drafts are never exported while unapproved: approval is the only way a draft's
files become a job resume, and there is no draft download route.
"""
from datetime import timedelta, timezone
from functools import wraps
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from urllib.parse import quote

from flask import Blueprint, current_app, g, jsonify, request, send_file
from sqlalchemy import insert, select, update

from . import documents, services, storage, taskqueue
from .db import get_session
from . import extraction_tasks as tasks
from .extraction import extract_posting, validate_url
from .models import ActivationToken, ExtractionTask, HARNESS_SCOPES, HarnessGrant, MasterResume, TailoredDraft, User, new_id, now
from .profile import RESUME_LIMIT, REQUEST_LIMIT, master_folder, save_version

harness = Blueprint('harness', __name__)
GRANT_PREFIX = 'nm_harness_'
SIGNATURE_SKEW_SECONDS = 300
ACTIVATION_HOURS = 24
DRAFT_LIMIT = 5 * 1024 * 1024
FILE_STEM = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,119}')
_nonces, _nonce_lock = {}, threading.Lock()


@harness.errorhandler(services.ServiceError)
def service_error(exc):
    return jsonify(error=str(exc)), exc.status_code


def _engine():
    return current_app.extensions['jobtracker_db']['engine']


def _error(message, status):
    return jsonify(error=message), status


def _transport_allowed():
    return (current_app.config['APP_ENV'] == 'development' or current_app.config.get('HARNESS_ALLOW_HTTP')
            or request.is_secure)


# Service signature ------------------------------------------------------
def _signature_valid():
    """HMAC over timestamp, nonce, method, path and body digest; the same scheme the harness signs with."""
    secret = current_app.config.get('HARNESS_SERVICE_SECRET')
    timestamp = request.headers.get('X-RA-Timestamp', '')
    nonce = request.headers.get('X-RA-Nonce', '')
    supplied = request.headers.get('X-RA-Signature', '')
    if not secret or not timestamp.isdigit() or not 16 <= len(nonce) <= 64 or not supplied:
        return False
    moment = time.time()
    if abs(moment - int(timestamp)) > SIGNATURE_SKEW_SECONDS:
        return False
    digest = hashlib.sha256(request.get_data()).hexdigest()
    message = f'{timestamp}\n{nonce}\n{request.method}\n{request.script_root}{request.path}\n{digest}'.encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        return False
    with _nonce_lock:
        for seen, at in list(_nonces.items()):
            if moment - at > SIGNATURE_SKEW_SECONDS:
                del _nonces[seen]
        if nonce in _nonces:
            return False
        _nonces[nonce] = moment
    return True


def require_service_signature(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _transport_allowed():
            return _error('HTTPS required', 403)
        if not _signature_valid():
            return _error('Authentication required', 401)
        return view(*args, **kwargs)
    return wrapped


# Grants -----------------------------------------------------------------
def create_grant(connection, user_id):
    raw = GRANT_PREFIX + secrets.token_urlsafe(32)
    connection.execute(insert(HarnessGrant).values(
        id=new_id(), user_id=user_id, prefix=raw[:20], token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        scopes=list(HARNESS_SCOPES), created_at=now(), last_used_at=None, revoked_at=None))
    return raw


def authenticate_grant(raw, scope):
    """The grant owner's user_id when the grant is live and carries `scope`, else None."""
    if not isinstance(raw, str) or not raw.startswith(GRANT_PREFIX) or not 32 <= len(raw) <= 256:
        return None
    table = HarnessGrant.__table__
    live = (table.c.token_hash == hashlib.sha256(raw.encode()).hexdigest(), table.c.revoked_at.is_(None))
    with _engine().connect() as connection:
        row = connection.execute(select(table).where(*live)).mappings().first()
    if row is None or scope not in row['scopes']:
        return None
    last_used = row['last_used_at']
    if last_used is None or last_used.replace(tzinfo=timezone.utc) < now() - timedelta(seconds=60):
        with _engine().begin() as connection:
            connection.execute(update(table).where(*live).values(last_used_at=now()))
    return row['user_id']


def require_grant(scope):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not _transport_allowed():
                return _error('HTTPS required', 403)
            scheme, _, raw = request.headers.get('Authorization', '').partition(' ')
            user_id = authenticate_grant(raw, scope) if scheme.lower() == 'bearer' else None
            if user_id is None:
                response, status = _error('Authentication required', 401)
                response.headers['WWW-Authenticate'] = 'Bearer realm="NextMove harness"'
                return response, status
            g.user_id, g.auth_type = user_id, 'harness'
            return view(*args, **kwargs)
        return wrapped
    return decorator


def revoke_grants(user_id):
    with _engine().begin() as connection:
        connection.execute(update(HarnessGrant).where(HarnessGrant.user_id == user_id,
                                                      HarnessGrant.revoked_at.is_(None)).values(revoked_at=now()))


# Accounts ---------------------------------------------------------------
@harness.post('/harness/accounts')
@require_service_signature
def create_account():
    """A messaging-first account: no email or password until the user activates the dashboard."""
    with _engine().begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        user_id = connection.execute(insert(User).values(
            email=None, password_hash=None, session_version=1, created_at=now())).inserted_primary_key[0]
        grant = create_grant(connection, user_id)
    return jsonify(user_id=user_id, grant=grant), 201


@harness.post('/me/activation-links')
@require_grant('resumes:read')
def activation_link():
    base = current_app.config.get('PUBLIC_BASE_URL', '').rstrip('/')
    if not base:
        raise services.ServiceError('PUBLIC_BASE_URL is not configured', 503)
    with _engine().begin() as connection:
        active = connection.scalar(select(User.password_hash).where(User.id == g.user_id)) is not None
        if active:
            return jsonify(url=base + '/', activated=True)
        raw = secrets.token_urlsafe(32)
        expires = now() + timedelta(hours=ACTIVATION_HOURS)
        connection.execute(insert(ActivationToken).values(
            id=new_id(), user_id=g.user_id, token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_at=now(), expires_at=expires, used_at=None))
    return jsonify(url=f'{base}/?activate={raw}', activated=False,
                   expires_at=expires.isoformat().replace('+00:00', 'Z')), 201


# Master resumes ---------------------------------------------------------
@harness.post('/me/master-resumes')
@require_grant('resumes:write')
def master_resume_upload():
    request.max_content_length = REQUEST_LIMIT
    folder = master_folder(g.user_id)
    stored = documents.receive(request.files.get('file'), folder, RESUME_LIMIT, folder / '.incoming')
    try:
        stored.commit()
        result, created = save_version(g.user_id, stored)
    except BaseException:
        stored.discard()
        raise
    if not created:
        stored.discard()
    return jsonify(id=result['id'], version=result['version'], created=created), 201 if created else 200


@harness.get('/me/master-resumes/<resume_id>/file')
@require_grant('resumes:read')
def master_resume_file(resume_id):
    resume = services._record(MasterResume, g.user_id, resume_id)
    root = master_folder(g.user_id).resolve()
    target = (root / resume.stored_filename).resolve()
    if target.parent != root or not target.is_file():
        raise services.ServiceError('Resume file is unavailable', 404)
    response = send_file(target, mimetype=resume.media_type, conditional=False)
    response.headers['X-Original-Filename'] = quote(resume.original_filename)
    return response


# Jobs and postings ------------------------------------------------------
def _job(job):
    return {'id': job['id'], 'url': job['url'], 'title': job['title'], 'company': job['company'],
            'stage': job['stage'], 'posting': job['description'] or None}


@harness.post('/me/jobs')
@require_grant('jobs:write')
def jobs_create():
    data = request.get_json(silent=True)
    services._payload(data, {'url'})
    validate_url(data.get('url'))
    job = services.create_job(g.user_id, {'url': data['url'].strip(), 'stage': 'needs_review'})
    extraction_id = new_id()
    try:
        with _engine().begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            tasks.expire_tasks(connection)
            tasks.admit(connection, g.user_id)
            connection.execute(ExtractionTask.__table__.insert().values(
                id=extraction_id, user_id=g.user_id, status='running', result={}, created_at=now()))
            taskqueue.enqueue(connection, g.user_id, 'harness_fill_job',
                              {'task_id': extraction_id, 'job_id': job['id'], 'url': job['url']})
    except services.ServiceError:
        extraction_id = None  # Busy: the role is saved; the user can paste the description instead.
    return jsonify(**_job(job), extraction_id=extraction_id), 202


@harness.get('/me/jobs/<job_id>')
@require_grant('jobs:read')
def jobs_get(job_id):
    return jsonify(_job(services.get_job(g.user_id, job_id)))


@harness.put('/me/jobs/<job_id>/posting')
@require_grant('jobs:write')
def jobs_posting(job_id):
    data = request.get_json(silent=True)
    services._payload(data, {'text'})
    text = services._text(data.get('text', ''), 'text')
    if not text:
        raise services.ServiceError('Paste the job description')
    return jsonify(_job(services.update_job(g.user_id, job_id, {'description': text})))


@harness.get('/me/extractions/<task_id>')
@require_grant('jobs:read')
def extraction_status(task_id):
    return jsonify(tasks.get_extraction(g.user_id, task_id))


def give_up_fill(engine, _user_id, payload):
    tasks.finish(engine, payload['task_id'], 'failed', tasks.EXPIRED)


@taskqueue.handler('harness_fill_job', give_up=give_up_fill, lease_seconds=tasks.LEASE_SECONDS)
def run_fill_job(app, task):
    engine = app.extensions['jobtracker_db']['engine']
    identifier = task.payload['task_id']
    if not tasks.still_running(engine, ExtractionTask, identifier, task.user_id):
        return
    try:
        fields = extract_posting(task.payload['url'])['fields']
        services.update_job(task.user_id, task.payload['job_id'], {
            key: fields[key] for key in ('title', 'company', 'salary', 'notes', 'description') if fields.get(key)})
        status, result = 'succeeded', {'job_id': task.payload['job_id']}
    except services.ServiceError as exc:
        status, result = 'failed', {'error': str(exc)}
    except Exception:
        status, result = 'failed', {'error': tasks.UNREADABLE}
    tasks.finish(engine, identifier, status, result)


# Drafts -----------------------------------------------------------------
def drafts_folder(user_id):
    return storage.user_directory(current_app.config['UPLOAD_FOLDER'], user_id) / 'drafts'


def _draft(row):
    return {'id': row.id, 'approved': row.approved_at is not None, 'has_pdf': row.pdf_filename is not None,
            'draft': row.draft}


def _draft_metadata(raw):
    try:
        draft = json.loads(raw or '')
    except ValueError:
        raise services.ServiceError('draft must be a JSON object') from None
    if not isinstance(draft, dict):
        raise services.ServiceError('draft must be a JSON object')
    request_id, revision = draft.get('request_id'), draft.get('revision')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        raise services.ServiceError('draft.request_id is required')
    if type(revision) is not int or revision < 1:
        raise services.ServiceError('draft.revision must be a positive integer')
    if not isinstance(draft.get('job_id'), str) or not isinstance(draft.get('master_resume_version_id'), str):
        raise services.ServiceError('draft.job_id and draft.master_resume_version_id are required')
    # The approved files' name, without extension, e.g. NicolasMelo_Resume_Acme_SrEng.
    file_stem = draft.get('file_stem')
    if file_stem is not None and not (isinstance(file_stem, str) and FILE_STEM.fullmatch(file_stem)):
        raise services.ServiceError('draft.file_stem must be letters, digits, _ or -, at most 120 characters')
    return draft


@harness.post('/me/drafts')
@require_grant('drafts:write')
def drafts_create():
    request.max_content_length = 2 * DRAFT_LIMIT + 256 * 1024
    draft = _draft_metadata(request.form.get('draft'))
    user_id = g.user_id
    services.get_job(user_id, draft['job_id'])
    master = services._record(MasterResume, user_id, draft['master_resume_version_id'])
    session = get_session()
    existing = session.scalar(select(TailoredDraft).where(
        TailoredDraft.user_id == user_id, TailoredDraft.request_id == draft['request_id'],
        TailoredDraft.revision == draft['revision']))
    if existing is not None:
        return jsonify(_draft(existing)), 200  # Redelivered request: the first draft stands.
    folder = drafts_folder(user_id)
    storage.user_directory(current_app.config['UPLOAD_FOLDER'], user_id, create=True)
    uploads = []
    try:
        for field, suffix in (('docx', '.docx'), ('pdf', '.pdf')):
            upload = request.files.get(field)
            if upload is None:
                if field == 'docx':
                    raise services.ServiceError('A DOCX draft is required')
                continue
            stored = documents.receive(upload, folder, DRAFT_LIMIT, folder / '.incoming')
            if stored.suffix != suffix:
                stored.discard()
                raise services.ServiceError(f'{field} must be a {suffix} file')
            stored.commit()
            uploads.append(stored)
        row = TailoredDraft(user_id=user_id, job_id=draft['job_id'], master_resume_id=master.id,
                            request_id=draft['request_id'], revision=draft['revision'], draft=draft,
                            docx_filename=uploads[0].stored_filename,
                            pdf_filename=uploads[1].stored_filename if len(uploads) > 1 else None)
        session.add(row)
        session.flush()
        services._activity(user_id, draft['job_id'], 'draft_created',
                           f'Tailored resume draft {draft["revision"]} ready for review', {'draft_id': row.id})
        session.commit()
    except BaseException:
        session.rollback()
        for stored in uploads:
            stored.discard()
        raise
    return jsonify(_draft(row)), 201


@harness.get('/me/drafts/<draft_id>')
@require_grant('drafts:write')
def drafts_get(draft_id):
    return jsonify(_draft(services._record(TailoredDraft, g.user_id, draft_id)))


@harness.post('/me/drafts/<draft_id>/approve')
@require_grant('drafts:write')
def drafts_approve(draft_id):
    """Approval files the draft as resume versions on its job. It never moves the job's stage."""
    user_id = g.user_id
    session = get_session()
    row = services._record(TailoredDraft, user_id, draft_id)
    if row.approved_at is not None:
        return jsonify(_draft(row))
    claimed = session.execute(update(TailoredDraft).where(
        TailoredDraft.id == row.id, TailoredDraft.user_id == user_id, TailoredDraft.approved_at.is_(None)
    ).values(approved_at=now())).rowcount
    session.commit()
    if not claimed:
        return jsonify(_draft(session.get(TailoredDraft, row.id)))
    root = storage.user_directory(current_app.config['UPLOAD_FOLDER'], user_id, create=True)
    name = row.draft.get('file_stem') or f'tailored-resume-r{row.revision}'
    for source, suffix in ((row.docx_filename, '.docx'), (row.pdf_filename, '.pdf')):
        if source is None:
            continue
        data = (drafts_folder(user_id) / source).read_bytes()
        stored = f'{new_id().replace("-", "")}{suffix}'
        (root / stored).write_bytes(data)
        (root / stored).chmod(0o600)
        services.create_resume(user_id, row.job_id, {
            'original_filename': f'{name}{suffix}', 'stored_filename': stored,
            'media_type': documents.MEDIA_TYPES[suffix], 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest()})
    services._activity(user_id, row.job_id, 'draft_approved',
                       'Tailored resume approved; consider moving this role to Resume updated', {'draft_id': row.id})
    session.commit()
    return jsonify(_draft(session.get(TailoredDraft, row.id)))
