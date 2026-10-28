"""Per-user onboarding progress and master resume routes (browser session only)."""
from datetime import timezone
import re

from flask import Blueprint, current_app, g, jsonify, request, send_file
from sqlalchemy import insert, select

from . import documents, onboarding, services, storage
from .auth import require_browser_auth
from .db import get_session
from .models import MasterResume, new_id, now

profile = Blueprint('profile', __name__)
RESUME_LIMIT = 5 * 1024 * 1024
# Multipart framing around a 5 MB file is a few hundred bytes; refuse anything
# far larger before werkzeug spools it to disk.
REQUEST_LIMIT = RESUME_LIMIT + 64 * 1024
MAX_VERSIONS = 20
STORED_NAME = re.compile(r'[0-9a-f]{32}\.(?:pdf|docx|txt)')
PUBLIC_FIELDS = ('id', 'version', 'original_filename', 'media_type', 'size', 'sha256', 'created_at')


@profile.errorhandler(services.ServiceError)
def service_error(exc):
    return jsonify(error=str(exc)), exc.status_code


def current_user_id():
    return g.user_id


# Browser session only: agent tokens and the legacy bearer token never reach
# personal profile data or onboarding state.
require_user = require_browser_auth


def master_folder(user_id):
    return storage.user_directory(current_app.config['UPLOAD_FOLDER'], user_id) / 'master'


def public(resume):
    data = services.serialize(resume) if isinstance(resume, MasterResume) else dict(resume)
    created = data['created_at']
    if hasattr(created, 'isoformat'):
        data['created_at'] = created.replace(tzinfo=created.tzinfo or timezone.utc).isoformat().replace('+00:00', 'Z')
    return {key: data[key] for key in PUBLIC_FIELDS}


@profile.get('/onboarding')
@require_user
def onboarding_list():
    return jsonify(onboarding.state(current_user_id()))


@profile.post('/onboarding/steps/<step_id>')
@require_user
def onboarding_update(step_id):
    return jsonify(onboarding.set_status(current_user_id(), step_id, request.get_json(silent=True)))


@profile.get('/profile/resume')
@require_user
def resume_get():
    versions = get_session().scalars(select(MasterResume).where(
        MasterResume.user_id == current_user_id()).order_by(MasterResume.version.desc())).all()
    return jsonify(resume=public(versions[0]) if versions else None,
                   versions=[public(item) for item in versions])


@profile.post('/profile/resume')
@require_user
def resume_upload():
    # Applies before werkzeug parses (and spools) the multipart body.
    request.max_content_length = REQUEST_LIMIT
    user_id = current_user_id()
    folder = master_folder(user_id)
    stored = documents.receive(request.files.get('file'), folder, RESUME_LIMIT, folder / '.incoming')
    try:
        stored.commit()
        result, created = save_version(user_id, stored)
    except BaseException:
        stored.discard()
        raise
    if not created:
        stored.discard()
    return jsonify(resume=result, created=created, **onboarding.state(user_id)), 201 if created else 200


def save_version(user_id, stored):
    """Number the version under the SQLite write lock; returns (metadata, created)."""
    table = MasterResume.__table__
    with current_app.extensions['jobtracker_db']['engine'].begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        rows = connection.execute(select(table).where(table.c.user_id == user_id)
                                  .order_by(table.c.version.desc())).mappings().all()
        if rows and rows[0]['sha256'] == stored.sha256:
            return public(rows[0]), False  # Re-uploading the same bytes is a no-op.
        if len(rows) >= MAX_VERSIONS:
            raise services.ServiceError(f'You can keep up to {MAX_VERSIONS} resume versions', 409)
        values = dict(id=new_id(), user_id=user_id, version=rows[0]['version'] + 1 if rows else 1,
                      original_filename=stored.original_filename or 'resume' + stored.suffix,
                      stored_filename=stored.stored_filename, media_type=stored.media_type,
                      size=stored.size, sha256=stored.sha256, created_at=now())
        connection.execute(insert(table).values(**values))
    return public(values), True


@profile.get('/profile/resume/download')
@require_user
def resume_download():
    user_id = current_user_id()
    query = select(MasterResume).where(MasterResume.user_id == user_id)
    version = request.args.get('version')
    if version is not None:
        if not (version.isascii() and version.isdigit()):
            raise services.ServiceError('version must be a positive integer')
        query = query.where(MasterResume.version == int(version))
    resume = get_session().scalars(query.order_by(MasterResume.version.desc()).limit(1)).first()
    if resume is None:
        raise services.ServiceError('No resume uploaded yet', 404)
    root = master_folder(user_id).resolve()
    target = (root / resume.stored_filename).resolve()
    if not STORED_NAME.fullmatch(resume.stored_filename) or target.parent != root or not target.is_file():
        raise services.ServiceError('Resume file is unavailable', 404)
    return send_file(target, mimetype=resume.media_type, as_attachment=True,
                     download_name=resume.original_filename, conditional=False)
