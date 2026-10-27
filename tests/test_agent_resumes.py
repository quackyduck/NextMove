import io
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from jobtracker import create_app, db
from jobtracker.models import AgentToken, now
from test_api import app, client, login  # noqa: F401
from test_agent_tokens import create, bearer


def upload(client, path, headers, content=b'revised resume', filename='resume.txt'):
    return client.post(path, headers=headers, data={'file': (io.BytesIO(content), filename)})


@pytest.mark.parametrize('scopes,expected', [
    (['roles:ingest'], 201), (['roles:search', 'roles:ingest'], 201), (['roles:search'], 401),
])
def test_existing_add_roles_tokens_can_append_versions_without_other_authority(client, app, scopes, expected):
    browser = login(client)
    # Mint the token before the role exists: no receipt ownership or replacement needed.
    token = create(client, browser, permissions=scopes)
    job = client.post('/api/v1/jobs', headers=browser, json={
        'title': 'New role', 'stage': 'needs_review', 'notes': 'private notes',
    }).json
    path = f"/api/v1/jobs/{job['id']}/resumes"
    headers = bearer(token)
    first = upload(client, path, headers)
    assert first.status_code == expected
    if expected == 201:
        second = upload(client, path, headers, b'another version')
        assert second.status_code == 201 and first.json['id'] != second.json['id']
        assert first.json['job_id'] == job['id']
        assert 'private notes' not in first.get_data(as_text=True)
        saved = client.get(path).json['resumes']
        assert len(saved) == 2
        assert client.get('/api/v1/resumes/' + first.json['id'] + '/download').data == b'revised resume'
        assert len(client.get(f"/api/v1/jobs/{job['id']}/activities").json['activities']) == 3
        with app.app_context():
            assert db.get_session().get(AgentToken, token.json['agent_token']['id']).last_used_at is not None
    else:
        assert client.get(path).json['resumes'] == []
        assert [p for p in Path(app.config['UPLOAD_FOLDER']).rglob('*') if p.is_file()] == []
    assert client.get('/api/v1/jobs/' + job['id']).json == job
    for endpoint in (path, '/api/v1/jobs', f"/api/v1/jobs/{job['id']}",
                     f"/api/v1/jobs/{job['id']}/contacts", f"/api/v1/jobs/{job['id']}/activities",
                     '/api/v1/resumes/missing/download', '/api/v1/agent-tokens'):
        assert client.get(endpoint, headers=headers).status_code == 401
    assert client.patch('/api/v1/jobs/' + job['id'], headers=headers, json={'notes': 'changed'}).status_code == 401
    assert client.post('/api/v1/jobs', headers=headers, json={'title': 'Not allowed'}).status_code == 401


@pytest.mark.parametrize('failure', ['expired', 'revoked', 'invalid', 'missing', 'legacy_search'])
def test_invalid_upload_credentials_do_not_write_files(client, app, failure):
    browser = login(client)
    job = client.post('/api/v1/jobs', headers=browser, json={'title': 'New role'}).json
    token = create(client, browser)
    headers = bearer(token)
    if failure in {'expired', 'revoked'}:
        with app.app_context():
            values = {'expires_at': now() - timedelta(seconds=1)} if failure == 'expired' else {'revoked_at': now()}
            db.get_session().execute(update(AgentToken).where(AgentToken.id == token.json['agent_token']['id']).values(**values))
            db.get_session().commit()
    elif failure == 'invalid':
        headers = {'Authorization': 'Bearer nm_ingest_' + 'x' * 43}
    elif failure == 'missing':
        client = app.test_client()  # no browser cookie
        headers = {}
    else:
        import hashlib
        raw = 'search-only-legacy-token-' + 'x' * 32
        app.config['ROLE_SEARCH_TOKEN_SHA256'] = hashlib.sha256(raw.encode()).hexdigest()
        headers = {'Authorization': 'Bearer ' + raw}
    assert upload(client, f"/api/v1/jobs/{job['id']}/resumes", headers).status_code == 401
    assert [p for p in Path(app.config['UPLOAD_FOLDER']).rglob('*') if p.is_file()] == []


def test_managed_upload_preserves_file_validation_limits_and_csrf(client, app):
    browser = login(client)
    token = create(client, browser)
    headers = bearer(token)
    job = client.post('/api/v1/jobs', headers=browser, json={'title': 'New role'}).json
    path = f"/api/v1/jobs/{job['id']}/resumes"
    assert upload(client, path, {}).status_code == 403
    assert upload(client, path, headers, b'not PDF', 'resume.pdf').status_code == 400
    assert upload(client, path, headers, b'bad', 'resume.exe').status_code == 400
    assert client.post(path, headers=headers, data={}).status_code == 400
    assert upload(client, '/api/v1/jobs/missing/resumes', headers).status_code == 404
    app.config['MAX_CONTENT_LENGTH'] = 512
    assert upload(client, path, headers, b'x' * 1024).status_code == 413
    assert [p for p in Path(app.config['UPLOAD_FOLDER']).rglob('*') if p.is_file()] == []


def test_managed_upload_requires_https_and_works_under_prefix(client, app):
    browser = login(client)
    token = create(client, browser)
    headers = {'Authorization': 'bEaReR ' + token.json['token']}
    job = client.post('/api/v1/jobs', headers=browser, json={'title': 'New role'}).json
    fresh = create_app({**app.config, 'APP_ROOT_PATH': '/nextmove', 'APP_ENV': 'production',
                        'SECRET_KEY': 'disposable-production-key-' + 'x' * 32}).test_client()
    path = f"/nextmove/api/v1/jobs/{job['id']}/resumes"
    assert upload(fresh, path, headers).status_code == 403
    response = fresh.post(path, headers=headers, base_url='https://localhost',
                          data={'file': (io.BytesIO(b'%PDF-1.4\ntest'), 'resume.pdf')})
    assert response.status_code == 201
    assert response.headers['Cache-Control'] == 'no-store'
