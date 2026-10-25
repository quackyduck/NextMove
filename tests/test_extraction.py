import json
import socket
import time

import pytest

from jobtracker import extraction
from jobtracker.services import ServiceError
from test_api import app, client, login  # noqa: F401


def structured(**overrides):
    data = {'@type': 'JobPosting', 'title': 'Platform Engineer',
            'hiringOrganization': {'name': 'Acme'},
            'description': '<p>Build systems.</p><h2>Qualifications</h2><ul><li>Python experience</li></ul><h2>Benefits</h2><p>Health cover</p>',
            'baseSalary': {'currency': 'USD', 'value': {'minValue': 150000, 'maxValue': 190000, 'unitText': 'YEAR'}},
            'qualifications': '<ul><li>SQL</li><li>Distributed systems</li></ul>'}
    data.update(overrides)
    return '<script type="application/ld+json">' + json.dumps({'@graph': [data]}) + '</script>'


def test_structured_preview_and_saved_qualifications(client, monkeypatch):
    monkeypatch.setattr(extraction, 'fetch_page', lambda url: structured())
    headers = login(client)
    result = client.post('/api/v1/jobs/extract', json={'url': 'https://jobs.example/123'}, headers=headers)
    assert result.status_code == 202
    result = wait_for_preview(client, result.json['id'])
    fields = result.json['fields']
    assert fields['company'] == 'Acme'
    assert fields['title'] == 'Platform Engineer'
    assert fields['salary'] == 'USD 150000–190000 / year'
    assert fields['url'] == 'https://jobs.example/123'
    assert fields['notes'] == 'Key qualifications\nSQL\nDistributed systems'
    assert result.json['warnings'] == []
    assert client.get('/api/v1/jobs').json['jobs'] == []
    saved = client.post('/api/v1/jobs', json=fields, headers=headers)
    assert saved.status_code == 201
    assert saved.json['stage'] == 'interesting'
    assert client.get(f'/api/v1/jobs/{saved.json["id"]}').json['notes'] == fields['notes']


def test_extraction_auth_csrf_and_payload(client, monkeypatch):
    calls = []
    monkeypatch.setattr(extraction, 'fetch_page', lambda url: calls.append(url) or structured())
    assert client.post('/api/v1/jobs/extract', json={'url': 'https://example.com'}).status_code == 401
    headers = login(client)
    assert client.post('/api/v1/jobs/extract', json={'url': 'https://example.com'}).status_code == 403
    assert client.post('/api/v1/jobs/extract', json=[] , headers=headers).status_code == 400
    assert client.post('/api/v1/jobs/extract', json={'url': 'https://example.com', 'extra': True}, headers=headers).status_code == 400
    assert calls == []
    response = client.post('/api/v1/jobs/extract', json={'url': 'https://example.com'}, headers={'Authorization': 'Bearer test-agent-token'})
    assert response.status_code == 202
    wait_for_preview(client, response.json['id'])


def test_description_qualifications_fallback_and_salary_variants():
    result = extraction.parse_posting(structured(qualifications=None, baseSalary={'currency': 'EUR', 'value': {'value': 80, 'unitText': 'HOUR'}}), 'https://example.com/job')
    assert result['fields']['salary'] == 'EUR 80 / hour'
    assert 'Python experience' in result['fields']['notes']
    assert 'Health cover' not in result['fields']['notes']
    assert extraction.salary_text({'value': {'minValue': 100}}) == 'From 100'
    assert extraction.salary_text({'value': {'maxValue': 200}}) == 'Up to 200'
    assert extraction.salary_text(100, 'USD') == 'USD 100'


def test_plain_page_and_missing_fields():
    html = '''<title>Careers</title><script>doEvil()</script><h1>Engineer at Small Co</h1>
    <h2>Requirements</h2><ul><li>Python and SQL</li><li>3 years experience</li></ul>
    <h2>Benefits</h2><p>Salary: $120,000 - $160,000 per year</p>'''
    result = extraction.parse_posting(html, 'https://example.com/job')
    assert result['fields']['title'] == 'Engineer'
    assert result['fields']['company'] == 'Small Co'
    assert result['fields']['salary'] == '$120,000 - $160,000 per year'
    assert 'Python and SQL' in result['fields']['notes']
    assert 'Salary:' not in result['fields']['notes']
    assert 'doEvil' not in result['fields']['description']
    result = extraction.parse_posting('<h1>Engineer</h1>', 'https://example.com/job')
    assert result['fields']['company'] == result['fields']['salary'] == result['fields']['notes'] == ''
    assert 'company, salary, key qualifications' in result['warnings'][-1]


