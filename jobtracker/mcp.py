"""Model Context Protocol server for third-party agents (Streamable HTTP, stateless).

`POST /mcp` takes one JSON-RPC message and answers with a JSON body: there are
no server-initiated messages, so no SSE stream or session is needed. Every
request carries an OAuth access token (see oauth.py); tools are listed and run
only within the scopes the user approved. Tools call the same services as the
HTTP API, so tenancy, validation, versions and activity history are identical.
"""
import base64
import binascii
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, request

from . import resume_files, services
from .models import OUTCOMES, STAGES
from .oauth import base_url, require_oauth

mcp = Blueprint('mcp', __name__)
PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
SERVER_INFO = {'name': 'my-next-moov', 'title': 'My Next Moov', 'version': '1.0.0'}
STAGE_LABELS = {
    'needs_review': 'Needs review', 'interesting': 'Interesting', 'resume_updated': 'Resume updated',
    'outreach': 'Outreach', 'applied': 'Applied', 'recruiter_interview': 'Recruiter interview',
    'hiring_manager_interview': 'Hiring manager interview', 'panel_interview': 'Panel interview', 'offer': 'Offer',
}
INSTRUCTIONS = (
    'My Next Moov is the user\'s job search tracker. Roles move through stages in this order: '
    + ', '.join(f'{key} ({label})' for key, label in STAGE_LABELS.items())
    + '. Outcome (active, rejected, withdrawn, closed, accepted, not_interested) is separate from stage. '
    'Only move a role to a stage that reflects work the user actually did: uploading a resume is not applying. '
    'Resume uploads always add a new version; nothing is ever deleted. Confirm with the user before changing '
    'stage or outcome unless they asked for that exact change.'
)
MAX_INLINE_UPLOAD = 5 * 1024 * 1024
JOB_ID = {'type': 'string', 'description': 'Role ID from list_jobs'}
SUMMARY_FIELDS = ('id', 'title', 'company', 'stage', 'outcome', 'priority', 'location', 'url', 'outreach_completed',
                  'next_action', 'next_action_date', 'updated_at', 'version')
EDITABLE = {
    'title': {'type': 'string'}, 'company': {'type': 'string'}, 'url': {'type': 'string'},
    'location': {'type': 'string'}, 'salary': {'type': 'string'}, 'lane': {'type': 'string'},
    'priority': {'type': 'string', 'enum': ['low', 'medium', 'high']},
    'description': {'type': 'string', 'description': 'The job posting text'},
    'notes': {'type': 'string', 'description': 'Replaces all notes; prefer add_note to append'},
    'next_action': {'type': 'string'},
    'next_action_date': {'type': 'string', 'description': 'YYYY-MM-DD, or empty to clear'},
    'outreach_completed': {'type': 'boolean'},
}
TOOLS = {}


def tool(name, scope, description, properties=None, required=(), read_only=False, destructive=False, title=None):
    def register(handler):
        TOOLS[name] = {
            'scope': scope, 'handler': handler,
            'definition': {
                'name': name, 'title': title or name.replace('_', ' ').capitalize(), 'description': description,
                'inputSchema': {'type': 'object', 'properties': properties or {}, 'required': list(required),
                                'additionalProperties': False},
                'annotations': {'readOnlyHint': read_only, 'destructiveHint': destructive,
                                'idempotentHint': read_only, 'openWorldHint': False},
            },
        }
        return handler
    return register


class ToolError(Exception):
    pass


def summary(job):
    return {key: job[key] for key in SUMMARY_FIELDS}


def _text_arg(arguments, name, required=False, limit=100000):
    value = arguments.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolError(f'{name} must be {"non-empty " if required else ""}text')
    if len(value) > limit:
        raise ToolError(f'{name} exceeds {limit} characters')
    return value.strip()


def _with_fresh_version(job_id, change):
    """Apply change(job) -> fields under the job's current version, retrying once if it moved."""
    for attempt in range(2):
        job = services.get_job(g.user_id, job_id)
        try:
            return services.update_job(g.user_id, job_id, {**change(job), 'version': job['version']})
        except services.ServiceError as exc:
            if exc.status_code != 409 or attempt:
                raise


