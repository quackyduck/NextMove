"""Resume templates (schema 13): named master resumes with per-template versions, diffs, and MCP access."""
import io
import sqlite3

from sqlalchemy import text

from jobtracker import create_app, migrations, resume_templates
from accounts import add_user, sign_in
from legacy import rewind_resume_templates
from test_documents import PDF, docx
from test_mcp_oauth import WORD_NS, app, call, client, connect, ok, rpc  # noqa: F401  (fixtures)

PLATFORM, MANAGER, MASTER = docx(), b'Engineering manager who grows teams\n', '/api/v1/profile/resume'


def upload(client, headers, path, data):
    name = 'r.docx' if data.startswith(b'PK') else 'r.pdf' if data.startswith(b'%PDF') else 'r.txt'
    return client.post(path, headers=headers, data={'file': (io.BytesIO(data), name)})


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


# Diffs ----------------------------------------------------------------------
def resume(*paragraphs):
    """A DOCX of ('h', text) headings, ('b', text) bullets and plain body strings."""
    props = {'h': '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>', 'b': '<w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr>'}
    body = ''.join(f'<w:p>{props.get(item[0], "") if isinstance(item, tuple) else ""}<w:r><w:t>'
                   f'{item[1] if isinstance(item, tuple) else item}</w:t></w:r></w:p>' for item in paragraphs)
    return docx(document=f'<w:document xmlns:w="{WORD_NS}"><w:body>{body}</w:body></w:document>'.encode())


IC = resume(('h', 'Summary'), 'Platform engineer who scales infrastructure', ('h', 'Experience'),
            ('b', 'Led a platform team of six'), ('b', 'Cut deploy time by 40%'), ('h', 'Skills'), 'Go, Kubernetes')
EM = resume(('h', 'Summary'), 'Engineering manager who grows teams', ('h', 'Experience'),
            ('b', 'Led a platform team of six'), ('b', 'Hired and coached eight engineers'), ('h', 'Skills'), 'Go, Kubernetes')


def diff(client, headers=None, **ids):
    return client.get('/api/v1/resume-diff', headers=headers, query_string={'from': ids.get('a'), 'to': ids.get('b')})


def test_diff_between_templates_names_the_section_of_each_change(client):
    session = sign_in(client)
    main = upload(client, session, MASTER, IC).json['resume']['template_id']
    manager = create(client, session, name='Engineering manager')
    upload(client, session, f"/api/v1/templates/{manager['id']}/versions", EM)
    body = diff(client, a=main, b=manager['id']).json
    assert (body['identical'], body['truncated'], body['from']['template']) == (False, False, {'id': main, 'name': 'Main resume'})
    assert (body['to']['template']['name'], body['to']['version'], body['to']['kind']) == ('Engineering manager', 1, 'master')
    assert body['summary'] == {'added': 0, 'removed': 0, 'changed': 2, 'unchanged': 5}
    assert [(c['op'], c['section'], c['from']['text'], c['to']['text'], c['to']['kind']) for c in body['changes']] == [
        ('changed', 'Summary', 'Platform engineer who scales infrastructure', 'Engineering manager who grows teams', 'body'),
        ('changed', 'Experience', 'Cut deploy time by 40%', 'Hired and coached eight engineers', 'bullet')]
    assert 'stored_filename' not in str(body)


def test_diff_between_versions_identical_files_and_a_role_resume(client):
    session = sign_in(client)
    first = upload(client, session, MASTER, IC).json['resume']
    longer = resume(('h', 'Summary'), 'Platform engineer who scales infrastructure', ('h', 'Experience'),
                    ('b', 'Led a platform team of six'), ('h', 'Skills'), 'Go, Kubernetes', 'Terraform')
    second = upload(client, session, MASTER, longer).json['resume']
    body = diff(client, a=first['id'], b=second['id']).json
    assert body['summary'] == {'added': 1, 'removed': 1, 'changed': 0, 'unchanged': 6}
    assert [(c['op'], c['section']) for c in body['changes']] == [('removed', 'Experience'), ('added', 'Skills')]
    same = diff(client, a=first['id'], b=first['id']).json
    assert (same['identical'], same['changes'], same['summary']['unchanged']) == (True, [], 7)
    # A role's resume can be compared with any template, whichever template it was tailored from.
    other = create(client, session, name='Other')['id']
    upload(client, session, f'/api/v1/templates/{other}/versions', EM)
    job = client.post('/api/v1/jobs', headers=session, json={'title': 'SRE'}).json
    tailored = client.post(f"/api/v1/jobs/{job['id']}/resumes", headers=session,
                           data={'file': (io.BytesIO(EM), 'tailored.docx')}).json
    against = diff(client, a=first['template_id'], b=tailored['id']).json
    assert (against['from']['version'], against['to']['kind'], against['to']['template']) == (2, 'job', None)
    assert 'stored_filename' not in str(against) and diff(client, a=other, b=tailored['id']).json['identical'] is True