def test_malformed_data_and_multiple_postings():
    result = extraction.parse_posting('<script type="application/ld+json">invalid</script><h1>Developer</h1>', 'https://example.com')
    assert result['fields']['title'] == 'Developer'
    with pytest.raises(ServiceError, match='multiple roles'):
        extraction.parse_posting(structured() + structured(title='Other role'), 'https://example.com')
    with pytest.raises(ServiceError, match='No role details'):
        extraction.parse_posting('<script>render()</script>', 'https://example.com')


@pytest.mark.parametrize('url', [None, '', 'file:///etc/passwd', 'ftp://example.com', 'https://user:pass@example.com', 'https://example.com:8443', 'https://example.com\\@127.0.0.1', 'https://example.com/\r\nsecret'])
def test_invalid_urls(url):
    with pytest.raises(ServiceError):
        extraction.public_target(url)


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '192.168.0.1', '0.0.0.0', '::1', 'fc00::1', '::ffff:127.0.0.1'])
def test_private_addresses_and_mixed_dns(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 80))])
    with pytest.raises(ServiceError, match='public'):
        extraction.public_target('http://example.com')


class Response:
    status = 200
    def __init__(self, body=b'<h1>Engineer</h1>', headers=None):
        from email.message import Message
        self.body = body
        self.headers = Message()
        for key, value in (headers or {'Content-Type': 'text/html; charset=utf-8'}).items():
            self.headers[key] = value
    def getheader(self, name, default=None):
        return self.headers.get(name, default)
    def read1(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


@pytest.fixture
def network(monkeypatch):
    class Sock:
        def __init__(self, *args):
            self.connected = None
        def settimeout(self, value):
            pass
        def connect(self, address):
            self.connected = address
        def close(self):
            pass
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs):
            pass
        def request(self, method, path, headers):
            assert self.sock.connected == ('93.184.216.34', 80)
        def getresponse(self):
            return responses.pop(0)
        def close(self):
            pass
    responses = []
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1' if host == 'localhost' else '93.184.216.34', 80))])
    monkeypatch.setattr(socket, 'socket', Sock)
    monkeypatch.setattr(extraction.http.client, 'HTTPConnection', Connection)
    return responses


def test_fetch_and_redirect_private_rejection(network):
    network.append(Response())
    assert extraction.fetch_page('http://example.com') == '<h1>Engineer</h1>'
    redirect = Response(headers={'Location': 'http://localhost/internal'})
    redirect.status = 302
    network.append(redirect)
    with pytest.raises(ServiceError, match='public'):
        extraction.fetch_page('http://example.com')


def test_redirect_limit_and_relative_redirect(network):
    redirect = Response(headers={'Location': '/new'})
    redirect.status = 301
    network.extend([redirect, Response()])
    assert 'Engineer' in extraction.fetch_page('http://example.com/old')
    network.extend([redirect] * 5)
    with pytest.raises(ServiceError, match='too many'):
        extraction.fetch_page('http://example.com')


def test_fetch_size_type_status_and_timeout(network, monkeypatch):
    monkeypatch.setattr(extraction, 'MAX_BYTES', 100)
    network.append(Response(b'x' * 101))
    with pytest.raises(ServiceError, match='too large'):
        extraction.fetch_page('http://example.com')
    network.append(Response(headers={'Content-Type': 'application/pdf'}))
    with pytest.raises(ServiceError, match='HTML'):
        extraction.fetch_page('http://example.com')
    response = Response()
    response.status = 403
    network.append(response)
    with pytest.raises(ServiceError, match='block automated access'):
        extraction.fetch_page('http://example.com')
    class TimedOut(Response):
        def read1(self, size):
            raise TimeoutError()
    network.append(TimedOut())
    with pytest.raises(ServiceError, match='could not be loaded'):
        extraction.fetch_page('http://example.com')


def wait_for_preview(client, identifier):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get('/api/v1/extractions/' + identifier)
        if response.json['status'] != 'running':
            return response
        time.sleep(0.01)
    pytest.fail('Background extraction did not finish')


