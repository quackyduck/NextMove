"""Resume templates (schema 13): named master resumes with per-template versions, diffs, and MCP access."""
import base64
import io
import sqlite3
from urllib.parse import urlsplit

from sqlalchemy import text

from jobtracker import create_app, migrations, resume_files, resume_templates
from accounts import add_user, sign_in
from legacy import rewind_resume_templates
from test_documents import PDF, docx
from test_mcp_oauth import WORD_NS, app, bearer, call, client, connect, ok, rpc  # noqa: F401  (fixtures)

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
    assert client.get(f"{MASTER}/download?version={'9' * 30}").status_code == 400  # Not an SQLite overflow (500).
    for index in range(19):  # The 20-version cap is per template.
        upload(client, session, MASTER, f'v{index}\n'.encode())
    assert upload(client, session, MASTER, b'One more\n').status_code == 409
    assert upload(client, session, path, b'One more\n').status_code == 201


def test_names_are_validated_unique_ignoring_case_and_capped(client, app):
    session = sign_in(client)
    create(client, session, name='Platform engineer', description='Line one\n\tLine two')
    create(client, session, name='Ingénieur')
    create(client, session, name='می\u200cخواهم 👩\u200d💻')  # ZWNJ and ZWJ are allowed.
    for body, status in (({'name': 'platform ENGINEER'}, 409), ({'name': ' INGÉNIEUR '}, 409), ({'name': '  '}, 400),
                         ({'name': 'Inge\u0301nieur'}, 409), ({'name': 'A\x00'}, 400), ({'name': 'A\u202eB'}, 400),
                         ({'name': '\u0958' * 100}, 400), ({'name': 'A\u2028B'}, 400), ({'name': '\ufeffA'}, 400),
                         ({'name': 'A\u2066B'}, 400),
                         ({'name': 'Ok', 'description': 'a\x07'}, 400), ({}, 400), ({'name': 'x' * 101}, 400),
                         ({'name': 'Ok', 'colour': 'blue'}, 400), ({'name': 'Ok', 'is_default': True}, 400),
                         ({'name': 'Ok', 'description': 7}, 400)):
        assert client.post('/api/v1/templates', headers=session, json=body).status_code == status, body
    for index in range(7):
        create(client, session, name=f'Role {index}')
    assert client.post('/api/v1/templates', headers=session, json={'name': 'One more'}).status_code == 409
    # Ten empty templates, no default, none named "Main resume": a plain upload has nowhere to go.
    assert upload(client, session, MASTER, PLATFORM).status_code == 409
    assert client.get(MASTER).json['template'] is None
    add_user(app, 'other@example.com')  # Another account can reuse a name.
    create(other := app.test_client(), sign_in(other, 'other@example.com', 'other-password-123'), name='Platform engineer')


def test_promoting_a_template_moves_master_uploads(client):
    session = sign_in(client)
    manager, ic = (create(client, session, name=name)['id'] for name in ('Engineering manager', 'IC'))
    # Several templates and no default: a master upload goes to (a new) "Main resume", which becomes the default.
    main = upload(client, session, MASTER, PLATFORM).json['resume']['template_id']
    assert main not in (manager, ic) and client.get(MASTER).json['template']['name'] == 'Main resume'
    promote = lambda: client.patch(f'/api/v1/templates/{manager}', headers=session, json={'is_default': True})
    assert promote().status_code == 409  # ...and an empty one cannot become the default.
    upload(client, session, f'/api/v1/templates/{manager}/versions', PLATFORM)
    assert promote().json['is_default']
    assert [t['id'] for t in client.get('/api/v1/templates').json['templates'] if t['is_default']] == [manager]
    upload(client, session, MASTER, MANAGER)
    assert client.get(MASTER).json['template']['id'] == manager and client.get(f'{MASTER}/download').data == MANAGER
    assert [v['version'] for v in client.get(MASTER).json['versions']] == [2, 1]
    assert client.patch(f'/api/v1/templates/{manager}', headers=session, json={'is_default': False}).status_code == 400
    renamed = client.patch(f'/api/v1/templates/{main}', headers=session, json={'name': 'Platform', 'description': 'IC'})
    assert (renamed.json['name'], renamed.json['is_default'], renamed.json['version_count']) == ('Platform', False, 1)
    assert client.patch(f'/api/v1/templates/{main}', headers=session, json={'name': 'PLATFORM'}).status_code == 200
    assert client.patch(f'/api/v1/templates/{main}', headers=session, json={'name': 'engineering MANAGER'}).status_code == 409


