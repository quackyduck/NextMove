"""Independent integration regressions from the first-demo review."""
import io
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from jobtracker import create_app, services


@pytest.fixture
def app_factory(tmp_path):
    def build(**overrides):
        config = {
            'TESTING': True,
            'APP_ENV': 'production',
            'SECRET_KEY': 'review-secret-' + 'x' * 40,
            'APP_PASSWORD_HASH': generate_password_hash('review-password'),
            'AGENT_API_TOKEN': 'review-agent-token',
            'DATABASE_URL': f'sqlite:///{tmp_path / "review.sqlite3"}',
            'UPLOAD_FOLDER': str(tmp_path / 'resumes'),
            'TRUST_PROXY': False,
        }
        config.update(overrides)
        return create_app(config)
    return build


def test_forwarded_scheme_is_ignored_without_explicit_proxy_trust(app_factory):
    client = app_factory().test_client()
    response = client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'review-password'},
                           base_url='http://tracker.example',
                           headers={'Origin': 'https://tracker.example', 'X-Forwarded-Proto': 'https'})
    assert response.status_code == 403


def test_one_trusted_proxy_allows_https_login_without_trusting_forwarded_host(app_factory):
    client = app_factory(TRUST_PROXY=True).test_client()
    response = client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'review-password'},
                           base_url='http://tracker.example',
                           headers={'Origin': 'https://tracker.example', 'X-Forwarded-Proto': 'https'})
    assert response.status_code == 200
    assert 'Secure;' in response.headers['Set-Cookie']
    forged = client.post('/api/v1/auth/login', json={'email': 'owner@localhost', 'password': 'review-password'},
                        base_url='http://tracker.example',
                        headers={'Origin': 'https://evil.example', 'X-Forwarded-Proto': 'https',
                                 'X-Forwarded-Host': 'evil.example'})
    assert forged.status_code == 403


def test_failed_resume_metadata_transaction_removes_file(app_factory, monkeypatch):
    app = app_factory()
    client = app.test_client()
    headers = {'Authorization': 'Bearer review-agent-token'}
    job = client.post('/api/v1/jobs', headers=headers, json={'title': 'Engineer'}).json

    def fail(*_args, **_kwargs):
        raise services.ServiceError('Simulated database rejection', 409)

    monkeypatch.setattr(services, 'create_resume', fail)
    response = client.post(f'/api/v1/jobs/{job["id"]}/resumes', headers=headers,
                           data={'file': (io.BytesIO(b'Resume'), 'resume.txt')})
    assert response.status_code == 409
    assert list(Path(app.config['UPLOAD_FOLDER']).iterdir()) == []
    assert client.get(f'/api/v1/jobs/{job["id"]}/resumes', headers=headers).json == {'resumes': []}


def test_all_private_routes_require_auth_and_disable_caching(app_factory):
    client = app_factory().test_client()
    routes = [('/jobs', 'GET'), ('/jobs', 'POST'), ('/jobs/id', 'GET'), ('/jobs/id', 'PATCH'),
              ('/jobs/id/contacts', 'GET'), ('/jobs/id/contacts', 'POST'),
              ('/jobs/id/resumes', 'GET'), ('/jobs/id/resumes', 'POST'),
              ('/jobs/id/activities', 'GET'), ('/resumes/id/download', 'GET')]
    for route, method in routes:
        response = client.open('/api/v1' + route, method=method)
        assert response.status_code == 401, (route, method)
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['X-Content-Type-Options'] == 'nosniff'


def test_found_date_uses_los_angeles_when_utc_has_rolled_over(app_factory, monkeypatch):
    from datetime import datetime, timezone

    class AtUtcMidnight(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 10, 20, 0, 30, tzinfo=timezone.utc)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(services, 'datetime', AtUtcMidnight)
    client = app_factory().test_client()
    response = client.post('/api/v1/jobs', json={'title': 'Night owl role'},
                           headers={'Authorization': 'Bearer review-agent-token'})
    assert response.status_code == 201
    assert response.json['found_date'] == '2026-10-19'


def test_host_allowlist_rejects_unknown_and_forwarded_hosts(app_factory):
    client = app_factory(TRUSTED_HOSTS=['tracker.example', '127.0.0.1'], TRUST_PROXY=True).test_client()
    assert client.get('/health', base_url='https://tracker.example').status_code == 200
    assert client.get('/health', base_url='http://127.0.0.1').status_code == 200
    assert client.get('/health', base_url='https://evil.example',
                      headers={'X-Forwarded-Host': 'tracker.example'}).status_code == 400
