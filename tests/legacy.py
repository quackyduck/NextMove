"""Genuine pre-tenancy databases for migration tests.

`fixtures/schema8.sql` is the exact DDL produced by main at schema 8 (commit
0d4b426, the release before accounts). Schema 7 differs only in the jobs outcome
CHECK. Nothing here imports current models, so fixtures cannot drift with them.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3

SCHEMA8 = (Path(__file__).parent / 'fixtures' / 'schema8.sql').read_text()
SCHEMA7 = SCHEMA8.replace(",'accepted','not_interested')", ",'accepted')")


def create_legacy_db(path, version=8, password_hash=None, session_version=1):
    """A new, empty schema-7/8 database file with an optional workspace credential."""
    assert version in (7, 8) and SCHEMA7 != SCHEMA8
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(SCHEMA8 if version == 8 else SCHEMA7)
        connection.execute('INSERT INTO schema_version (version) VALUES (?)', (version,))
        if password_hash:
            connection.execute('INSERT INTO workspace_credentials VALUES (1, ?, ?)', (password_hash, session_version))
        connection.commit()


def database_path(app):
    return app.config['DATABASE_URL'].removeprefix('sqlite:///')


def downgrade_to_v8(app):
    """Rewrite a schema-9 test database as the frozen schema 8 (user #1's rows only).

    Lets older migration tests create data through today's API, then exercise
    the real upgrade path. Resume files move back to the flat upload root.
    """
    app.extensions['jobtracker_db']['engine'].dispose()
    path = database_path(app)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        # Keep children's REFERENCES text pointing at the original table names.
        connection.execute('PRAGMA legacy_alter_table=ON')
        connection.execute('BEGIN')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        current = {table: [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')] for table in tables}
        credential = connection.execute('SELECT password_hash, session_version FROM users WHERE id=1').fetchone()
        for name in [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")]:
            connection.execute(f'DROP INDEX "{name}"')
        legacy = sqlite3.connect(':memory:')
        legacy.executescript(SCHEMA8)
        for (table,) in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table == 'schema_version':
                continue
            columns = [row[1] for row in legacy.execute(f'PRAGMA table_info("{table}")')]
            if table in tables:
                connection.execute(f'ALTER TABLE "{table}" RENAME TO "{table}_v9"')
            ddl = legacy.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            connection.execute(ddl)
            if table in tables:
                listed = ', '.join(f'"{column}"' for column in columns)
                owner = ' WHERE user_id=1' if 'user_id' in current[table] else ''
                connection.execute(f'INSERT INTO "{table}" ({listed}) SELECT {listed} FROM "{table}_v9"{owner}')
                connection.execute(f'DROP TABLE "{table}_v9"')
        for (sql,) in legacy.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
            connection.execute(sql)
        for table in ('oauth_tokens', 'oauth_codes', 'oauth_grants', 'oauth_clients', 'tailored_drafts', 'harness_grants', 'activation_tokens', 'onboarding_steps', 'master_resumes', 'resume_templates',
                      'users', 'invite_codes', 'queued_tasks'):
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        if credential:
            connection.execute('INSERT INTO workspace_credentials VALUES (1, ?, ?)', credential)
        connection.execute('UPDATE schema_version SET version=8')
        connection.commit()
    root = Path(app.config.get('UPLOAD_FOLDER') or '/nonexistent')
    owner = root / '1'
    if owner.is_dir():
        for file in owner.iterdir():
            os.replace(file, root / file.name)
        owner.rmdir()


def without_owner(result):
    """Rows as tuples with any user_id column removed, for before/after comparisons."""
    keys = list(result.keys())
    keep = [index for index, key in enumerate(keys) if key != 'user_id']
    return [tuple(row[index] for index in keep) for row in result]


def rewind_resume_templates(connection):
    """Turn a schema-13 sqlite3 connection's resume tables back into schema 12 (rows kept)."""
    columns = 'id, user_id, version, original_filename, stored_filename, media_type, size, sha256, created_at'
    connection.executescript(
        # legacy_alter_table keeps tailored_drafts' REFERENCES master_resumes pointing at the rebuilt table.
        'PRAGMA foreign_keys=OFF; PRAGMA legacy_alter_table=ON; ALTER TABLE master_resumes RENAME TO master_resumes_v13; '
        'CREATE TABLE master_resumes (id VARCHAR(36) NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), '
        'version INTEGER NOT NULL, original_filename VARCHAR(500) NOT NULL, stored_filename VARCHAR(100) NOT NULL UNIQUE, '
        'media_type VARCHAR(200) NOT NULL, size INTEGER NOT NULL, sha256 VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL, '
        'UNIQUE (user_id, version), CHECK (size >= 0), CHECK (version > 0)); '
        f'INSERT INTO master_resumes ({columns}) SELECT {columns} FROM master_resumes_v13; DROP TABLE master_resumes_v13; '
        'CREATE INDEX ix_master_resumes_user_id ON master_resumes (user_id); DROP TABLE resume_templates; '
        'UPDATE schema_version SET version=12; PRAGMA legacy_alter_table=OFF; PRAGMA foreign_keys=ON;')
