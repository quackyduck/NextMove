"""Job resumes record the template version they were tailored from (schema 14)."""
import io
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text

from jobtracker import create_app, migrations
from accounts import sign_in
from legacy import rewind_resume_sources
from test_mcp_oauth import app, call, client, connect, ok  # noqa: F401  (fixtures)
from test_resume_templates import MASTER, create, shape, upload

STRANGER = '00000000-0000-0000-0000-000000000000'


def attach(client, headers, job_id, source=None, body=b'Tailored\n'):
    data = {'file': (io.BytesIO(body), 'tailored.txt'), **({'source_resume_id': source} if source else {})}
    return client.post(f'/api/v1/jobs/{job_id}/resumes', headers=headers, data=data)


def test_rest_uploads_record_the_source_and_show_the_templates_current_name(client, app):
    session = sign_in(client)
    manager = create(client, session, name='Engineering manager')['id']
    path = f'/api/v1/templates/{manager}/versions'
    upload(client, session, path, b'v1\n')
    version = upload(client, session, path, b'v2\n').json['resume']
    job = client.post('/api/v1/jobs', headers=session, json={'title': 'EM at Acme'}).json['id']
    added = attach(client, session, job, version['id'])
    assert added.status_code == 201, added.json
    source = {'resume_id': version['id'], 'template_id': manager, 'template_name': 'Engineering manager', 'version': 2}
    assert added.json['source'] == source and 'source_master_resume_id' not in added.json
    assert attach(client, session, job).json['source'] is None  # Optional.
    [history] = [a for a in client.get(f'/api/v1/jobs/{job}/activities').json['activities']
                 if a['details'].get('resume_id') == added.json['id']]
    assert history['details']['source'] == source
    client.patch(f'/api/v1/templates/{manager}', headers=session, json={'name': 'People leadership'})
    listed = client.get(f'/api/v1/jobs/{job}/resumes').json['resumes']
    assert [r['source'] and r['source']['template_name'] for r in listed] == ['People leadership', None]
    folder = Path(app.config['UPLOAD_FOLDER'])
    files = sorted(folder.rglob('*'))
    unknown = attach(client, session, job, STRANGER)
    # A job resume is not a template version; neither is anything else.
    for bad in (added.json['id'], job, 'x' * 300):
        assert attach(client, session, job, bad).json == unknown.json
    assert unknown.status_code == 404 and unknown.json == {'error': 'Source resume version not found'}
    assert sorted(folder.rglob('*')) == files and len(client.get(f'/api/v1/jobs/{job}/resumes').json['resumes']) == 2


def test_mcp_uploads_and_links_record_the_source(client):
    tokens, _ = connect(client)
    headers = {'Authorization': 'Bearer ' + tokens['access_token']}
    version = upload(client, headers, MASTER, b'Platform engineer\n').json['resume']
    job = ok(client, tokens, 'add_job', title='Engineer')['job']['id']
    inline = ok(client, tokens, 'upload_resume', target=job, source_resume_id=version['id'],
                filename='t.txt', content_base64='VGFpbG9yZWQK')['resume']
    assert inline['source'] == {'resume_id': version['id'], 'template_id': version['template_id'],
                                'template_name': 'Main resume', 'version': 1}
    link = ok(client, tokens, 'create_resume_upload_link', target=job, source_resume_id=version['id'])
    linked = client.post(urlsplit(link['url']).path, data={'file': (io.BytesIO(b'Linked\n'), 'l.txt')})
    assert linked.status_code == 201 and linked.json['resume']['source'] == inline['source']
    for tool in ('get_job', 'list_resumes'):
        result = ok(client, tokens, tool, job_id=job)
        resumes = result['resumes'] if tool == 'get_job' else result['job']
        assert [r['source'] for r in resumes] == [inline['source']] * 2
        assert all('stored_filename' not in r for r in resumes)
    for arguments in ({'target': 'master'}, {'template_id': version['template_id']}):
        refused = call(client, tokens, 'create_resume_upload_link', source_resume_id=version['id'], **arguments)
        assert refused['isError'] and 'only for a resume attached to a role' in refused['content'][0]['text']
    unknown = call(client, tokens, 'create_resume_upload_link', target=job, source_resume_id=STRANGER)
    assert unknown['isError'] and 'Source resume version not found' in unknown['content'][0]['text']


def test_schema_13_job_resumes_upgrade_with_no_source(client, app):
    session = sign_in(client)
    version = upload(client, session, MASTER, b'Main\n').json['resume']
    job = client.post('/api/v1/jobs', headers=session, json={'title': 'Role'}).json['id']
    resume = attach(client, session, job, version['id']).json
    path = app.config['DATABASE_URL'].removeprefix('sqlite:///')
    app.extensions['jobtracker_db']['engine'].dispose()
    with sqlite3.connect(path) as connection:
        rewind_resume_sources(connection)
    upgraded = create_app({**app.config, 'TESTING': True})
    create_app({**app.config, 'TESTING': True, 'DATABASE_URL': f'sqlite:///{path}.fresh'})
    for table in ('resumes', 'master_resumes'):  # The migrated tables match a fresh database's.
        assert shape(path, table) == shape(f'{path}.fresh', table)
    with upgraded.extensions['jobtracker_db']['engine'].begin() as connection:
        assert connection.execute(text('SELECT version FROM schema_version')).scalar() == migrations.SCHEMA_VERSION
        assert connection.execute(text('SELECT id, source_master_resume_id FROM resumes')).all() == [(resume['id'], None)]
        connection.execute(text('UPDATE resumes SET source_master_resume_id=:id'), {'id': version['id']})
    browser = upgraded.test_client()
    sign_in(browser)
    assert browser.get(f'/api/v1/jobs/{job}/resumes').json['resumes'][0]['source']['resume_id'] == version['id']
