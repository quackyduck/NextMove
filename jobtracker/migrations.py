"""Numbered, transactional SQLite schema upgrades; run with writers stopped."""
import json
from types import SimpleNamespace

from sqlalchemy import inspect, text


SCHEMA_VERSION = 12
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


def upgrade_v10_to_v11(connection):
    """Messaging-first accounts and the Resume Agent harness API.

    users.email and users.password_hash become nullable: an account created from
    chat has neither until dashboard activation. SQLite cannot drop NOT NULL in
    place, so users is rebuilt row for row (IDs, generations, flags kept), and
    the AUTOINCREMENT high-water mark is carried over so no ID is ever reissued.
    """
    sequence = connection.execute(text("SELECT seq FROM sqlite_sequence WHERE name='users'")).scalar()

    def rebuild():
        connection.execute(text(
            'CREATE TABLE users_new (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, email VARCHAR(254), '
            'password_hash VARCHAR(512), session_version INTEGER NOT NULL, '
            "owns_legacy_tokens BOOLEAN DEFAULT '0' NOT NULL, created_at DATETIME NOT NULL, "
            'CHECK (session_version > 0), UNIQUE (email))'))
        connection.execute(text(
            'INSERT INTO users_new (id, email, password_hash, session_version, owns_legacy_tokens, created_at) '
            'SELECT id, email, password_hash, session_version, owns_legacy_tokens, created_at FROM users'))
        connection.exec_driver_sql('DROP TABLE users')
        connection.exec_driver_sql('ALTER TABLE users_new RENAME TO users')
        connection.execute(text(
            'CREATE UNIQUE INDEX uq_users_legacy_owner ON users (owns_legacy_tokens) WHERE owns_legacy_tokens = 1'))
    with_deferred_foreign_keys(connection, rebuild)
    if sequence is not None:
        connection.execute(text("UPDATE sqlite_sequence SET seq=MAX(seq, :seq) WHERE name='users'"), {'seq': sequence})
    connection.execute(text(
        'CREATE TABLE harness_grants (id VARCHAR(36) NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), '
        'prefix VARCHAR(20) NOT NULL, token_hash VARCHAR(64) NOT NULL UNIQUE, scopes JSON NOT NULL, '
        'created_at DATETIME NOT NULL, last_used_at DATETIME, revoked_at DATETIME)'))
    connection.execute(text('CREATE INDEX ix_harness_grants_user_id ON harness_grants (user_id)'))
    connection.execute(text(
        'CREATE TABLE activation_tokens (id VARCHAR(36) NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), '
        'token_hash VARCHAR(64) NOT NULL UNIQUE, created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL, '
        'used_at DATETIME)'))
    connection.execute(text('CREATE INDEX ix_activation_tokens_user_id ON activation_tokens (user_id)'))
    connection.execute(text(
        'CREATE TABLE tailored_drafts (id VARCHAR(36) NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), '
        'job_id VARCHAR(36) NOT NULL, master_resume_id VARCHAR(36) NOT NULL REFERENCES master_resumes (id), '
        'request_id VARCHAR(64) NOT NULL, revision INTEGER NOT NULL, draft JSON NOT NULL, '
        'docx_filename VARCHAR(100) NOT NULL UNIQUE, pdf_filename VARCHAR(100) UNIQUE, created_at DATETIME NOT NULL, '
        'approved_at DATETIME, CHECK (revision > 0), '
        'CONSTRAINT fk_tailored_drafts_job_owner FOREIGN KEY (job_id, user_id) REFERENCES jobs (id, user_id), '
        'CONSTRAINT uq_tailored_drafts_request UNIQUE (user_id, request_id, revision))'))
    connection.execute(text('CREATE INDEX ix_tailored_drafts_job_owner ON tailored_drafts (job_id, user_id)'))
    connection.execute(text('UPDATE schema_version SET version=11'))


def upgrade_v11_to_v12(connection):
    """OAuth 2.1 clients, per-user grants, codes and tokens for third-party agents."""
    connection.execute(text(
        'CREATE TABLE oauth_clients (id VARCHAR(64) NOT NULL PRIMARY KEY, name VARCHAR(200) NOT NULL, '
        'redirect_uris JSON NOT NULL, created_at DATETIME NOT NULL)'))
    connection.execute(text(
        'CREATE TABLE oauth_grants (id VARCHAR(36) NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), '
        'client_id VARCHAR(64) NOT NULL REFERENCES oauth_clients (id), scopes JSON NOT NULL, '
        'created_at DATETIME NOT NULL, last_used_at DATETIME, revoked_at DATETIME)'))
    connection.execute(text('CREATE INDEX ix_oauth_grants_user_client ON oauth_grants (user_id, client_id)'))
    connection.execute(text(
        'CREATE TABLE oauth_codes (code_hash VARCHAR(64) NOT NULL PRIMARY KEY, '
        'grant_id VARCHAR(36) NOT NULL REFERENCES oauth_grants (id), redirect_uri TEXT NOT NULL, '
        'code_challenge VARCHAR(128) NOT NULL, expires_at DATETIME NOT NULL, used_at DATETIME)'))
    connection.execute(text('CREATE INDEX ix_oauth_codes_grant_id ON oauth_codes (grant_id)'))
    connection.execute(text(
        'CREATE TABLE oauth_tokens (id VARCHAR(36) NOT NULL PRIMARY KEY, '
        'grant_id VARCHAR(36) NOT NULL REFERENCES oauth_grants (id), kind VARCHAR(10) NOT NULL, '
        'token_hash VARCHAR(64) NOT NULL UNIQUE, created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL, '
        "used_at DATETIME, CHECK (kind IN ('access','refresh')))"))
    connection.execute(text('CREATE INDEX ix_oauth_tokens_grant_id ON oauth_tokens (grant_id)'))
    connection.execute(text('UPDATE schema_version SET version=12'))


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
    if versions == [10]:
        upgrade_v10_to_v11(connection)
        versions = [11]
    if versions == [11]:
        upgrade_v11_to_v12(connection)
        versions = [12]
    if versions != [SCHEMA_VERSION]:
        raise RuntimeError(f'Unsupported database schema version {versions}; expected {SCHEMA_VERSION}. Run migrations before starting.')
