"""Master-resume intake: sniffing, size, DOCX hardening and path isolation."""
import io
import os
import zipfile

import pytest
from werkzeug.datastructures import FileStorage

from jobtracker import documents
from jobtracker.services import ServiceError

LIMIT = 5 * 1024 * 1024
PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n'
CONTENT_TYPES = (b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 b'wordprocessingml.document.main+xml"/></Types>')


def docx(extra=(), content_types=CONTENT_TYPES, document=b'<w:document/>', compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression) as archive:
        archive.writestr('[Content_Types].xml', content_types)
        archive.writestr('word/document.xml', document)
        for name, data in extra:
            archive.writestr(name, data)
    return buffer.getvalue()


def upload(tmp_path, data, filename='resume.pdf'):
    folder = tmp_path / 'uploads' / '7' / 'master'
    (tmp_path / 'uploads').mkdir(exist_ok=True)
    storage = FileStorage(stream=io.BytesIO(data), filename=filename)
    return documents.receive(storage, folder, LIMIT, folder / '.incoming')


def leftovers(tmp_path):
    return [path for path in (tmp_path / 'uploads').rglob('*') if path.is_file()]


@pytest.mark.parametrize('filename,data,media_type', [
    ('Resume.PDF', PDF, 'application/pdf'),
    ('cv.docx', docx(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    ('notes.txt', 'Résumé — engineer\n'.encode(), 'text/plain'),
])
def test_accepts_real_documents_and_names_them_independently(tmp_path, filename, data, media_type):
    stored = upload(tmp_path, data, filename)
    assert stored.media_type == media_type and stored.size == len(data)
    name = stored.commit()
    assert name.endswith(stored.suffix) and len(name) == 32 + len(stored.suffix)
    target = stored.folder / name
    assert target.read_bytes() == data
    assert oct(target.stat().st_mode & 0o777) == oct(0o600)
    assert oct((tmp_path / 'uploads' / '7').stat().st_mode & 0o777) == oct(0o700)
    assert list((stored.folder / '.incoming').iterdir()) == []


@pytest.mark.parametrize('filename,data', [
    ('resume.pdf', docx()),             # extension says PDF, bytes are a ZIP
    ('resume.docx', PDF),                # extension says DOCX, bytes are a PDF
    ('resume.txt', PDF),                 # sniffed as PDF regardless of the name
    ('resume.txt', b'\xff\xd8\xff\xe0 jpeg bytes'),
    ('resume.txt', b'MZ\x90\x00\x03\x00 executable'),
    ('resume.txt', b'caf\xe9 latin-1 text'),
    ('resume.txt', 'truncated é'.encode()[:-1]),  # partial multibyte character at EOF
    ('resume.docx', b'PK\x03\x04 not really a zip'),
    ('resume.docx', docx()[:200]),
])
def test_type_sniffing_beyond_the_extension(tmp_path, filename, data):
    with pytest.raises(ServiceError) as error:
        upload(tmp_path, data, filename)
    assert error.value.status_code == 400
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize('filename', ['resume.exe', 'resume.doc', 'resume', 'resume.pdf.html'])
def test_rejects_unsupported_extensions_before_writing(tmp_path, filename):
    with pytest.raises(ServiceError):
        upload(tmp_path, PDF, filename)
    assert not (tmp_path / 'uploads' / '7').exists()


def test_size_limit_is_enforced_while_streaming(tmp_path):
    stored = upload(tmp_path, b'a' * LIMIT, 'big.txt')
    stored.discard()
    with pytest.raises(ServiceError) as error:
        upload(tmp_path, b'a' * (LIMIT + 1), 'too-big.txt')
    assert error.value.status_code == 413
    assert leftovers(tmp_path) == []


def test_empty_and_missing_files(tmp_path):
    with pytest.raises(ServiceError, match='empty'):
        upload(tmp_path, b'', 'empty.txt')
    with pytest.raises(ServiceError, match='Choose'):
        documents.receive(None, tmp_path / 'x', LIMIT, tmp_path / 'x' / '.incoming')


def test_zip_bomb_by_ratio(tmp_path):
    bomb = docx(document=b'<w:document>' + b'0' * (20 * 1024 * 1024) + b'</w:document>')
    assert len(bomb) < LIMIT
    with pytest.raises(ServiceError, match='could not be accepted'):
        upload(tmp_path, bomb, 'bomb.docx')
    assert leftovers(tmp_path) == []


def test_zip_bomb_by_declared_total_and_entry_count(tmp_path):
    many = docx(extra=[(f'word/media/{index}.xml', b'x') for index in range(documents.DOCX_MAX_ENTRIES)])
    with pytest.raises(ServiceError):
        upload(tmp_path, many, 'many.docx')
    # Stored (uncompressed) entries are within ratio but exceed the total budget
    # only once declared sizes are summed; build one by patching the header.
    archive = bytearray(docx(compression=zipfile.ZIP_STORED))
    central = archive.rfind(b'PK\x01\x02')
    archive[central + 24:central + 28] = (60 * 1024 * 1024).to_bytes(4, 'little')
    with pytest.raises(ServiceError):
        upload(tmp_path, bytes(archive), 'lying.docx')


def test_overlapping_members_are_rejected(tmp_path):
    # Non-recursive bomb: two central-directory entries pointing at one local file.
    archive = bytearray(docx(compression=zipfile.ZIP_STORED))
    end = archive.rfind(b'PK\x05\x06')
    entries = int.from_bytes(archive[end + 10:end + 12], 'little')
    directory_offset = int.from_bytes(archive[end + 16:end + 20], 'little')
    first = archive[directory_offset:archive.find(b'PK\x01\x02', directory_offset + 4)]
    duplicate = bytearray(first)
    name_length = int.from_bytes(duplicate[28:30], 'little')
    duplicate[46:46 + name_length] = b'X' * name_length
    archive[end:end] = duplicate
    end += len(duplicate)
    archive[end + 8:end + 10] = (entries + 1).to_bytes(2, 'little')
    archive[end + 10:end + 12] = (entries + 1).to_bytes(2, 'little')
    archive[end + 12:end + 16] = (int.from_bytes(archive[end + 12:end + 16], 'little') + len(duplicate)).to_bytes(4, 'little')
    with pytest.raises(ServiceError):
        upload(tmp_path, bytes(archive), 'overlap.docx')


@pytest.mark.parametrize('variant', [
    {'extra': [('word/vbaProject.bin', b'\x00' * 64)]},
    {'extra': [('word/vbaData.xml', b'<x/>')]},
    {'extra': [('word/activeX/activeX1.xml', b'<x/>')]},
    {'content_types': CONTENT_TYPES.replace(b'document.main+xml', b'document.macroEnabled.main+xml')},
    {'extra': [('word/_rels/settings.xml.rels', b'<Relationship Type=".../attachedTemplate" '
                                                  b'Target="https://evil.example/t.dotm" TargetMode="External"/>')]},
])
def test_macro_documents_are_rejected(tmp_path, variant):
    with pytest.raises(ServiceError, match='Macro-enabled'):
        upload(tmp_path, docx(**variant), 'macro.docx')
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize('name', ['../escape.xml', '/absolute.xml', 'word\\back.xml'])
def test_unsafe_member_names_are_rejected(tmp_path, name):
    with pytest.raises(ServiceError):
        upload(tmp_path, docx(extra=[(name, b'<x/>')]), 'names.docx')


def test_prefixed_polyglot_and_comment_are_rejected(tmp_path):
    with pytest.raises(ServiceError):
        upload(tmp_path, PDF + docx(), 'polyglot.docx')
    commented = io.BytesIO(docx())
    with zipfile.ZipFile(commented, 'a') as archive:
        archive.comment = b'%PDF-1.4 hidden'
    with pytest.raises(ServiceError):
        upload(tmp_path, commented.getvalue(), 'comment.docx')


def test_encrypted_members_are_rejected(tmp_path):
    archive = bytearray(docx(compression=zipfile.ZIP_STORED))
    central = archive.rfind(b'PK\x01\x02')
    archive[central + 8] |= 0x1
    with pytest.raises(ServiceError):
        upload(tmp_path, bytes(archive), 'encrypted.docx')


@pytest.mark.parametrize('filename', ['../../etc/passwd.txt', '..\\..\\boot.txt', '/tmp/abs.txt', 'a/b/../../c.txt'])
def test_client_filename_never_reaches_the_filesystem(tmp_path, filename):
    stored = upload(tmp_path, b'plain resume', filename)
    stored.commit()
    assert stored.original_filename == os.path.basename(filename.replace('\\', '/'))
    files = leftovers(tmp_path)
    assert files == [stored.folder / stored.stored_filename]
    assert all('passwd' not in str(path) and 'boot' not in str(path) for path in files)


def test_display_name_strips_control_characters():
    assert documents.display_name('re\x00su\nme‮.txt') == 'resume.txt'
    assert len(documents.display_name('a' * 400 + '.pdf')) == 255


def test_ordinary_word_parts_are_not_mistaken_for_macros(tmp_path):
    ordinary = docx(extra=[('word/printerSettings/printerSettings1.bin', b'\x00' * 64),
                           ('word/embeddings/oleObject1.bin', b'\x00' * 64),
                           ('customXml/item1.xml', b'<x/>'), ('docProps/thumbnail.jpeg', b'\xff\xd8\xff'),
                           ('word/_rels/settings.xml.rels', b'<Relationship Type=".../attachedTemplate" Target="Normal.dotm"/>')])
    upload(tmp_path, ordinary, 'ordinary.docx').discard()


@pytest.mark.parametrize('relationship,accepted', [
    (b'<Relationship Id="r1" Type="http://schemas/attachedTemplate" '
     b'Target="file:///C:\\Users\\me\\Templates\\TF16392740.dotx" TargetMode="External"/>', True),
    (b'<Relationship Target="https://evil.example/t.dotm" TargetMode="External" Type=".../attachedTemplate"/>', False),
    (b'<Relationship Type=".../attachedTemplate" Target="\\\\\\\\host\\share\\t.dotm" TargetMode="External"/>', False),
    (b'<Relationship Type=".../hyperlink" Target="https://example.com/portfolio" TargetMode="External"/>', True),
])
def test_only_remote_attached_templates_are_rejected(tmp_path, relationship, accepted):
    data = docx(extra=[('word/_rels/settings.xml.rels', b'<Relationships>' + relationship + b'</Relationships>')])
    if accepted:
        upload(tmp_path, data, 'template.docx').discard()
    else:
        with pytest.raises(ServiceError, match='Macro-enabled'):
            upload(tmp_path, data, 'template.docx')


def test_repetitive_parts_below_the_ratio_floor_are_accepted(tmp_path):
    styles = b'<w:styles>' + b'<w:style w:type="paragraph"/>' * 80000 + b'</w:styles>'
    assert len(styles) > 2 * 1024 * 1024
    upload(tmp_path, docx(extra=[('word/styles.xml', styles)]), 'styles.docx').discard()


def test_legacy_and_encrypted_office_files_get_a_clear_message(tmp_path):
    with pytest.raises(ServiceError, match='Password-protected and legacy'):
        upload(tmp_path, documents.OLE_MAGIC + b'\x00' * 512, 'locked.docx')
    assert leftovers(tmp_path) == []
