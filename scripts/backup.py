#!/usr/bin/env python3
"""Offline coordinated SQLite + uploads snapshot; never loads app configuration."""
import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(snapshot):
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    actual = {str(path.relative_to(snapshot)): digest(path) for path in snapshot.rglob('*')
              if path.is_file() and path != snapshot / 'manifest.json'}
    if any(path.is_symlink() for path in snapshot.rglob('*')):
        raise ValueError('Snapshot may not contain symbolic links')
    if actual != manifest['sha256']:
        raise ValueError('Snapshot checksum mismatch')
    with sqlite3.connect((snapshot / 'database.sqlite3').as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity check failed')
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('SQLite foreign key check failed')
    return manifest


def backup(database, uploads, destination):
    if destination.is_relative_to(uploads):
        raise ValueError('Snapshot destination cannot be inside uploads')
    if not database.is_file() or not uploads.is_dir():
        raise ValueError('Database file and uploads directory must exist')
    if any(path.is_symlink() for path in uploads.rglob('*')) or uploads.is_symlink():
        raise ValueError('Uploads may not contain symbolic links')
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(destination / 'database.sqlite3') as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
    shutil.copytree(uploads, destination / 'uploads')
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'sha256': {
        str(path.relative_to(destination)): digest(path)
        for path in destination.rglob('*') if path.is_file()}}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    verify(destination)


def restore(snapshot, destination):
    if destination.is_relative_to(snapshot):
        raise ValueError('Restore destination cannot be inside snapshot')
    verify(snapshot)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    shutil.copy2(snapshot / 'database.sqlite3', destination / 'database.sqlite3')
    shutil.copytree(snapshot / 'uploads', destination / 'uploads')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--maintenance', action='store_true', required=True,
                        help='Confirm ALL web/worker/agent writers are stopped; this script does not stop them')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('backup')
    create.add_argument('--database', type=Path, required=True)
    create.add_argument('--uploads', type=Path, required=True)
    create.add_argument('--destination', type=Path, required=True)
    recover = commands.add_parser('restore')
    recover.add_argument('--snapshot', type=Path, required=True)
    recover.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'backup':
            backup(args.database.resolve(), args.uploads.resolve(), args.destination.resolve())
        else:
            restore(args.snapshot.resolve(), args.destination.resolve())
    except (ValueError, OSError, sqlite3.Error, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f'{error}\n')
    print(f'{args.command} complete: {args.destination.resolve()}')


if __name__ == '__main__':
    main()
