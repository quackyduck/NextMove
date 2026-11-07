"""Outbound mail: configuration refusals, backends, the deliverable() rule, quotas, recipient checks and logging."""
import json
import logging
import smtplib
import ssl
import urllib.error

import pytest
from werkzeug.security import generate_password_hash

from jobtracker import create_app, mailer, throttle

BASE = {
    'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'mailer-test-secret-0123456789abcdef',
    'APP_PASSWORD_HASH': generate_password_hash('test-password', method='pbkdf2:sha256:1000'),
    'SESSION_COOKIE_SECURE': False, 'PUBLIC_BASE_URL': 'https://example.test/nextmove', 'APP_ROOT_PATH': '',
    'MAIL_FROM': 'My Next Moov <no-reply@mail.example.test>', 'MAIL_REPLY_TO': 'support@example.test',
}


@pytest.fixture
def make(tmp_path, monkeypatch):
    for name in list(mailer.SETTINGS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv('WORKER_LOG_LEVEL', raising=False)

    def build(**overrides):
        config = {**BASE, 'DATABASE_URL': f'sqlite:///{tmp_path / "mail.sqlite3"}',
                  'UPLOAD_FOLDER': str(tmp_path / 'uploads'), 'LOGIN_THROTTLE_DATABASE': str(tmp_path / 't.sqlite3'),
                  'SCHEMA_WAIT_SECONDS': 5, **overrides}
        return create_app(config)
    mailer.outbox.clear()
    mailer._dns_cache.clear()
    return build


def production(**overrides):
    return {'TESTING': False, 'APP_ENV': 'production', 'MAIL_BACKEND': 'resend', **overrides}


# deliverable() ------------------------------------------------------------------
@pytest.mark.parametrize('address', [
    'foo<bar@evil.com>', 'a,b@x.com', 'x@10.0.0.25', 'x@[1.2.3.4]', 'x@localhost', 'owner@localhost', 'x@com',
    'x@a.123', '"q"@x.com', 'a..b@x.com', '.a@x.com', 'a.@x.com', 'ü@x.com', 'x@bücher.de', 'a b@x.com',
    'x@-bad.com', 'x@bad-.com', 'a@b@c.com', '', None, 'x' * 65 + '@example.com', 'x@x.localhost',
    'a\r\nBcc: e@x.com@x.com', 'x@x..com'])
def test_deliverable_refuses(address):
    assert mailer.deliverable(address) is None


@pytest.mark.parametrize('address, canonical', [
    ('x@xn--bcher-kva.de', 'x@xn--bcher-kva.de'), ('a+tag@sub.example.co.uk', 'a+tag@sub.example.co.uk'),
    ('  Mixed.Case@Example.COM ', 'mixed.case@example.com'), ("o'brien@example.org", "o'brien@example.org"),
    ('x@example.xn--p1ai', 'x@example.xn--p1ai')])
def test_deliverable_accepts(address, canonical):
    assert mailer.deliverable(address) == canonical


# Configuration ------------------------------------------------------------------
@pytest.mark.parametrize('overrides, message', [
    ({'MAIL_BACKEND': None}, 'requires MAIL_BACKEND'),
    ({'MAIL_BACKEND': 'console'}, 'development only'),
    ({'MAIL_BACKEND': 'file', 'MAIL_FILE_DIR': '/tmp/x'}, 'development only'),
    ({'MAIL_BACKEND': 'memory'}, 'tests only'),
    ({'MAIL_BACKEND': 'pigeon'}, 'must be one of'),
    ({'MAIL_BACKEND': 'none'}, 'Sign-up needs email'),
    ({'MAIL_BACKEND': 'smtp', 'MAIL_HOST': 'smtp.example.test', 'MAIL_SECURITY': 'none'}, 'starttls or ssl'),
    ({'MAIL_FROM': 'nobody'}, 'MAIL_FROM'),
    ({'MAIL_FROM': 'A <a@x.com>\r\nBcc: b@x.com'}, 'single line'),
    ({'PUBLIC_BASE_URL': 'http://example.test'}, 'PUBLIC_BASE_URL'),
    ({'PUBLIC_BASE_URL': 'https://example.test', 'APP_ROOT_PATH': '/nextmove'}, 'APP_ROOT_PATH'),
    ({'MAIL_API_URL': 'http://api.resend.com/emails'}, 'https URL'),
    ({'MAIL_TIMEOUT_SECONDS': 30}, 'MAIL_TIMEOUT_SECONDS'),
    ({'MAIL_DAILY_LIMIT': 90, 'MAIL_LIMIT_SIGNUP': 60, 'MAIL_LIMIT_RESET': 25}, 'at least 10%'),
    ({'MAIL_CHECK_RECIPIENT_DNS': '0'}, 'cannot be disabled'),
    ({'QUEUE_WORKER': True}, 'MAIL_API_KEY_FILE'),
])
def test_production_refuses_misconfiguration(make, overrides, message):
    if overrides.get('QUEUE_WORKER'):
        make(**production())  # The web process creates the schema the worker waits for.
    with pytest.raises(RuntimeError, match=message):
        make(**production(**overrides))


def test_production_accepts_resend_without_a_key_in_web_and_none_with_signup_off(make):
    app = make(**production())
    assert app.config['MAIL_BACKEND'] == 'resend' and app.config['MAIL_CHECK_RECIPIENT_DNS'] is True
    assert make(**production(MAIL_BACKEND='none', SIGNUP_ENABLED=False)).config['MAIL_BACKEND'] == 'none'
    assert make(**production(QUEUE_WORKER=True, MAIL_API_KEY='re_test')).config['MAIL_API_KEY'] == 're_test'


def test_production_worker_warns_about_debug_logging(make, monkeypatch, caplog):
    make(**production())
    monkeypatch.setenv('WORKER_LOG_LEVEL', 'DEBUG')
    caplog.set_level(logging.WARNING, logger='jobtracker.worker')
    make(**production(QUEUE_WORKER=True, MAIL_API_KEY='re_test'))  # Allowed: the operator's choice.
    assert 'WORKER_LOG_LEVEL=DEBUG in production' in caplog.text


def test_development_defaults_to_console_and_tests_to_memory(make):
    assert make().config['MAIL_BACKEND'] == 'memory'
    assert make(TESTING=False).config['MAIL_BACKEND'] == 'console'


def test_public_url_never_uses_the_host_header_or_appends_the_root_path(make):
    app = make()
    with app.test_request_context('/', headers={'Host': 'evil.example'}):
        assert mailer.public_url('/verify-email', {'token': 'abc'}) == 'https://example.test/nextmove/verify-email#token=abc'


# Sending --------------------------------------------------------------------------
def test_memory_send_builds_a_safe_multipart_message(make):
    app = make()
    with app.app_context():
        mailer.send_email('Person@Example.com', 'Hello', 'Plain body', '<p>Html</p>', category='test')
    message = mailer.outbox[-1]
    assert message['To'] == 'person@example.com' and message['Auto-Submitted'] == 'auto-generated'
    assert message['Reply-To'] == 'support@example.test'
    assert message['Message-ID'].endswith('@mail.example.test>')
    assert message.get_body(('plain',)).get_content().strip() == 'Plain body'
    assert message.get_body(('html',)).get_content().strip() == '<p>Html</p>'


@pytest.mark.parametrize('to, subject', [('a@x.com\r\nBcc: b@x.com', 'Hi'), ('a@x.com', 'Hi\r\nBcc: b@x.com'),
                                         ('owner@localhost', 'Hi'), ('foo<bar@evil.com>', 'Hi')])
def test_header_injection_and_undeliverable_addresses_are_refused(make, to, subject):
    with make().app_context():
        with pytest.raises(mailer.PermanentMailError):
            mailer.send_email(to, subject, 'body', category='test')
    assert not mailer.outbox


def test_unknown_category_is_a_programming_error(make):
    with make().app_context(), pytest.raises(ValueError):
        mailer.send_email('a@example.com', 'Hi', 'body', category='marketing')


def test_file_backend_writes_private_eml_files(make, tmp_path):
    folder = tmp_path / 'mail'
    with make(MAIL_BACKEND='file', MAIL_FILE_DIR=str(folder)).app_context():
        mailer.send_email('a@example.com', 'Hi', 'body https://x', category='test')
    files = list(folder.glob('*.eml'))
    assert len(files) == 1 and oct(files[0].stat().st_mode & 0o777) == '0o600'
    assert b'body https://x' in files[0].read_bytes()


def test_none_backend_refuses_and_reports_off(make):
    app = make(MAIL_BACKEND='none')
    with app.app_context():
        assert not mailer.enabled() and mailer.status() == 'off'
        with pytest.raises(mailer.MailNotConfigured):
            mailer.send_email('a@example.com', 'Hi', 'body', category='test')


class FakeResponse:
    def __init__(self, body=b'{"id": "msg_123"}'):
        self.body = body

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def http_error(code):
    return urllib.error.HTTPError('https://api.resend.com/emails', code, 'x', {}, None)


def test_resend_backend_posts_json_with_the_key_only_in_the_header(make, monkeypatch, caplog):
    opener = FakeOpener(FakeResponse())
    monkeypatch.setattr(mailer, '_opener', lambda: opener)
    app = make(MAIL_BACKEND='resend', MAIL_API_KEY='re_secret_key')
    caplog.set_level(logging.DEBUG)
    with app.app_context():
        mailer.send_email('a@example.com', 'Hi', 'Body', '<p>Body</p>', category='test')
    request, timeout = opener.requests[0]
    body = json.loads(request.data)
    assert request.full_url == 'https://api.resend.com/emails' and request.get_method() == 'POST'
    assert request.get_header('Authorization') == 'Bearer re_secret_key' and timeout == 15
    assert body['to'] == ['a@example.com'] and body['reply_to'] == 'support@example.test'
    assert body['headers'] == {'Auto-Submitted': 'auto-generated'} and body['html'].strip() == '<p>Body</p>'
    assert 're_secret_key' not in json.dumps(body) and 're_secret_key' not in caplog.text
    assert 'msg_123' in caplog.text and 'a@example.com' not in caplog.text


@pytest.mark.parametrize('outcome, error', [
    (http_error(429), mailer.TransientMailError), (http_error(500), mailer.TransientMailError),
    (http_error(503), mailer.TransientMailError), (urllib.error.URLError('down'), mailer.TransientMailError),
    (TimeoutError(), mailer.TransientMailError), (ssl.SSLError('bad cert'), mailer.TransientMailError),
    (http_error(401), mailer.PermanentMailError), (http_error(403), mailer.PermanentMailError),
    (http_error(422), mailer.PermanentMailError), (http_error(302), mailer.TransientMailError)])
def test_resend_errors_map_to_transient_or_permanent(make, monkeypatch, outcome, error):
    monkeypatch.setattr(mailer, '_opener', lambda: FakeOpener(outcome))
    with make(MAIL_BACKEND='resend', MAIL_API_KEY='re_k').app_context():
        with pytest.raises(error) as raised:
            mailer.send_email('a@example.com', 'Hi', 'Body', category='test')
    assert 'a@example.com' not in str(raised.value)


def test_resend_refuses_to_follow_redirects():
    handler = mailer._NoRedirect()
    assert handler.redirect_request(None, None, 302, 'Found', {}, 'https://elsewhere') is None


def test_rejected_credentials_log_one_alert_and_pause_signup(make, monkeypatch, caplog):
    monkeypatch.setattr(mailer, '_opener', lambda: FakeOpener(http_error(401)))
    app = make(MAIL_BACKEND='resend', MAIL_API_KEY='re_k')
    caplog.set_level(logging.INFO, logger='jobtracker.mail')
    with app.app_context():
        for _ in range(2):
            with pytest.raises(mailer.PermanentMailError):
                mailer.send_email('a@example.com', 'Hi', 'Body', category='test')
        assert mailer.signup_paused() and mailer.status() == 'degraded'
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count('mail provider rejected credentials (HTTP 401)') == 1
    assert 'signup paused: credentials' in messages and 'mail degraded: credentials' in messages


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ehlo(self):
        self.calls.append('ehlo')

    def starttls(self, context):
        self.calls.append(('starttls', context.verify_mode, context.check_hostname))

    def login(self, user, password):
        self.calls.append(('login', user))

    def send_message(self, message, from_addr, to_addrs):
        self.calls.append(('send', from_addr, tuple(to_addrs)))


def test_smtp_starttls_verifies_certificates_and_sends_to_the_explicit_envelope(make, monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, 'SMTP', FakeSMTP)
    app = make(MAIL_BACKEND='smtp', MAIL_HOST='smtp.resend.com', MAIL_PORT=2587, MAIL_USERNAME='resend',
               MAIL_PASSWORD='secret')
    with app.app_context():
        mailer.send_email('a@example.com', 'Hi', 'Body', category='test')
    client = FakeSMTP.instances[0]
    assert (client.host, client.port, client.timeout) == ('smtp.resend.com', 2587, 15)
    assert ('starttls', ssl.CERT_REQUIRED, True) in client.calls and ('login', 'resend') in client.calls
    assert ('send', 'no-reply@mail.example.test', ('a@example.com',)) in client.calls


def test_smtp_without_starttls_never_falls_back_to_plaintext(make, monkeypatch):
    class NoTLS(FakeSMTP):
        def starttls(self, context):
            raise smtplib.SMTPNotSupportedError('STARTTLS extension not supported by server.')

        def send_message(self, *args, **kwargs):
            raise AssertionError('sent in plaintext')
    monkeypatch.setattr(smtplib, 'SMTP', NoTLS)
    with make(MAIL_BACKEND='smtp', MAIL_HOST='smtp.example.test').app_context():
        with pytest.raises(mailer.TransientMailError):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='test')


