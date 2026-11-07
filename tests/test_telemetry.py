"""Error telemetry: no-op without a DSN, allowlist scrubbing, the 500 handler, CSP, worker and mail alerts.

Every scrub test puts canary strings into every channel an event could read (body, query, path, headers,
cookie, exception message, local variables, argv, tags, extras, contexts) and asserts that none of them is
anywhere in the serialized envelopes or in the JSON log output.
"""
import logging
import sys
import threading

import pytest
import sentry_sdk
from flask import g
from sqlalchemy import text

from jobtracker import create_app, taskqueue, telemetry, worker
from jobtracker.logs import REQUEST_ID
from jobtracker.services import ServiceError

from telemetry_helpers import (CANARY_BODY, CANARY_COOKIE, CANARY_EMAIL, CANARY_LOCAL, CANARY_PHONE, CANARY_QUERY,
                               CANARY_RESUME, CANARY_TOKEN, DSN, CaptureTransport, LogCapture, assert_no_canary,
                               base_config, sentry_app, shutdown_sentry)

BASE_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'")


@pytest.fixture(autouse=True)
def clean_sentry(monkeypatch):
    for name in ('SENTRY_DSN', 'SENTRY_DSN_FILE', 'SENTRY_FRONTEND_DSN', 'SENTRY_ENVIRONMENT', 'SENTRY_RELEASE'):
        monkeypatch.delenv(name, raising=False)
    shutdown_sentry()
    yield
    shutdown_sentry()


@pytest.fixture()
def transport():
    return CaptureTransport()


def _boom_routes(app):
    @app.post('/api/v1/test-boom/<item>')
    def boom(item):
        g.user_id = 42
        secret_local = CANARY_LOCAL  # noqa: F841 - must never be sent
        raise ValueError(f'{CANARY_RESUME} {CANARY_EMAIL} {CANARY_PHONE}')

    @app.get('/api/v1/test-sql')
    def sql():
        from jobtracker.db import get_session
        get_session().execute(text('SELECT * FROM no_such_table WHERE v = :v'), {'v': CANARY_RESUME})


def _hit_boom(client):
    return client.post(f'/api/v1/test-boom/x?token={CANARY_QUERY}', json={'posting': CANARY_BODY},
                       headers={'Authorization': f'Bearer {CANARY_TOKEN}', 'X-CSRF-Token': CANARY_QUERY,
                                'Cookie': f'next_move_session={CANARY_COOKIE}'})


# 1. No DSN, no telemetry --------------------------------------------------------
def test_no_dsn_is_a_complete_noop(tmp_path):
    app = create_app(base_config(tmp_path))
    client = app.test_client()
    assert not sentry_sdk.get_client().is_active()
    assert not any('sentry' in thread.name.lower() for thread in threading.enumerate())
    assert client.get('/api/v1/client-config').json == {'sentry': None}
    assert client.get('/').headers['Content-Security-Policy'] == BASE_CSP
    assert client.get('/health').headers['Content-Security-Policy'] == BASE_CSP
    assert app.extensions['jobtracker_telemetry']['backend'] is False


def test_empty_dsn_file_keeps_telemetry_off(tmp_path, monkeypatch):
    empty = tmp_path / 'dsn'
    empty.write_text('\n')
    monkeypatch.setenv('SENTRY_DSN_FILE', str(empty))
    create_app(base_config(tmp_path))
    assert not sentry_sdk.get_client().is_active()


def test_dsn_file_turns_telemetry_on(tmp_path, monkeypatch, transport):
    dsn = tmp_path / 'dsn'
    dsn.write_text(DSN + '\n')
    monkeypatch.setenv('SENTRY_DSN_FILE', str(dsn))
    app = create_app(base_config(tmp_path, SENTRY_TRANSPORT=transport))
    assert sentry_sdk.get_client().is_active() and app.extensions['jobtracker_telemetry']['backend']


