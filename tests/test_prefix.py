import io

import pytest
from werkzeug.security import generate_password_hash

from jobtracker import create_app


@pytest.fixture
def prefixed(tmp_path):
    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'index.html').write_text('<html>Next Move</html>')
    (dist / 'assets').mkdir()
    (dist / 'assets' / 'app.js').write_text('/* app */')
    return create_app({
        'TESTING': True, 'APP_ENV': 'production', 'SECRET_KEY': 's' * 40,
        'APP_PASSWORD_HASH': generate_password_hash('prefix-test-password'),
        'AGENT_API_TOKEN': '', 'APP_ROOT_PATH': '/nextmove', 'TRUST_PROXY': True,
        'TRUSTED_HOSTS': ['nicolasmelo.com', '127.0.0.1', 'localhost'],
        'DATABASE_URL': f'sqlite:///{tmp_path / "jobs.sqlite3"}',
        'UPLOAD_FOLDER': str(tmp_path / 'resumes'), 'FRONTEND_DIST': str(dist),
    })


def test_redirect_boundaries_assets_health_and_cookie_scope(prefixed):
    client = prefixed.test_client()
    response = client.get('/nextmove?source=home', base_url='https://nicolasmelo.com')
    assert response.status_code == 308
    assert response.headers['Location'] == '/nextmove/?source=home'
    for path in ('/', '/nextmove-other/', '/api/v1/jobs', '/health'):
        assert client.get(path).status_code == 404
    assert client.get('/nextmove/').status_code == 200
    assert client.get('/nextmove/assets/app.js').data == b'/* app */'
    assert client.get('/nextmove/health').json == {'status': 'ok'}
    assert client.get('/nextmove/api/v1/jobs').status_code == 401
    login = client.post('/nextmove/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'prefix-test-password'},
                        base_url='http://nicolasmelo.com',
                        headers={'Origin': 'https://nicolasmelo.com', 'X-Forwarded-Proto': 'https'})
    assert login.status_code == 200
    cookie = login.headers['Set-Cookie']
    assert cookie.startswith('next_move_session=')
    assert 'Path=/nextmove/' in cookie
    assert all(flag in cookie for flag in ('Secure;', 'HttpOnly;', 'SameSite=Lax'))


def test_prefixed_session_csrf_resume_and_logout(prefixed):
    client = prefixed.test_client()
    base = {'base_url': 'https://nicolasmelo.com'}
    endpoint = '/nextmove/api/v1'
    login = client.post(endpoint + '/auth/login', json={'email': 'owner@localhost', 'password': 'prefix-test-password'}, **base)
    headers = {'X-CSRF-Token': login.json['csrf_token']}
    assert client.post(endpoint + '/jobs', json={'title': 'Demo'}, **base).status_code == 403
    job = client.post(endpoint + '/jobs', json={'title': 'Demo'}, headers=headers, **base).json
    resume = client.post(endpoint + f'/jobs/{job["id"]}/resumes',
                         data={'file': (io.BytesIO(b'Private demo resume'), 'resume.txt')}, headers=headers, **base)
    assert resume.status_code == 201
    download = endpoint + f'/resumes/{resume.json["id"]}/download'
    assert client.get(download, **base).data == b'Private demo resume'
    assert client.get(endpoint + '/jobs', **base).json['jobs'][0]['title'] == 'Demo'
    assert client.post(endpoint + '/auth/logout', headers=headers, **base).status_code == 200
    assert client.get(download, **base).status_code == 401


def test_unsafe_mount_configuration_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match='APP_ROOT_PATH'):
        create_app({'APP_ROOT_PATH': '//evil.example'})
