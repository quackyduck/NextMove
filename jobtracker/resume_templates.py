"""Resume templates: named master resumes, one per kind of role, each with versions numbered 1, 2, ...

The default template is "the master resume" wherever none is named: the first template, or "Main resume" on the
first upload, until another is promoted. Nothing is deleted. Writes take the SQLite write lock before reading.
Comparing two resumes is a paragraph-level diff of their text (DOCX and TXT; PDFs have no text layer we read).
"""
import difflib
from contextlib import contextmanager
from datetime import timezone

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func, insert, select, update

from . import services
from .auth import require_browser_auth, require_scope
from .db import get_session
from .models import MasterResume, ResumeTemplate, new_id, now
from .profile import REQUEST_LIMIT, public, send_version
from .resume_files import save_upload

templates = Blueprint('templates', __name__)
TEMPLATES, VERSIONS = ResumeTemplate.__table__, MasterResume.__table__
DEFAULT_NAME, NOT_FOUND = 'Main resume', 'Template not found'
MAX_TEMPLATES, NAME_LIMIT, DESCRIPTION_LIMIT = 10, 100, 2000
MAX_PARAGRAPHS, MAX_CHANGES = 2000, 500  # difflib is quadratic: 2,000 a side stays well under a second.
FIELDS = ('id', 'name', 'description', 'is_default', 'created_at', 'updated_at')


@templates.errorhandler(services.ServiceError)
def service_error(exc):
    return jsonify(error=str(exc)), exc.status_code


@contextmanager
def write_lock():
    with current_app.extensions['jobtracker_db']['engine'].begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        yield connection


# Reading ------------------------------------------------------------------
def owned(connection, user_id, template_id):
    """The caller's template; another account's ID fails exactly like an unknown one."""
    row = connection.execute(select(TEMPLATES).where(TEMPLATES.c.id == template_id, TEMPLATES.c.user_id == user_id)
                             ).mappings().first()
    if row is None:
        raise services.ServiceError(NOT_FOUND, 404)
    return row


def versions(connection, user_id, template_id):
    return connection.execute(select(VERSIONS).where(VERSIONS.c.user_id == user_id, VERSIONS.c.template_id == template_id)
                              .order_by(VERSIONS.c.version.desc())).mappings().all()


def serialize(template, rows, full=False):
    """Metadata, version count and latest version; `full` adds every version, newest first."""
    iso = lambda value: value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat().replace('+00:00', 'Z')
    result = {key: template[key] for key in FIELDS}
    result.update(created_at=iso(template['created_at']), updated_at=iso(template['updated_at']),
                  version_count=len(rows), latest=public(rows[0]) if rows else None)
    if full:
        result['versions'] = [public(row) for row in rows]
    return result


def list_templates(user_id):
    connection = get_session().connection()
    rows = connection.execute(select(TEMPLATES).where(TEMPLATES.c.user_id == user_id).order_by(
        TEMPLATES.c.is_default.desc(), func.lower(TEMPLATES.c.name))).mappings().all()
    return [serialize(row, versions(connection, user_id, row['id'])) for row in rows]


def get_template(user_id, template_id):
    connection = get_session().connection()
    template = owned(connection, user_id, template_id)
    return serialize(template, versions(connection, user_id, template['id']), full=True)


def default_template(user_id):
    """(template, its versions newest first) for the default template, or (None, [])."""
    connection = get_session().connection()
    template = connection.execute(select(TEMPLATES).where(TEMPLATES.c.user_id == user_id, TEMPLATES.c.is_default)
                                  ).mappings().first()
    return (template, versions(connection, user_id, template['id'])) if template else (None, [])


def latest_version(user_id, template_id=None):
    """The newest MasterResume of the caller's `template_id` (the default template when None), or None."""
    chosen = ResumeTemplate.is_default if template_id is None else ResumeTemplate.id == template_id
    return get_session().scalars(select(MasterResume).join(ResumeTemplate).where(MasterResume.user_id == user_id, chosen)
                                 .order_by(MasterResume.version.desc()).limit(1)).first()


# Writing ------------------------------------------------------------------
def _fields(data):
    """Validated name/description/is_default from a create or update body."""
    services._payload(data, {'name', 'description', 'is_default'})
    fields = {}
    if 'name' in data:
        fields['name'] = services._text(data['name'], 'name', NAME_LIMIT)
        if not fields['name']:
            raise services.ServiceError('name cannot be empty')
    if 'description' in data:
        fields['description'] = services._text(data['description'], 'description', DESCRIPTION_LIMIT)
    if 'is_default' in data and type(data['is_default']) is not bool:
        raise services.ServiceError('is_default must be true or false')
    return fields


