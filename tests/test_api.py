import io
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from jobtracker import create_app


@pytest.fixture()
def app(tmp_path):
    return create_app({
        'TESTING': True, 'APP_ENV': 'development',
        'SECRET_KEY': 'test-secret', 'APP_PASSWORD_HASH': generate_password_hash('test-password'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "test.sqlite3"}',
        'UPLOAD_FOLDER': str(tmp_path / 'resumes'), 'AGENT_API_TOKEN': 'test-agent-token',
        'SESSION_COOKIE_SECURE': False,
    })


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    result = client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'})
    assert result.status_code == 200
    return {'X-CSRF-Token': result.json['csrf_token']}


def test_auth_csrf_and_logout(client):
    assert client.get('/health').json == {'status': 'ok'}
    assert client.get('/api/v1/jobs').status_code == 401
    assert client.get('/api/v1/auth/session').json['authenticated'] is False
    assert client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'bad'}).status_code == 401
    assert client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    headers = login(client)
    assert client.get('/api/v1/auth/session').json['csrf_token'] == headers['X-CSRF-Token']
    assert client.post('/api/v1/jobs', json={'title': 'Engineer'}).status_code == 403
    assert client.post('/api/v1/auth/logout', headers=headers).status_code == 200
    assert client.get('/api/v1/jobs').status_code == 401


def test_agent_auth_and_job_history(client):
    headers = {'Authorization': 'Bearer test-agent-token'}
    assert client.get('/api/v1/jobs', headers={'Authorization': 'Bearer bad'}).status_code == 401
    created = client.post('/api/v1/jobs', json={'title': 'Engineer', 'company': 'Acme'}, headers=headers)
    assert created.status_code == 201
    job = created.json
    changed = client.patch(f'/api/v1/jobs/{job["id"]}', json={'stage': 'applied', 'version': job['version']}, headers=headers)
    assert changed.status_code == 200
    assert changed.json['stage'] == 'applied'
    assert client.patch(f'/api/v1/jobs/{job["id"]}', json={'notes': 'stale', 'version': job['version']}, headers=headers).status_code == 409
    assert len(client.get(f'/api/v1/jobs/{job["id"]}/activities', headers=headers).json['activities']) >= 2
    assert client.get('/api/v1/jobs', headers=headers).json['jobs'][0]['id'] == job['id']


def test_validation_and_contacts(client):
    headers = login(client)
    assert client.post('/api/v1/jobs', data='[]', content_type='application/json', headers=headers).status_code == 400
    job = client.post('/api/v1/jobs', json={'url': 'https://example.com/job'}, headers=headers).json
    result = client.post(f'/api/v1/jobs/{job["id"]}/contacts', json={'name': 'Pat', 'email': 'pat@example.com'}, headers=headers)
    assert result.status_code == 201
    assert client.get(f'/api/v1/jobs/{job["id"]}/contacts').json['contacts'][0]['name'] == 'Pat'
    assert client.patch(f'/api/v1/jobs/{job["id"]}', json={'stage': 'invalid'}, headers=headers).status_code == 400
    assert client.get('/api/v1/jobs/missing').status_code == 404


def test_resume_versions_download_and_private_storage(client, app):
    headers = login(client)
    job = client.post('/api/v1/jobs', json={'title': 'Engineer'}, headers=headers).json
    endpoint = f'/api/v1/jobs/{job["id"]}/resumes'
    results = []
    for content in (b'Resume version 1', b'Resume version 2'):
        result = client.post(endpoint, data={'file': (io.BytesIO(content), '../../resume.txt')}, headers=headers)
        assert result.status_code == 201
        assert result.json['original_filename'] == 'resume.txt'
        results.append(result.json)
    assert results[0]['id'] != results[1]['id']
    assert len(client.get(endpoint).json['resumes']) == 2
    download = client.get(f'/api/v1/resumes/{results[0]["id"]}/download')
    assert download.data == b'Resume version 1'
    assert download.headers['Content-Disposition'].startswith('attachment;')
    assert len([p for p in Path(app.config['UPLOAD_FOLDER']).rglob('*') if p.is_file()]) == 2
    assert client.post(endpoint, data={'file': (io.BytesIO(b'fake'), 'resume.pdf')}, headers=headers).status_code == 400
    assert client.post(endpoint, data={'file': (io.BytesIO(b'bad'), 'resume.exe')}, headers=headers).status_code == 400
    client.post('/api/v1/auth/logout', headers=headers)
    assert client.get(f'/api/v1/resumes/{results[0]["id"]}/download').status_code == 401


def test_production_fails_closed(monkeypatch):
    for key in ('SECRET_KEY', 'APP_PASSWORD_HASH', 'APP_ENV'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match='Production requires'):
        create_app({'APP_ENV': 'production', 'SECRET_KEY': None, 'APP_PASSWORD_HASH': None})


def test_login_throttling(client):
    for _ in range(10):
        assert client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'wrong'}).status_code == 401
    assert client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'test-password'}).status_code == 429


