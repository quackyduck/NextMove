"""Backup/restore uses disposable files and never imports app configuration."""
import sqlite3

import pytest

from scripts.backup import backup, restore, verify


def test_backup_restore_round_trip_and_tamper_detection(tmp_path):
    database = tmp_path / 'source.sqlite3'
    uploads = tmp_path / 'source-uploads'
    uploads.mkdir()
    (uploads / 'opaque-resume.txt').write_bytes(b'Private resume version one')
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT)')
        connection.execute('INSERT INTO jobs VALUES (?, ?)', ('test-id', 'Test role'))
        connection.commit()
        backup(database, uploads, tmp_path / 'snapshot')
    snapshot = tmp_path / 'snapshot'
    restore(snapshot, tmp_path / 'restored')
    with sqlite3.connect(tmp_path / 'restored/database.sqlite3') as connection:
        assert connection.execute('SELECT * FROM jobs').fetchall() == [('test-id', 'Test role')]
    assert (tmp_path / 'restored/uploads/opaque-resume.txt').read_bytes() == b'Private resume version one'
    with pytest.raises(FileExistsError):
        restore(snapshot, tmp_path / 'restored')
    (snapshot / 'uploads/opaque-resume.txt').write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum'):
        verify(snapshot)


def test_backup_rejects_recursive_destinations_and_keeps_nested_manifest(tmp_path):
    database = tmp_path / 'source.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE jobs (id INTEGER)')
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    (uploads / 'manifest.json').write_text('ordinary uploaded content')
    with pytest.raises(ValueError, match='inside uploads'):
        backup(database, uploads, uploads / 'snapshot')
    snapshot = tmp_path / 'snapshot'
    backup(database, uploads, snapshot)
    assert 'uploads/manifest.json' in verify(snapshot)['sha256']
    with pytest.raises(ValueError, match='inside snapshot'):
        restore(snapshot, snapshot / 'restore')
