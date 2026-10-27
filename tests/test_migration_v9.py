"""Schema 7/8 -> 9 upgrade: the singleton workspace becomes user #1, losslessly.

Each legacy database is the frozen pre-tenancy DDL (tests/legacy.py) filled by
raw SQL with realistic rows and resume files in the flat upload root. The
upgrade must keep every ID, version and timestamp, move files under uploads/1/
with identical bytes, and be reversible: a failed upgrade leaves the database
untouched, and a pre-upgrade backup restores the old schema and layout.
"""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from werkzeug.security import check_password_hash, generate_password_hash

from jobtracker import create_app, db, migrations, storage

from accounts import sign_in
from legacy import create_legacy_db

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('backup_script', ROOT / 'scripts' / 'backup.py')
backup_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_script)

PASSWORD = 'legacy-workspace-password'
PASSWORD_HASH = generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000')
SESSION_VERSION = 4
TABLES = ('jobs', 'contacts', 'activities', 'resumes', 'agent_tokens', 'extraction_tasks', 'ingestion_requests')
JOBS = [
    ('job-1', 'Staff Engineer', 'Acme', 'applied', 'active', 3),
    ('job-2', 'Platform Lead', 'Globex', 'panel_interview', 'rejected', 7),
    ('job-3', 'Data Engineer', 'Initech', 'offer', 'accepted', 2),
]
FILES = {'resume-1': ('a1b2c3d4-resume.txt', b'Resume for Acme, version one\n'),
         'resume-2': ('e5f6a7b8-resume.pdf', b'%PDF-1.4 resume for Globex\n' + bytes(range(256)))}


def stamp(day, hour=9):
    return f'2026-03-{day:02d} {hour:02d}:15:30.123456'


def seed(path, uploads, version, credential=True):
    """A realistic legacy workspace; returns {stored filename: sha256}."""
    create_legacy_db(path, version, PASSWORD_HASH if credential else None, SESSION_VERSION)
    uploads.mkdir(exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        for day, (job_id, title, company, stage, outcome, job_version) in enumerate(JOBS, start=1):
            connection.execute(
                'INSERT INTO jobs (id, title, company, url, found_date, description, location, salary, lane, priority, '
                'stage, outreach_completed, outcome, notes, next_action, next_action_date, created_at, updated_at, '
                'version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (job_id, title, company, f'https://example.com/{job_id}', f'2026-03-{day:02d}', 'Role description',
                 'Remote', '$150k', 'platform', 'high', stage, day % 2, outcome, 'Notes', 'Follow up',
                 f'2026-04-{day:02d}', stamp(day), stamp(day, 17), job_version))
            connection.execute('INSERT INTO contacts VALUES (?, ?, ?, ?, ?, ?)',
                               (f'contact-{day}', job_id, f'Recruiter {day}', f'r{day}@example.com', '', stamp(day, 10)))
            for index, kind in enumerate(('created', 'stage_changed')):
                connection.execute('INSERT INTO activities VALUES (?, ?, ?, ?, ?, ?)', (
                    f'activity-{day}-{index}', job_id, kind, f'{kind} {title}',
                    json.dumps({'stage': {'from': 'interesting', 'to': stage}} if index else {}), stamp(day, 11 + index)))
        hashes = {}
        for day, (resume_id, (stored, content)) in enumerate(FILES.items(), start=1):
            (uploads / stored).write_bytes(content)
            hashes[stored] = hashlib.sha256(content).hexdigest()
            connection.execute('INSERT INTO resumes VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (
                resume_id, f'job-{day}', f'resume-{day}.txt', stored, 'text/plain', len(content), hashes[stored],
                stamp(day, 12)))
        connection.execute('INSERT INTO agent_tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (
            'token-1', 'Laptop agent', 'nm_ingest_abcdefgh', hashlib.sha256(b'raw-token').hexdigest(),
            '["roles:ingest", "roles:search"]', stamp(1), '2027-03-01 00:00:00.000000', stamp(3), None))
        connection.execute('INSERT INTO extraction_tasks VALUES (?, ?, ?, ?)', (
            'extraction-1', 'succeeded', json.dumps({'fields': {'title': 'Staff Engineer'}, 'warnings': []}), stamp(2)))
        connection.execute('INSERT INTO ingestion_requests VALUES (?, ?, ?, ?, ?, ?, ?)', (
            'receipt-1', 'retry-key-0123456789abcdef', 'token-1', 'https://example.com/job-3', 'succeeded',
            json.dumps({'job_id': 'job-3'}), stamp(3)))
        connection.commit()
    return hashes


