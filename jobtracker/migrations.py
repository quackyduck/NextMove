"""Numbered, transactional SQLite schema upgrades; run with writers stopped."""
import json
from types import SimpleNamespace

from sqlalchemy import inspect, text


SCHEMA_VERSION = 10
PLACEHOLDER_ADMIN_EMAIL = 'owner@localhost'


def upgrade_v1_to_v2(connection):
    connection.execute(text(
        'ALTER TABLE jobs ADD COLUMN outreach_completed BOOLEAN NOT NULL DEFAULT 0 '
        'CHECK (outreach_completed IN (0, 1))'
    ))
    # A later stage alone is not evidence: roles can skip outreach entirely.
    connection.execute(text("UPDATE jobs SET outreach_completed=1 WHERE stage='outreach'"))
    for job_id, raw_details in connection.execute(text('SELECT job_id, details FROM activities')):
        details = json.loads(raw_details) if isinstance(raw_details, str) else raw_details
        change = details.get('stage') if isinstance(details, dict) else None
        if isinstance(change, dict) and 'outreach' in (change.get('from'), change.get('to')):
            connection.execute(text('UPDATE jobs SET outreach_completed=1 WHERE id=:id'), {'id': job_id})
    connection.execute(text('UPDATE schema_version SET version=2'))


def upgrade_v2_to_v3(connection, bootstrap_hash=None):
    connection.execute(text(
        'CREATE TABLE workspace_credentials ('
        'id INTEGER PRIMARY KEY CHECK (id=1), '
        'password_hash VARCHAR(512) NOT NULL, '
        'session_version INTEGER NOT NULL CHECK (session_version>0))'
    ))
    if bootstrap_hash:
        # Seeded once from configuration; schema 9 converts it into user #1.
        connection.execute(text('INSERT INTO workspace_credentials (id,password_hash,session_version) '
                                'VALUES (1,:password_hash,1)'), {'password_hash': bootstrap_hash})
    connection.execute(text('UPDATE schema_version SET version=3'))


def upgrade_v3_to_v4(connection):
    connection.execute(text(
        'CREATE TABLE IF NOT EXISTS extraction_tasks (id VARCHAR(36) NOT NULL PRIMARY KEY, status VARCHAR(20) NOT NULL, '
        'result JSON NOT NULL, created_at DATETIME NOT NULL)'))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_extraction_tasks_created_at ON extraction_tasks (created_at)'))
    connection.execute(text('UPDATE schema_version SET version=4'))


def upgrade_v4_to_v5(connection):
    # Historical DDL is frozen here: later model changes must not leak into old steps.
    connection.execute(text(
        'CREATE TABLE IF NOT EXISTS ingestion_requests (id VARCHAR(36) NOT NULL PRIMARY KEY, '
        'idempotency_key VARCHAR(128) NOT NULL UNIQUE, url TEXT NOT NULL, status VARCHAR(20) NOT NULL, '
        'result JSON NOT NULL, created_at DATETIME NOT NULL)'))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_ingestion_requests_created_at ON ingestion_requests (created_at)'))
    connection.execute(text('UPDATE schema_version SET version=5'))


def upgrade_v5_to_v6(connection):
    connection.execute(text(
        'CREATE TABLE IF NOT EXISTS agent_tokens (id VARCHAR(36) NOT NULL PRIMARY KEY, name VARCHAR(80) NOT NULL, '
        'prefix VARCHAR(18) NOT NULL, token_hash VARCHAR(64) NOT NULL UNIQUE, permissions JSON NOT NULL, '
        'created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL, last_used_at DATETIME, revoked_at DATETIME)'))
    if 'token_id' not in {column['name'] for column in inspect(connection).get_columns('ingestion_requests')}:
        connection.execute(text('ALTER TABLE ingestion_requests ADD COLUMN token_id VARCHAR(36) REFERENCES agent_tokens(id)'))
    connection.execute(text('CREATE INDEX IF NOT EXISTS ix_ingestion_requests_token_id ON ingestion_requests (token_id)'))
    connection.execute(text('UPDATE schema_version SET version=6'))


def upgrade_v6_to_v7(connection):
    # Originally rebuilt jobs for the needs_review CHECK. Schema 9 rebuilds jobs
    # from the current model, so older databases receive that constraint there.
    connection.execute(text('UPDATE schema_version SET version=7'))


def upgrade_v7_to_v8(connection):
    # Originally rebuilt jobs for the not_interested CHECK; see upgrade_v6_to_v7.
    connection.execute(text('UPDATE schema_version SET version=8'))


def rebuild_table(connection, model, defaults=None):
    """Replace a table with the current model definition, copying every row.

    SQLite cannot add constrained columns or alter CHECK/UNIQUE constraints in
    place. A column missing from the old table (or NULL there) takes the SQL
    literal from `defaults`. Callers run inside one transaction with deferred FKs.
    """
    from sqlalchemy import MetaData
    from sqlalchemy.schema import CreateTable
    table = model.__table__
    defaults = defaults or {}
    existing = {column['name'] for column in inspect(connection).get_columns(table.name)}
    # Copy every table so the replacement's foreign keys resolve (DDL only).
    metadata = MetaData()
    for other in table.metadata.sorted_tables:
        if other is not table:
            other.to_metadata(metadata)
    connection.execute(CreateTable(table.to_metadata(metadata, name=table.name + '_new')))
    targets, sources = [], []
    for column in table.columns:
        targets.append(f'"{column.name}"')
        if column.name in existing:
            sources.append(f'COALESCE("{column.name}", {defaults[column.name]})'
                           if column.name in defaults else f'"{column.name}"')
        elif column.name in defaults:
            sources.append(defaults[column.name])
        else:
            raise RuntimeError(f'Cannot rebuild {table.name}: no value for new column {column.name}')
    connection.exec_driver_sql(f'INSERT INTO "{table.name}_new" ({", ".join(targets)}) '
                               f'SELECT {", ".join(sources)} FROM "{table.name}"')
    connection.exec_driver_sql(f'DROP TABLE "{table.name}"')
    connection.exec_driver_sql(f'ALTER TABLE "{table.name}_new" RENAME TO "{table.name}"')
    for index in table.indexes:
        index.create(connection)


