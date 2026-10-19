"""Versioned HTTP boundary; domain transactions belong to services."""
import hashlib
import io
from pathlib import Path
import uuid
import zipfile

from flask import Blueprint, current_app, jsonify, request, send_file
from . import services
from .auth import require_auth

api = Blueprint('api', __name__)


@api.errorhandler(services.ServiceError)
def service_error(exc):
    return jsonify(error=str(exc)), exc.status_code


def payload():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise services.ServiceError('A JSON object is required', 400)
    return value


@api.get('/jobs')
@require_auth
def jobs_list():
    return jsonify(jobs=services.list_jobs())


@api.post('/jobs')
@require_auth
def jobs_create():
    return jsonify(services.create_job(payload())), 201


@api.get('/jobs/<job_id>')
@require_auth
def jobs_get(job_id):
    return jsonify(services.get_job(job_id))


@api.patch('/jobs/<job_id>')
@require_auth
def jobs_update(job_id):
    return jsonify(services.update_job(job_id, payload()))


@api.get('/jobs/<job_id>/activities')
@require_auth
def activities_list(job_id):
    return jsonify(activities=services.list_activities(job_id))


@api.get('/jobs/<job_id>/contacts')
@require_auth
def contacts_list(job_id):
    return jsonify(contacts=services.list_contacts(job_id))


@api.post('/jobs/<job_id>/contacts')
@require_auth
def contacts_create(job_id):
    return jsonify(services.create_contact(job_id, payload())), 201


@api.get('/jobs/<job_id>/resumes')
@require_auth
def resumes_list(job_id):
    return jsonify(resumes=services.list_resumes(job_id))


def validate_document(filename, data):
    suffix = Path(filename).suffix.lower()
    if suffix == '.pdf':
        valid = data.startswith(b'%PDF-')
        media_type = 'application/pdf'
    elif suffix == '.docx':
        media_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                valid = {'[Content_Types].xml', 'word/document.xml'} <= set(archive.namelist())
        except (zipfile.BadZipFile, ValueError):
            valid = False
    elif suffix == '.txt':
        media_type = 'text/plain'
        try:
            data.decode('utf-8')
            valid = b'\x00' not in data
        except UnicodeDecodeError:
            valid = False
    else:
        raise services.ServiceError('Resume must be a PDF, DOCX, or UTF-8 TXT file', 400)
    if not data or not valid:
        raise services.ServiceError('File content does not match its document type', 400)
    return suffix, media_type


@api.post('/jobs/<job_id>/resumes')
@require_auth
def resumes_create(job_id):
    services.get_job(job_id)
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        raise services.ServiceError('A resume file is required', 400)
    # Normalize display name; all storage paths are generated independently.
    filename = upload.filename.replace('\\', '/').split('/')[-1][:255]
    data = upload.read(current_app.config['MAX_CONTENT_LENGTH'] + 1)
    if len(data) > current_app.config['MAX_CONTENT_LENGTH']:
        raise services.ServiceError('Resume exceeds upload size limit', 413)
    suffix, media_type = validate_document(filename, data)
    stored = f'{uuid.uuid4().hex}{suffix}'
    target = Path(current_app.config['UPLOAD_FOLDER']) / stored
    with target.open('xb') as stream:
        stream.write(data)
    target.chmod(0o600)
    try:
        result = services.create_resume(job_id, {
            'original_filename': filename,
            'stored_filename': stored,
            'media_type': media_type,
            'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
        })
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return jsonify(result), 201


@api.get('/resumes/<resume_id>/download')
@require_auth
def resumes_download(resume_id):
    resume = services.get_resume(resume_id)
    root = Path(current_app.config['UPLOAD_FOLDER']).resolve()
    target = (root / resume['stored_filename']).resolve()
    if target.parent != root or not target.is_file():
        raise services.ServiceError('Resume file is unavailable', 404)
    return send_file(target, mimetype=resume['media_type'], as_attachment=True,
                     download_name=resume['original_filename'], conditional=False)