# 2. Unhandled exceptions --------------------------------------------------------
def test_unhandled_exception_is_scrubbed(tmp_path, transport, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['flask', 'user', 'reset-password', CANARY_EMAIL])
    app = sentry_app(tmp_path, transport)
    _boom_routes(app)
    with LogCapture() as logs:
        response = _hit_boom(app.test_client())

    assert response.status_code == 500
    events = transport.events()
    assert len(events) == 1
    event = events[0]
    assert_no_canary(transport.raw())
    assert_no_canary(logs.text)
    [exception] = event['exception']['values']
    assert exception['type'] == 'ValueError' and exception['value'] == '[redacted]'
    assert all('vars' not in frame for frame in exception['stacktrace']['frames'])
    assert event['tags']['service'] == 'jobtracker-web'
    assert event['tags']['request_id'] == response.headers['X-Request-ID']
    assert event['user'] == {'id': '42'}
    assert event['transaction'] == '/api/v1/test-boom/<item>'
    assert event['request'] == {'method': 'POST'}
    assert set(event) <= {'event_id', 'timestamp', 'level', 'platform', 'release', 'environment', 'server_name',
                          'logger', 'fingerprint', 'exception', 'tags', 'user', 'transaction', 'request',
                          'contexts', 'sdk'}
    assert set(event.get('contexts', {})) <= {'runtime', 'os'}
    # No transactions, spans, sessions or attachments: only one error event.
    assert {item.headers.get('type') for item in transport.items()} == {'event'}
    # Exactly one log line, holding the route template and the exception type, not its message.
    errors = [line for line in logs.lines() if line['level'] == 'ERROR']
    assert len(errors) == 1
    assert errors[0]['msg'] == 'Unhandled exception on POST /api/v1/test-boom/<item>'
    assert errors[0]['exc_type'] == 'ValueError'


def test_only_the_explicit_integrations_are_installed(tmp_path, transport):
    sentry_app(tmp_path, transport)
    installed = set(sentry_sdk.get_client().integrations)
    assert installed == {'dedupe', 'excepthook', 'atexit', 'logging', 'flask'}


def test_incoming_trace_headers_never_reach_the_envelope(tmp_path, transport):
    app = sentry_app(tmp_path, transport)
    _boom_routes(app)
    app.test_client().post('/api/v1/test-boom/x', json={}, headers={
        'sentry-trace': '771a43a4192642f0b136d5159a501700-1234567890abcdef-1',
        'baggage': 'sentry-release=CANARYBAGGAGE,sentry-environment=CANARYBAGGAGE,sentry-user_segment=CANARYBAGGAGE',
        'traceparent': '00-771a43a4192642f0b136d5159a501700-1234567890abcdef-01'})
    assert transport.events()
    assert b'CANARYBAGGAGE' not in transport.raw()
    assert b'771a43a4192642f0b136d5159a501700' not in transport.raw()


def test_sqlalchemy_parameters_never_leave(tmp_path, transport):
    app = sentry_app(tmp_path, transport)
    _boom_routes(app)
    with LogCapture() as logs:
        assert app.test_client().get('/api/v1/test-sql').status_code == 500
    assert transport.events()[0]['exception']['values'][-1]['value'] == '[redacted]'
    assert_no_canary(transport.raw())
    assert_no_canary(logs.text)


def test_token_paths_are_logged_as_templates(tmp_path, transport):
    app = sentry_app(tmp_path, transport)
    endpoint = next(rule.endpoint for rule in app.url_map.iter_rules() if rule.rule == '/api/v1/files/<token>')

    def explode(token):
        raise RuntimeError('boom')
    app.view_functions[endpoint] = explode
    with LogCapture() as logs:
        assert app.test_client().get(f'/api/v1/files/{CANARY_TOKEN}.{CANARY_QUERY}?token={CANARY_QUERY}').status_code == 500
    assert_no_canary(logs.text)
    assert_no_canary(transport.raw())
    assert 'Unhandled exception on GET /api/v1/files/<token>' in logs.text
    assert transport.events()[0]['transaction'] == '/api/v1/files/<token>'


def test_internal_server_error_handler_keeps_capture_and_logging(tmp_path, transport):
    app = sentry_app(tmp_path, transport)
    _boom_routes(app)
    # The JSON 500 handler sits on InternalServerError; a handler for Exception would swallow Sentry and logs.
    handlers = app.error_handler_spec[None][None]
    assert Exception not in handlers
    with LogCapture() as logs:
        response = _hit_boom(app.test_client())
    assert response.status_code == 500
    assert response.json == {'error': 'Something went wrong on our side.',
                             'request_id': response.headers['X-Request-ID']}
    assert len(transport.events()) == 1
    assert len([line for line in logs.lines() if line['level'] == 'ERROR']) == 1