def test_smtp_error_codes(make, monkeypatch):
    def failing(error):
        class Failing(FakeSMTP):
            def send_message(self, *args, **kwargs):
                raise error
        return Failing
    app = make(MAIL_BACKEND='smtp', MAIL_HOST='smtp.example.test')
    for error, expected in ((smtplib.SMTPRecipientsRefused({'a@example.com': (550, b'no')}), mailer.PermanentMailError),
                            (smtplib.SMTPDataError(451, b'later'), mailer.TransientMailError),
                            (smtplib.SMTPDataError(554, b'no'), mailer.PermanentMailError),
                            (smtplib.SMTPServerDisconnected('gone'), mailer.TransientMailError)):
        monkeypatch.setattr(smtplib, 'SMTP', failing(error))
        with app.app_context(), pytest.raises(expected):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='test')


# Quotas ----------------------------------------------------------------------------
def test_category_shares_keep_room_for_notices_and_the_legacy_reserve(make):
    app = make(MAIL_DAILY_LIMIT=10, MAIL_LIMIT_SIGNUP=6, MAIL_LIMIT_RESET=2)
    with app.app_context():
        for _ in range(6 - mailer.LEGACY_RESERVE):  # New sign-ups stop LEGACY_RESERVE before the share.
            mailer.send_email('a@example.com', 'Hi', 'Body', category='signup')
        assert not mailer.has_room('signup') and mailer.has_room('signup', 'existing')
        assert mailer.signup_paused()
        with pytest.raises(mailer.MailQuotaExceeded):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='signup')
        for _ in range(mailer.LEGACY_RESERVE):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='signup', kind='existing')
        with pytest.raises(mailer.MailQuotaExceeded):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='signup', kind='existing')
        for _ in range(2):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='reset')
        with pytest.raises(mailer.MailQuotaExceeded):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='reset')
        # Notices still have the remainder of the hard cap, then nothing does.
        mailer.send_email('a@example.com', 'Hi', 'Body', category='notice')
        mailer.send_email('a@example.com', 'Hi', 'Body', category='notice')
        with pytest.raises(mailer.MailQuotaExceeded):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='notice')
        assert mailer.sent_today() == 10 and mailer.sent_this_month() == 10
        assert mailer.status() == 'degraded'