def test_a_master_upload_with_no_default_fills_the_only_template(client):
    session = sign_in(client)
    only = create(client, session, name='Senior product manager')['id']
    assert client.get(MASTER).json['template'] is None  # Templates but no default yet.
    assert upload(client, session, MASTER, PLATFORM).json['resume']['template_id'] == only
    assert [(t['id'], t['is_default']) for t in client.get('/api/v1/templates').json['templates']] == [(only, True)]


def test_oauth_scopes_and_mcp_master_list_follow_the_default_template(client):
    tokens, _ = connect(client, chosen=['jobs:read', 'resumes:read'])
    reader = {'Authorization': 'Bearer ' + tokens['access_token']}
    assert client.post('/api/v1/templates', headers=reader, json={'name': 'X'}).status_code == 403
    tokens, _ = connect(client)
    headers = sign_in(client)  # Only the user creates and fills templates before a default exists.
    manager = create(client, headers, name='EM')['id']
    first = upload(client, headers, f'/api/v1/templates/{manager}/versions', MANAGER).json['resume']
    assert client.get(MASTER).json['template']['id'] == manager  # The first template to get a version is the default.
    upload(client, bearer(tokens), f"/api/v1/templates/{create(client, bearer(tokens), name='IC')['id']}/versions", PLATFORM)
    assert [r['id'] for r in ok(client, tokens, 'list_resumes')['master']] == [first['id']]
    link = urlsplit(ok(client, tokens, 'create_resume_upload_link', target='master')['url']).path
    assert client.post(link, data={'file': (io.BytesIO(b'Later\n'), 'r.txt')}).json['resume']['template_id'] == manager


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
    job = client.post('/api/v1/jobs', headers=session, json={'title': 'SRE'}).json['id']
    with sqlite3.connect(path) as connection:  # A tailored draft depends on a version through the rebuild.
        connection.execute("INSERT INTO tailored_drafts VALUES ('d1', 1, ?, ?, 'rq', 1, '{}', 'd.docx', NULL, "
                           "'2026-01-01 00:00:00', NULL)", (job, first['id']))
        rewind_resume_templates(connection)
    upgraded = create_app({**app.config, 'TESTING': True})
    with upgraded.extensions['jobtracker_db']['engine'].connect() as connection:
        assert connection.execute(text('SELECT version FROM schema_version')).scalar() == migrations.SCHEMA_VERSION == 13
        assert connection.execute(text('SELECT user_id, name, is_default FROM resume_templates ORDER BY user_id')
                                  ).all() == [(1, 'Main resume', 1), (other_id, 'Main resume', 1)]
        assert connection.execute(text('SELECT m.id, m.version, t.user_id FROM master_resumes m JOIN resume_templates t '
                                       'ON t.id = m.template_id ORDER BY m.user_id, m.version')).all() == [
            (first['id'], 1, 1), (second['id'], 2, 1), (mine['id'], 1, other_id)]
        assert connection.execute(text('SELECT m.version FROM tailored_drafts d JOIN master_resumes m '
                                       'ON m.id = d.master_resume_id')).all() == [(1,)]
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
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
    assert [(c['from_section'], c['to_section']) for c in body['changes']] == [('Summary',) * 2, ('Experience',) * 2]
    assert body['outline'] == {'from': ['Summary', 'Experience', 'Skills'], 'to': ['Summary', 'Experience', 'Skills']}
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
    assert diff(client, a='nope', b=main).json == {'error': 'from: template or resume not found'}
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
    monkeypatch.setattr(resume_templates, 'MAX_PARAGRAPHS', 2000)
    monkeypatch.setattr(resume_templates, 'MAX_TEXT', 20)  # Bounds the text a response can carry.
    refused = diff(client, a=first, b=first)
    assert refused.status_code == 422 and 'more than 20 characters' in refused.json['error']


def test_diff_paragraphs_pairs_replacements_and_reports_surplus():
    old = [{'id': 'p0', 'text': 'A', 'kind': 'heading'}, {'id': 'p1', 'text': 'x', 'kind': 'body'}]
    new = old[:1] + [{'id': 'p1', 'text': 'y', 'kind': 'body'}, {'id': 'p2', 'text': 'z', 'kind': 'body'}]
    changes, summary = resume_templates.diff_paragraphs(old, new)
    assert summary == {'added': 1, 'removed': 0, 'changed': 1, 'unchanged': 1}
    assert [(c['op'], c['section'], c['from'] and c['from']['text']) for c in changes] == [
        ('changed', 'A', 'x'), ('added', 'A', None)]
    assert resume_templates.diff_paragraphs([], new)[1]['added'] == 3
    # Each side keeps its own section; NFC-equal text and kind-only changes are not differences.
    moved = [{'id': 'p0', 'text': 'B', 'kind': 'heading'}, {'id': 'p1', 'text': 'w', 'kind': 'body'}]
    [change] = resume_templates.diff_paragraphs(old, moved)[0][1:]
    assert (change['op'], change['from_section'], change['to_section'], change['section']) == ('changed', 'A', 'B', 'B')
    composed = [{'id': 'p0', 'text': 'Caf\u00e9', 'kind': 'heading'}]
    assert resume_templates.diff_paragraphs(composed, [{'id': 'p0', 'text': 'Cafe\u0301', 'kind': 'body'}])[0] == []


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
    assert 'cannot be empty' in call(client, tokens, 'read_resume', template_id='')['content'][0]['text']