def test_upload_rejections_and_bearer_unicode(client, app):
    assert client.get('/api/v1/jobs', headers={'Authorization': 'Bearer 🔒'}).status_code == 401
    headers = login(client)
    job = client.post('/api/v1/jobs', json={'title': 'Engineer'}, headers=headers).json
    endpoint = f'/api/v1/jobs/{job["id"]}/resumes'
    assert client.post(endpoint, data={}, headers=headers).status_code == 400
    app.config['MAX_CONTENT_LENGTH'] = 512
    assert client.post(endpoint, data={'file': (io.BytesIO(b'A' * 1024), 'large.txt')}, headers=headers).status_code == 413
    assert [p for p in Path(app.config['UPLOAD_FOLDER']).rglob('*') if p.is_file()] == []


def test_outreach_badge_api_persistence_and_validation(client):
    headers = {'Authorization': 'Bearer test-agent-token'}
    job = client.post('/api/v1/jobs', json={'title': 'Engineer', 'stage': 'outreach'}, headers=headers).json
    assert job['outreach_completed'] is True
    result = client.patch(f'/api/v1/jobs/{job["id"]}', json={'stage': 'applied', 'version': job['version']}, headers=headers)
    assert result.status_code == 200
    assert result.json['outreach_completed'] is True
    assert client.get('/api/v1/jobs', headers=headers).json['jobs'][0]['outreach_completed'] is True
    result = client.patch(f'/api/v1/jobs/{job["id"]}', json={'outreach_completed': False}, headers=headers)
    assert result.json['stage'] == 'applied'
    assert result.json['outreach_completed'] is False
    assert client.patch(f'/api/v1/jobs/{job["id"]}', json={'outreach_completed': 'false'}, headers=headers).status_code == 400


def test_unpaired_surrogates_are_malformed_json_and_pairs_are_kept(client):
    headers = {'Authorization': 'Bearer test-agent-token'}
    for body in ('{"title": "Engineer", "company": "\\ud800"}', '{"title": "Engineer", "\\udfff": 1}',
                 b'{"title": "Engineer", "company": "\xed\xa0\x80"}', b'{"title": "Engineer \xff"}'):
        # Escaped, as a key, as raw surrogate bytes, and as invalid UTF-8.
        response = client.post('/api/v1/jobs', data=body, content_type='application/json', headers=headers)
        assert response.status_code == 400 and response.json == {'error': 'A JSON object is required'}
    assert client.get('/api/v1/jobs', headers=headers).json['jobs'] == []
    job = client.post('/api/v1/jobs', data='{"title": "Engineer", "company": "Acme \\ud83d\\ude00"}',
                      content_type='application/json', headers=headers).json
    assert job['company'] == 'Acme \U0001F600'
    changed = client.patch(f'/api/v1/jobs/{job["id"]}', json={'notes': ['\udc00'], 'version': job['version']},
                           headers=headers)
    assert changed.status_code == 400
    changed = client.patch(f'/api/v1/jobs/{job["id"]}', json={'notes': 'Ship it \U0001F680', 'version': job['version']},
                           headers=headers)
    assert changed.status_code == 200
    assert client.get(f'/api/v1/jobs/{job["id"]}', headers=headers).json['notes'] == 'Ship it \U0001F680'


def test_deeply_nested_json_is_malformed_not_a_crash(client):
    headers = {'Authorization': 'Bearer test-agent-token'}
    for depth, status in ((63, 400), (64, 400), (100000, 400)):  # 63 reaches the service, which rejects the list.
        body = '{"title": "Engineer", "notes": ' + '[' * depth + ']' * depth + '}'
        response = client.post('/api/v1/jobs', data=body, content_type='application/json', headers=headers)
        assert response.status_code == status
        assert (response.json == {'error': 'A JSON object is required'}) is (depth >= 64)
    deep = '{"email": ' + '[' * 100000 + ']' * 100000 + '}'
    malformed = client.post('/api/v1/auth/login', data='{', content_type='application/json')
    nested = client.post('/api/v1/auth/login', data=deep, content_type='application/json')
    assert nested.status_code == malformed.status_code != 500 and nested.json == malformed.json


@pytest.mark.parametrize('path', ['/api/v1/jobs', '/api/v1/jobs/search', '/oauth/authorize'])
@pytest.mark.parametrize('query', [b'q=\xed\xa0\x80', b'client_id=\xff'])
def test_query_strings_must_be_utf8(client, path, query):
    # WSGI servers pass the raw bytes through as latin-1; Werkzeug decodes them strictly.
    response = client.get(path, environ_overrides={'QUERY_STRING': query.decode('latin-1')},
                          headers={'Authorization': 'Bearer test-agent-token'})
    assert response.status_code == 400 and response.json == {'error': 'The query string must be valid UTF-8'}


def test_utf8_query_strings_still_work(client):
    query = 'q=%F0%9F%98%80'  # An emoji, percent-encoded as browsers send it.
    assert client.get('/api/v1/jobs', environ_overrides={'QUERY_STRING': query},
                      headers={'Authorization': 'Bearer test-agent-token'}).status_code == 200