def test_monthly_limit_defers_everything_and_alerts_once(make, caplog):
    app = make(MAIL_MONTHLY_LIMIT=5)
    caplog.set_level(logging.WARNING, logger='jobtracker.mail')
    with app.app_context():
        for _ in range(5):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='notice')
        with pytest.raises(mailer.MailQuotaExceeded):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='notice')
        assert not mailer.has_room('notice') and mailer.signup_paused()
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count('mail monthly usage at 80% (4 of 5)') == 1
    assert messages.count('mail monthly limit reached (5 of 5); sends deferred') == 1


def test_usage_alerts_use_the_documented_templates_once(make, caplog):
    app = make(MAIL_DAILY_LIMIT=10, MAIL_LIMIT_SIGNUP=6, MAIL_LIMIT_RESET=2)
    caplog.set_level(logging.INFO, logger='jobtracker.mail')
    with app.app_context():
        for _ in range(8):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='notice')
        mailer.send_email('a@example.com', 'Hi', 'Body', category='reset')
        mailer.send_email('a@example.com', 'Hi', 'Body', category='reset')
        with pytest.raises(mailer.MailQuotaExceeded):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='notice')
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count('mail daily usage at 80% (8 of 10)') == 1
    assert messages.count('mail reset share at 80% (2 of 2)') == 1
    assert messages.count('mail daily limit reached (10 of 10); sends deferred') == 1


