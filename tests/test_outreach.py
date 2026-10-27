"""Outreach is an independent fact, not inferred from pipeline ordering."""
import json
import sqlite3

from flask import Flask
import pytest
from sqlalchemy import text

from jobtracker import db, services


@pytest.fixture
def app(tmp_path):
    app = Flask(__name__)
    app.config['DATABASE_URL'] = f'sqlite:///{tmp_path / "jobs.sqlite3"}'
    db.init_app(app)
    return app


def test_outreach_survives_stage_changes_and_can_be_corrected(app):
    with app.app_context():
        job = services.create_job(1, {'title': 'Engineer'})
        assert job['outreach_completed'] is False
        job = services.update_job(1, job['id'], {'stage': 'outreach', 'version': job['version']})
        assert job['outreach_completed'] is True
        assert services.list_activities(1, job['id'])[-1]['details']['outreach_completed'] == {'from': False, 'to': True}
        job = services.update_job(1, job['id'], {'stage': 'applied', 'version': job['version']})
    with app.app_context():
        assert services.get_job(1, job['id'])['outreach_completed'] is True
        job = services.update_job(1, job['id'], {'stage': 'panel_interview', 'outreach_completed': False, 'version': job['version']})
        assert job['stage'] == 'panel_interview'
        assert job['outreach_completed'] is False
        assert services.list_activities(1, job['id'])[-1]['details']['outreach_completed'] == {'from': True, 'to': False}


def test_skipped_stage_or_saved_contact_does_not_claim_outreach(app):
    with app.app_context():
        job = services.create_job(1, {'title': 'Engineer', 'stage': 'applied'})
        services.create_contact(1, job['id'], {'name': 'Someone I might contact'})
        assert services.get_job(1, job['id'])['outreach_completed'] is False
        assert services.update_job(1, job['id'], {'outreach_completed': True})['stage'] == 'applied'
        assert services.create_job(1, {'title': 'Already reached out', 'stage': 'outreach'})['outreach_completed'] is True
        assert services.create_job(1, {'title': 'Direct interview', 'stage': 'recruiter_interview', 'outreach_completed': True})['outreach_completed'] is True


@pytest.mark.parametrize('invalid', ['false', 1, None, []])
def test_outreach_requires_json_boolean(app, invalid):
    with app.app_context():
        with pytest.raises(services.ServiceError, match='must be true or false'):
            services.create_job(1, {'title': 'Engineer', 'outreach_completed': invalid})


def legacy_database(app, malformed=False):
    """Restore the exact v1 shape of jobs/history in disposable storage."""
    with app.app_context():
        reached = services.create_job(1, {'title': 'Historical outreach', 'stage': 'applied'})
        current = services.create_job(1, {'title': 'Currently outreach', 'stage': 'outreach'})
        skipped = services.create_job(1, {'title': 'Skipped outreach', 'stage': 'applied'})
        services.create_contact(1, reached['id'], {'name': 'Existing colleague'})
        services.create_resume(1, reached['id'], {'original_filename': 'original.txt', 'stored_filename': 'old.txt', 'media_type': 'text/plain', 'size': 42, 'sha256': 'a' * 64})
        services._activity(1, reached['id'], 'updated', 'Old stage change', {'stage': {'from': 'outreach', 'to': 'applied'}})
        db.get_session().commit()
    engine = app.extensions['jobtracker_db']['engine']
    app.extensions['jobtracker_db']['sessions'].remove()
    engine.dispose()
    filename = app.config['DATABASE_URL'].removeprefix('sqlite:///')
    with sqlite3.connect(filename) as connection:
        connection.execute('DROP TABLE workspace_credentials')
        connection.execute('ALTER TABLE jobs DROP COLUMN outreach_completed')
        connection.execute('UPDATE schema_version SET version=1')
        if malformed:
            connection.execute('UPDATE activities SET details=? WHERE job_id=?', ('invalid json', reached['id']))
    return filename, reached, current, skipped


def test_v1_migration_preserves_rows_and_backfills_only_evidence(app):
    _, reached, current, skipped = legacy_database(app)
    db.init_app(app)
    with app.app_context():
        jobs = {j['id']: j for j in services.list_jobs(1)}
        assert jobs[reached['id']]['outreach_completed'] is True
        assert jobs[current['id']]['outreach_completed'] is True
        assert jobs[skipped['id']]['outreach_completed'] is False
        assert jobs[reached['id']]['title'] == reached['title']
        assert jobs[reached['id']]['version'] == reached['version']
        assert services.list_contacts(1, reached['id'])[0]['name'] == 'Existing colleague'
        assert services.list_resumes(1, reached['id'])[0]['stored_filename'] == 'old.txt'
        assert len(services.list_activities(1, reached['id'])) == 4
        assert db.get_session().scalar(text('SELECT version FROM schema_version')) == db.SCHEMA_VERSION
        services.update_job(1, reached['id'], {'outreach_completed': False})
    db.init_app(app)  # Restart must not re-backfill a deliberate correction.
    with app.app_context():
        assert services.get_job(1, reached['id'])['outreach_completed'] is False


def test_migration_failure_rolls_back_schema_and_version(app):
    filename, *_ = legacy_database(app, malformed=True)
    with pytest.raises(json.JSONDecodeError):
        db.init_app(app)
    with sqlite3.connect(filename) as connection:
        assert connection.execute('SELECT version FROM schema_version').fetchone() == (1,)
        assert 'outreach_completed' not in [row[1] for row in connection.execute('PRAGMA table_info(jobs)')]
