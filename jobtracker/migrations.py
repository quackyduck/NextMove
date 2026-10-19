"""Numbered, transactional SQLite schema upgrades; run with writers stopped."""
import json

from sqlalchemy import text


SCHEMA_VERSION = 2


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


def migrate(connection, versions):
    if versions == [1]:
        upgrade_v1_to_v2(connection)
        versions = [2]
    if versions != [SCHEMA_VERSION]:
        raise RuntimeError(f'Unsupported database schema version {versions}; expected {SCHEMA_VERSION}. Run migrations before starting.')
