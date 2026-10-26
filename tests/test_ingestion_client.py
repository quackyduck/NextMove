import io
import json
from urllib.error import HTTPError

import pytest

from scripts import ingest_url

TOKEN = 'disposable-client-token-' + 'x' * 32
IDENTIFIER = '00000000-0000-4000-8000-000000000001'


def test_private_file_post_and_poll_keep_token_out_of_output(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / 'token'
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    monkeypatch.setattr('sys.argv', ['ingest_url.py', 'https://example.com/job', '--token-file', str(token_file), '--idempotency-key', 'a' * 32, '--wait'])
    requests = []
    results = [{'id': IDENTIFIER, 'status': 'running'}, {'id': IDENTIFIER, 'status': 'succeeded', 'job_id': 'saved-job'}]
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return io.StringIO(json.dumps(results.pop(0)))
    monkeypatch.setattr(ingest_url, 'build_opener', lambda *args: Opener())
    monkeypatch.setattr(ingest_url.time, 'sleep', lambda value: None)
    assert ingest_url.main() == 0
    assert requests[0].get_header('Authorization') == 'Bearer ' + TOKEN
    assert json.loads(requests[0].data) == {'url': 'https://example.com/job'}
    assert requests[0].get_header('Idempotency-key') == 'a' * 32
    assert requests[1].full_url == 'https://nicolasmelo.com/nextmove/api/v1/ingestions/' + IDENTIFIER
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert 'saved-job' in output.out and 'Idempotency-Key' in output.err


def test_redirects_are_rejected_before_credentials_follow():
    with pytest.raises(ValueError, match='Redirect refused'):
        ingest_url.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.example')


def test_http_error_body_and_token_are_not_logged(monkeypatch, capsys):
    monkeypatch.setenv('NEXTMOVE_INGEST_TOKEN', TOKEN)
    monkeypatch.setattr('sys.argv', ['ingest_url.py', 'https://example.com/job'])
    class Opener:
        def open(self, *args, **kwargs):
            raise HTTPError('https://example.com', 401, 'Unauthorized', {}, io.BytesIO(TOKEN.encode()))
    monkeypatch.setattr(ingest_url, 'build_opener', lambda *args: Opener())
    assert ingest_url.main() == 1
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert '401' in output.err and 'Idempotency-Key' in output.err


@pytest.mark.parametrize('base', ['http://remote.example', 'https://user:password@example.com', 'https://example.com?token=secret', 'file:///tmp/api'])
def test_rejects_unsafe_base_urls(monkeypatch, base):
    monkeypatch.setattr('sys.argv', ['ingest_url.py', 'https://example.com/job', '--base-url', base])
    with pytest.raises(SystemExit) as error:
        ingest_url.main()
    assert error.value.code == 2


def test_world_readable_token_file_is_refused(tmp_path, monkeypatch):
    token_file = tmp_path / 'token'
    token_file.write_text(TOKEN)
    token_file.chmod(0o644)
    monkeypatch.setattr('sys.argv', ['ingest_url.py', 'https://example.com/job', '--token-file', str(token_file)])
    with pytest.raises(SystemExit) as error:
        ingest_url.main()
    assert error.value.code == 2


def test_connection_mode_checks_token_without_submitting_or_printing_secret(monkeypatch, capsys):
    monkeypatch.setenv('NEXTMOVE_INGEST_TOKEN', TOKEN)
    monkeypatch.setattr('sys.argv', ['ingest_url.py', '--test-connection'])
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return io.StringIO(json.dumps({'connected': True, 'agent_token': {'name': 'Local agent'}}))
    monkeypatch.setattr(ingest_url, 'build_opener', lambda *args: Opener())
    assert ingest_url.main() == 0
    assert len(requests) == 1 and requests[0].data is None
    assert requests[0].full_url == 'https://nicolasmelo.com/nextmove/api/v1/agent-connection'
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert 'connected' in output.out and 'Idempotency-Key' not in output.err
