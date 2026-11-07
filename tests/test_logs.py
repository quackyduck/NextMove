"""JSON log lines: one object per line, type and stack for errors (never the message), redaction safety net."""
import json
import logging

import pytest

from jobtracker import logs
from jobtracker.logs import REQUEST_ID, JsonFormatter, gunicorn_logconfig, json_logs_enabled, redact


def _format(message, *args, exc_info=None, name='jobtracker.test'):
    record = logging.LogRecord(name, logging.ERROR if exc_info else logging.INFO, __file__, 10, message, args,
                               exc_info)
    return JsonFormatter('jobtracker-web').format(record)


def test_lines_are_json_with_the_expected_keys():
    entry = json.loads(_format('Queue worker started (pid %s)', 123))
    assert set(entry) == {'ts', 'level', 'logger', 'service', 'msg'}
    assert entry['msg'] == 'Queue worker started (pid 123)' and entry['service'] == 'jobtracker-web'
    assert entry['ts'].endswith('Z')


def test_errors_carry_type_and_stack_but_never_the_message():
    message = 'CANARY-RESUME-TEXT and canary@example.com'  # user data reaches code only through variables
    try:
        secret = message  # noqa: F841
        raise ValueError(message)
    except ValueError:
        import sys
        line = _format('Unhandled exception on %s %s', 'GET', '/api/v1/jobs/<job_id>', exc_info=sys.exc_info())
    entry = json.loads(line)
    assert entry['exc_type'] == 'ValueError'
    assert 'raise ValueError' in entry['stack']
    assert 'CANARY' not in line and 'canary@example.com' not in line


def test_user_values_cannot_forge_lines():
    line = _format('job title %s', 'Engineer\n{"level": "ERROR", "msg": "forged"}\r\n%s %d')
    assert '\n' not in line and '\r' not in line
    assert json.loads(line)['msg'].startswith('job title Engineer\n{')


def test_request_id_is_included_when_set():
    token = REQUEST_ID.set('abc-123-def-456')
    try:
        assert json.loads(_format('hello'))['request_id'] == 'abc-123-def-456'
    finally:
        REQUEST_ID.reset(token)


@pytest.mark.parametrize('raw,leak', [
    ('sent to canary.person@example.com', 'canary.person@example.com'),
    ('from +15555550123 now', '+15555550123'),
    ('Authorization: Bearer abc.def-ghi', 'abc.def-ghi'),
    ('token nm_oat_CANARYTOKEN123 used', 'CANARYTOKEN123'),
    ('grant nm_harness_CANARYGRANT used', 'CANARYGRANT'),
    ('agent nm_ingest_CANARYINGEST used', 'CANARYINGEST'),
    ('client nmc_CANARYCLIENT', 'CANARYCLIENT'),
    ('GET /api/v1/files/eyJnIjoiYSJ9.Zk9xYw.CANARYSIG', 'CANARYSIG'),
    ('POST /api/v1/uploads/CANARYUPLOAD', 'CANARYUPLOAD'),
    ('GET /api/v1/exports/CANARYEXPORT.abc', 'CANARYEXPORT'),
    ('POST /api/v1/exports/CANARYEXPORT2?x=1', 'CANARYEXPORT2'),
    ('GET /api/v1/auth/activation?token=CANARYACT', 'CANARYACT'),
    ('open /?activate=CANARYACTIVATE', 'CANARYACTIVATE'),
    ('open /verify-email#token=CANARYVERIFY', 'CANARYVERIFY'),
    ('open /reset-password#reset=CANARYRESET', 'CANARYRESET'),
    ('POST /auth/password-reset/CANARYPATH', 'CANARYPATH'),
    ('POST /auth/email-verification/CANARYPATH2', 'CANARYPATH2'),
    ('callback ?code=CANARYCODE&state=CANARYSTATE', 'CANARY'),
])
def test_redaction_safety_net(raw, leak):
    assert leak not in redact(raw)


def test_redaction_keeps_ordinary_text():
    assert redact('Queue task 3f2e (extract_preview) attempt 2 failed: TimeoutError') == \
        'Queue task 3f2e (extract_preview) attempt 2 failed: TimeoutError'


def test_format_choice(monkeypatch):
    assert json_logs_enabled({'APP_ENV': 'production'})
    assert not json_logs_enabled({'APP_ENV': 'development'})
    assert json_logs_enabled({'APP_ENV': 'development', 'LOG_FORMAT': 'json'})
    assert not json_logs_enabled({'LOG_FORMAT': 'text'})


def test_gunicorn_logconfig_is_valid_and_drops_access_logs():
    import logging.config
    config = gunicorn_logconfig()
    logging.config.dictConfig(config)
    try:
        error_logger = logging.getLogger('gunicorn.error')
        assert isinstance(error_logger.handlers[0].formatter, JsonFormatter)
        assert logging.getLogger('gunicorn.access').handlers == []
    finally:
        logging.getLogger().handlers.clear()
        error_logger.handlers.clear()


def test_gunicorn_conf_uses_json_unless_text(monkeypatch):
    import runpy
    from pathlib import Path
    conf = Path(__file__).resolve().parent.parent / 'deploy' / 'gunicorn.conf.py'
    monkeypatch.delenv('LOG_FORMAT', raising=False)
    monkeypatch.setenv('APP_ENV', 'production')
    assert runpy.run_path(str(conf))['logconfig_dict']['formatters']['json']['()'] == 'jobtracker.logs.JsonFormatter'
    assert runpy.run_path(str(conf))['accesslog'] is None
    monkeypatch.setenv('LOG_FORMAT', 'text')
    assert 'logconfig_dict' not in runpy.run_path(str(conf))


def test_worker_logs_are_configured_as_json(monkeypatch, capsys):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.delenv('LOG_FORMAT', raising=False)
    handler = logs.configure_logging('jobtracker-worker')
    try:
        logging.getLogger('jobtracker.worker').warning('Queue worker started (pid %s)', 1)
        line = capsys.readouterr().err.strip().splitlines()[-1]
        assert json.loads(line)['service'] == 'jobtracker-worker'
    finally:
        logging.getLogger().removeHandler(handler)


def test_failing_handlers_do_not_print_records_outside_development(monkeypatch, capsys):
    from jobtracker.logs import quiet_logging_errors
    monkeypatch.setattr(logging, 'raiseExceptions', True)
    quiet_logging_errors({'APP_ENV': 'development'})
    assert logging.raiseExceptions is True
    quiet_logging_errors({'APP_ENV': 'production'})
    assert logging.raiseExceptions is False

    class FullDisk:
        def write(self, text):
            raise OSError('disk full')

        def flush(self):
            pass
    logger = logging.getLogger('jobtracker.test.broken')
    handler = logging.StreamHandler(FullDisk())  # emit() catches and calls handleError(), which prints the record
    logger.addHandler(handler)
    try:
        logger.error('mail for %s failed', 'CANARY-ADDRESS@example.com')
    finally:
        logger.removeHandler(handler)
    assert 'CANARY' not in capsys.readouterr().err
