"""X-Request-ID: validated (re.fullmatch) or generated, echoed on every response, named in 5xx bodies."""
import pytest

from jobtracker import create_app, telemetry

from telemetry_helpers import base_config


@pytest.fixture()
def client(tmp_path):
    app = create_app(base_config(tmp_path))

    @app.get('/api/v1/test-crash')
    def crash():
        raise RuntimeError('boom')
    return app.test_client()


def test_missing_header_gets_a_generated_id(client):
    response = client.get('/health')
    assert telemetry.valid_request_id(response.headers['X-Request-ID'])
    assert len(response.headers['X-Request-ID']) == 32


def test_valid_incoming_id_is_kept(client):
    assert client.get('/health', headers={'X-Request-ID': 'abc-123-DEF-456'}).headers['X-Request-ID'] == \
        'abc-123-DEF-456'
    caddy_uuid = '3f2504e0-4f89-41d3-9a0c-0305e82c3301'
    assert client.get('/health', headers={'X-Request-ID': caddy_uuid}).headers['X-Request-ID'] == caddy_uuid


@pytest.mark.parametrize('supplied', [
    'short', 'a' * 65, 'has_underscore_123', 'has.dot.12345', '../../etc/passwd',
    'abcdefgh ijk', 'ünïcödé-12345', '<script>alert(1)</script>', '',
])
def test_invalid_incoming_ids_are_replaced(client, supplied):
    response = client.get('/health', headers={'X-Request-ID': supplied})
    assert response.headers['X-Request-ID'] != supplied
    assert telemetry.valid_request_id(response.headers['X-Request-ID'])


def test_fullmatch_rejects_a_trailing_newline():
    assert telemetry.valid_request_id('abcdefgh12')
    assert not telemetry.valid_request_id('abcdefgh12\n')
    assert not telemetry.valid_request_id(None)


def test_error_responses_carry_the_header_and_5xx_bodies_name_it(client):
    not_found = client.get('/api/v1/jobs/nope')
    assert telemetry.valid_request_id(not_found.headers['X-Request-ID'])
    assert 'request_id' not in (not_found.json or {})  # 4xx bodies are unchanged; the header carries it
    crash = client.get('/api/v1/test-crash', headers={'X-Request-ID': 'support-ref-0001'})
    assert crash.status_code == 500
    assert crash.json == {'error': 'Something went wrong on our side.', 'request_id': 'support-ref-0001'}
    assert crash.headers['X-Request-ID'] == 'support-ref-0001'


def test_bad_utf8_query_still_gets_an_id(client):
    response = client.get('/api/v1/jobs', environ_overrides={'QUERY_STRING': b'q=\xff'.decode('latin-1')})
    assert response.status_code == 400
    assert telemetry.valid_request_id(response.headers['X-Request-ID'])


def test_jsonrpc_bodies_are_left_alone(client):
    response = client.post('/mcp', data=b'{', headers={'Content-Type': 'application/json'})
    if response.is_json and isinstance(response.json, dict):
        assert 'request_id' not in response.json
    assert telemetry.valid_request_id(response.headers['X-Request-ID'])


def test_streamed_files_are_untouched(client):
    response = client.get('/')
    assert telemetry.valid_request_id(response.headers['X-Request-ID'])