def test_internal_server_error_without_telemetry_still_returns_json(tmp_path):
    app = create_app(base_config(tmp_path))
    _boom_routes(app)
    with LogCapture() as logs:
        response = _hit_boom(app.test_client())
    assert response.status_code == 500 and response.json['error'] == 'Something went wrong on our side.'
    assert telemetry.valid_request_id(response.json['request_id'])
    assert_no_canary(logs.text)


def test_scrubber_failure_drops_the_event(tmp_path, transport, monkeypatch):
    app = sentry_app(tmp_path, transport)
    _boom_routes(app)

    def broken(values):
        raise RuntimeError('scrubber bug')
    monkeypatch.setattr(telemetry, '_exception', broken)
    _hit_boom(app.test_client())
    assert transport.events() == []


def test_scrubber_drops_denylisted_and_unknown_fields():
    noisy = {
        'event_id': 'e' * 32, 'level': 'error',
        'tags': {name: CANARY_EMAIL for name in telemetry.EXTRA_DENYLIST} | {'service': 'jobtracker-web',
                                                                            'request_id': 'bad id\n'},
        'extra': {'resume': CANARY_RESUME, 'sys.argv': [CANARY_EMAIL]},
        'contexts': {'payload': {'text': CANARY_RESUME}, 'trace': {'dynamic_sampling_context': {'x': CANARY_EMAIL}},
                     'runtime': {'name': 'CPython', 'version': '3.12', 'raw_description': CANARY_RESUME}},
        'request': {'url': f'https://x/?token={CANARY_QUERY}', 'query_string': CANARY_QUERY, 'data': CANARY_BODY,
                    'cookies': {'s': CANARY_COOKIE}, 'headers': {'Authorization': CANARY_TOKEN}, 'method': 'GET'},
        'user': {'id': '7', 'email': CANARY_EMAIL, 'ip_address': '203.0.113.9'},
        'breadcrumbs': {'values': [{'message': CANARY_RESUME}]},
        'logentry': {'message': f'failed for {CANARY_EMAIL}', 'params': [CANARY_EMAIL]},
        'message': CANARY_RESUME, 'transaction': f'/api/v1/files/{CANARY_TOKEN}',
        'modules': {'x': '1'}, 'threads': {'values': [{'name': CANARY_RESUME}]},
    }
    scrubbed = telemetry.scrub_event(noisy)
    assert_no_canary(repr(scrubbed))
    assert '203.0.113.9' not in repr(scrubbed)
    assert scrubbed['user'] == {'id': '7'}
    # Only the valid service tag survives: denylisted keys and the malformed request_id are dropped.
    assert scrubbed['tags'] == {'service': 'jobtracker-web'}
    assert scrubbed['contexts'] == {'runtime': {'name': 'CPython', 'version': '3.12'}}
    assert 'transaction' not in scrubbed and 'message' not in scrubbed and 'logentry' not in scrubbed


def test_checkins_keep_only_their_own_fields():
    checkin = {'type': 'check_in', 'check_in_id': 'c' * 32, 'monitor_slug': 'jobtracker-worker', 'status': 'ok',
               'contexts': {'trace': {'trace_id': 'x'}}, 'extra': {'resume': CANARY_RESUME},
               'monitor_config': {'schedule': {'type': 'interval', 'value': 5, 'unit': 'minute'}}}
    scrubbed = telemetry.scrub_event(checkin)
    assert set(scrubbed) == {'type', 'check_in_id', 'monitor_slug', 'status', 'monitor_config'}


# 3. CSP and the browser config ----------------------------------------------------
@pytest.mark.parametrize('dsn,origin', [
    ('https://0123456789abcdef0123456789abcdef@o123.ingest.us.sentry.io/456', 'https://o123.ingest.us.sentry.io'),
    ('https://0123456789abcdef0123456789abcdef@o123.ingest.sentry.io/456', 'https://o123.ingest.sentry.io'),
])
def test_valid_frontend_dsn_adds_exactly_its_ingest_host(tmp_path, dsn, origin):
    client = create_app(base_config(tmp_path, SENTRY_FRONTEND_DSN=dsn)).test_client()
    assert client.get('/').headers['Content-Security-Policy'] == f"{BASE_CSP}; connect-src 'self' {origin}"
    config = client.get('/api/v1/client-config').json['sentry']
    assert config['dsn'] == dsn and config['environment'] == 'development'
    assert 'release' not in config  # private repositories: no git SHA in public output


