"""Resume files for third-party agents: reading text, saving uploads, and signed links.

Agents get short-lived signed URLs instead of streaming bytes through MCP tool
arguments. A link names its OAuth grant, so revoking the connection (or narrowing
its scopes) also disables every link issued under it. Links are bearer
capabilities: keep their lifetimes short and never log them.
"""
import io
import xml.etree.ElementTree as ElementTree
import zipfile

from flask import Blueprint, current_app, g, jsonify, request, send_file
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from werkzeug.datastructures import FileStorage

from . import documents, services, storage
from .db import get_session
from .models import MasterResume, Resume
from .profile import RESUME_LIMIT, REQUEST_LIMIT, STORED_NAME, master_folder, public, save_version

files = Blueprint('files', __name__)
DOWNLOAD_SECONDS = 300
UPLOAD_SECONDS = 900
WORD = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
BULLET_MARKERS = ('•', '-', '–', '*', '·', '▪')
MAX_DOCUMENT_XML = 8 * 1024 * 1024


@files.errorhandler(services.ServiceError)
def service_error(exc):
    return jsonify(error=str(exc)), exc.status_code


# Locating and reading -----------------------------------------------------
def locate(user_id, resume_id):
    """(record, path, kind) for a job attachment or master resume version this user owns."""
    session = get_session()
    if isinstance(resume_id, str):
        record = session.scalar(select(Resume).where(Resume.id == resume_id, Resume.user_id == user_id))
        if record is not None:
            path = storage.owned_path(current_app.config['UPLOAD_FOLDER'], user_id, record.stored_filename)
            return record, path, 'job'
        record = session.scalar(select(MasterResume).where(MasterResume.id == resume_id, MasterResume.user_id == user_id))
        if record is not None:
            root = master_folder(user_id).resolve()
            path = (root / record.stored_filename).resolve()
            valid = STORED_NAME.fullmatch(record.stored_filename) and path.parent == root
            return record, path if valid else None, 'master'
    raise services.ServiceError('Resume not found', 404)


def latest_master(user_id):
    from .resume_templates import latest_version
    return latest_version(user_id)


def read_paragraphs(path, media_type):
    """[{id, text, kind, style}] for DOCX/TXT, in document order; None for PDF (no text extraction)."""
    if media_type == 'text/plain':
        lines = path.read_text(encoding='utf-8').splitlines()
        return [{'id': f'p{index}', 'text': line.strip(), 'kind': _kind('', line), 'style': ''}
                for index, line in enumerate(lines) if line.strip()]
    if media_type != documents.MEDIA_TYPES['.docx']:
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            data = documents._capped(archive, 'word/document.xml', MAX_DOCUMENT_XML)
        if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
            raise ValueError('DTDs are not allowed')
        root = ElementTree.fromstring(data)
    except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError):
        raise services.ServiceError('This resume could not be read as a Word document', 422) from None
    body = root.find(f'{WORD}body')
    paragraphs = []
    # Body paragraphs and table-cell paragraphs, in reading order.
    for index, paragraph in enumerate(body.iter(f'{WORD}p') if body is not None else []):
        text = ''.join('\t' if node.tag == f'{WORD}tab' else '\n' if node.tag in (f'{WORD}br', f'{WORD}cr')
                       else node.text or '' for node in paragraph.iter()
                       if node.tag in (f'{WORD}t', f'{WORD}tab', f'{WORD}br', f'{WORD}cr')).strip()
        if not text:
            continue
        style_node = paragraph.find(f'{WORD}pPr/{WORD}pStyle')
        style = style_node.get(f'{WORD}val', '') if style_node is not None else ''
        numbered = paragraph.find(f'{WORD}pPr/{WORD}numPr') is not None
        paragraphs.append({'id': f'p{index}', 'text': text, 'kind': 'bullet' if numbered else _kind(style, text),
                           'style': style})
    return paragraphs


def _kind(style, text):
    style = style.lower()
    if style.startswith(('heading', 'title')):
        return 'heading'
    if 'list' in style or text.lstrip().startswith(BULLET_MARKERS):
        return 'bullet'
    return 'body'


# Saving -------------------------------------------------------------------
def save_upload(user_id, target, upload, template_id=None):
    """Validate and store `upload` as a new master version (target 'master') or a job attachment."""
    if target == 'master':
        folder = master_folder(user_id)
        stored = documents.receive(upload, folder, RESUME_LIMIT, folder / '.incoming')
        try:
            stored.commit()
            result, created = save_version(user_id, stored, template_id)
        except BaseException:
            stored.discard()
            raise
        if not created:
            stored.discard()
        return {'target': 'master', 'resume': result, 'created': created}
    services.get_job(user_id, target)  # 404 before any bytes are written.
    folder = storage.user_directory(current_app.config['UPLOAD_FOLDER'], user_id)
    stored = documents.receive(upload, folder, RESUME_LIMIT, folder / '.incoming')
    try:
        stored.commit()
        result = services.create_resume(user_id, target, {
            'original_filename': stored.original_filename or 'resume' + stored.suffix,
            'stored_filename': stored.stored_filename, 'media_type': stored.media_type,
            'size': stored.size, 'sha256': stored.sha256})
    except BaseException:
        stored.discard()
        raise
    return {'target': target, 'resume': result, 'created': True}


def upload_bytes(user_id, target, filename, data):
    return save_upload(user_id, target, FileStorage(io.BytesIO(data), filename=filename))


def metadata(record):
    if isinstance(record, MasterResume):
        return {**public(record), 'kind': 'master'}
    data = services.serialize(record)
    data.pop('stored_filename', None)
    return {**data, 'kind': 'job'}


# Signed links -------------------------------------------------------------
def _serializer(purpose):
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=f'nextmove-{purpose}-link')


def download_link(base, grant_id, resume_id):
    token = _serializer('download').dumps({'g': grant_id, 'r': resume_id})
    return {'url': f'{base}/api/v1/files/{token}', 'expires_in': DOWNLOAD_SECONDS}


def upload_link(base, grant_id, target):
    token = _serializer('upload').dumps({'g': grant_id, 't': target})
    url = f'{base}/api/v1/uploads/{token}'
    return {'url': url, 'expires_in': UPLOAD_SECONDS, 'method': 'POST', 'field': 'file',
            'example': f'curl -sS -F "file=@resume.docx" "{url}"'}


def _redeem(purpose, token, seconds, scope):
    from .oauth import grant_scopes
    try:
        claims = _serializer(purpose).loads(token, max_age=seconds)
    except BadSignature:
        raise services.ServiceError('This link is invalid or has expired; ask your agent for a new one', 404) from None
    granted = grant_scopes(claims.get('g'))
    if granted is None or scope not in granted[1]:
        raise services.ServiceError('This link is invalid or has expired; ask your agent for a new one', 404)
    return granted[0], claims


@files.get('/files/<token>')
def download(token):
    user_id, claims = _redeem('download', token, DOWNLOAD_SECONDS, 'resumes:read')
    record, path, _kind_name = locate(user_id, claims['r'])
    if path is None or not path.is_file():
        raise services.ServiceError('Resume file is unavailable', 404)
    return send_file(path, mimetype=record.media_type, as_attachment=True,
                     download_name=record.original_filename, conditional=False)


@files.post('/uploads/<token>')
def upload(token):
    request.max_content_length = REQUEST_LIMIT
    user_id, claims = _redeem('upload', token, UPLOAD_SECONDS, 'resumes:write')
    g.user_id = user_id
    result = save_upload(user_id, claims['t'], request.files.get('file'))
    return jsonify(result), 201 if result['created'] else 200

