import hashlib

import pytest
from sqlalchemy import select, func
from werkzeug.security import generate_password_hash

from jobtracker import create_app, services
from jobtracker.db import get_session
from jobtracker.models import Activity, Contact, Job, Resume

TOKEN = 'disposable-search-token-for-tests-only'
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
ENDPOINT = '/api/v1/jobs/search'


@pytest.fixture()
def app(tmp_path):
    return create_app({
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'test',
        'APP_PASSWORD_HASH': generate_password_hash('test-password'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "test.sqlite3"}',
        'UPLOAD_FOLDER': str(tmp_path / 'resumes'), 'SESSION_COOKIE_SECURE': False,
        'AGENT_API_TOKEN': 'existing-broad-test-token',
        'ROLE_SEARCH_TOKEN_SHA256': hashlib.sha256(TOKEN.encode()).hexdigest(),
    })


def save(app, **values):
    defaults = dict(title='Backend Engineer', company='Acme', url='https://example.com/jobs/123')
    defaults.update(values)
    with app.app_context():
        return services.create_job(defaults)


def find(app, **params):
    return app.test_client().get(ENDPOINT, query_string=params, headers=HEADERS)


@pytest.mark.parametrize('url', [
    'http://WWW.EXAMPLE.COM/jobs/123/?utm_source=mail#application',
    'https://example.com:443/jobs/123?fbclid=track',
    'http://example.com:80/jobs/123/',
])
def test_base_url_wins_over_different_metadata(app, url):
    saved = save(app)
    result = find(app, url=url, company='Other', title='Designer')
    match = result.json['matches'][0]
    assert match['id'] == saved['id']
    assert match['classification'] == 'duplicate'
    assert match['score'] == 100
    assert 'same_normalized_posting_url' in match['reasons']


@pytest.mark.parametrize('saved_url,query_url,classification', [
    ('https://example.com/jobs/123', 'https://example.com/jobs/456', None),
    ('https://example.com/jobs/ABC', 'https://example.com/jobs/abc', None),
    ('https://example.com/jobs?id=1', 'https://example.com/jobs?id=2', 'possible_match'),
    ('https://example.com/jobs?id=1', 'https://example.com/jobs', 'possible_match'),
    ('https://example.com/jobs?id=1&lang=en', 'http://www.example.com/jobs?lang=en&id=1&utm_campaign=x', 'duplicate'),
    ('https://example.com/jobs?ref=1', 'https://example.com/jobs?ref=2', 'possible_match'),
    ('https://example.com/jobs?source=1', 'https://example.com/jobs?source=2', 'possible_match'),
    ('https://example.com/jobs?gh_jid=AbC', 'https://example.com/jobs?gh_jid=abc', 'possible_match'),
    ('https://example.com/jobs?jid=1&jid=2', 'https://example.com/jobs?jid=2&jid=1', 'duplicate'),
    ('https://example.com/jobs?requisitionId=1&jobId=2', 'https://example.com/jobs?jobId=2&requisitionId=1', 'duplicate'),
    ('https://example.com:0/jobs/123', 'https://example.com/jobs/123', None),
    ('https://[::1]:8080/jobs', 'https://[::1:8080]/jobs', None),
    ('https://example.com:8443/jobs/123', 'https://example.com/jobs/123', None),
])
def test_url_identity_and_ambiguity(app, saved_url, query_url, classification):
    save(app, url=saved_url)
    result = find(app, url=query_url).json['matches']
    if classification is None:
        assert result == []
    else:
        assert result[0]['classification'] == classification
        if classification == 'possible_match':
            assert 'same_base_url_query_ambiguous' in result[0]['reasons']


@pytest.mark.parametrize('company,title,classification,reason', [
    ('  ACME! ', ' backend   ENGINEER ', 'duplicate', 'company_and_specific_title_exact'),
    ('Acme Corporation', 'Backend Engineer', 'possible_match', 'company_overlap'),
    ('Acme', 'Backend Software Engineer', 'possible_match', 'title_overlap'),
    ('Acme', None, 'possible_match', 'company_exact'),
    (None, 'Backend Engineer', 'possible_match', 'title_exact'),
])
def test_partial_company_title_search(app, company, title, classification, reason):
    save(app)
    params = {key: val for key, val in dict(company=company, title=title).items() if val}
    match = find(app, **params).json['matches'][0]
    assert match['classification'] == classification
    assert reason in match['reasons']


@pytest.mark.parametrize('saved_title,query_title,reason', [
    ('Software Engineer', 'Software Engineer', 'generic_title_requires_review'),
    ('Senior Backend Platform Engineer', 'Backend Platform Engineer', 'seniority_differs'),
    ('Backend Platform Engineer II', 'Backend Platform Engineer III', 'seniority_differs'),
])
def test_conservative_titles(app, saved_title, query_title, reason):
    save(app, title=saved_title)
    match = find(app, company='Acme', title=query_title).json['matches'][0]
    assert match['classification'] == 'possible_match'
    # Seniority is also explicit when similarity falls below duplicate threshold.
    assert reason in match['reasons']


@pytest.mark.parametrize('params', [
    {}, {'title': ''}, {'company': '   '}, {'title': '!!!'}, {'title': 'x' * 501},
    {'url': 'example.com/job'}, {'url': 'ftp://example.com/job'}, {'url': 'https://u:p@example.com'},
    {'url': 'https://example.com:bad/job'}, {'url': 'https://exa mple.com'},
    {'url': 'https://example.com/?' + '&'.join('a=1' for _ in range(101))},
    {'title': 'Engineer', 'limit': '0'}, {'title': 'Engineer', 'limit': '101'},
    {'title': 'Engineer', 'offset': '-1'}, {'title': 'Engineer', 'offset': '1001'},
    {'title': 'Engineer', 'limit': '1.5'}, {'title': 'Engineer', 'q': 'x'},
    [('title', 'Engineer'), ('title', 'Developer')],
])
def test_validation(app, params):
    assert app.test_client().get(ENDPOINT, query_string=params, headers=HEADERS).status_code == 400


