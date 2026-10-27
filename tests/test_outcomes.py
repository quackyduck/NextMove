import io

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jobtracker import create_app, migrations
from legacy import downgrade_to_v8, without_owner
from test_api import app, client, login  # noqa: F401


def test_not_interested_api_creation_update_history_and_search(client):
    browser = login(client)
    created = client.post('/api/v1/jobs', headers=browser, json={
        'title': 'Backend Engineer', 'company': 'Acme', 'outcome': 'not_interested', 'stage': 'applied',
    })
    assert created.status_code == 201
    job = created.json
    assert job['outcome'] == 'not_interested' and job['stage'] == 'applied'
    assert client.get('/api/v1/jobs/search?company=Acme').json['matches'][0]['outcome'] == 'not_interested'
    changed = client.patch('/api/v1/jobs/' + job['id'], headers=browser,
                           json={'outcome': 'active', 'version': job['version']})
    assert changed.status_code == 200
    assert changed.json['stage'] == 'applied'
    updated = client.patch('/api/v1/jobs/' + job['id'], headers=browser,
                           json={'outcome': 'not_interested', 'version': changed.json['version']})
    assert updated.status_code == 200
    activities = client.get(f"/api/v1/jobs/{job['id']}/activities").json['activities']
    assert activities[-1]['details']['outcome'] == {'from': 'active', 'to': 'not_interested'}
    assert client.patch('/api/v1/jobs/' + job['id'], headers=browser,
                        json={'outcome': 'active', 'version': job['version']}).status_code == 409
    assert client.post('/api/v1/jobs', headers=browser,
                       json={'title': 'Bad', 'outcome': 'not interested'}).status_code == 400


def prepare_v7(client, app):
    browser = login(client)
    for outcome in ('active', 'rejected', 'withdrawn', 'closed', 'accepted'):
        saved = client.post('/api/v1/jobs', headers=browser, json={
            'title': outcome, 'outcome': outcome, 'stage': 'applied', 'notes': 'Preserve notes',
        }).json
    client.post(f"/api/v1/jobs/{saved['id']}/contacts", headers=browser, json={'name': 'Pat'})
    resume = client.post(f"/api/v1/jobs/{saved['id']}/resumes", headers=browser,
                         data={'file': (io.BytesIO(b'resume'), 'Specific_Resume.txt')}).json
    downgrade_to_v8(app)
    engine = app.extensions['jobtracker_db']['engine']
    with engine.begin() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        schema = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='jobs'").scalar()
        assert ",'not_interested'" in schema
        schema = schema.replace('CREATE TABLE jobs', 'CREATE TABLE jobs_v7').replace(",'not_interested'", '')
        connection.exec_driver_sql('PRAGMA defer_foreign_keys=ON')
        connection.exec_driver_sql(schema)
        connection.exec_driver_sql('INSERT INTO jobs_v7 SELECT * FROM jobs')
        connection.exec_driver_sql('DROP TABLE jobs')
        connection.exec_driver_sql('ALTER TABLE jobs_v7 RENAME TO jobs')
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        connection.exec_driver_sql('PRAGMA defer_foreign_keys=OFF')
        connection.execute(text('UPDATE schema_version SET version=7'))
    return saved['id'], resume


# Tables that exist in both schema 7 and today's schema; user_id is added by schema 9.
LEGACY_TABLES = ('jobs', 'contacts', 'resumes', 'activities', 'agent_tokens', 'extraction_tasks', 'ingestion_requests')


def snapshot(connection):
    return {name: without_owner(connection.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY id'))
            for name in LEGACY_TABLES}


def test_schema7_upgrade_preserves_all_rows_and_attachments(client, app):
    job_id, resume = prepare_v7(client, app)
    engine = app.extensions['jobtracker_db']['engine']
    with engine.connect() as connection:
        before = snapshot(connection)
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE jobs SET outcome='not_interested' WHERE id=:id"), {'id': job_id})
        connection.rollback()
    fresh = create_app(dict(app.config))
    with fresh.extensions['jobtracker_db']['engine'].begin() as connection:
        assert snapshot(connection) == before
        assert connection.exec_driver_sql('SELECT version FROM schema_version').scalar() == migrations.SCHEMA_VERSION
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        connection.execute(text("UPDATE jobs SET outcome='not_interested' WHERE id=:id"), {'id': job_id})
    with fresh.extensions['jobtracker_db']['engine'].begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE jobs SET outcome='invalid' WHERE id=:id"), {'id': job_id})
    assert client.get('/api/v1/resumes/' + resume['id'] + '/download').data == b'resume'
    assert client.get(f'/api/v1/jobs/{job_id}/resumes').json['resumes'][0]['original_filename'] == 'Specific_Resume.txt'
    restarted = create_app(dict(app.config))
    with restarted.extensions['jobtracker_db']['engine'].connect() as connection:
        assert connection.execute(text('SELECT outcome FROM jobs WHERE id=:id'), {'id': job_id}).scalar() == 'not_interested'


def test_outcome_migration_failure_rolls_back_rows_constraint_and_version(client, app, monkeypatch):
    job_id, _ = prepare_v7(client, app)
    engine = app.extensions['jobtracker_db']['engine']
    with engine.connect() as connection:
        before = snapshot(connection)
    upgrade = migrations.upgrade_v7_to_v8
    def fail_after_rebuild(connection):
        upgrade(connection)
        raise RuntimeError('Simulated migration failure')
    monkeypatch.setattr(migrations, 'upgrade_v7_to_v8', fail_after_rebuild)
    with pytest.raises(RuntimeError, match='Simulated migration failure'):
        create_app(dict(app.config))
    with engine.connect() as connection:
        assert snapshot(connection) == before
        assert connection.exec_driver_sql('SELECT version FROM schema_version').scalar() == 7
        assert not connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='jobs_new'").all()
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE jobs SET outcome='not_interested' WHERE id=:id"), {'id': job_id})
