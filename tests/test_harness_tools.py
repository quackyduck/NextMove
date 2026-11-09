"""The connector's tools, run under a harness grant: what the chat agent uses to work a user's tracker."""
import io

from jobtracker import harness, mcp

from test_documents import docx
from test_harness_api import FIELDS, account, add_job, app, client, upload_resume  # noqa: F401 (fixtures)


def call(client, grant, name, **arguments):
    return client.post(f'/api/v1/me/tools/{name}', headers=grant, json={'arguments': arguments})


def test_the_grant_lists_every_connector_tool_with_the_instructions(client):
    _, grant = account(client)
    listed = client.get('/api/v1/me/tools', headers=grant)
    assert listed.status_code == 200
    assert {tool['name'] for tool in listed.json['tools']} == set(mcp.TOOLS)
    assert 'Only move a role to a stage that reflects work the user actually did' in listed.json['instructions']


def test_tools_list_edit_move_and_annotate_the_grant_owners_roles(client, app):
    _, grant = account(client)
    job = add_job(client, app, grant)
    listed = call(client, grant, 'list_jobs')
    assert listed.status_code == 200
    assert [row['id'] for row in listed.json['result']['jobs']] == [job['id']]
    assert listed.json['result']['jobs'][0]['stage'] == 'needs_review'

    moved = call(client, grant, 'move_job', job_id=job['id'], stage='interesting', note='Strong platform fit')
    assert moved.status_code == 200, moved.json
    assert moved.json['result']['stage_label'] == 'Interesting'
    noted = call(client, grant, 'add_note', job_id=job['id'], text='Recruiter is Dana')
    assert f'· {harness.CLIENT_NAME}] Strong platform fit' in noted.json['result']['notes']
    assert noted.json['result']['notes'].endswith(f'· {harness.CLIENT_NAME}] Recruiter is Dana')

    current = call(client, grant, 'get_job', job_id=job['id']).json['result']
    assert current['resumes'] == []  # harness grants carry resumes:read
    updated = call(client, grant, 'update_job', job_id=job['id'], version=current['job']['version'],
                   fields={'priority': 'high'})
    assert updated.json['result']['job']['priority'] == 'high'
    history = call(client, grant, 'get_job_history', job_id=job['id']).json['result']['activities']
    assert history


def test_tool_failures_are_422_with_a_message_for_the_model(client, app):
    _, grant = account(client)
    job = add_job(client, app, grant)
    bad_stage = call(client, grant, 'move_job', job_id=job['id'], stage='hired')
    assert bad_stage.status_code == 422
    assert 'Unknown stage' in bad_stage.json['error']
    stale = call(client, grant, 'update_job', job_id=job['id'], version=99, fields={'priority': 'low'})
    assert stale.status_code == 422 and 'changed since you read it' in stale.json['error']
    assert call(client, grant, 'list_jobs', colour='red').status_code == 422
    assert call(client, grant, 'drop_tables').status_code == 404
    body = client.post('/api/v1/me/tools/list_jobs', headers=grant, json={'arguments': {}, 'tenant': 'other'})
    assert body.status_code == 400


def test_tools_never_reach_another_accounts_roles(client, app):
    _, owner = account(client)
    _, other = account(client)
    job = add_job(client, app, owner)
    assert call(client, other, 'list_jobs').json['result']['jobs'] == []
    for name, arguments in (('get_job', {}), ('move_job', {'stage': 'applied'}), ('add_note', {'text': 'x'}),
                            ('set_outcome', {'outcome': 'rejected'})):
        response = call(client, other, name, job_id=job['id'], **arguments)
        assert response.status_code == 422, name
    assert call(client, owner, 'get_job', job_id=job['id']).json['result']['job']['stage'] == 'needs_review'


def test_tools_need_a_live_grant(client, app):
    user_id, grant = account(client)
    assert client.get('/api/v1/me/tools').status_code == 401
    with app.app_context():
        harness.revoke_grants(user_id)
    assert call(client, grant, 'list_jobs').status_code == 401


def test_upload_and_download_links_work_for_the_grant_and_die_with_it(client, app):
    user_id, grant = account(client)
    assert upload_resume(client, grant).status_code == 201
    link = call(client, grant, 'create_resume_upload_link', target='master')
    assert link.status_code == 200, link.json
    path = link.json['result']['url'].removeprefix('https://nextmove.example.com')
    uploaded = client.post(path, data={'file': (io.BytesIO(docx(document=b'<w:document>v2</w:document>')), 'resume.docx')})
    assert uploaded.status_code == 201, uploaded.json

    versions = call(client, grant, 'list_resumes').json['result']['master']
    download = call(client, grant, 'get_resume_download_link', resume_id=versions[0]['id'])
    file_path = download.json['result']['url'].removeprefix('https://nextmove.example.com')
    assert client.get(file_path).status_code == 200

    with app.app_context():
        harness.revoke_grants(user_id)
    assert client.get(file_path).status_code == 404
    again = client.post(path, data={'file': (io.BytesIO(docx(document=b'<w:document>v3</w:document>')), 'resume.docx')})
    assert again.status_code == 404


def test_a_harness_link_never_resolves_as_an_oauth_grant(client, app):
    with app.test_request_context():
        assert harness.grant_scopes('not-prefixed') is None
        assert harness.grant_scopes(harness.LINK_GRANT_PREFIX + 'missing') is None