def test_every_alert_record_matches_a_constant_template_with_safe_arguments(make, monkeypatch, caplog):
    app = make(MAIL_DAILY_LIMIT=10, MAIL_LIMIT_SIGNUP=6, MAIL_LIMIT_RESET=2, MAIL_MONTHLY_LIMIT=9)
    caplog.set_level(logging.DEBUG, logger='jobtracker.mail')
    with app.app_context():
        for _ in range(12):
            try:
                mailer.send_email('person@example.com', 'Hi', 'Body', category='notice')
            except mailer.MailQuotaExceeded:
                pass
        mailer.status()
        mailer.signup_paused()
    alerts = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert alerts
    for record in alerts:
        assert record.msg in mailer.TEMPLATES.values()
        assert not any(isinstance(arg, str) and '@' in arg for arg in record.args)
    assert 'person@example.com' not in caplog.text


def test_provider_failures_degrade_then_recover(make, monkeypatch, caplog):
    opener = FakeOpener(urllib.error.URLError('down'))
    monkeypatch.setattr(mailer, '_opener', lambda: opener)
    app = make(MAIL_BACKEND='resend', MAIL_API_KEY='re_k')
    caplog.set_level(logging.INFO, logger='jobtracker.mail')
    with app.app_context():
        for _ in range(3):
            with pytest.raises(mailer.TransientMailError):
                mailer.send_email('a@example.com', 'Hi', 'Body', category='test')
        assert mailer.status() == 'degraded'
        opener.outcome = FakeResponse()
        mailer.send_email('a@example.com', 'Hi', 'Body', category='test')
        assert mailer.status() == 'ok'
    messages = [record.getMessage() for record in caplog.records]
    assert 'mail degraded: provider' in messages and 'mail recovered' in messages


