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
    result = client.post('/api/v1/auth/login', json={'password': 'test-password'})
    assert result.status_code == 200
    return {'X-CSRF-Token': result.json['csrf_token']}


def test_auth_csrf_and_logout(client):
    assert client.get('/health').json == {'status': 'ok'}
    assert client.get('/api/v1/jobs').status_code == 401
    assert client.get('/api/v1/auth/session').json['authenticated'] is False
    assert client.post('/api/v1/auth/login', json={'password': 'bad'}).status_code == 401
    assert client.post('/api/v1/auth/login', json={'password': 'test-password'}, headers={'Origin': 'https://evil.example'}).status_code == 403
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
    assert len(list(Path(app.config['UPLOAD_FOLDER']).iterdir())) == 2
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
        assert client.post('/api/v1/auth/login', json={'password': 'wrong'}).status_code == 401
    assert client.post('/api/v1/auth/login', json={'password': 'test-password'}).status_code == 429


def test_upload_rejections_and_bearer_unicode(client, app):
    assert client.get('/api/v1/jobs', headers={'Authorization': 'Bearer 🔒'}).status_code == 401
    headers = login(client)
    job = client.post('/api/v1/jobs', json={'title': 'Engineer'}, headers=headers).json
    endpoint = f'/api/v1/jobs/{job["id"]}/resumes'
    assert client.post(endpoint, data={}, headers=headers).status_code == 400
    app.config['MAX_CONTENT_LENGTH'] = 512
    assert client.post(endpoint, data={'file': (io.BytesIO(b'A' * 1024), 'large.txt')}, headers=headers).status_code == 413
    assert list(Path(app.config['UPLOAD_FOLDER']).iterdir()) == []