def rows(path):
    """Every tenant table's rows by id, without user_id, plus the schema version."""
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        snapshot = {table: [{key: row[key] for key in row.keys() if key != 'user_id'}
                            for row in connection.execute(f'SELECT * FROM {table} ORDER BY id')] for table in TABLES}
        snapshot['version'] = [tuple(row) for row in connection.execute('SELECT version FROM schema_version')]
    return snapshot


def dump(path):
    with closing(sqlite3.connect(path)) as connection:
        return list(connection.iterdump())


def query(path, sql):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql).fetchall()


def digests(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob('*') if path.is_file()}


@pytest.fixture
def legacy(tmp_path, request):
    version = getattr(request, 'param', 8)
    path, uploads = tmp_path / 'jobs.sqlite3', tmp_path / 'uploads'
    hashes = seed(path, uploads, version)
    return SimpleNamespace(version=version, path=path, uploads=uploads, hashes=hashes, before=rows(path),
                           tmp=tmp_path)


def start(legacy, **overrides):
    return create_app({
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'migration-test-secret',
        'APP_PASSWORD_HASH': generate_password_hash('a-different-configured-password', method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{legacy.path}', 'UPLOAD_FOLDER': str(legacy.uploads),
        'LOGIN_THROTTLE_DATABASE': str(legacy.tmp / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
        'BOOTSTRAP_ADMIN_EMAIL': 'me@example.com', **overrides,
    })


def assert_upgraded(legacy):
    after = rows(legacy.path)
    assert after.pop('version') == [(migrations.SCHEMA_VERSION,)]
    assert after == {table: legacy.before[table] for table in TABLES}
    for table in TABLES:
        assert query(legacy.path, f'SELECT DISTINCT user_id FROM {table}') == [(1,)]
    assert query(legacy.path, "SELECT name FROM sqlite_master WHERE name='workspace_credentials'") == []
    assert query(legacy.path, 'PRAGMA foreign_key_check') == []
    assert query(legacy.path, 'PRAGMA integrity_check') == [('ok',)]
    assert digests(legacy.uploads) == {f'1/{name}': digest for name, digest in legacy.hashes.items()}
    assert (legacy.uploads / '1').stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('legacy', [7, 8], indirect=True, ids=['schema7', 'schema8'])
def test_upgrade_keeps_every_row_and_file_for_user_one(legacy):
    snapshot = legacy.tmp / 'snapshot'
    backup_script.backup(legacy.path, legacy.uploads, snapshot)
    app = start(legacy)
    assert_upgraded(legacy)
    [(user_id, email, password_hash, session_version, owner)] = query(
        legacy.path, 'SELECT id, email, password_hash, session_version, owns_legacy_tokens FROM users')
    assert (user_id, email, password_hash, session_version, owner) == (1, 'me@example.com', PASSWORD_HASH,
                                                                       SESSION_VERSION, 1)

    client = app.test_client()
    sign_in(client, 'me@example.com', PASSWORD)
    jobs = client.get('/api/v1/jobs').json['jobs']
    assert sorted((job['id'], job['version'], job['stage'], job['outcome']) for job in jobs) == sorted(
        (job_id, version, stage, outcome) for job_id, _, _, stage, outcome, version in JOBS)
    for day, (resume_id, (_, content)) in enumerate(FILES.items(), start=1):
        assert [resume['id'] for resume in client.get(f'/api/v1/jobs/job-{day}/resumes').json['resumes']] == [resume_id]
        assert client.get(f'/api/v1/resumes/{resume_id}/download').data == content
    assert len(client.get('/api/v1/jobs/job-2/activities').json['activities']) == 2
    assert client.get('/api/v1/jobs/job-1/contacts').json['contacts'][0]['name'] == 'Recruiter 1'
    assert client.get('/api/v1/extractions/extraction-1').status_code in (200, 404)  # expires after a day by design

    # Rollback path (b): the pre-upgrade backup restores schema N and the flat layout.
    restored = legacy.tmp / 'restored'
    backup_script.restore(snapshot, restored)
    assert rows(restored / 'database.sqlite3') == legacy.before
    assert query(restored / 'database.sqlite3', 'SELECT password_hash, session_version FROM workspace_credentials') == [
        (PASSWORD_HASH, SESSION_VERSION)]
    assert digests(restored / 'uploads') == legacy.hashes


@pytest.mark.parametrize('legacy', [7, 8], indirect=True, ids=['schema7', 'schema8'])
def test_failed_upgrade_rolls_back_completely(legacy, monkeypatch):
    original = migrations.upgrade_v8_to_v9
    before, raw = dump(legacy.path), legacy.path.read_bytes()

    def upgrade_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('simulated crash after the upgrade ran')

    monkeypatch.setattr(migrations, 'upgrade_v8_to_v9', upgrade_then_crash)
    with pytest.raises(RuntimeError, match='simulated crash'):
        start(legacy)
    # Every page is byte-identical; only the 100-byte file header differs (the
    # engine switches to WAL and bumps SQLite's change counter on connect).
    assert legacy.path.read_bytes()[100:] == raw[100:]
    assert dump(legacy.path) == before
    assert rows(legacy.path) == legacy.before
    assert digests(legacy.uploads) == legacy.hashes
    monkeypatch.undo()
    start(legacy)
    assert_upgraded(legacy)


def test_placeholder_email_when_none_is_configured(legacy):
    app = start(legacy, BOOTSTRAP_ADMIN_EMAIL=None)
    assert query(legacy.path, 'SELECT id, email FROM users') == [(1, migrations.PLACEHOLDER_ADMIN_EMAIL)]
    sign_in(app.test_client(), 'owner@localhost', PASSWORD)


def test_invalid_bootstrap_email_aborts_the_upgrade(legacy):
    with pytest.raises(RuntimeError, match='BOOTSTRAP_ADMIN_EMAIL'):
        start(legacy, BOOTSTRAP_ADMIN_EMAIL='not an email')
    assert rows(legacy.path) == legacy.before
    assert query(legacy.path, "SELECT name FROM sqlite_master WHERE name='users'") == []
    assert digests(legacy.uploads) == legacy.hashes


def test_restart_moves_and_duplicates_nothing(legacy):
    start(legacy)
    users = query(legacy.path, 'SELECT * FROM users')
    start(legacy)
    assert_upgraded(legacy)
    assert query(legacy.path, 'SELECT * FROM users') == users


def test_startup_finishes_an_interrupted_file_relocation(legacy, monkeypatch):
    moves = []

    def crash_on_second_move(source, target):
        if moves:
            raise OSError('simulated crash mid-relocation')
        moves.append(target)
        os.rename(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, 'replace', crash_on_second_move)
        with pytest.raises(OSError, match='mid-relocation'):
            start(legacy)
    layout = digests(legacy.uploads)
    assert len(layout) == 2 and sum(name.startswith('1/') for name in layout) == 1
    assert query(legacy.path, 'SELECT version FROM schema_version') == [(migrations.SCHEMA_VERSION,)]
    start(legacy)
    assert_upgraded(legacy)


@pytest.mark.parametrize('with_rows', [False, True], ids=['empty', 'with-jobs'])
def test_no_credential_and_no_configured_hash_refuses_to_upgrade(tmp_path, with_rows):
    path = tmp_path / 'jobs.sqlite3'
    if with_rows:
        seed(path, tmp_path / 'uploads', 8, credential=False)
    else:
        create_legacy_db(path, 8)
    before = dump(path)
    # create_app always configures a verifier (development generates one), so
    # drive the web process's migration step directly with none configured.
    engine = create_engine(f'sqlite:///{path}')
    with pytest.raises(RuntimeError, match='no credential'):
        db.migrate_or_create(SimpleNamespace(config={}), engine)
    engine.dispose()
    assert dump(path) == before
    assert query(path, 'SELECT version FROM schema_version') == [(8,)]


def test_configured_hash_seeds_the_owner_when_the_credential_is_missing(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    hashes = seed(path, tmp_path / 'uploads', 8, credential=False)
    legacy = SimpleNamespace(path=path, uploads=tmp_path / 'uploads', hashes=hashes, before=rows(path), tmp=tmp_path)
    configured = generate_password_hash('configured-password', method='pbkdf2:sha256:1000')
    app = start(legacy, APP_PASSWORD_HASH=configured)
    assert_upgraded(legacy)
    assert query(path, 'SELECT id, email, password_hash, session_version, owns_legacy_tokens FROM users') == [
        (1, 'me@example.com', configured, 1, 1)]
    client = app.test_client()
    sign_in(client, 'me@example.com', 'configured-password')
    assert len(client.get('/api/v1/jobs').json['jobs']) == 3


def test_signups_after_the_upgrade_never_take_or_reuse_ids(legacy):
    app = start(legacy, SIGNUP_REQUIRES_INVITE=False)

    def signup(email):
        response = app.test_client().post('/api/v1/auth/signup', json={
            'email': email, 'password': 'new-user-password', 'password_confirmation': 'new-user-password'})
        assert response.status_code == 201
        return response.json['user']['id']

    first = signup('second@example.com')
    assert first >= 2
    with closing(sqlite3.connect(legacy.path)) as connection:
        connection.execute('DELETE FROM users WHERE id=?', (first,))
        connection.commit()
    assert signup('third@example.com') > first
    assert query(legacy.path, 'SELECT id FROM users WHERE owns_legacy_tokens=1') == [(1,)]
    assert check_password_hash(query(legacy.path, 'SELECT password_hash FROM users WHERE id=1')[0][0], PASSWORD)