def b64(data):
    return base64.b64encode(data).decode()


def test_mcp_creates_templates_only_with_resumes_write(client):
    reader, _ = connect(client, chosen=['resumes:read'])
    assert 'create_template' not in {t['name'] for t in rpc(client, reader, 'tools/list')['result']['tools']}
    refused = call(client, reader, 'create_template', name='Engineering manager')
    assert refused['isError'] and 'resumes:write' in refused['content'][0]['text']
    tokens, _ = connect(client)
    ok(client, tokens, 'upload_resume', target='master', filename='ic.txt', content_base64='SUM=')
    created = ok(client, tokens, 'create_template', name='  Engineering manager ', description='People leadership')
    assert {key: created['template'][key] for key in ('name', 'description', 'is_default', 'version_count')} == {
        'name': 'Engineering manager', 'description': 'People leadership', 'is_default': False, 'version_count': 0}
    for arguments, message in (({'name': ' '}, 'name cannot be empty'), ({'name': 'x' * 101}, 'exceeds 100'),
                               ({'name': 'A\u202eB'}, 'control or formatting'), ({'name': 7}, 'name must be text'),
                               ({'name': 'ENGINEERING MANAGER'}, 'already exists'), ({}, 'name is required'),
                               ({'name': 'PM', 'is_default': True}, 'Unknown arguments')):
        result = call(client, tokens, 'create_template', **arguments)
        assert result['isError'] and message in result['content'][0]['text'], (arguments, result)
    for index in range(3):  # Main resume and Engineering manager exist: agents stop at 5 of the 10.
        ok(client, tokens, 'create_template', name=f'Role {index}')
    capped = call(client, tokens, 'create_template', name='One more')
    assert capped['isError'] and 'fewer than 5' in capped['content'][0]['text']
    rest = client.post('/api/v1/templates', headers=bearer(tokens), json={'name': 'One more'})
    assert rest.status_code == 409 and rest.json == {'error': capped['content'][0]['text']}  # REST: the same policy.
    assert create(client, sign_in(client), name='One more')['name'] == 'One more'  # The user's own slots remain.
    assert {'update_template', 'set_default_template'}.isdisjoint(t['name'] for t in rpc(client, tokens, 'tools/list')['result']['tools'])


def test_template_names_collide_across_case_spacing_and_width(client):
    session = sign_in(client)
    create(client, session, name='Main resume')
    for name in ('main RESUME', 'Main  resume', 'Main\u00a0resume', '\uff2d\uff41\uff49\uff4e resume'):
        assert client.post('/api/v1/templates', headers=session, json={'name': name}).status_code == 409, name
    assert create(client, session, name='M\u0430in resume')['name'] == 'M\u0430in resume'  # Cyrillic: another script.


def test_agents_never_choose_the_default(client):
    tokens, _ = connect(client)
    users = create(client, sign_in(client), name='Product manager')['id']  # The user's own template, still empty.
    for name, arguments in (('create_template', {'name': 'Senior PM'}), ('create_resume_upload_link', {'template_id': users}),
                            ('upload_resume', {'template_id': users, 'filename': 'a.txt', 'content_base64': 'YQ=='})):
        result = call(client, tokens, name, **arguments)
        assert result['isError'] and 'no master resume yet' in result['content'][0]['text'], name
    agent = bearer(tokens)  # The same policy for OAuth tokens on REST.
    assert client.post('/api/v1/templates', headers=agent, json={'name': 'Senior PM'}).status_code == 403
    assert upload(client, agent, f'/api/v1/templates/{users}/versions', MANAGER).status_code == 403
    assert ok(client, tokens, 'list_templates')['templates'][0]['is_default'] is False
    ok(client, tokens, 'upload_resume', target='master', filename='a.txt', content_base64='YQ==')  # #26's rule decides.
    senior = ok(client, tokens, 'create_template', name='Senior PM')['template']
    assert senior['is_default'] is False and create(client, agent, name='Staff PM')['is_default'] is False
    assert upload(client, agent, f"/api/v1/templates/{senior['id']}/versions", MANAGER).status_code == 201
    for body in ({'name': 'Main'}, {'is_default': True}, {'description': 'Use for every role'}):
        refused = client.patch(f"/api/v1/templates/{senior['id']}", headers=agent, json=body)
        assert refused.status_code == 403 and 'cannot rename' in refused.json['error'], body
    assert client.patch(f"/api/v1/templates/{senior['id']}", headers=sign_in(client), json={'is_default': True}).json['is_default']


