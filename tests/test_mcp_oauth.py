"""OAuth 2.1 for third-party agents, the MCP server, and OAuth access to the published API."""
import base64
import hashlib
import io
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from jobtracker import create_app, migrations
from accounts import add_user, sign_in
from legacy import rewind_resume_templates
from test_documents import PDF, docx

REDIRECT = 'https://agent.example.com/callback'
WORD_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
RESUME_XML = f'''<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{WORD_NS}"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Experience</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr><w:r><w:t>Led a platform team</w:t></w:r></w:p>
<w:p/>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Python</w:t></w:r><w:r><w:tab/><w:t>Go</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>'''.encode()
ALL = 'jobs:read jobs:write resumes:read resumes:write'


@pytest.fixture()
def app(tmp_path):
    return create_app({
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'test-secret',
        'APP_PASSWORD_HASH': generate_password_hash('test-password'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "test.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'resumes'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
    })


@pytest.fixture()
def client(app):
    return app.test_client()


def register(client, redirect_uris=(REDIRECT,), name='Test Agent'):
    response = client.post('/oauth/register', json={'redirect_uris': list(redirect_uris), 'client_name': name})
    assert response.status_code == 201, response.json
    return response.json['client_id']


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


def authorize_params(client_id, challenge, scope=ALL, state='xyz'):
    return {'response_type': 'code', 'client_id': client_id, 'redirect_uri': REDIRECT, 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'scope': scope, 'state': state}


def approve(client, csrf, client_id, challenge, scope=ALL, chosen=None, decision='allow'):
    form = {**authorize_params(client_id, challenge, scope), 'csrf_token': csrf['X-CSRF-Token'], 'decision': decision}
    form['scopes'] = chosen if chosen is not None else scope.split()
    response = client.post('/oauth/authorize', data=form)
    assert response.status_code == 302, response.data
    return parse_qs(urlsplit(response.headers['Location']).query)


def connect(client, scope=ALL, chosen=None, email='owner@localhost', password='test-password'):
    """Register, sign in, approve and exchange: (tokens, client_id)."""
    client_id = register(client)
    csrf = sign_in(client, email, password)
    verifier, challenge = pkce()
    query = approve(client, csrf, client_id, challenge, scope, chosen)
    tokens = exchange(client, client_id, query['code'][0], verifier)
    assert tokens.status_code == 200, tokens.json
    return tokens.json, client_id


def exchange(client, client_id, code, verifier):
    return client.post('/oauth/token', data={'grant_type': 'authorization_code', 'code': code, 'client_id': client_id,
                                             'redirect_uri': REDIRECT, 'code_verifier': verifier})


def bearer(tokens):
    return {'Authorization': 'Bearer ' + tokens['access_token']}


def rpc(client, tokens, method, params=None, identifier=1):
    response = client.post('/mcp', headers={**bearer(tokens), 'MCP-Protocol-Version': '2025-06-18'},
                           json={'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params or {}})
    assert response.status_code == 200, response.data
    return response.json


def call(client, tokens, tool_name, **arguments):
    return rpc(client, tokens, 'tools/call', {'name': tool_name, 'arguments': arguments})['result']


def ok(client, tokens, tool_name, **arguments):
    result = call(client, tokens, tool_name, **arguments)
    assert result['isError'] is False, result['content'][0]['text']
    return result['structuredContent']


# Discovery and registration ----------------------------------------------
def test_discovery_metadata_describes_the_server(client):
    server = client.get('/.well-known/oauth-authorization-server').json
    assert server['code_challenge_methods_supported'] == ['S256']
    assert server['token_endpoint_auth_methods_supported'] == ['none']
    assert server['registration_endpoint'].endswith('/oauth/register')
    resource = client.get('/.well-known/oauth-protected-resource/mcp')
    assert resource.json['resource'] == 'http://localhost/mcp'
    assert resource.json['authorization_servers'] == ['http://localhost']
    assert resource.headers['Access-Control-Allow-Origin'] == '*'
    unauthenticated = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
    assert unauthenticated.status_code == 401
    assert 'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"' in \
        unauthenticated.headers['WWW-Authenticate']


def test_public_base_url_sets_the_issuer(tmp_path):
    app = create_app({'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'k',
                      'APP_PASSWORD_HASH': generate_password_hash('p'), 'PUBLIC_BASE_URL': 'https://example.com/nextmove/',
                      'DATABASE_URL': f'sqlite:///{tmp_path / "t.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'r'),
                      'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'th.sqlite3')})
    metadata = app.test_client().get('/.well-known/oauth-authorization-server').json
    assert metadata['issuer'] == 'https://example.com/nextmove'
    assert metadata['authorization_endpoint'] == 'https://example.com/nextmove/oauth/authorize'


@pytest.mark.parametrize('uri', ['http://agent.example.com/cb', 'javascript:alert(1)', 'https://x.example/#frag',
                                 'data:text/html,hi', 'https://exa mple.com/'])
def test_registration_rejects_unsafe_redirects(client, uri):
    response = client.post('/oauth/register', json={'redirect_uris': [uri]})
    assert response.status_code == 400
    assert response.json['error'] == 'invalid_redirect_uri'


def test_registration_accepts_loopback_and_native_schemes(client):
    register(client, ['http://127.0.0.1:33418/callback', 'cursor://anysphere.cursor/oauth'])
    response = client.post('/oauth/register', json={'redirect_uris': [REDIRECT],
                                                    'token_endpoint_auth_method': 'client_secret_basic'})
    assert response.json['token_endpoint_auth_method'] == 'none'
    assert 'client_secret' not in response.json


def test_registration_is_rate_limited_per_ip(client):
    statuses = [client.post('/oauth/register', json={'redirect_uris': [REDIRECT]}).status_code for _ in range(11)]
    assert statuses[:10] == [201] * 10 and statuses[10] == 429


# Authorization -------------------------------------------------------------
def test_signed_out_users_sign_in_then_return_to_consent(client):
    client_id = register(client)
    _, challenge = pkce()
    response = client.get('/oauth/authorize', query_string=authorize_params(client_id, challenge))
    assert response.status_code == 302
    location = urlsplit(response.headers['Location'])
    assert location.path == '/'
    back = parse_qs(location.query)['oauth_return'][0]
    assert back.startswith('oauth/authorize?')
    sign_in(client)
    consent = client.get('/' + back)
    assert consent.status_code == 200
    assert b'Test Agent' in consent.data and b'agent.example.com' in consent.data
    assert consent.headers['X-Frame-Options'] == 'DENY'


def test_consent_escapes_the_self_declared_client_name(client):
    client_id = register(client, name='<script>alert(1)</script>')
    sign_in(client)
    consent = client.get('/oauth/authorize', query_string=authorize_params(client_id, pkce()[1]))
    assert b'<script>alert' not in consent.data and b'&lt;script&gt;' in consent.data


def test_unknown_client_or_redirect_never_redirects(client):
    client_id = register(client)
    sign_in(client)
    challenge = pkce()[1]
    assert client.get('/oauth/authorize', query_string=authorize_params('nmc_missing', challenge)).status_code == 400
    params = {**authorize_params(client_id, challenge), 'redirect_uri': 'https://evil.example.com/cb'}
    response = client.get('/oauth/authorize', query_string=params)
    assert response.status_code == 400 and 'Location' not in response.headers


def test_pkce_and_scope_errors_redirect_back_with_state(client):
    client_id = register(client)
    sign_in(client)
    params = {**authorize_params(client_id, pkce()[1]), 'code_challenge_method': 'plain'}
    query = parse_qs(urlsplit(client.get('/oauth/authorize', query_string=params).headers['Location']).query)
    assert query['error'] == ['invalid_request'] and query['state'] == ['xyz']
    params = authorize_params(client_id, pkce()[1], scope='jobs:read admin')
    query = parse_qs(urlsplit(client.get('/oauth/authorize', query_string=params).headers['Location']).query)
    assert query['error'] == ['invalid_scope']


def test_consent_requires_the_session_csrf_token(client):
    client_id = register(client)
    sign_in(client)
    form = {**authorize_params(client_id, pkce()[1]), 'csrf_token': 'forged', 'decision': 'allow', 'scopes': ['jobs:read']}
    assert client.post('/oauth/authorize', data=form).status_code == 403


def test_deny_returns_access_denied(client):
    client_id = register(client)
    csrf = sign_in(client)
    query = approve(client, csrf, client_id, pkce()[1], decision='deny')
    assert query['error'] == ['access_denied'] and query['state'] == ['xyz'] and 'code' not in query


def test_users_can_narrow_scopes_on_the_consent_page(client):
    tokens, _ = connect(client, chosen=['jobs:read'])
    assert tokens['scope'] == 'jobs:read'
    names = {item['name'] for item in rpc(client, tokens, 'tools/list')['result']['tools']}
    assert 'list_jobs' in names and 'move_job' not in names and 'upload_resume' not in names


# Tokens --------------------------------------------------------------------
def test_code_requires_the_matching_verifier_and_is_single_use(client):
    client_id = register(client)
    csrf = sign_in(client)
    verifier, challenge = pkce()
    code = approve(client, csrf, client_id, challenge)['code'][0]
    assert exchange(client, client_id, code, pkce()[0]).json['error'] == 'invalid_grant'
    # The failed attempt used the code up: a stolen code cannot be retried with guesses.
    assert exchange(client, client_id, code, verifier).json['error'] == 'invalid_grant'


def test_code_replay_revokes_tokens_already_issued(client):
    client_id = register(client)
    csrf = sign_in(client)
    verifier, challenge = pkce()
    code = approve(client, csrf, client_id, challenge)['code'][0]
    tokens = exchange(client, client_id, code, verifier).json
    assert client.get('/api/v1/jobs', headers=bearer(tokens)).status_code == 200
    assert exchange(client, client_id, code, verifier).status_code == 400
    assert client.get('/api/v1/jobs', headers=bearer(tokens)).status_code == 401


def test_code_is_bound_to_its_client(client):
    client_id, other = register(client), register(client)
    csrf = sign_in(client)
    verifier, challenge = pkce()
    code = approve(client, csrf, client_id, challenge)['code'][0]
    assert exchange(client, other, code, verifier).json['error'] == 'invalid_grant'


def test_refresh_rotates_and_reuse_disconnects_the_agent(client):
    tokens, client_id = connect(client)
    refreshed = client.post('/oauth/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
                                                  'refresh_token': tokens['refresh_token']})
    assert refreshed.status_code == 200 and refreshed.headers['Cache-Control'] == 'no-store'
    new = refreshed.json
    assert new['refresh_token'] != tokens['refresh_token']
    assert client.get('/api/v1/jobs', headers=bearer(new)).status_code == 200
    replay = client.post('/oauth/token', data={'grant_type': 'refresh_token', 'client_id': client_id,
                                               'refresh_token': tokens['refresh_token']})
    assert replay.json['error'] == 'invalid_grant'
    assert client.get('/api/v1/jobs', headers=bearer(new)).status_code == 401


def test_revocation_endpoint_disconnects(client):
    tokens, client_id = connect(client)
    assert client.post('/oauth/revoke', data={'token': tokens['refresh_token'], 'client_id': client_id}).status_code == 200
    assert client.get('/api/v1/jobs', headers=bearer(tokens)).status_code == 401


def test_users_see_and_revoke_connections(client):
    tokens, _ = connect(client)
    csrf = sign_in(client)
    connections = client.get('/api/v1/oauth/connections').json['connections']
    assert [item['client_name'] for item in connections] == ['Test Agent']
    assert connections[0]['scopes'] == sorted(ALL.split())
    revoked = client.post(f'/api/v1/oauth/connections/{connections[0]["id"]}/revoke', headers=csrf)
    assert revoked.status_code == 200
    assert client.get('/api/v1/jobs', headers=bearer(tokens)).status_code == 401
    assert client.get('/api/v1/oauth/connections').json['connections'] == []


def test_reapproving_the_same_client_reuses_its_connection(client):
    client_id = register(client)
    csrf = sign_in(client)
    for chosen in (['jobs:read'], ['jobs:read', 'jobs:write']):
        verifier, challenge = pkce()
        exchange(client, client_id, approve(client, csrf, client_id, challenge, chosen=chosen)['code'][0], verifier)
    connections = client.get('/api/v1/oauth/connections').json['connections']
    assert len(connections) == 1 and connections[0]['scopes'] == ['jobs:read', 'jobs:write']


# The published API with OAuth ----------------------------------------------
def test_rest_routes_enforce_scopes(client):
    tokens, _ = connect(client, chosen=['jobs:read'])
    headers = bearer(tokens)
    assert client.get('/api/v1/jobs', headers=headers).status_code == 200
    denied = client.post('/api/v1/jobs', headers=headers, json={'title': 'X'})
    assert denied.status_code == 403
    assert 'error="insufficient_scope"' in denied.headers['WWW-Authenticate']
    assert client.get('/api/v1/profile/resume', headers=headers).status_code == 403


def test_oauth_tokens_never_reach_browser_only_routes(client):
    tokens, _ = connect(client)
    headers = bearer(tokens)
    assert client.get('/api/v1/agent-tokens', headers=headers).status_code == 401
    assert client.get('/api/v1/onboarding', headers=headers).status_code == 401
    assert client.get('/api/v1/oauth/connections', headers=headers).status_code == 401
    assert client.post('/api/v1/auth/password', headers=headers, json={}).status_code == 401


def test_rest_resume_routes_accept_oauth(client):
    tokens, _ = connect(client)
    headers = bearer(tokens)
    job = client.post('/api/v1/jobs', headers=headers, json={'title': 'Engineer', 'company': 'Acme'}).json
    upload = client.post(f'/api/v1/jobs/{job["id"]}/resumes', headers=headers,
                         data={'file': (io.BytesIO(PDF), 'tailored.pdf')})
    assert upload.status_code == 201, upload.json
    master = client.post('/api/v1/profile/resume', headers=headers, data={'file': (io.BytesIO(docx()), 'resume.docx')})
    assert master.status_code == 201, master.json
    assert client.get('/api/v1/profile/resume/download', headers=headers).status_code == 200


def test_openapi_is_published(client):
    spec = client.get('/api/v1/openapi.json').json
    assert spec['openapi'].startswith('3.1')
    assert spec['servers'] == [{'url': 'http://localhost/api/v1'}]
    assert spec['paths']['/jobs/{job_id}']['patch']['security'] == [{'oauth2': ['jobs:write']}]
    assert spec['components']['securitySchemes']['oauth2']['flows']['authorizationCode']['tokenUrl'] == \
        'http://localhost/oauth/token'


# MCP -----------------------------------------------------------------------
def test_initialize_negotiates_the_protocol(client):
    tokens, _ = connect(client)
    result = rpc(client, tokens, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                                'clientInfo': {'name': 'test', 'version': '1'}})['result']
    assert result['protocolVersion'] == '2025-06-18'
    assert result['capabilities'] == {'tools': {'listChanged': False}}
    assert 'stage' in result['instructions']
    future = rpc(client, tokens, 'initialize', {'protocolVersion': '2099-01-01'})['result']
    assert future['protocolVersion'] == '2025-11-25'
    notification = client.post('/mcp', headers=bearer(tokens), json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert notification.status_code == 202
    assert client.get('/mcp', headers=bearer(tokens)).status_code == 405
    bad_version = client.post('/mcp', headers={**bearer(tokens), 'MCP-Protocol-Version': '1999-01-01'},
                              json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    assert bad_version.status_code == 400
    assert rpc(client, tokens, 'nope')['error']['code'] == -32601


def test_mcp_rejects_foreign_browser_origins(client):
    tokens, _ = connect(client)
    response = client.post('/mcp', headers={**bearer(tokens), 'Origin': 'https://evil.example.com'},
                           json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    assert response.status_code == 403


def test_tools_move_roles_through_the_pipeline(client):
    tokens, _ = connect(client)
    added = ok(client, tokens, 'add_job', title='Staff Engineer', company='Acme', description='Build things.')
    job = added['job']
    assert job['stage'] == 'needs_review' and 'posting' not in added
    moved = ok(client, tokens, 'move_job', job_id=job['id'], stage='applied', note='Applied through the careers site')
    assert moved['job']['stage'] == 'applied' and moved['stage_label'] == 'Applied'
    ok(client, tokens, 'add_note', job_id=job['id'], text='Recruiter replied')
    full = ok(client, tokens, 'get_job', job_id=job['id'])
    notes = full['job']['notes']
    assert re.search(r'\[\d{4}-\d{2}-\d{2} · Test Agent\] Applied through the careers site', notes)
    assert notes.endswith('Recruiter replied')
    ok(client, tokens, 'set_outcome', job_id=job['id'], outcome='rejected')
    assert ok(client, tokens, 'list_jobs')['total'] == 0
    assert ok(client, tokens, 'list_jobs', outcome='all', query='acme')['jobs'][0]['outcome'] == 'rejected'
    history = ok(client, tokens, 'get_job_history', job_id=job['id'])['activities']
    assert [item['kind'] for item in history][0] == 'created' and len(history) >= 4
    contact = ok(client, tokens, 'add_contact', job_id=job['id'], name='Pat', email='pat@acme.example')
    assert contact['contact']['name'] == 'Pat'


def test_update_job_reports_stale_versions_as_tool_errors(client):
    tokens, _ = connect(client)
    job = ok(client, tokens, 'add_job', title='Engineer')['job']
    ok(client, tokens, 'update_job', job_id=job['id'], version=job['version'], fields={'salary': '$200k'})
    stale = call(client, tokens, 'update_job', job_id=job['id'], version=job['version'], fields={'salary': '$1'})
    assert stale['isError'] and 'get_job' in stale['content'][0]['text']
    invalid = call(client, tokens, 'move_job', job_id=job['id'], stage='hired')
    assert invalid['isError'] and 'Unknown stage' in invalid['content'][0]['text']
    extra = call(client, tokens, 'list_jobs', admin=True)
    assert extra['isError']


def test_add_job_from_a_url_queues_the_posting_reader(client):
    tokens, _ = connect(client)
    added = ok(client, tokens, 'add_job', url='https://jobs.example.com/roles/1')
    assert added['job']['stage'] == 'needs_review'
    assert added['posting'].startswith('Reading the posting')


def test_tools_respect_tenancy(client, app):
    add_user(app, 'other@example.com')
    other, _ = connect(client, email='other@example.com', password='other-password-123')
    client.post('/api/v1/auth/logout', headers=sign_in(client))
    mine, _ = connect(client)
    job = ok(client, mine, 'add_job', title='Private role')['job']
    missing = call(client, other, 'get_job', job_id=job['id'])
    assert missing['isError'] and 'not found' in missing['content'][0]['text']
    assert ok(client, other, 'list_jobs')['jobs'] == []


def test_resume_tools_upload_read_and_link(client):
    tokens, _ = connect(client)
    assert call(client, tokens, 'read_resume')['isError']  # No master resume yet.
    encoded = base64.b64encode(docx(document=RESUME_XML)).decode()
    uploaded = ok(client, tokens, 'upload_resume', target='master', filename='Jane.docx', content_base64=encoded)
    assert uploaded['created'] and uploaded['resume']['version'] == 1
    again = ok(client, tokens, 'upload_resume', target='master', filename='Jane.docx', content_base64=encoded)
    assert again['created'] is False
    read = ok(client, tokens, 'read_resume')
    assert read['paragraphs'] == [
        {'id': 'p0', 'text': 'Experience', 'kind': 'heading', 'style': 'Heading1'},
        {'id': 'p1', 'text': 'Led a platform team', 'kind': 'bullet', 'style': ''},
        {'id': 'p3', 'text': 'Python\tGo', 'kind': 'body', 'style': ''},
    ]
    job = ok(client, tokens, 'add_job', title='Engineer')['job']
    attached = ok(client, tokens, 'upload_resume', target=job['id'], filename='tailored.pdf',
                  content_base64=base64.b64encode(PDF).decode())
    assert attached['resume']['job_id'] == job['id'] and attached['target'] == job['id']
    listed = ok(client, tokens, 'list_resumes', job_id=job['id'])
    assert len(listed['master']) == 1 and len(listed['job']) == 1
    assert ok(client, tokens, 'get_job', job_id=job['id'])['resumes'][0]['original_filename'] == 'tailored.pdf'
    pdf = call(client, tokens, 'read_resume', resume_id=listed['job'][0]['id'])
    assert pdf['isError'] and 'download' in pdf['content'][0]['text']
    link = ok(client, tokens, 'get_resume_download_link', resume_id=listed['job'][0]['id'])
    path = urlsplit(link['url']).path
    downloaded = client.get(path)
    assert downloaded.status_code == 200 and downloaded.data == PDF
    rejected = call(client, tokens, 'upload_resume', target='master', filename='x.docx',
                    content_base64=base64.b64encode(b'not a docx').decode())
    assert rejected['isError']


def test_resumes_with_a_dtd_are_refused(client):
    tokens, _ = connect(client)
    bomb = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]><w:document xmlns:w="' + WORD_NS.encode() + b'"/>'
    ok(client, tokens, 'upload_resume', target='master', filename='r.docx',
       content_base64=base64.b64encode(docx(document=bomb)).decode())
    refused = call(client, tokens, 'read_resume')
    assert refused['isError'] and 'could not be read' in refused['content'][0]['text']


def test_upload_links_accept_one_file_and_die_with_the_connection(client):
    tokens, client_id = connect(client)
    job = ok(client, tokens, 'add_job', title='Engineer')['job']
    link = ok(client, tokens, 'create_resume_upload_link', target=job['id'])
    path = urlsplit(link['url']).path
    response = client.post(path, data={'file': (io.BytesIO(docx()), 'tailored.docx')})
    assert response.status_code == 201, response.json
    assert response.json['resume']['job_id'] == job['id']
    assert client.post(path.replace('/uploads/', '/uploads/x'), data={'file': (io.BytesIO(docx()), 'a.docx')}).status_code == 404
    download = urlsplit(ok(client, tokens, 'get_resume_download_link', resume_id=response.json['resume']['id'])['url']).path
    client.post('/oauth/revoke', data={'token': tokens['access_token'], 'client_id': client_id})
    assert client.post(path, data={'file': (io.BytesIO(docx()), 'again.docx')}).status_code == 404
    assert client.get(download).status_code == 404


def test_upload_target_must_be_an_owned_role(client):
    tokens, _ = connect(client)
    result = call(client, tokens, 'create_resume_upload_link', target='00000000-0000-0000-0000-000000000000')
    assert result['isError']


# Schema --------------------------------------------------------------------
def test_schema_11_databases_upgrade_to_12(app, tmp_path):
    engine = app.extensions['jobtracker_db']['engine']
    path = app.config['DATABASE_URL'].removeprefix('sqlite:///')
    engine.dispose()
    with sqlite3.connect(path) as raw:
        rewind_resume_templates(raw)
    with engine.begin() as connection:
        for table in ('oauth_tokens', 'oauth_codes', 'oauth_grants', 'oauth_clients'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('UPDATE schema_version SET version=11')
    upgraded = create_app({**app.config, 'TESTING': True})
    with upgraded.extensions['jobtracker_db']['engine'].connect() as connection:
        assert connection.execute(text('SELECT version FROM schema_version')).scalar() == migrations.SCHEMA_VERSION
        tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'oauth_clients', 'oauth_grants', 'oauth_codes', 'oauth_tokens'} <= tables
    tokens, _ = connect(upgraded.test_client())
    assert tokens['token_type'] == 'Bearer'