def test_async_ack_is_under_one_second_while_extractor_is_blocked(client, monkeypatch):
    import threading
    from jobtracker import extraction_tasks
    entered, release = threading.Event(), threading.Event()
    def slow(url):
        entered.set()
        assert release.wait(5)
        return extraction.parse_posting(structured(), url)
    monkeypatch.setattr(extraction_tasks, 'extract_posting', slow)
    headers = login(client)
    try:
        started = time.perf_counter()
        response = client.post('/api/v1/jobs/extract', json={'url': 'https://example.com/job'}, headers=headers)
        elapsed = time.perf_counter() - started
        print(f'Async acknowledgement with blocked extractor: {elapsed * 1000:.1f} ms')
        assert response.status_code == 202
        assert elapsed < 1
        assert entered.wait(1)
        assert client.get(response.headers['Location']).json['status'] == 'running'
        assert client.get('/api/v1/jobs').json['jobs'] == []
        second = client.post('/api/v1/jobs/extract', json={'url': 'https://example.com/second'}, headers=headers)
        assert second.status_code == 202
        assert client.post('/api/v1/jobs/extract', json={'url': 'https://example.com/third'}, headers=headers).status_code == 429
    finally:
        release.set()
    assert wait_for_preview(client, response.json['id']).json['status'] == 'succeeded'
    assert wait_for_preview(client, second.json['id']).json['status'] == 'succeeded'


def test_async_failures_expiry_and_authorized_polling(client, app, monkeypatch):
    from datetime import timedelta
    from jobtracker import db, extraction_tasks
    from jobtracker.models import ExtractionTask, now
    monkeypatch.setattr(extraction_tasks, 'extract_posting', lambda url: (_ for _ in ()).throw(ServiceError('Site blocked')))
    headers = login(client)
    response = client.post('/api/v1/jobs/extract', json={'url': 'https://example.com'}, headers=headers)
    result = wait_for_preview(client, response.json['id'])
    assert result.json['status'] == 'failed'
    assert result.json['error'] == 'Site blocked'
    assert client.get('/api/v1/jobs').json['jobs'] == []
    with app.app_context():
        session = db.get_session()
        session.add_all([
            ExtractionTask(id='interrupted', status='running', result={}, created_at=now() - timedelta(seconds=61)),
            ExtractionTask(id='old', status='succeeded', result={}, created_at=now() - timedelta(days=2)),
        ])
        session.commit()
    assert client.get('/api/v1/extractions/interrupted').json['status'] == 'failed'
    extraction_tasks.finish(app, 'interrupted', 'succeeded', {'fields': {}})
    assert client.get('/api/v1/extractions/interrupted').json['status'] == 'failed'
    assert client.get('/api/v1/extractions/old').status_code == 404
    assert client.get('/api/v1/extractions/missing').status_code == 404
    client.post('/api/v1/auth/logout', headers=headers)
    assert client.get('/api/v1/extractions/interrupted').status_code == 401
    assert client.get('/api/v1/extractions/interrupted', headers={'Authorization': 'Bearer test-agent-token'}).status_code == 200


def test_v3_migration_preserves_jobs_and_adds_preview_storage(app, client):
    from sqlalchemy import inspect, text
    from jobtracker import db
    headers = login(client)
    job = client.post('/api/v1/jobs', json={'title': 'Existing role', 'notes': 'Keep me'}, headers=headers).json
    engine = app.extensions['jobtracker_db']['engine']
    with engine.begin() as connection:
        connection.execute(text('DROP TABLE extraction_tasks'))
        connection.execute(text('UPDATE schema_version SET version=3'))
    from jobtracker import create_app
    upgraded = create_app(dict(app.config))
    with upgraded.app_context():
        assert 'extraction_tasks' in inspect(upgraded.extensions['jobtracker_db']['engine']).get_table_names()
        assert db.get_session().scalar(text('SELECT version FROM schema_version')) == db.SCHEMA_VERSION
    fresh = upgraded.test_client()
    login(fresh)
    assert fresh.get(f'/api/v1/jobs/{job["id"]}').json['notes'] == 'Keep me'


def test_https_pins_ip_but_verifies_original_hostname(monkeypatch):
    from urllib.parse import urlsplit
    connected, verified = [], []
    class Sock:
        def __init__(self, *args):
            pass
        def settimeout(self, value):
            pass
        def connect(self, address):
            connected.append(address)
        def close(self):
            pass
    class Context:
        def wrap_socket(self, sock, server_hostname):
            verified.append(server_hostname)
            return sock
    class Connection:
        def __init__(self, hostname, port):
            assert (hostname, port) == ('careers.example', 443)
        def request(self, method, path, headers):
            assert path == '/role?q=1'
            assert 'Authorization' not in headers and 'Cookie' not in headers
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(extraction, 'public_target', lambda url: (
        urlsplit(url), 'careers.example', (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))))
    monkeypatch.setattr(socket, 'socket', Sock)
    monkeypatch.setattr(extraction.ssl, 'create_default_context', Context)
    monkeypatch.setattr(extraction.http.client, 'HTTPConnection', Connection)
    assert 'Engineer' in extraction.fetch_page('https://careers.example/role?q=1')
    assert connected == [('93.184.216.34', 443)]
    assert verified == ['careers.example']