def _check_name(connection, user_id, name, except_id=None):
    rows = connection.execute(select(TEMPLATES.c.id, TEMPLATES.c.name).where(TEMPLATES.c.user_id == user_id)).all()
    if any(row.id != except_id and row.name.casefold() == name.casefold() for row in rows):
        raise services.ServiceError('A template with this name already exists', 409)
    return rows


def create_template(user_id, data):
    fields = _fields(data)
    if 'name' not in fields:
        raise services.ServiceError('name is required')
    with write_lock() as connection:
        existing = _check_name(connection, user_id, fields['name'])
        if len(existing) >= MAX_TEMPLATES:
            raise services.ServiceError(f'You can keep up to {MAX_TEMPLATES} resume templates', 409)
        is_default = data.get('is_default', False) or not existing  # An account's first template is its default.
        if is_default:
            connection.execute(update(TEMPLATES).where(TEMPLATES.c.user_id == user_id).values(is_default=False))
        values = {'description': '', **fields, 'id': new_id(), 'user_id': user_id, 'is_default': is_default,
                  'created_at': now(), 'updated_at': now()}
        connection.execute(insert(TEMPLATES).values(**values))
    return serialize(values, [], full=True)


def update_template(user_id, template_id, data):
    """Rename, describe, or make default. A default stops being one only when another template takes over."""
    fields = _fields(data)
    if data.get('is_default') is False:
        raise services.ServiceError('is_default can only be set to true; make another template the default instead')
    with write_lock() as connection:
        template_id = owned(connection, user_id, template_id)['id']
        if 'name' in fields:
            _check_name(connection, user_id, fields['name'], except_id=template_id)
        if data.get('is_default'):
            connection.execute(update(TEMPLATES).where(TEMPLATES.c.user_id == user_id).values(is_default=False))
            fields['is_default'] = True
        connection.execute(update(TEMPLATES).where(TEMPLATES.c.id == template_id).values(**fields, updated_at=now()))
        return serialize(owned(connection, user_id, template_id), versions(connection, user_id, template_id))


def target_template(connection, user_id, template_id=None):
    """The template ID an upload lands in, inside the caller's locked transaction; None means the default.
    No default means no templates at all (the first is always the default): create "Main resume"."""
    if template_id is not None:
        return owned(connection, user_id, template_id)['id']
    found = connection.scalar(select(TEMPLATES.c.id).where(TEMPLATES.c.user_id == user_id, TEMPLATES.c.is_default))
    if found is None:
        found = new_id()
        connection.execute(insert(TEMPLATES).values(id=found, user_id=user_id, name=DEFAULT_NAME, description='',
                                                    is_default=True, created_at=now(), updated_at=now()))
    return found


# Comparing ----------------------------------------------------------------
def _resolve(user_id, identifier, field):
    """(record, path, template or None) for a template ID (its latest version) or any resume version the caller owns."""
    from .resume_files import locate
    if not isinstance(identifier, str) or not identifier.strip():
        raise services.ServiceError(f'{field} is required: a template ID or a resume version ID')
    identifier, connection = identifier.strip(), get_session().connection()
    template = connection.execute(select(TEMPLATES).where(TEMPLATES.c.id == identifier, TEMPLATES.c.user_id == user_id)
                                  ).mappings().first()
    if template is not None:
        rows = versions(connection, user_id, template['id'])
        if not rows:
            raise services.ServiceError(f'Template "{template["name"]}" has no uploaded version yet', 409)
        identifier = rows[0]['id']
    try:
        record, path, _ = locate(user_id, identifier)
    except services.ServiceError:  # Unknown and another account's IDs alike.
        raise services.ServiceError(f'{field}: no template or resume with this ID', 404) from None
    if template is None and isinstance(record, MasterResume):
        template = owned(connection, user_id, record.template_id)
    if path is None or not path.is_file():
        raise services.ServiceError('Resume file is unavailable', 404)
    return record, path, template


def _paragraphs(record, path):
    from .resume_files import read_paragraphs
    paragraphs = read_paragraphs(path, record.media_type, MAX_PARAGRAPHS)
    if paragraphs is None:
        raise services.ServiceError(f'"{record.original_filename}" is a PDF; only DOCX and TXT resumes can be compared. '
                                    'Upload a DOCX or TXT version to compare it.', 422)
    if len(paragraphs) > MAX_PARAGRAPHS:
        raise services.ServiceError(f'"{record.original_filename}" has more than {MAX_PARAGRAPHS} paragraphs, '
                                    'too many to compare', 422)
    return paragraphs