def _dated(note):
    today = datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
    return f'[{today} · {g.oauth.client_name}] {note}'


def _append_note(job, note):
    return (job['notes'] + '\n\n' if job['notes'] else '') + _dated(note)


# Jobs -------------------------------------------------------------------
@tool('list_jobs', 'jobs:read', 'List saved roles in the tracker, most recently updated first. Returns summaries; '
      'call get_job for the posting text, notes, contacts and resumes.', {
          'stage': {'type': 'string', 'enum': list(STAGES)},
          'outcome': {'type': 'string', 'enum': [*OUTCOMES, 'all'], 'default': 'active'},
          'query': {'type': 'string', 'description': 'Matches title, company, location or lane'},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 50},
      }, read_only=True, title='List roles')
def list_jobs(arguments):
    outcome, stage = arguments.get('outcome', 'active'), arguments.get('stage')
    query = (_text_arg(arguments, 'query', limit=200) or '').lower()
    limit = arguments.get('limit', 50)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ToolError('limit must be 1–200')
    if outcome != 'all' and outcome not in OUTCOMES:
        raise ToolError('Unknown outcome')
    if stage is not None and stage not in STAGES:
        raise ToolError('Unknown stage')
    jobs = [job for job in services.list_jobs(g.user_id)
            if (outcome == 'all' or job['outcome'] == outcome) and (stage is None or job['stage'] == stage)
            and query in f"{job['title']} {job['company']} {job['location']} {job['lane']}".lower()]
    return {'jobs': [summary(job) for job in jobs[:limit]], 'total': len(jobs)}


@tool('get_job', 'jobs:read', 'Read one role in full: posting description, notes, next action, contacts and '
      'attached resume versions.', {'job_id': JOB_ID}, ['job_id'], read_only=True, title='Get role')
def get_job(arguments):
    job_id = _text_arg(arguments, 'job_id', required=True)
    job = services.get_job(g.user_id, job_id)
    resumes = [resume_files.metadata(record) for record in _job_resumes(job_id)] \
        if 'resumes:read' in g.oauth.scopes else None
    return {'job': job, 'stage_label': STAGE_LABELS[job['stage']],
            'contacts': services.list_contacts(g.user_id, job_id), 'resumes': resumes}


def _job_resumes(job_id):
    from sqlalchemy import select
    from .db import get_session
    from .models import Resume
    return get_session().scalars(select(Resume).where(Resume.job_id == job_id, Resume.user_id == g.user_id)
                                 .order_by(Resume.created_at, Resume.id)).all()


@tool('get_job_history', 'jobs:read', 'The activity timeline of a role: creation, stage and field changes, '
      'contacts and resumes added.', {'job_id': JOB_ID}, ['job_id'], read_only=True, title='Get role history')
def get_job_history(arguments):
    return {'activities': services.list_activities(g.user_id, _text_arg(arguments, 'job_id', required=True))}


@tool('add_job', 'jobs:write', 'Save a new role at Needs review. Give a posting URL and/or the details. With a URL '
      'and no description, My Next Moov reads the posting in the background; call get_job a few seconds later. '
      'Check list_jobs first to avoid duplicates.', {
          'url': {'type': 'string', 'description': 'Public HTTP(S) posting URL'},
          'title': {'type': 'string'}, 'company': {'type': 'string'}, 'location': {'type': 'string'},
          'salary': {'type': 'string'}, 'description': {'type': 'string', 'description': 'Posting text'},
          'notes': {'type': 'string'},
      }, title='Add role')
def add_job(arguments):
    fields = {key: _text_arg(arguments, key) for key in ('url', 'title', 'company', 'location', 'salary',
                                                         'description', 'notes') if arguments.get(key) is not None}
    if fields.get('url'):
        from .extraction import validate_url
        validate_url(fields['url'])
    job = services.create_job(g.user_id, {**fields, 'stage': 'needs_review'})
    reading = None
    if job['url'] and not job['description']:
        from .harness import queue_fill
        reading = queue_fill(g.user_id, job) is not None
    result = {'job': summary(job)}
    if reading is not None:
        result['posting'] = ('Reading the posting in the background.' if reading else
                             'The reader is busy; ask the user to paste the description, then call update_job.')
    return result


