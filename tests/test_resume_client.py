import io
import json
from urllib.error import HTTPError, URLError

import pytest

from scripts import agent_example

TOKEN = 'nm_ingest_disposable-' + 'x' * 40
ROLE = '00000000-0000-4000-8000-000000000001'


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    resume = tmp_path / 'resume.txt'
    resume.write_bytes(b'resume content')
    monkeypatch.setenv('NEXTMOVE_INGEST_TOKEN', TOKEN)
    argv = ['agent_example.py', ROLE, str(resume), '--base-url', 'https://example.com/nextmove']
    monkeypatch.setattr('sys.argv', argv)
    return resume, argv


def test_private_file_multipart_upload_uses_existing_token(setup, tmp_path, monkeypatch, capsys):
    _, argv = setup
    token_file = tmp_path / 'token'
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    argv.extend(['--token-file', str(token_file)])
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return io.StringIO(json.dumps({'id': 'new-attachment', 'job_id': ROLE}))
    monkeypatch.setattr(agent_example, 'build_opener', lambda *args: Opener())
    assert agent_example.main() == 0
    request, = calls
    assert request.full_url == f'https://example.com/nextmove/api/v1/jobs/{ROLE}/resumes'
    assert request.get_header('Authorization') == 'Bearer ' + TOKEN
    assert request.get_method() == 'POST'
    assert request.get_header('Content-type').startswith('multipart/form-data; boundary=')
    assert b'name="file"' in request.data and b'resume content' in request.data
    output = capsys.readouterr()
    assert 'new-attachment' in output.out and TOKEN not in output.out + output.err


@pytest.mark.parametrize('failure', ['http', 'network', 'redirect'])
def test_upload_failures_never_print_error_body_or_retry(setup, monkeypatch, capsys, failure):
    calls = []
    class Opener:
        def open(self, *args, **kwargs):
            calls.append(1)
            if failure == 'http':
                raise HTTPError('https://example.com', 401, 'Unauthorized', {}, io.BytesIO(TOKEN.encode()))
            if failure == 'redirect':
                raise ValueError('Redirect refused')
            raise URLError(TOKEN)
    monkeypatch.setattr(agent_example, 'build_opener', lambda *args: Opener())
    assert agent_example.main() == 1
    assert len(calls) == 1
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert ('401' if failure == 'http' else 'another version') in output.err


@pytest.mark.parametrize('base', ['http://remote.example', 'https://user:pass@example.com',
                                  'https://example.com?token=secret', 'file:///tmp/api'])
def test_rejects_unsafe_upload_base(setup, base):
    _, argv = setup
    argv[-1] = base
    with pytest.raises(SystemExit) as error:
        agent_example.main()
    assert error.value.code == 2


def test_rejects_public_token_file_and_redirect_forwarding(setup, tmp_path):
    _, argv = setup
    token_file = tmp_path / 'token'
    token_file.write_text(TOKEN)
    token_file.chmod(0o644)
    argv.extend(['--token-file', str(token_file)])
    with pytest.raises(SystemExit) as error:
        agent_example.main()
    assert error.value.code == 2
    with pytest.raises(ValueError, match='Redirect refused'):
        agent_example.NoRedirect().redirect_request(None, None, 307, '', {}, 'https://elsewhere.example')
