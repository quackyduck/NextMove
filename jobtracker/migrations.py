"""Numbered, transactional SQLite schema upgrades; run with writers stopped."""
import json

from sqlalchemy import inspect, text


SCHEMA_VERSION = 6


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


def upgrade_v2_to_v3(connection):
    connection.execute(text(
        'CREATE TABLE workspace_credentials ('
        'id INTEGER PRIMARY KEY CHECK (id=1), '
        'password_hash VARCHAR(512) NOT NULL, '
        'session_version INTEGER NOT NULL CHECK (session_version>0))'
    ))
    connection.execute(text('UPDATE schema_version SET version=3'))


def upgrade_v3_to_v4(connection):
    from .models import ExtractionTask
    ExtractionTask.__table__.create(connection, checkfirst=True)
    connection.execute(text('UPDATE schema_version SET version=4'))


def upgrade_v4_to_v5(connection):
    from .models import IngestionRequest
    IngestionRequest.__table__.create(connection, checkfirst=True)
    connection.execute(text('UPDATE schema_version SET version=5'))


def upgrade_v5_to_v6(connection):
    from .models import AgentToken, IngestionRequest
    AgentToken.__table__.create(connection, checkfirst=True)
    if 'token_id' not in {column['name'] for column in inspect(connection).get_columns('ingestion_requests')}:
        connection.execute(text('ALTER TABLE ingestion_requests ADD COLUMN token_id VARCHAR(36) REFERENCES agent_tokens(id)'))
    for index in IngestionRequest.__table__.indexes:
        index.create(connection, checkfirst=True)
    connection.execute(text('UPDATE schema_version SET version=6'))


def migrate(connection, versions):
    if versions == [1]:
        upgrade_v1_to_v2(connection)
        versions = [2]
    if versions == [2]:
        upgrade_v2_to_v3(connection)
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
    if versions != [SCHEMA_VERSION]:
        raise RuntimeError(f'Unsupported database schema version {versions}; expected {SCHEMA_VERSION}. Run migrations before starting.')