def with_deferred_foreign_keys(connection, step):
    # Defer child references while replacing parents, then validate every FK
    # before clearing SQLite's deferred counter. Enforcement stays on throughout.
    connection.exec_driver_sql('PRAGMA defer_foreign_keys=ON')
    step()
    if connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Foreign key validation failed during migration')
    connection.exec_driver_sql('PRAGMA defer_foreign_keys=OFF')


def bootstrap_email(value):
    from .credentials import normalize_email
    email = normalize_email(value) if value else PLACEHOLDER_ADMIN_EMAIL
    if email is None:
        raise RuntimeError('BOOTSTRAP_ADMIN_EMAIL is not a valid email address')
    return email


TENANT_TABLES = ('Job', 'Contact', 'Resume', 'Activity', 'AgentToken', 'ExtractionTask', 'IngestionRequest')


def upgrade_v8_to_v9(connection, admin_email=None, bootstrap_hash=None):
    """Multi-user tenancy: the singleton workspace becomes user #1.

    Every row keeps its ID, version and timestamps and gains user_id=1. Resume
    files move under <uploads>/1/ after commit (see storage.relocate_legacy_files).
    """
    from . import models
    models.User.__table__.create(connection)
    models.InviteCode.__table__.create(connection)
    models.QueuedTask.__table__.create(connection)
    credential = connection.execute(text(
        'SELECT password_hash, session_version FROM workspace_credentials WHERE id=1')).first()
    if credential is None and bootstrap_hash:
        credential = SimpleNamespace(password_hash=bootstrap_hash, session_version=1)
    if credential is None:
        # Never leave ID 1 for the first sign-up: it would inherit the workspace's
        # records and the deprecated environment tokens.
        raise RuntimeError('The workspace has no credential to become user #1; restore the '
                           'credential or set APP_PASSWORD_HASH before upgrading')
    connection.execute(models.User.__table__.insert().values(
        id=1, email=bootstrap_email(admin_email), password_hash=credential.password_hash,
        session_version=credential.session_version, owns_legacy_tokens=True, created_at=models.now()))

    def rebuild():
        for name in TENANT_TABLES:
            rebuild_table(connection, getattr(models, name), {'user_id': '1'})
    with_deferred_foreign_keys(connection, rebuild)
    connection.exec_driver_sql('DROP TABLE workspace_credentials')
    connection.execute(text('UPDATE schema_version SET version=9'))


def upgrade_v9_to_v10(connection):
    """Onboarding progress and master resumes (Phase 1).

    No progress rows are seeded: existing accounts never enter the guided flow
    (it needs a signup-time create_account row) and see optional steps on the
    dashboard checklist instead.
    """
    connection.execute(text(
        'CREATE TABLE onboarding_steps (user_id INTEGER NOT NULL REFERENCES users (id), '
        'step_id VARCHAR(40) NOT NULL, version INTEGER NOT NULL, status VARCHAR(10) NOT NULL, '
        'updated_at DATETIME NOT NULL, PRIMARY KEY (user_id, step_id), '
        "CHECK (status IN ('pending','done','skipped')), CHECK (version > 0))"))
    connection.execute(text(
        'CREATE TABLE master_resumes (id VARCHAR(36) NOT NULL PRIMARY KEY, '
        'user_id INTEGER NOT NULL REFERENCES users (id), version INTEGER NOT NULL, '
        'original_filename VARCHAR(500) NOT NULL, stored_filename VARCHAR(100) NOT NULL UNIQUE, '
        'media_type VARCHAR(200) NOT NULL, size INTEGER NOT NULL, sha256 VARCHAR(64) NOT NULL, '
        'created_at DATETIME NOT NULL, UNIQUE (user_id, version), CHECK (size >= 0), CHECK (version > 0))'))
    connection.execute(text('CREATE INDEX ix_master_resumes_user_id ON master_resumes (user_id)'))
    connection.execute(text('UPDATE schema_version SET version=10'))


def migrate(connection, versions, bootstrap_hash=None, admin_email=None):
    if versions == [1]:
        upgrade_v1_to_v2(connection)
        versions = [2]
    if versions == [2]:
        upgrade_v2_to_v3(connection, bootstrap_hash)
        versions = [3]
    if versions == [3]:
        upgrade_v3_to_v4(connection)
        versions = [4]
    if versions == [4]:
        upgrade_v4_to_v5(connection)
        versions = [5]
    if versions == [5]:
        upgrade_v5_to_v6(connection)
        versions = [6]
    if versions == [6]:
        upgrade_v6_to_v7(connection)
        versions = [7]
    if versions == [7]:
        upgrade_v7_to_v8(connection)
        versions = [8]
    if versions == [8]:
        upgrade_v8_to_v9(connection, admin_email, bootstrap_hash)
        versions = [9]
    if versions == [9]:
        upgrade_v9_to_v10(connection)
        versions = [10]
    if versions != [SCHEMA_VERSION]:
        raise RuntimeError(f'Unsupported database schema version {versions}; expected {SCHEMA_VERSION}. Run migrations before starting.')