def test_diff_refuses_pdfs_empty_templates_unknown_ids_and_oversized_input(client, monkeypatch):
    session = sign_in(client)
    main = upload(client, session, MASTER, PDF).json['resume']['template_id']
    pdf = diff(client, a=main, b=main)
    assert pdf.status_code == 422 and 'PDF' in pdf.json['error']
    empty = create(client, session, name='Empty')['id']
    assert diff(client, a=empty, b=main).status_code == 409
    assert diff(client, a='nope', b=main).json == {'error': 'from: no template or resume with this ID'}
    assert diff(client, b=main).status_code == 400
    first, second = (upload(client, session, f'/api/v1/templates/{empty}/versions', data).json['resume']['id']
                     for data in (IC, EM))
    monkeypatch.setattr(resume_templates, 'MAX_CHANGES', 1)
    capped = diff(client, a=first, b=second).json
    assert (len(capped['changes']), capped['summary']['changed'], capped['truncated']) == (1, 2, True)
    monkeypatch.setattr(resume_templates, 'MAX_PARAGRAPHS', 6)
    text = upload(client, session, f'/api/v1/templates/{empty}/versions', b'line\n' * 9).json['resume']['id']
    for pair in ((first, second), (text, text)):  # DOCX and TXT stop reading past the cap.
        refused = diff(client, a=pair[0], b=pair[1])
        assert refused.status_code == 422 and 'more than 6 paragraphs' in refused.json['error']


def test_diff_paragraphs_pairs_replacements_and_reports_surplus():
    old = [{'id': 'p0', 'text': 'A', 'kind': 'heading'}, {'id': 'p1', 'text': 'x', 'kind': 'body'}]
    new = old[:1] + [{'id': 'p1', 'text': 'y', 'kind': 'body'}, {'id': 'p2', 'text': 'z', 'kind': 'body'}]
    changes, summary = resume_templates.diff_paragraphs(old, new)
    assert summary == {'added': 1, 'removed': 0, 'changed': 1, 'unchanged': 1}
    assert [(c['op'], c['section'], c['from'] and c['from']['text']) for c in changes] == [
        ('changed', 'A', 'x'), ('added', 'A', None)]
    assert resume_templates.diff_paragraphs([], new)[1]['added'] == 3


# MCP ------------------------------------------------------------------------
def test_mcp_reads_templates_and_differences(client, app):
    tokens, _ = connect(client, chosen=['jobs:read'])
    assert not {'list_templates', 'get_template', 'diff_resumes'} & {
        t['name'] for t in rpc(client, tokens, 'tools/list')['result']['tools']}
    tokens, _ = connect(client)
    headers = {'Authorization': 'Bearer ' + tokens['access_token']}
    assert ok(client, tokens, 'list_templates') == {'templates': []}
    assert ok(client, tokens, 'list_resumes') == {'template': None, 'master': []}
    upload(client, headers, MASTER, IC)
    manager = create(client, headers, name='Engineering manager', description='People leadership')
    upload(client, headers, f"/api/v1/templates/{manager['id']}/versions", EM)
    listed = ok(client, tokens, 'list_templates')['templates']
    assert [(t['name'], t['is_default'], t['version_count']) for t in listed] == [
        ('Main resume', True, 1), ('Engineering manager', False, 1)]
    detail = ok(client, tokens, 'get_template', template_id=manager['id'])['template']
    assert (detail['description'], [v['version'] for v in detail['versions']]) == ('People leadership', [1])
    assert ok(client, tokens, 'read_resume', template_id=manager['id'])['paragraphs'][1]['text'].startswith('Engineering')
    assert ok(client, tokens, 'read_resume')['paragraphs'][1]['text'].startswith('Platform')
    assert call(client, tokens, 'read_resume', template_id=manager['id'], resume_id='x')['isError']
    result = ok(client, tokens, 'diff_resumes', from_id=listed[0]['id'], to_id=manager['id'])
    assert result['summary']['changed'] == 2 and result['to']['template']['name'] == 'Engineering manager'
    assert ok(client, tokens, 'list_resumes')['template'] == {'id': listed[0]['id'], 'name': 'Main resume'}
    # Another account's template behaves like one that never existed.
    add_user(app, 'other@example.com')
    theirs, _ = connect(client, email='other@example.com', password='other-password-123')
    stranger = '00000000-0000-0000-0000-000000000000'
    for name, key in (('get_template', 'template_id'), ('read_resume', 'template_id'), ('diff_resumes', 'from_id')):
        arguments = lambda value: {key: value, **({'to_id': value} if key == 'from_id' else {})}
        mine, unknown = (call(client, theirs, name, **arguments(value)) for value in (manager['id'], stranger))
        assert mine['isError'] and mine == unknown, name