# Recipient checks --------------------------------------------------------------------
class FakeMX:
    def __init__(self, preference, host):
        import dns.name
        self.preference, self.exchange = preference, dns.name.from_text(host)


def fake_lookup(table):
    def lookup(domain):
        outcome = table[domain]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return lookup


def test_recipient_dns_outcomes(make, monkeypatch):
    import dns.name
    import dns.resolver

    class Answer:
        def __init__(self, records):
            self.rrset = records

    def resolve(self, domain, rdtype, raise_on_no_answer=True):
        table = {('mx.example', 'MX'): [FakeMX(10, 'b.mx.example.'), FakeMX(5, 'a.mx.example.')],
                 ('nullmx.example', 'MX'): [FakeMX(0, '.')],
                 ('aonly.example', 'MX'): [], ('aonly.example', 'A'): ['192.0.2.1'],
                 ('nothing.example', 'MX'): [], ('nothing.example', 'A'): [], ('nothing.example', 'AAAA'): []}
        if domain == 'gone.example':
            raise dns.resolver.NXDOMAIN()
        if domain == 'slow.example':
            raise dns.resolver.LifetimeTimeout(timeout=3, errors={})
        return Answer(table[(domain, rdtype)])
    monkeypatch.setattr(dns.resolver.Resolver, 'resolve', resolve)
    with make(MAIL_CHECK_RECIPIENT_DNS='1').app_context():
        assert mailer._lookup('mx.example') == ('ok', 'a.mx.example')
        assert mailer._lookup('nullmx.example') == ('dns', None)
        assert mailer._lookup('aonly.example') == ('ok', 'aonly.example')
        assert mailer._lookup('nothing.example') == ('dns', None)
        assert mailer._lookup('gone.example') == ('dns', None)
        with pytest.raises(mailer.TransientMailError):
            mailer._lookup('slow.example')
        assert mailer.recipient_ok('a@gone.example', 'signup').status == 'dns'
        with pytest.raises(mailer.TransientMailError):
            mailer.recipient_ok('a@slow.example', 'signup')