@pytest.mark.parametrize('dsn', [
    'http://0123456789abcdef0123456789abcdef@o123.ingest.us.sentry.io/456',
    'https://0123456789abcdef0123456789abcdef@o123.ingest.de.sentry.io/456',
    'https://0123456789abcdef0123456789abcdef@o123.ingest.us.sentry.io.evil.com/456',
    'https://0123456789abcdef0123456789abcdef@*.ingest.us.sentry.io/456',
    'https://0123456789abcdef0123456789abcdef@sentry.example.com/456',
    'https://o123.ingest.us.sentry.io/456',
    'https://0123456789abcdef0123456789abcdef:secret@o123.ingest.us.sentry.io/456',
    'https://0123456789abcdef0123456789abcdef@o123.ingest.us.sentry.io:8443/456',
    'https://0123456789abcdef0123456789abcdef@o123.ingest.us.sentry.io\n/456',
    'https://0123456789abcdef0123456789abcdef@o123.ingest.us.sentry.io/456\nX',
    "https://0123456789abcdef0123456789abcdef@o123.ingest.us.sentry.io/456'; script-src *",
])
def test_invalid_frontend_dsn_keeps_csp_and_config_closed(tmp_path, dsn):
    client = create_app(base_config(tmp_path, SENTRY_FRONTEND_DSN=dsn)).test_client()
    assert client.get('/').headers['Content-Security-Policy'] == BASE_CSP
    assert client.get('/api/v1/client-config').json == {'sentry': None}


def test_client_config_is_not_cached(tmp_path):
    response = create_app(base_config(tmp_path)).test_client().get('/api/v1/client-config')
    assert response.headers['Cache-Control'] == 'no-store'


# 4. Worker and queue ---------------------------------------------------------------
@pytest.fixture()
def worker_app(tmp_path, transport):
    app = sentry_app(tmp_path, transport)
    yield app
    for kind in [kind for kind in taskqueue.HANDLERS if kind.startswith('test_')]:
        del taskqueue.HANDLERS[kind]


def _run_task(app, kind, exc, user_id=None, mail=False):
    @taskqueue.handler(kind, max_attempts=1, mail=mail)
    def run(app, task):
        raise exc
    engine = app.extensions['jobtracker_db']['engine']
    with engine.begin() as connection:
        taskqueue.enqueue(connection, user_id or 1, kind, {'url': f'https://jobs.example/?token={CANARY_QUERY}'})
    return taskqueue.run_once(app)


def test_unexpected_final_failure_is_captured_without_payload(worker_app, transport):
    with LogCapture('jobtracker-worker') as logs:
        assert _run_task(worker_app, 'test_unexpected', ValueError(CANARY_RESUME)) == 'failed'
    [event] = transport.events()
    assert event['tags']['task_kind'] == 'test_unexpected' and event['user'] == {'id': '1'}
    assert event['exception']['values'][0]['type'] == 'ValueError'
    assert_no_canary(transport.raw())
    assert_no_canary(logs.text)
    detail = [line for line in logs.lines() if line['msg'].endswith('failure detail')]
    assert detail and detail[0]['exc_type'] == 'ValueError' and 'stack' in detail[0]


@pytest.mark.parametrize('kind,exc', [
    ('test_expected_service', ServiceError('The posting could not be loaded', 422)),
    ('test_expected_timeout', TimeoutError()),
    ('test_expected_network', ConnectionResetError()),
    ('test_expected_retry', taskqueue.Retry('later')),
])
def test_expected_failures_are_not_captured(worker_app, transport, kind, exc):
    _run_task(worker_app, kind, exc)
    assert transport.events() == []


