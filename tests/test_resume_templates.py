"""Resume templates (schema 13): named master resumes with per-template versions."""
import io
import sqlite3

from sqlalchemy import text

from jobtracker import create_app, migrations
from accounts import add_user, sign_in
from legacy import rewind_resume_templates
from test_documents import docx
from test_mcp_oauth import app, client, connect, ok  # noqa: F401  (fixtures)

PLATFORM, MANAGER, MASTER = docx(), b'Engineering manager who grows teams\n', '/api/v1/profile/resume'


def upload(client, headers, path, data):
    return client.post(path, headers=headers, data={'file': (io.BytesIO(data), 'r.docx' if data == PLATFORM else 'r.txt')})


def create(client, headers, **fields):
    response = client.post('/api/v1/templates', headers=headers, json=fields)
    assert response.status_code == 201, response.json
    return response.json


def test_master_uploads_and_template_versions_are_numbered_per_template(client):
    session = sign_in(client)
    assert client.get('/api/v1/templates').json == {'templates': []}
    first = upload(client, session, MASTER, PLATFORM).json['resume']
    [main] = client.get('/api/v1/templates').json['templates']  # The first upload creates the default.
    assert (main['name'], main['is_default'], main['version_count']) == ('Main resume', True, 1)
    assert first['template_id'] == main['id'] == client.get(MASTER).json['template']['id']
    manager = create(client, session, name='Engineering manager', description='People leadership roles')
    assert (manager['is_default'], manager['versions']) == (False, [])
    path = f"/api/v1/templates/{manager['id']}/versions"
    assert upload(client, session, path, MANAGER).json['resume']['version'] == 1
    assert upload(client, session, path, PLATFORM).status_code == 201
    assert upload(client, session, path, PLATFORM).json['created'] is False
    assert [v['version'] for v in client.get(f"/api/v1/templates/{manager['id']}").json['versions']] == [2, 1]
    assert [v['version'] for v in client.get(MASTER).json['versions']] == [1]  # The default is untouched.
    assert client.get(f"/api/v1/templates/{manager['id']}/download?version=1").data == MANAGER
    assert client.get(f"/api/v1/templates/{manager['id']}/download").data == PLATFORM
    assert client.get(f"/api/v1/templates/{manager['id']}/download?version=9").status_code == 404


def test_names_are_validated_unique_ignoring_case_and_capped(client, app):
    session = sign_in(client)
    assert create(client, session, name='Platform engineer')['is_default'] is True  # First one is the default.
    create(client, session, name='Ingénieur')
    for body, status in (({'name': 'platform ENGINEER'}, 409), ({'name': ' INGÉNIEUR '}, 409), ({'name': '  '}, 400),
                         ({}, 400), ({'name': 'x' * 101}, 400), ({'name': 'Ok', 'colour': 'blue'}, 400),
                         ({'name': 'Ok', 'is_default': 'yes'}, 400), ({'name': 'Ok', 'description': 7}, 400)):
        assert client.post('/api/v1/templates', headers=session, json=body).status_code == status, body
    for index in range(8):
        create(client, session, name=f'Role {index}')
    assert client.post('/api/v1/templates', headers=session, json={'name': 'One more'}).status_code == 409
    add_user(app, 'other@example.com')  # Another account can reuse a name.
    create(other := app.test_client(), sign_in(other, 'other@example.com', 'other-password-123'), name='Platform engineer')


def test_promoting_a_template_moves_master_uploads(client):
    session = sign_in(client)
    main = upload(client, session, MASTER, PLATFORM).json['resume']['template_id']
    manager = create(client, session, name='Engineering manager')['id']
    assert client.patch(f'/api/v1/templates/{manager}', headers=session, json={'is_default': True}).json['is_default']
    assert [t['id'] for t in client.get('/api/v1/templates').json['templates'] if t['is_default']] == [manager]
    upload(client, session, MASTER, MANAGER)
    assert client.get(MASTER).json['template']['id'] == manager and client.get(f'{MASTER}/download').data == MANAGER
    assert client.patch(f'/api/v1/templates/{manager}', headers=session, json={'is_default': False}).status_code == 400
    renamed = client.patch(f'/api/v1/templates/{main}', headers=session, json={'name': 'Platform', 'description': 'IC'})
    assert (renamed.json['name'], renamed.json['is_default'], renamed.json['version_count']) == ('Platform', False, 1)
    assert client.patch(f'/api/v1/templates/{main}', headers=session, json={'name': 'PLATFORM'}).status_code == 200
    assert client.patch(f'/api/v1/templates/{main}', headers=session, json={'name': 'engineering MANAGER'}).status_code == 409


