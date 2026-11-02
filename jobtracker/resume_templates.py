"""Resume templates: named master resumes, one per kind of role, each with versions numbered 1, 2, ...

The default is "the master resume" wherever no template is named. It is never empty: the first template to get a
version becomes it, and only templates with versions can be promoted. Writes take the write lock before reading.
"""
from contextlib import contextmanager
from datetime import timezone
import unicodedata

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
def _clean(data, field, limit, allowed=''):
    """NFC text without control or format characters (bidi overrides, NUL...) other than `allowed`."""
    value = unicodedata.normalize('NFC', services._text(data[field], field, limit))
    if any(unicodedata.category(char) in ('Cc', 'Cf') and char not in allowed for char in value):
        raise services.ServiceError(f'{field} cannot contain control or formatting characters')
    return value


def _fields(data, allowed):
    """Validated name/description (and is_default when allowed) from a create or update body."""
    services._payload(data, allowed)
    fields = {}
    if 'name' in data:
        fields['name'] = _clean(data, 'name', NAME_LIMIT)
        if not fields['name']:
            raise services.ServiceError('name cannot be empty')
    if 'description' in data:
        fields['description'] = _clean(data, 'description', DESCRIPTION_LIMIT, allowed='\n\t')
    if 'is_default' in data:
        if data['is_default'] is not True:
            raise services.ServiceError('is_default can only be set to true; make another template the default instead')
        fields['is_default'] = True
    return fields


def _check_name(connection, user_id, name, except_id=None):
    rows = connection.execute(select(TEMPLATES.c.id, TEMPLATES.c.name).where(TEMPLATES.c.user_id == user_id)).all()
    if any(row.id != except_id and row.name.casefold() == name.casefold() for row in rows):
        raise services.ServiceError('A template with this name already exists', 409)
    return rows


def create_template(user_id, data):
    """A new, empty template; it becomes the default only once it has a version."""
    fields = _fields(data, {'name', 'description'})
    if 'name' not in fields:
        raise services.ServiceError('name is required')
    with write_lock() as connection:
        if len(_check_name(connection, user_id, fields['name'])) >= MAX_TEMPLATES:
            raise services.ServiceError(f'You can keep up to {MAX_TEMPLATES} resume templates', 409)
        values = {'description': '', **fields, 'id': new_id(), 'user_id': user_id, 'is_default': False,
                  'created_at': now(), 'updated_at': now()}
        connection.execute(insert(TEMPLATES).values(**values))
    return serialize(values, [], full=True)


def update_template(user_id, template_id, data):
    """Rename, describe, or make default. A default stops being one only when another template takes over."""
    fields = _fields(data, {'name', 'description', 'is_default'})
    with write_lock() as connection:
        template_id = owned(connection, user_id, template_id)['id']
        if 'name' in fields:
            _check_name(connection, user_id, fields['name'], except_id=template_id)
        if 'is_default' in fields:
            if not versions(connection, user_id, template_id):
                raise services.ServiceError('Upload a version to this template before making it the default', 409)
            connection.execute(update(TEMPLATES).where(TEMPLATES.c.user_id == user_id).values(is_default=False))
        connection.execute(update(TEMPLATES).where(TEMPLATES.c.id == template_id).values(**fields, updated_at=now()))
        return serialize(owned(connection, user_id, template_id), versions(connection, user_id, template_id))


def target_template(connection, user_id, template_id=None):
    """The template ID an upload lands in, inside the caller's locked transaction; None means the default.

    With no default yet (no versions at all), the target becomes it. For None that is the account's only
    template when it has exactly one, else "Main resume" (reused by name, or created).
    """
    default = connection.scalar(select(TEMPLATES.c.id).where(TEMPLATES.c.user_id == user_id, TEMPLATES.c.is_default))
    if template_id is not None:
        template_id = owned(connection, user_id, template_id)['id']
    elif default is not None:
        return default
    else:
        rows = connection.execute(select(TEMPLATES.c.id, TEMPLATES.c.name).where(TEMPLATES.c.user_id == user_id)).all()
        named = [row.id for row in rows if len(rows) == 1 or row.name.casefold() == DEFAULT_NAME.casefold()]
        template_id = named[0] if named else None
        if template_id is None and len(rows) >= MAX_TEMPLATES:
            raise services.ServiceError('Upload into one of your resume templates', 409)
    if template_id is None:
        template_id = new_id()
        connection.execute(insert(TEMPLATES).values(id=template_id, user_id=user_id, name=DEFAULT_NAME, description='',
                                                    is_default=False, created_at=now(), updated_at=now()))
    if default is None:
        connection.execute(update(TEMPLATES).where(TEMPLATES.c.id == template_id).values(is_default=True))
    return template_id


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