def test_mcp_uploads_into_a_named_template_inline_and_by_link(client):
    tokens, _ = connect(client)
    main = ok(client, tokens, 'upload_resume', target='master', filename='ic.docx', content_base64=b64(IC))['resume']['template_id']
    manager = ok(client, tokens, 'create_template', name='Engineering manager')['template']['id']
    inline = ok(client, tokens, 'upload_resume', template_id=manager, filename='em.docx', content_base64=b64(EM))
    assert (inline['resume']['template_id'], inline['resume']['version'], inline['created']) == (manager, 1, True)
    link = ok(client, tokens, 'create_resume_upload_link', template_id=manager)
    assert link['template'] == {'id': manager, 'name': 'Engineering manager'}
    redeemed = client.post(urlsplit(link['url']).path, data={'file': (io.BytesIO(b'Engineering manager v2\n'), 'em.txt')})
    assert redeemed.status_code == 201 and redeemed.json['resume']['template_id'] == manager
    assert [v['version'] for v in ok(client, tokens, 'get_template', template_id=manager)['template']['versions']] == [2, 1]
    templates = {t['id']: t for t in ok(client, tokens, 'list_templates')['templates']}
    assert (templates[main]['is_default'], templates[main]['version_count'], templates[manager]['is_default']) == (True, 1, False)
    # A "master" link is signed with the default it was issued for, even if the user promotes another meanwhile.
    master = ok(client, tokens, 'create_resume_upload_link', target='master')
    assert master['template'] == {'id': main, 'name': 'Main resume'}
    assert client.patch(f'/api/v1/templates/{manager}', headers=sign_in(client), json={'is_default': True}).status_code == 200
    late = client.post(urlsplit(master['url']).path, data={'file': (io.BytesIO(b'IC v2\n'), 'ic.txt')})
    assert late.json['resume']['template_id'] == main
    job = ok(client, tokens, 'add_job', title='Engineer')['job']
    for arguments, message in (({'target': job['id'], 'template_id': manager}, 'not both'),
                               ({'target': 'master', 'template_id': manager}, 'not both'),
                               ({'template_id': ' '}, 'template_id cannot be empty'), ({}, 'or template_id')):
        result = call(client, tokens, 'upload_resume', filename='a.txt', content_base64='YQ==', **arguments)
        assert result['isError'] and message in result['content'][0]['text'], arguments
    assert 'or template_id' in call(client, tokens, 'create_resume_upload_link')['content'][0]['text']


def test_mcp_template_uploads_never_reach_other_accounts(client, app):
    tokens, client_id = connect(client)
    ok(client, tokens, 'upload_resume', target='master', filename='a.txt', content_base64='YQ==')
    mine = ok(client, tokens, 'create_template', name='Engineering manager')['template']['id']
    path = urlsplit(ok(client, tokens, 'create_resume_upload_link', template_id=mine)['url']).path
    add_user(app, 'other@example.com')
    theirs, _ = connect(client, email='other@example.com', password='other-password-123')
    stranger = '00000000-0000-0000-0000-000000000000'
    for name, extra in (('upload_resume', {'filename': 'x.txt', 'content_base64': 'eA=='}), ('create_resume_upload_link', {})):
        foreign, unknown = (call(client, theirs, name, template_id=value, **extra) for value in (mine, stranger))
        assert foreign['isError'] and foreign == unknown and 'not found' in foreign['content'][0]['text'].lower(), name
    # A link whose claims named another account's template (were the signing key ever misused) still fails as unknown.
    other_template = create(client, sign_in(client, 'other@example.com', 'other-password-123'), name='Theirs')['id']
    with app.app_context():
        serializer = resume_files._serializer('upload')
        forged = serializer.dumps({**serializer.loads(path.rsplit('/', 1)[1]), 'm': other_template})
    response = client.post(f'/api/v1/uploads/{forged}', data={'file': (io.BytesIO(b'intruder\n'), 'x.txt')})
    assert response.status_code == 404 and response.json == {'error': 'Template not found'}
    assert ok(client, theirs, 'get_template', template_id=other_template)['template']['version_count'] == 0
    client.post('/oauth/revoke', data={'token': tokens['access_token'], 'client_id': client_id})
    revoked = client.post(path, data={'file': (io.BytesIO(b'late\n'), 'late.txt')})
    assert revoked.status_code == 404 and 'expired' in revoked.json['error']
