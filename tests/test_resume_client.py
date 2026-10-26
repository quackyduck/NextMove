import io
import json
from urllib.error import HTTPError, URLError

import pytest
from werkzeug.http import parse_options_header

from scripts import agent_example
from test_api import app, client, login  # noqa: F401
from test_agent_tokens import create

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


@pytest.mark.parametrize('filename', ['Nicolas_Melo_Acme_Backend.txt', 'Résumé – Acme.txt', 'Resume "Acme".txt'])
def test_specific_filename_survives_client_upload_listing_and_download(setup, client, monkeypatch, capsys, filename):
    resume, argv = setup
    specific = resume.with_name(filename)
    resume.rename(specific)
    argv[2] = str(specific)
    browser = login(client)
    token = create(client, browser).json['token']
    monkeypatch.setenv('NEXTMOVE_INGEST_TOKEN', token)
    job = client.post('/api/v1/jobs', headers=browser, json={'title': 'New role'}).json
    argv[1] = job['id']
    class Opener:
        def open(self, request, timeout):
            assert str(specific.parent).encode() not in request.data
            response = client.post(f"/api/v1/jobs/{job['id']}/resumes", data=request.data,
                                   headers=dict(request.header_items()))
            assert response.status_code == 201
            return io.StringIO(response.get_data(as_text=True))
    monkeypatch.setattr(agent_example, 'build_opener', lambda *args: Opener())
    assert agent_example.main() == 0
    attachment = json.loads(capsys.readouterr().out)
    assert attachment['original_filename'] == filename
    listed = client.get(f"/api/v1/jobs/{job['id']}/resumes").json['resumes']
    assert listed[0]['original_filename'] == filename
    downloaded = client.get('/api/v1/resumes/' + attachment['id'] + '/download')
    assert downloaded.data == b'resume content'
    assert parse_options_header(downloaded.headers['Content-Disposition'])[1]['filename'] == filename


@pytest.mark.parametrize('control', ['\r', '\n', '\x01', '\x7f'])
def test_rejects_filename_header_injection_before_upload(setup, monkeypatch, control):
    resume, argv = setup
    unsafe = resume.with_name('resume' + control + 'Injected.txt')
    resume.rename(unsafe)
    argv[2] = str(unsafe)
    def no_network(*args):
        pytest.fail('Unsafe filename reached network')
    monkeypatch.setattr(agent_example, 'build_opener', no_network)
    with pytest.raises(SystemExit) as error:
        agent_example.main()
    assert error.value.code == 2