@tool('move_job', 'jobs:write', 'Move a role to another pipeline stage, optionally adding a dated note explaining '
      'why. Stages in order: ' + ', '.join(STAGES) + '. Entering outreach also marks outreach as made.', {
          'job_id': JOB_ID, 'stage': {'type': 'string', 'enum': list(STAGES)},
          'note': {'type': 'string', 'description': 'Optional; appended to the role\'s notes'},
      }, ['job_id', 'stage'], title='Move role to stage')
def move_job(arguments):
    job_id, stage = _text_arg(arguments, 'job_id', required=True), arguments.get('stage')
    if stage not in STAGES:
        raise ToolError('Unknown stage. Use one of: ' + ', '.join(STAGES))
    note = _text_arg(arguments, 'note', limit=10000)
    job = _with_fresh_version(job_id, lambda current: {'stage': stage, **({'notes': _append_note(current, note)} if note else {})})
    return {'job': summary(job), 'stage_label': STAGE_LABELS[stage]}


@tool('set_outcome', 'jobs:write', 'Set the outcome of a role without changing its stage: active, rejected, '
      'withdrawn, closed, accepted or not_interested.', {
          'job_id': JOB_ID, 'outcome': {'type': 'string', 'enum': list(OUTCOMES)},
          'note': {'type': 'string', 'description': 'Optional; appended to the role\'s notes'},
      }, ['job_id', 'outcome'], title='Set role outcome')
def set_outcome(arguments):
    job_id, outcome = _text_arg(arguments, 'job_id', required=True), arguments.get('outcome')
    if outcome not in OUTCOMES:
        raise ToolError('Unknown outcome. Use one of: ' + ', '.join(OUTCOMES))
    note = _text_arg(arguments, 'note', limit=10000)
    job = _with_fresh_version(job_id, lambda current: {'outcome': outcome, **({'notes': _append_note(current, note)} if note else {})})
    return {'job': summary(job)}


@tool('add_note', 'jobs:write', 'Append a dated note to a role. Existing notes are kept.',
      {'job_id': JOB_ID, 'text': {'type': 'string'}}, ['job_id', 'text'], title='Add note')
def add_note(arguments):
    job_id, text = _text_arg(arguments, 'job_id', required=True), _text_arg(arguments, 'text', required=True, limit=10000)
    job = _with_fresh_version(job_id, lambda current: {'notes': _append_note(current, text)})
    return {'job': summary(job), 'notes': job['notes']}


@tool('update_job', 'jobs:write', 'Edit a role\'s fields. Pass the version from get_job or list_jobs; if the role '
      'changed since, the call fails and you should re-read it and reapply the edit. Use move_job, set_outcome and '
      'add_note for those changes.', {'job_id': JOB_ID, 'version': {'type': 'integer', 'minimum': 1},
                                     'fields': {'type': 'object', 'properties': EDITABLE, 'additionalProperties': False}},
      ['job_id', 'version', 'fields'], destructive=True, title='Edit role')
def update_job(arguments):
    fields = arguments.get('fields')
    if not isinstance(fields, dict) or not fields or not set(fields) <= set(EDITABLE):
        raise ToolError('fields must name at least one of: ' + ', '.join(EDITABLE))
    try:
        job = services.update_job(g.user_id, _text_arg(arguments, 'job_id', required=True),
                                  {**fields, 'version': arguments.get('version')})
    except services.ServiceError as exc:
        if exc.status_code == 409:
            raise ToolError('This role changed since you read it. Call get_job, then reapply the edit.') from exc
        raise
    return {'job': job}


@tool('add_contact', 'jobs:write', 'Save a person related to a role (recruiter, referral, hiring manager). '
      'Saving a contact does not mean they were contacted.', {
          'job_id': JOB_ID, 'name': {'type': 'string'}, 'email': {'type': 'string'}, 'notes': {'type': 'string'},
      }, ['job_id'], title='Add contact')