def test_mail_task_failures_alert_once_after_three(worker_app, transport, monkeypatch):
    monkeypatch.setattr(telemetry, 'mail_failures', telemetry.MailFailureWindow())
    for _ in range(2):
        _run_task(worker_app, 'test_mail', RuntimeError(CANARY_EMAIL), mail=True)
    assert transport.events() == []  # a single failure (a bounce) never pages anyone
    _run_task(worker_app, 'test_mail', RuntimeError(CANARY_EMAIL), mail=True)
    _run_task(worker_app, 'test_mail', RuntimeError(CANARY_EMAIL), mail=True)
    [event] = transport.events()
    assert event['message'] == telemetry.MAIL_TASK_FAILURES
    assert event['tags']['alert'] == 'mail_task_failures' and event['tags']['mail_reason'] == 'task_failures'
    assert_no_canary(transport.raw())


def test_worker_iteration_failures_are_throttled_and_checkins_sent(worker_app, transport, monkeypatch):
    stop = threading.Event()
    calls = []

    def failing(app):
        calls.append(1)
        if len(calls) >= 3:
            stop.set()
        raise RuntimeError(CANARY_RESUME)
    monkeypatch.setattr(taskqueue, 'run_once', failing)
    monkeypatch.setattr(stop, 'wait', lambda timeout=None: stop.is_set())
    worker.run(worker_app, stop, poll_seconds=0.01)
    assert len(calls) == 3
    assert len(transport.events()) == 1  # three failures, one event per 5 minutes
    [checkin] = transport.checkins()
    assert checkin['monitor_slug'] == 'jobtracker-worker' and checkin['status'] == 'ok'
    assert_no_canary(transport.raw())


# 5. Mail alerts (templates from the email-verification plan, logger jobtracker.mail) ------------------
@pytest.fixture()
def mail_log(tmp_path, transport):
    sentry_app(tmp_path, transport)
    return logging.getLogger(telemetry.MAIL_LOGGER)


@pytest.mark.parametrize('template,args,alert,level', [
    ('mail degraded: %s', ('quota',), 'mail_degraded', 'error'),
    ('signup paused: %s', ('daily_limit',), 'signup_paused', 'error'),
    ('mail daily usage at 80%% (%d of %d)', (72, 90), 'mail_daily_80', 'warning'),
    ('mail daily limit reached (%d of %d); sends deferred', (90, 90), 'mail_daily_limit', 'error'),
    ('mail monthly usage at 80%% (%d of %d)', (2400, 3000), 'mail_monthly_80', 'warning'),
    ('mail monthly limit reached (%d of %d); sends deferred', (3000, 3000), 'mail_monthly_limit', 'error'),
    ('mail %s share at 80%% (%d of %d)', ('signup', 36, 45), 'mail_share_80', 'warning'),
    ('mail provider rejected credentials (HTTP %d)', (401,), 'mail_credentials', 'error'),
])
def test_mail_templates_become_one_alert_event(mail_log, transport, template, args, alert, level):
    mail_log.log(logging.ERROR if level == 'error' else logging.WARNING, template, *args)
    mail_log.log(logging.ERROR, template, *args)  # throttled
    [event] = transport.events()
    assert event.get('logentry') == {'message': template} or event.get('message') == template
    assert event['tags']['alert'] == alert and event['level'] == level
    raw = transport.raw()
    for arg in args:
        if isinstance(arg, int):
            assert f'"{arg}"'.encode() not in raw.replace(b' ', b'')  # arguments are never sent


def test_mail_reason_is_tagged_only_when_allowlisted(mail_log, transport):
    mail_log.error('mail degraded: %s', CANARY_EMAIL)
    mail_log.error('mail degraded: %s', 'dns')
    events = transport.events()
    assert [event['tags'].get('mail_reason') for event in events] == [None, 'dns']
    assert_no_canary(transport.raw())


def test_other_mail_lines_are_not_alerts(mail_log, transport):
    mail_log.error('mail send failed for %s', CANARY_EMAIL)
    mail_log.warning('mail recipient skipped: %s', 'dns')
    mail_log.info('mail recovered')
    assert transport.events() == []


def test_mail_alert_templates_match_the_verification_plan():
    # Pinned against plan-email-verification.md section 6.5. If the mail code renames a line, change both.
    assert set(telemetry.MAIL_ALERTS) == {
        'mail daily usage at 80%% (%d of %d)', 'mail daily limit reached (%d of %d); sends deferred',
        'mail monthly usage at 80%% (%d of %d)', 'mail monthly limit reached (%d of %d); sends deferred',
        'mail %s share at 80%% (%d of %d)', 'signup paused: %s', 'mail degraded: %s',
        'mail provider rejected credentials (HTTP %d)'}


