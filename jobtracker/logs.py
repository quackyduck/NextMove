"""Structured JSON logs (stdlib only) for Gunicorn, the Flask app and the queue worker.

One JSON object per line: ts, level, logger, service, msg, request_id and, for errors, exc_type and stack.
Exceptions are logged as their type plus `traceback.format_tb` frames (code lines only). The exception
message is never written: messages can quote SQL parameters, page content or provider responses.

The message itself is the formatted log call; our loggers pass user data only as args, never as the format
string. As a safety net, `redact()` masks emails, phone numbers, bearer and Next Move tokens, token-bearing
query or fragment parameters, and signed-link paths in every message.
"""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
import re
import sys
import traceback

# The current request's validated X-Request-ID (set by telemetry.py for each web request).
REQUEST_ID: ContextVar[str | None] = ContextVar('request_id', default=None)

_REDACTIONS = [
    # Token-bearing paths: signed resume links, data-export links, activation, email verification and reset.
    (re.compile(r'(/api/v1/(?:files|uploads|exports))/[^\s?#"\']+'), r'\1/<token>'),
    (re.compile(r'(/(?:verify-email|reset-password))[^\s"\']*'), r'\1'),
    (re.compile(r'(/auth/(?:email-verification|password-reset))/[^\s?#"\']+'), r'\1/<redacted>'),
    # Token-bearing query and fragment parameters.
    (re.compile(r'([?&#](?:token|activate|code|reset|verify|state|code_verifier|refresh_token|access_token)=)'
                r'[^&#\s"\']*', re.IGNORECASE), r'\1<redacted>'),
    (re.compile(r'\bBearer\s+[A-Za-z0-9._~+/=-]+', re.IGNORECASE), 'Bearer <redacted>'),
    (re.compile(r'\bnm(?:_(?:oat|ort|ingest|harness))_[A-Za-z0-9_-]+'), '<token>'),
    (re.compile(r'\bnm[re]_[A-Za-z0-9_-]{16,}'), '<token>'),  # reset links (nmr_) and export links (nme_)
    (re.compile(r'\bnmc_[A-Za-z0-9_-]+'), '<client>'),
    # itsdangerous-shaped signed values: base64url.timestamp.signature
    (re.compile(r'\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{16,}\b'), '<signed>'),
    (re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'), '<email>'),
    (re.compile(r'(?<![\w.])\+\d{7,15}\b'), '<phone>'),
]


def redact(text):
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _message(record):
    try:
        return record.getMessage()
    except Exception:  # noqa: BLE001 - a broken log call must not break logging
        return str(record.msg)


class JsonFormatter(logging.Formatter):
    def __init__(self, service=None, **kwargs):
        super().__init__()
        self.service = service or os.environ.get('LOG_SERVICE') or 'jobtracker'

    def format(self, record):
        entry = {
            'ts': datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds')
            .replace('+00:00', 'Z'),
            'level': record.levelname,
            'logger': record.name,
            'service': self.service,
            'msg': redact(_message(record)),
        }
        request_id = REQUEST_ID.get()
        if request_id:
            entry['request_id'] = request_id
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, _, tb = record.exc_info
            entry['exc_type'] = exc_type.__name__
            entry['stack'] = ''.join(traceback.format_tb(tb)) if tb else ''
        if record.stack_info:
            entry['stack_info'] = record.stack_info
        # json.dumps escapes CR, LF and quotes, so a value can never start a forged log line.
        return json.dumps(entry, ensure_ascii=True)


class TextFormatter(logging.Formatter):
    """Development format: readable, still redacted, still without exception messages."""

    def format(self, record):
        line = f'{self.formatTime(record)} {record.levelname} {record.name}: {redact(_message(record))}'
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, _, tb = record.exc_info
            line += f'\n{exc_type.__name__}\n' + ''.join(traceback.format_tb(tb) if tb else [])
        return line


def json_logs_enabled(environ=os.environ):
    fmt = environ.get('LOG_FORMAT')
    if fmt in ('json', 'text'):
        return fmt == 'json'
    return environ.get('APP_ENV') != 'development'


def quiet_logging_errors(environ=os.environ):
    """Outside development, a failing handler must not print the record (message and args) to stderr."""
    if environ.get('APP_ENV', 'production') != 'development':
        logging.raiseExceptions = False


def configure_logging(service, level='INFO'):
    """Replace the root handlers with one stderr handler in the chosen format."""
    quiet_logging_errors()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(service) if json_logs_enabled() else TextFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return handler


def gunicorn_logconfig(service='jobtracker-web'):
    """`logconfig_dict` for deploy/gunicorn.conf.py: gunicorn.error and the root logger as JSON on stderr."""
    quiet_logging_errors()
    return {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {'json': {'()': 'jobtracker.logs.JsonFormatter', 'service': service}},
        'handlers': {'stderr': {'class': 'logging.StreamHandler', 'formatter': 'json', 'stream': 'ext://sys.stderr'}},
        'root': {'level': 'INFO', 'handlers': ['stderr']},
        'loggers': {
            'gunicorn.error': {'level': 'INFO', 'handlers': ['stderr'], 'propagate': False},
            # No access log: resume URLs and private record identifiers stay out of logs.
            'gunicorn.access': {'level': 'CRITICAL', 'handlers': [], 'propagate': False},
        },
    }