def add_contact(arguments):
    job_id = _text_arg(arguments, 'job_id', required=True)
    values = {key: arguments[key] for key in ('name', 'email', 'notes') if key in arguments}
    return {'contact': services.create_contact(g.user_id, job_id, values)}


# Resumes ----------------------------------------------------------------
@tool('list_resumes', 'resumes:read', 'List the user\'s master resume versions (newest first) and, with job_id, '
      'the resume versions attached to that role.', {'job_id': JOB_ID}, read_only=True, title='List resumes')
def list_resumes(arguments):
    from sqlalchemy import select
    from .db import get_session
    from .models import MasterResume
    masters = get_session().scalars(select(MasterResume).where(MasterResume.user_id == g.user_id)
                                    .order_by(MasterResume.version.desc())).all()
    result = {'master': [resume_files.metadata(record) for record in masters]}
    job_id = _text_arg(arguments, 'job_id')
    if job_id:
        services.get_job(g.user_id, job_id)
        result['job'] = [resume_files.metadata(record) for record in _job_resumes(job_id)]
    return result


@tool('read_resume', 'resumes:read', 'Read a resume\'s text as paragraphs (id, text, kind: heading, bullet or '
      'body). Defaults to the latest master resume. DOCX and TXT only; for a PDF use get_resume_download_link.',
      {'resume_id': {'type': 'string', 'description': 'From list_resumes; omit for the latest master resume'}},
      read_only=True, title='Read resume')
def read_resume(arguments):
    resume_id = _text_arg(arguments, 'resume_id')
    if not resume_id:
        latest = resume_files.latest_master(g.user_id)
        if latest is None:
            raise ToolError('No master resume yet. Ask the user to upload one, or use upload_resume.')
        resume_id = latest.id
    record, path, _ = resume_files.locate(g.user_id, resume_id)
    if path is None or not path.is_file():
        raise ToolError('Resume file is unavailable')
    paragraphs = resume_files.read_paragraphs(path, record.media_type)
    if paragraphs is None:
        raise ToolError('This resume is a PDF; text extraction is not available. Use get_resume_download_link.')
    return {'resume': resume_files.metadata(record), 'paragraphs': paragraphs}


@tool('get_resume_download_link', 'resumes:read', 'A private download URL for a resume file, valid for five '
      'minutes. Share it only with the user.', {'resume_id': {'type': 'string'}}, ['resume_id'], read_only=True,
      title='Get resume download link')
def get_resume_download_link(arguments):
    record, _, _ = resume_files.locate(g.user_id, _text_arg(arguments, 'resume_id', required=True))
    return {'resume': resume_files.metadata(record), **resume_files.download_link(base_url(), g.oauth.grant_id, record.id)}


TARGET = {'type': 'string', 'description': '"master" for a new master resume version, or a role ID to attach a '
                                           'tailored resume to that role'}


@tool('create_resume_upload_link', 'resumes:write', 'A URL to upload a resume file (PDF, DOCX or UTF-8 TXT, up to '
      '5 MB) with a multipart POST, field "file". Valid for 15 minutes. Use this when you can make HTTP requests or '
      'want to hand the user a link; otherwise use upload_resume.', {'target': TARGET}, ['target'],
      title='Create resume upload link')
def create_resume_upload_link(arguments):
    target = _target(arguments)
    return resume_files.upload_link(base_url(), g.oauth.grant_id, target)


@tool('upload_resume', 'resumes:write', 'Upload a resume file as base64 (PDF, DOCX or UTF-8 TXT, up to 5 MB). '
      'Adds a new version; earlier versions are kept. Uploading does not move the role to another stage.', {
          'target': TARGET, 'filename': {'type': 'string', 'description': 'e.g. Jane_Doe_Acme.docx'},
          'content_base64': {'type': 'string'},
      }, ['target', 'filename', 'content_base64'], title='Upload resume')