def test_recipient_domain_and_mx_caps_count_only_sent_mail(make, monkeypatch):
    calls = []

    def lookup(domain):
        calls.append(domain)
        return ('ok', 'mx.shared-host.example') if domain.endswith('.junk.example') else ('ok', 'gmail-smtp-in.l.google.com')
    monkeypatch.setattr(mailer, '_lookup', lookup)
    app = make(MAIL_CHECK_RECIPIENT_DNS='1', MAIL_DAILY_LIMIT=300, MAIL_LIMIT_SIGNUP=200, MAIL_LIMIT_RESET=50)
    with app.app_context():
        for index in range(mailer.DOMAIN_LIMIT):
            recipient = mailer.recipient_ok(f'p{index}@one.junk.example', 'signup')
            assert recipient.status == 'ok'
            mailer.send_email(f'p{index}@one.junk.example', 'Hi', 'Body', category='signup', recipient=recipient)
        assert mailer.recipient_ok('late@one.junk.example', 'signup').status == 'domain_cap'
        # Other junk subdomains share one MX host: capped at MX_LIMIT in total.
        for index in range(mailer.MX_LIMIT - mailer.DOMAIN_LIMIT):
            recipient = mailer.recipient_ok(f'p@{index}.junk.example', 'signup')
            mailer.send_email(f'p@{index}.junk.example', 'Hi', 'Body', category='signup', recipient=recipient)
        assert mailer.recipient_ok('p@fresh.junk.example', 'signup').status == 'mx_cap'
        # Checking alone never counts; big providers use the larger domain cap and no MX cap.
        for _ in range(50):
            assert mailer.recipient_ok('someone@gmail.com', 'signup').status == 'ok'
        # Reset mail checks DNS but not the caps.
        assert mailer.recipient_ok('late@one.junk.example', 'reset') == mailer.Recipient('ok')
    assert calls.count('one.junk.example') == 1  # Cached for an hour.


def test_dns_is_not_consulted_outside_production_unless_enabled(make, monkeypatch):
    monkeypatch.setattr(mailer, '_lookup', lambda domain: pytest.fail('DNS consulted'))
    with make().app_context():
        assert mailer.recipient_ok('a@example.com', 'signup').status == 'ok'
        assert mailer.recipient_ok('owner@localhost', 'signup').status == 'dns'


def test_quota_store_failure_is_transient(make, monkeypatch):
    def broken(*args, **kwargs):
        raise throttle.Unavailable()
    with make().app_context():
        monkeypatch.setattr(throttle, 'reserve', broken)
        with pytest.raises(mailer.TransientMailError):
            mailer.send_email('a@example.com', 'Hi', 'Body', category='test')


def test_mail_test_and_status_commands(make):
    app = make()
    runner = app.test_cli_runner()
    assert runner.invoke(args=['mail', 'test', 'support@example.com']).output.strip() == 'sent'
    assert 'not sent' in runner.invoke(args=['mail', 'test', 'owner@localhost']).output
    output = runner.invoke(args=['mail', 'status']).output
    assert 'last 24 h: 1 of 90' in output and 'this month (UTC): 1 of 3000' in output
