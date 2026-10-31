"""Versioned HTTP boundary; domain transactions belong to services."""
import hashlib
import io
from pathlib import Path
import uuid
import zipfile

from flask import Blueprint, current_app, g, jsonify, request, send_file
from . import services, storage
from .auth import require_browser_auth, require_agent_token, require_resume_upload_access, require_scope
from .search_access import require_search_access

api = Blueprint('api', __name__)


@api.errorhandler(services.ServiceError)
def service_error(exc):
    return jsonify(error=str(exc)), exc.status_code


def payload():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise services.ServiceError('A JSON object is required', 400)
    return value


@api.get('/jobs/search')
@require_search_access
def jobs_search():
    from .search import search
    return jsonify(search(g.user_id, request.args))


@api.get('/jobs')
@require_scope('jobs:read')
def jobs_list():
    return jsonify(jobs=services.list_jobs(g.user_id))


@api.post('/jobs')
@require_scope('jobs:write')
def jobs_create():
    return jsonify(services.create_job(g.user_id, payload())), 201


@api.post('/jobs/extract')
@require_scope('jobs:write')
def jobs_extract():
    from .extraction_tasks import start_extraction
    data = payload()
    services._payload(data, {'url'})
    result = start_extraction(g.user_id, data.get('url'))
    response = jsonify(result)
    response.status_code = 202
    response.headers['Location'] = request.script_root + '/api/v1/extractions/' + result['id']
    response.headers['Retry-After'] = '1'
    return response


@api.get('/extractions/<task_id>')
@require_scope('jobs:read')
def extraction_status(task_id):
    from .extraction_tasks import get_extraction
    return jsonify(get_extraction(g.user_id, task_id))


@api.get('/agent-tokens')
@require_browser_auth
def agent_tokens_list():
    from .agent_tokens import list_tokens
    return jsonify(tokens=list_tokens(g.user_id))


@api.post('/agent-tokens')
@require_browser_auth
def agent_tokens_create():
    from .agent_tokens import create_token
    return jsonify(create_token(g.user_id, payload())), 201


@api.post('/agent-tokens/<token_id>/revoke')
@require_browser_auth
def agent_tokens_revoke(token_id):
    from .agent_tokens import revoke_token
    return jsonify(agent_token=revoke_token(g.user_id, token_id))


@api.get('/oauth/connections')
@require_browser_auth
def oauth_connections_list():
    from .oauth import list_connections
    return jsonify(connections=list_connections(g.user_id))


@api.post('/oauth/connections/<grant_id>/revoke')
@require_browser_auth
def oauth_connections_revoke(grant_id):
    from .oauth import disconnect
    if not disconnect(g.user_id, grant_id):
        raise services.ServiceError('Connection not found', 404)
    return jsonify(revoked=True)


@api.get('/openapi.json')
def openapi():
    """The published API contract; the server URL follows this deployment."""
    import json
    from .oauth import base_url
    spec = json.loads((Path(__file__).parent / 'openapi.json').read_text())
    base = base_url()
    spec['servers'] = [{'url': base + '/api/v1'}]
    flow = spec['components']['securitySchemes']['oauth2']['flows']['authorizationCode']
    flow['authorizationUrl'], flow['tokenUrl'], flow['refreshUrl'] = (
        base + '/oauth/authorize', base + '/oauth/token', base + '/oauth/token')
    return jsonify(spec)


@api.get('/agent-connection')
@require_agent_token(permission=None)
def agent_connection():
    return jsonify(connected=True, agent_token=g.agent_token)


@api.post('/ingestions')
@require_agent_token
def ingestions_create():
    from .ingestion import start_ingestion
    data = payload()
    services._payload(data, {'url'})
    result = start_ingestion(g.user_id, data.get('url'), request.headers.get('Idempotency-Key'))
    response = jsonify(result)
    response.status_code = 202 if result['status'] == 'running' else 200
    response.headers['Location'] = request.script_root + '/api/v1/ingestions/' + result['id']
    response.headers['Retry-After'] = '1'
    return response


@api.get('/ingestions/<task_id>')
@require_agent_token
def ingestions_status(task_id):
    from .extraction_tasks import get_extraction
    from .models import IngestionRequest
    return jsonify(get_extraction(g.user_id, task_id, IngestionRequest, owner_id=g.agent_token['id']))


@api.get('/jobs/<job_id>')
@require_scope('jobs:read')
def jobs_get(job_id):
    return jsonify(services.get_job(g.user_id, job_id))


@api.patch('/jobs/<job_id>')
@require_scope('jobs:write')
def jobs_update(job_id):
    return jsonify(services.update_job(g.user_id, job_id, payload()))


@api.get('/jobs/<job_id>/activities')
@require_scope('jobs:read')
def activities_list(job_id):
    return jsonify(activities=services.list_activities(g.user_id, job_id))


@api.get('/jobs/<job_id>/contacts')
@require_scope('jobs:read')
def contacts_list(job_id):
    return jsonify(contacts=services.list_contacts(g.user_id, job_id))


@api.post('/jobs/<job_id>/contacts')
@require_scope('jobs:write')
def contacts_create(job_id):
    return jsonify(services.create_contact(g.user_id, job_id, payload())), 201


@api.get('/jobs/<job_id>/resumes')
@require_scope('resumes:read')
def resumes_list(job_id):
    return jsonify(resumes=services.list_resumes(g.user_id, job_id))


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
@require_scope('resumes:write', fallback=require_resume_upload_access)
def resumes_create(job_id):
    services.get_job(g.user_id, job_id)
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
    target = storage.user_directory(current_app.config['UPLOAD_FOLDER'], g.user_id, create=True) / stored
    with target.open('xb') as stream:
        stream.write(data)
    target.chmod(0o600)
    try:
        result = services.create_resume(g.user_id, job_id, {
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
@require_scope('resumes:read')
def resumes_download(resume_id):
    resume = services.get_resume(g.user_id, resume_id)
    # Only the owner's directory is consulted; another account's file is unreachable.
    target = storage.owned_path(current_app.config['UPLOAD_FOLDER'], g.user_id, resume['stored_filename'])
    if target is None or not target.is_file():
        raise services.ServiceError('Resume file is unavailable', 404)
    return send_file(target, mimetype=resume['media_type'], as_attachment=True,
                     download_name=resume['original_filename'], conditional=False)