def _sections(paragraphs):
    """The heading each paragraph sits under ('' before the first heading)."""
    current, result = '', []
    for paragraph in paragraphs:
        current = paragraph['text'] if paragraph['kind'] == 'heading' else current
        result.append(current)
    return result


def diff_paragraphs(old, new):
    """Paragraph-level changes turning `old` into `new`: (changes, summary).

    Each change is {op: added|removed|changed, section, from, to}, with `from`/`to` {id, text, kind} or None.
    A replaced run is paired in order as "changed"; any surplus is added or removed. `section` is the heading the
    paragraph sits under in `new` (in `old` for a removal).
    """
    sections = _sections(old), _sections(new)
    view = lambda item: {'id': item['id'], 'text': item['text'], 'kind': item['kind']}
    matcher = difflib.SequenceMatcher(a=[item['text'] for item in old], b=[item['text'] for item in new], autojunk=False)
    changes, summary = [], {'added': 0, 'removed': 0, 'changed': 0, 'unchanged': 0}

    def record(op, i=None, j=None):
        summary[op] += 1
        changes.append({'op': op, 'section': sections[1][j] if j is not None else sections[0][i],
                        'from': None if i is None else view(old[i]), 'to': None if j is None else view(new[j])})

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            summary['unchanged'] += i2 - i1
            continue
        paired = min(i2 - i1, j2 - j1) if tag == 'replace' else 0
        for offset in range(paired):
            record('changed', i1 + offset, j1 + offset)
        for i in range(i1 + paired, i2):
            record('removed', i=i)
        for j in range(j1 + paired, j2):
            record('added', j=j)
    return changes, summary


def compare(user_id, from_id, to_id):
    """Differences between two resumes; each ID is a template (its latest version) or a resume version.
    `summary` counts every change; `changes` holds the first MAX_CHANGES, and `truncated` says whether more exist."""
    from .resume_files import metadata
    sides = [_resolve(user_id, from_id, 'from'), _resolve(user_id, to_id, 'to')]
    result = {name: {**metadata(record), 'template': template and {'id': template['id'], 'name': template['name']}}
              for name, (record, _, template) in zip(('from', 'to'), sides)}
    old, new = (_paragraphs(record, path) for record, path, _ in sides)
    identical = sides[0][0].sha256 == sides[1][0].sha256
    if identical:  # Same bytes: nothing to diff.
        changes, summary = [], {'added': 0, 'removed': 0, 'changed': 0, 'unchanged': len(old)}
    else:
        changes, summary = diff_paragraphs(old, new)
    return {**result, 'identical': identical, 'summary': summary,
            'changes': changes[:MAX_CHANGES], 'truncated': len(changes) > MAX_CHANGES}


# Routes -------------------------------------------------------------------
@templates.get('/templates')
@require_scope('resumes:read', fallback=require_browser_auth)
def templates_list():
    return jsonify(templates=list_templates(g.user_id))


@templates.post('/templates')
@require_scope('resumes:write', fallback=require_browser_auth)
def templates_create():
    return jsonify(create_template(g.user_id, request.get_json(silent=True))), 201


@templates.get('/templates/<template_id>')
@require_scope('resumes:read', fallback=require_browser_auth)
def templates_get(template_id):
    return jsonify(get_template(g.user_id, template_id))


@templates.patch('/templates/<template_id>')
@require_scope('resumes:write', fallback=require_browser_auth)
def templates_update(template_id):
    return jsonify(update_template(g.user_id, template_id, request.get_json(silent=True)))


@templates.post('/templates/<template_id>/versions')
@require_scope('resumes:write', fallback=require_browser_auth)
def templates_upload(template_id):
    request.max_content_length = REQUEST_LIMIT
    get_template(g.user_id, template_id)  # 404 before any bytes are written; checked again under the lock.
    saved = save_upload(g.user_id, 'master', request.files.get('file'), template_id)
    return jsonify(resume=saved['resume'], created=saved['created']), 201 if saved['created'] else 200


@templates.get('/templates/<template_id>/download')
@require_scope('resumes:read', fallback=require_browser_auth)
def templates_download(template_id):
    get_template(g.user_id, template_id)
    return send_version(g.user_id, template_id, request.args.get('version'))


@templates.get('/resume-diff')
@require_scope('resumes:read', fallback=require_browser_auth)
def resume_diff():
    return jsonify(compare(g.user_id, request.args.get('from'), request.args.get('to')))