def test_mail_alerts_are_inert_without_telemetry(tmp_path, transport):
    create_app(base_config(tmp_path))
    handler = telemetry.MailAlertHandler()
    handler.emit(logging.LogRecord(telemetry.MAIL_LOGGER, logging.ERROR, __file__, 1, 'mail degraded: %s',
                                   ('quota',), None))
    assert transport.events() == []


# 6. The check command ---------------------------------------------------------------
def test_telemetry_check_command_sends_a_clean_event(tmp_path, transport):
    app = sentry_app(tmp_path, transport)
    result = app.test_cli_runner().invoke(args=['telemetry-check'])
    assert 'Sent check event' in result.output
    [event] = transport.events()
    assert event['tags'] == {'service': 'jobtracker-web', 'check': 'canary'}
    assert not any(canary.encode() in transport.raw() for canary in telemetry.CANARIES)
    assert b'CANARY' not in transport.raw()  # not even in the source lines Sentry shows around each frame


def test_request_id_contextvar_is_reset_after_requests(tmp_path):
    app = create_app(base_config(tmp_path))
    app.test_client().get('/health')
    assert REQUEST_ID.get() is None


def test_missing_dsn_file_keeps_telemetry_off_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv('SENTRY_DSN_FILE', str(tmp_path / 'not-there'))
    app = create_app(base_config(tmp_path))
    assert app.extensions['jobtracker_telemetry']['backend'] is False
    assert not sentry_sdk.get_client().is_active()


def test_mail_kinds_come_from_the_handler_flag_only(worker_app):
    @taskqueue.handler('test_flagged_mail', mail=True)
    def mail_task(app, task):
        pass

    @taskqueue.handler('test_plain_task')
    def plain_task(app, task):
        pass
    assert taskqueue.is_mail_kind('test_flagged_mail')
    assert not taskqueue.is_mail_kind('test_plain_task')
    assert not taskqueue.is_mail_kind('no_such_kind')  # an unregistered kind is never "expected"
    assert not taskqueue._expected_failure('no_such_kind', RuntimeError('x'))


def test_unflagged_kinds_with_mail_like_names_still_alert(worker_app, transport):
    _run_task(worker_app, 'test_resend_confirmation', RuntimeError(CANARY_EMAIL))
    assert len(transport.events()) == 1


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_data_export_links_are_logged_and_reported_as_templates(tmp_path, transport, method):
    # The data-deletion branch serves whole-account exports at /api/v1/exports/<token> (GET confirms, POST
    # downloads). Whatever the view does, the token must never reach logs or Sentry.
    app = sentry_app(tmp_path, transport)

    def export(token):
        raise RuntimeError('export failed')
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules() if rule.rule == '/api/v1/exports/<token>'}
    assert endpoints  # the real export routes (GET confirmation page, POST download)
    for endpoint in endpoints:
        app.view_functions[endpoint] = export
    marker = ''.join(['CANARY', 'EXPORT', 'TOKEN'])  # built at runtime: Sentry shows source lines near frames
    token = f'{marker}.{CANARY_QUERY}'
    with LogCapture() as logs:
        assert app.test_client().open(f'/api/v1/exports/{token}?confirm={CANARY_QUERY}', method=method).status_code == 500
    assert f'Unhandled exception on {method} /api/v1/exports/<token>' in logs.text
    assert transport.events()[0]['transaction'] == '/api/v1/exports/<token>'
    assert_no_canary(logs.text, (marker, CANARY_QUERY))
    assert_no_canary(transport.raw(), (marker, CANARY_QUERY))


def test_every_mail_handler_in_the_app_is_flagged(worker_app):
    # The stack's email tasks: verification (new and resent), password reset and its notice, account deletion.
    mail_kinds = {'send_confirmation', 'resend_confirmation', 'password_reset_request', 'password_changed_email',
                  'account_deleted_email'}
    assert {kind for kind in taskqueue.HANDLERS if taskqueue.is_mail_kind(kind)} == mail_kinds
    for kind in ('extract_preview', 'ingest_url', 'harness_fill_job'):
        assert not taskqueue.is_mail_kind(kind)