def test_oauth_scopes_and_mcp_master_list_follow_the_default_template(client):
    tokens, _ = connect(client, chosen=['jobs:read', 'resumes:read'])
    reader = {'Authorization': 'Bearer ' + tokens['access_token']}
    assert client.post('/api/v1/templates', headers=reader, json={'name': 'X'}).status_code == 403
    tokens, _ = connect(client)
    headers = {'Authorization': 'Bearer ' + tokens['access_token']}
    first = upload(client, headers, MASTER, PLATFORM).json['resume']
    upload(client, headers, f"/api/v1/templates/{create(client, headers, name='EM')['id']}/versions", MANAGER)
    assert [r['id'] for r in ok(client, tokens, 'list_resumes')['master']] == [first['id']]


def shape(database, table):
    """Columns, foreign keys and every index (UNIQUE constraints and lower(name) included) of a table."""
    with sqlite3.connect(database) as raw:
        sql = dict(raw.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)))
        return (sorted(row[1:4] + row[5:] for row in raw.execute(f'PRAGMA table_info("{table}")')),
                sorted(row[2:5] for row in raw.execute(f'PRAGMA foreign_key_list("{table}")')),
                sorted((unique, partial, tuple(c[2] for c in raw.execute(f'PRAGMA index_info("{name}")')),
                        ' '.join((sql[name] or '').split()))
                       for _, name, unique, _, partial in raw.execute(f'PRAGMA index_list("{table}")')))


def test_schema_12_master_resumes_become_default_templates(app, client):
    session = sign_in(client)
    first, second = (upload(client, session, MASTER, data).json['resume'] for data in (PLATFORM, MANAGER))
    other_id, other = add_user(app, 'other@example.com'), app.test_client()
    mine = upload(other, sign_in(other, 'other@example.com', 'other-password-123'), MASTER, PLATFORM).json['resume']
    add_user(app, 'empty@example.com')  # No master resume, so no template.
    path = app.config['DATABASE_URL'].removeprefix('sqlite:///')
    app.extensions['jobtracker_db']['engine'].dispose()
    with sqlite3.connect(path) as connection:
        rewind_resume_templates(connection)
    upgraded = create_app({**app.config, 'TESTING': True})
    with upgraded.extensions['jobtracker_db']['engine'].connect() as connection:
        assert connection.execute(text('SELECT version FROM schema_version')).scalar() == migrations.SCHEMA_VERSION == 13
        assert connection.execute(text('SELECT user_id, name, is_default FROM resume_templates ORDER BY user_id')
                                  ).all() == [(1, 'Main resume', 1), (other_id, 'Main resume', 1)]
        assert connection.execute(text('SELECT m.id, m.version, t.user_id FROM master_resumes m JOIN resume_templates t '
                                       'ON t.id = m.template_id ORDER BY m.user_id, m.version')).all() == [
            (first['id'], 1, 1), (second['id'], 2, 1), (mine['id'], 1, other_id)]
    create_app({**app.config, 'TESTING': True, 'DATABASE_URL': f'sqlite:///{path}.fresh'})
    for table in ('resume_templates', 'master_resumes'):  # The migrated tables match a fresh database's.
        assert shape(path, table) == shape(f'{path}.fresh', table)
    browser = upgraded.test_client()
    headers = sign_in(browser)
    assert [v['id'] for v in browser.get(MASTER).json['versions']] == [second['id'], first['id']]
    assert upload(browser, headers, MASTER, b'New\n').json['resume']['version'] == 3
