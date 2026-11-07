"""Shared fixtures for the telemetry tests: a capturing Sentry transport and canary strings."""
import io
import logging

import sentry_sdk
from sentry_sdk.transport import Transport
from werkzeug.security import generate_password_hash

from jobtracker import create_app, telemetry
from jobtracker.logs import JsonFormatter

DSN = 'https://0123456789abcdef0123456789abcdef@o1.ingest.us.sentry.io/1'
CANARY_RESUME = 'CANARY-RESUME-led-a-secret-migration'
CANARY_EMAIL = 'canary.person@example.com'
CANARY_PHONE = '+15555550177'
CANARY_TOKEN = 'nm_oat_CANARYTOKENVALUE1234567890'
CANARY_LOCAL = 'CANARY-LOCAL-VARIABLE'
CANARY_QUERY = 'CANARYQUERYVALUE'
CANARY_BODY = 'CANARY-BODY-posting-text'
CANARY_COOKIE = 'CANARYCOOKIE'
CANARIES = (CANARY_RESUME, CANARY_EMAIL, CANARY_PHONE, CANARY_TOKEN, CANARY_LOCAL, CANARY_QUERY, CANARY_BODY,
            CANARY_COOKIE, 'CANARYTOKENVALUE')


class CaptureTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    def flush(self, timeout=None, callback=None):
        return None

    def kill(self):
        return None

    def raw(self):
        return b''.join(envelope.serialize() for envelope in self.envelopes)

    def items(self, kind=None):
        found = []
        for envelope in self.envelopes:
            for item in envelope.items:
                if kind is None or item.headers.get('type') == kind:
                    found.append(item)
        return found

    def events(self):
        return [item.payload.json for item in self.items('event')]

    def checkins(self):
        return [item.payload.json for item in self.items('check_in')]


def base_config(tmp_path, **extra):
    return {
        'TESTING': True, 'APP_ENV': 'development', 'PROPAGATE_EXCEPTIONS': False,
        'SECRET_KEY': 'test-secret', 'APP_PASSWORD_HASH': generate_password_hash('test-password'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "test.sqlite3"}',
        'UPLOAD_FOLDER': str(tmp_path / 'resumes'), 'SESSION_COOKIE_SECURE': False, **extra,
    }


def sentry_app(tmp_path, transport, **extra):
    return create_app(base_config(tmp_path, SENTRY_DSN=DSN, SENTRY_TRANSPORT=transport, **extra))


def shutdown_sentry():
    client = sentry_sdk.get_client()
    if client.is_active():
        client.close(timeout=0)
    sentry_sdk.get_global_scope().set_client(None)
    telemetry._state['service'] = None
    telemetry.reset_throttle()


class LogCapture:
    """Attach the production JSON formatter to the root logger and collect its output."""

    def __init__(self, service='jobtracker-web', level=logging.DEBUG):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(JsonFormatter(service))
        self.level = level

    def __enter__(self):
        root = logging.getLogger()
        self._previous = root.level
        root.addHandler(self.handler)
        root.setLevel(self.level)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        root.removeHandler(self.handler)
        root.setLevel(self._previous)

    @property
    def text(self):
        return self.stream.getvalue()

    def lines(self):
        import json
        return [json.loads(line) for line in self.text.splitlines() if line.strip()]


def assert_no_canary(blob, canaries=CANARIES):
    text = blob.decode('utf-8', 'replace') if isinstance(blob, bytes) else blob
    leaked = [canary for canary in canaries if canary in text]
    assert not leaked, f'canaries leaked: {leaked}'