def test_auth_scope_and_browser(app):
    client = app.test_client()
    assert client.get(ENDPOINT + '?company=Acme').status_code == 401
    for token in ('bad', 'ingestion-only-token', '🔒'):
        assert client.get(ENDPOINT + '?company=Acme', headers={'Authorization': f'Bearer {token}'}).status_code == 401
    assert client.get(ENDPOINT + '?company=Acme', headers=HEADERS).status_code == 200
    for path in ('/api/v1/jobs', '/api/v1/jobs/missing', '/api/v1/resumes/missing/download'):
        assert client.get(path, headers=HEADERS).status_code == 401
    for method, path in [('post', '/api/v1/jobs'), ('patch', '/api/v1/jobs/missing'),
                         ('post', '/api/v1/auth/password'), ('post', ENDPOINT)]:
        assert getattr(client, method)(path, json={}, headers=HEADERS).status_code in (401, 405)
    assert client.get(ENDPOINT + '?company=Acme', headers={'Authorization': 'Bearer existing-broad-test-token'}).status_code == 200
    app.config['ROLE_SEARCH_TOKEN_SHA256'] = None
    assert client.get(ENDPOINT + '?company=Acme', headers=HEADERS).status_code == 401
    assert client.post('/api/v1/auth/login', json={'password': 'test-password'}).status_code == 200
    assert client.get(ENDPOINT + '?company=Acme').status_code == 200
    assert client.post('/api/v1/jobs', json={'title': 'x'}).status_code == 403


def test_privacy_ranking_pagination_and_read_only(app):
    first = save(app, notes='private-note', description='private-description')
    save(app, title='Other', url='https://different.example/job')
    with app.app_context():
        services.create_contact(first['id'], {'name': 'private-contact'})
        before = {model: get_session().scalar(select(func.count()).select_from(model))
                  for model in (Job, Activity, Contact, Resume)}
    response = find(app, company='Acme', title='Backend Engineer', limit='1')
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.json['matches'][0]['id'] == first['id']
    assert response.json['total'] == 2
    assert response.json['next_offset'] == 1
    match = response.json['matches'][0]
    assert set(match) == {'id', 'title', 'company', 'url', 'stage', 'outcome', 'score', 'classification', 'reasons', 'evidence'}
    assert 'private-' not in response.get_data(as_text=True)
    page = find(app, company='Acme', title='Backend Engineer', limit='1', offset='1')
    assert page.json['matches'][0]['id'] != first['id']
    assert page.json['next_offset'] is None
    assert find(app, company='Acme', offset='1000').json['matches'] == []
    with app.app_context():
        assert before == {model: get_session().scalar(select(func.count()).select_from(model)) for model in before}
        assert services.get_job(first['id']) == first


def test_rate_limit_fail_closed_and_capacity(app, monkeypatch):
    from jobtracker import search
    save(app)
    monkeypatch.setattr(search, 'MAX_ROLES', 0)
    assert find(app, company='Acme').status_code == 503
    monkeypatch.setattr(search, 'MAX_ROLES', 5000)
    for _ in range(119):
        assert find(app, company='Acme').status_code == 200
    response = find(app, company='Acme')
    assert response.status_code == 429
    assert response.headers['Retry-After'] == '60'
    # Search traffic never consumes login attempts.
    assert app.test_client().post('/api/v1/auth/login', json={'password': 'test-password'}).status_code == 200
    app.extensions['login_throttle_path'] = '/nonexistent-directory/search.sqlite3'
    assert find(app, company='Acme').status_code == 503


def test_prefix(tmp_path):
    app = create_app({'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'test',
                      'APP_PASSWORD_HASH': generate_password_hash('test-password'),
                      'DATABASE_URL': f'sqlite:///{tmp_path / "prefix.sqlite3"}',
                      'UPLOAD_FOLDER': str(tmp_path / 'resumes'), 'APP_ROOT_PATH': '/nextmove',
                      'ROLE_SEARCH_TOKEN_SHA256': hashlib.sha256(TOKEN.encode()).hexdigest()})
    result = app.test_client().get('/nextmove' + ENDPOINT + '?company=Acme', headers=HEADERS)
    assert result.status_code == 200
    assert result.json['matches'] == []


def test_text_evidence_can_independently_match_conflicting_query_ids(app):
    save(app, url='https://example.com/jobs?id=1')
    match = find(app, url='https://example.com/jobs?id=2', company='Acme', title='Backend Engineer').json['matches'][0]
    assert match['classification'] == 'duplicate'
    assert match['score'] == 90
    assert 'same_base_url_query_ambiguous' in match['reasons']
    assert 'company_and_specific_title_exact' in match['reasons']


def test_language_punctuation_is_significant(app):
    save(app, title='C++ Engineer')
    match = find(app, company='Acme', title='C# Engineer').json['matches'][0]
    assert match['classification'] == 'possible_match'
    assert 'title_exact' not in match['reasons']


def test_long_stored_url_is_bounded_and_disclosed(app):
    save(app, url='https://example.com/' + 'x' * 5000)
    match = find(app, company='Acme').json['matches'][0]
    assert match['url'] == ''
    assert 'stored_url_exceeds_limit' in match['reasons']


def test_head_and_malformed_verifier(app):
    assert app.test_client().head(ENDPOINT + '?company=Acme', headers=HEADERS).status_code == 200
    app.config['ROLE_SEARCH_TOKEN_SHA256'] = 'invalid-verifier'
    assert find(app, company='Acme').status_code == 401