def upload_resume(arguments):
    target = _target(arguments)
    filename = _text_arg(arguments, 'filename', required=True, limit=255)
    encoded = arguments.get('content_base64')
    if not isinstance(encoded, str) or len(encoded) > MAX_INLINE_UPLOAD * 4 // 3 + 8:
        raise ToolError('content_base64 must be base64 text of a file up to 5 MB')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ToolError('content_base64 is not valid base64') from None
    return resume_files.upload_bytes(g.user_id, target, filename, data)


def _target(arguments):
    target = _text_arg(arguments, 'target', required=True, limit=64)
    if target != 'master':
        services.get_job(g.user_id, target)
    return target


# Transport --------------------------------------------------------------
def _result(identifier, result):
    return jsonify(jsonrpc='2.0', id=identifier, result=result)


def _error(identifier, code, message, status=200):
    response = jsonify(jsonrpc='2.0', id=identifier, error={'code': code, 'message': message})
    response.status_code = status
    return response


def _origin_allowed():
    origin = request.headers.get('Origin')
    if not origin:
        return True
    allowed = {request.host_url.rstrip('/'), *current_app.config.get('MCP_ALLOWED_ORIGINS', ())}
    return origin.rstrip('/') in allowed


@mcp.route('/mcp', methods=['GET', 'DELETE'])
def unsupported():
    # Stateless server: no standalone SSE stream and no sessions to end.
    return jsonify(error='Method not allowed'), 405, {'Allow': 'POST'}


@mcp.post('/mcp')
@require_oauth(None)
def endpoint():
    if not _origin_allowed():
        return jsonify(error='Origin not allowed'), 403
    version = request.headers.get('MCP-Protocol-Version')
    if version and version not in PROTOCOL_VERSIONS:
        return _error(None, -32600, f'Unsupported MCP-Protocol-Version {version}', 400)
    try:
        message = json.loads(request.get_data(as_text=True))
    except ValueError:
        return _error(None, -32700, 'Parse error', 400)
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return _error(None, -32600, 'Expected one JSON-RPC 2.0 message', 400)
    if 'method' not in message or 'id' not in message:
        return '', 202  # Notifications and client responses need no reply.
    identifier, method, params = message['id'], message['method'], message.get('params') or {}
    if not isinstance(params, dict):
        return _error(identifier, -32602, 'params must be an object')
    if method == 'initialize':
        requested = params.get('protocolVersion')
        return _result(identifier, {
            'protocolVersion': requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': SERVER_INFO, 'instructions': INSTRUCTIONS})
    if method == 'ping':
        return _result(identifier, {})
    if method == 'tools/list':
        return _result(identifier, {'tools': [entry['definition'] for entry in TOOLS.values()
                                              if entry['scope'] in g.oauth.scopes]})
    if method == 'tools/call':
        return _call(identifier, params)
    return _error(identifier, -32601, f'Method not found: {method}')


def _call(identifier, params):
    entry = TOOLS.get(params.get('name'))
    if entry is None:
        return _error(identifier, -32602, f'Unknown tool: {params.get("name")}')
    arguments = params.get('arguments') or {}
    if not isinstance(arguments, dict):
        return _error(identifier, -32602, 'arguments must be an object')
    if entry['scope'] not in g.oauth.scopes:
        return _tool_error(identifier, f'This connection was not granted {entry["scope"]}. Ask the user to reconnect '
                                       'My Next Moov and allow it.')
    unknown = set(arguments) - set(entry['definition']['inputSchema']['properties'])
    if unknown:
        return _tool_error(identifier, 'Unknown arguments: ' + ', '.join(sorted(unknown)))
    try:
        result = entry['handler'](arguments)
    except (ToolError, services.ServiceError) as exc:
        return _tool_error(identifier, str(exc))
    return _result(identifier, {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                                'structuredContent': result, 'isError': False})


def _tool_error(identifier, message):
    # Tool failures are results, not protocol errors, so the model can read them and recover.
    return _result(identifier, {'content': [{'type': 'text', 'text': message}], 'isError': True})
