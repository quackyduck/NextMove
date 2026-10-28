"""Streamed, content-sniffed document intake. Client filenames never form paths."""
import codecs
import hashlib
import os
import re
from pathlib import Path
import uuid
import zipfile

from .services import ServiceError

CHUNK = 64 * 1024
MEDIA_TYPES = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.txt': 'text/plain',
}
# DOCX is a ZIP container; bound what reading it could ever cost.
DOCX_MAX_ENTRIES = 1000
DOCX_MAX_UNCOMPRESSED = 50 * 1024 * 1024
DOCX_MAX_RATIO = 100
# Small repetitive XML parts (styles, numbering) compress far beyond the ratio
# legitimately; only large entries are ratio-checked. The total cap bounds the rest.
DOCX_RATIO_FLOOR = 10 * 1024 * 1024
OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
# An attached template fetched from the network can bring macros in without the
# file containing any. Local templates (Word's resume templates) are ordinary.
RELATIONSHIP = re.compile(rb'<(?:\w+:)?Relationship\b[^>]*>', re.I)
REMOTE_TARGET = re.compile(rb'\bTarget\s*=\s*["\']\s*(?:https?:|ftp:|\\\\|//)', re.I)


def remote_template(relationships):
    return any(b'attachedtemplate' in element.lower() and REMOTE_TARGET.search(element)
               for element in RELATIONSHIP.findall(relationships))


class StoredUpload:
    """A validated file at a temporary path; commit() names it, discard() removes it."""

    def __init__(self, folder, temporary, original_filename, suffix, size, sha256):
        self.folder, self.temporary, self.suffix = folder, temporary, suffix
        self.original_filename = original_filename
        self.size, self.sha256 = size, sha256
        self.media_type = MEDIA_TYPES[suffix]
        self.stored_filename = None

    def commit(self):
        self.stored_filename = f'{uuid.uuid4().hex}{self.suffix}'
        os.replace(self.temporary, self.folder / self.stored_filename)
        return self.stored_filename

    def discard(self):
        self.temporary.unlink(missing_ok=True)
        if self.stored_filename:
            (self.folder / self.stored_filename).unlink(missing_ok=True)


def display_name(filename):
    # Shown back to the owner and used as a download name only.
    name = filename.replace('\\', '/').split('/')[-1].strip()
    return ''.join(char for char in name if char.isprintable())[:255]


def receive(upload, folder, limit, incoming):
    """Stream `upload` to a private temp file in `incoming`, then sniff it.

    Rejects oversized files while reading, so at most `limit + CHUNK` bytes ever
    reach disk. The caller commits on success or discards on any later failure.
    """
    if upload is None or not upload.filename:
        raise ServiceError('Choose a resume file to upload')
    filename = display_name(upload.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise ServiceError('Resume must be a PDF, DOCX, or plain-text (UTF-8) file')
    folder, incoming = Path(folder), Path(incoming)
    for directory in (folder.parent, folder, incoming):
        # parents=True would create intermediate folders with umask permissions.
        directory.mkdir(exist_ok=True, mode=0o700)
    temporary = incoming / f'upload-{uuid.uuid4().hex}'
    digest, size = hashlib.sha256(), 0
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            while chunk := upload.stream.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise ServiceError(f'Resume must be {limit // (1024 * 1024)} MB or smaller', 413)
                digest.update(chunk)
                stream.write(chunk)
        if not size:
            raise ServiceError('The selected file is empty')
        kind = sniff(temporary)
        if kind == '.ole':
            raise ServiceError('Password-protected and legacy .doc files are not supported. Save it as a regular .docx or PDF.')
        if kind != suffix:
            raise ServiceError('File content does not match its extension; upload a real PDF, DOCX, or TXT file')
        check = {'.docx': check_docx, '.txt': check_text}.get(suffix)
        if check:
            check(temporary)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return StoredUpload(folder, temporary, filename, suffix, size, digest.hexdigest())


def sniff(path):
    """Classify by magic bytes, independent of the client's name or MIME type."""
    with open(path, 'rb') as stream:
        head = stream.read(8)
    if head.startswith(b'%PDF-'):
        return '.pdf'
    if head.startswith(b'PK\x03\x04'):
        return '.docx'
    if head == OLE_MAGIC:
        return '.ole'  # Legacy .doc or a password-protected Office file.
    # Anything else is a text candidate; check_text rejects binary formats
    # (images, executables, legacy .doc) by their NUL bytes or invalid UTF-8.
    return '.txt'


def _capped(archive, name, limit=1024 * 1024):
    with archive.open(name) as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise ValueError('oversized part')
    return data


def check_docx(path):
    invalid = ServiceError('This DOCX file could not be accepted. Re-save it as a standard Word document (.docx) and try again.')
    macro = ServiceError('Macro-enabled Word files are not accepted. Save it as a regular .docx and try again.')
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (not entries or len(entries) > DOCX_MAX_ENTRIES or len(set(names)) != len(names)
                    or archive.comment or min(entry.header_offset for entry in entries) != 0):
                raise invalid  # Duplicates, prefixes and comments hide polyglots.
            if not {'[Content_Types].xml', 'word/document.xml'} <= set(names):
                raise invalid
            total, spans = 0, []
            for entry in entries:
                name = entry.filename
                if entry.flag_bits & 0x1 or name.startswith('/') or '..' in name.split('/') or '\\' in name:
                    raise invalid
                total += entry.file_size
                if total > DOCX_MAX_UNCOMPRESSED:
                    raise invalid
                if entry.file_size > DOCX_RATIO_FLOOR and entry.file_size / max(entry.compress_size, 1) > DOCX_MAX_RATIO:
                    raise invalid
                lowered = name.lower()
                # Name the VBA and ActiveX parts precisely: ordinary files carry
                # other .bin parts (printer settings, embedded OLE objects).
                if 'vbaproject' in lowered or 'vbadata' in lowered or 'activex/' in lowered:
                    raise macro
                spans.append((entry.header_offset, entry.header_offset + 30 + len(entry.orig_filename.encode()) + len(entry.extra) + entry.compress_size))
            spans.sort()
            # Members sharing compressed bytes are the classic non-recursive zip bomb.
            if any(previous[1] > current[0] for previous, current in zip(spans, spans[1:])):
                raise invalid
            if spans[-1][1] > archive.start_dir:
                raise invalid
            content_types = _capped(archive, '[Content_Types].xml').lower()
            if b'macroenabled' in content_types or b'vbaproject' in content_types:
                raise macro
            if 'word/_rels/settings.xml.rels' in names:
                if remote_template(_capped(archive, 'word/_rels/settings.xml.rels')):
                    raise macro
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, EOFError, KeyError, ValueError, OSError):
        raise invalid from None


def check_text(path):
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    try:
        with open(path, 'rb') as stream:
            while chunk := stream.read(CHUNK):
                if b'\x00' in chunk:
                    raise ValueError()
                decoder.decode(chunk)
            decoder.decode(b'', final=True)
    except (UnicodeDecodeError, ValueError):
        raise ServiceError('Text resumes must be UTF-8 plain text') from None
