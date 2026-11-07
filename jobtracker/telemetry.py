"""Error telemetry: Sentry (off unless a DSN is configured), request IDs, and the browser's telemetry config.

Privacy model (see docs/OPERATIONS.md, "Telemetry and alerting"):

* With no `SENTRY_DSN` / `SENTRY_DSN_FILE`, `sentry_sdk.init` is never called: no client, no thread, no traffic.
* Every integration is opt-in. No breadcrumbs, tracing, sessions, request bodies or local variables.
* `scrub_event` rebuilds each event from an allowlist: exception types and code frames, a few validated tags,
  a numeric user id and the route template. Exception messages are always "[redacted]". A denylist pass then
  runs over whatever survived, and if scrubbing fails the event is dropped, never sent raw.
* Log records never become events. The only messages sent are the constants in `MESSAGES`.
"""
from collections import deque
import logging
import os
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

from flask import Blueprint, current_app, g, has_request_context, jsonify, request

from .logs import REQUEST_ID

log = logging.getLogger(__name__)

REQUEST_ID_PATTERN = re.compile(r'[A-Za-z0-9-]{8,64}')
SERVICES = frozenset({'jobtracker-web', 'jobtracker-worker'})
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_.]{0,80}')
_DIGITS = re.compile(r'\d{1,12}')
_RELEASE = re.compile(r'[0-9a-f]{7,40}|unknown')
_INGEST_HOST = re.compile(r'o\d{1,12}\.ingest(\.us)?\.sentry\.io')
_PUBLIC_KEY = re.compile(r'[0-9a-f]{16,64}')
_PROJECT_PATH = re.compile(r'/\d{1,12}')

# Mail alerts: the verification branch's constant templates (logger `jobtracker.mail`) that become Sentry events.
# The event carries the template only, never its arguments. Keys are the exact `%`-style templates.
MAIL_LOGGER = 'jobtracker.mail'
MAIL_ALERTS = {
    'mail daily usage at 80%% (%d of %d)': ('mail_daily_80', 'warning'),
    'mail daily limit reached (%d of %d); sends deferred': ('mail_daily_limit', 'error'),
    'mail monthly usage at 80%% (%d of %d)': ('mail_monthly_80', 'warning'),
    'mail monthly limit reached (%d of %d); sends deferred': ('mail_monthly_limit', 'error'),
    'mail %s share at 80%% (%d of %d)': ('mail_share_80', 'warning'),
    'signup paused: %s': ('signup_paused', 'error'),
    'mail degraded: %s': ('mail_degraded', 'error'),
    'mail provider rejected credentials (HTTP %d)': ('mail_credentials', 'error'),
}
# Arguments that are fixed reason words may be tagged; everything else about the record is ignored.
_MAIL_REASON_TEMPLATES = {'signup paused: %s', 'mail degraded: %s'}
MAIL_REASONS = frozenset({'quota', 'provider', 'credentials', 'dns', 'signup_share', 'daily_limit',
                          'monthly_limit', 'task_failures'})
MAIL_TASK_FAILURES = 'mail tasks failing'

# The only message strings an event may carry.
MESSAGES = frozenset(set(MAIL_ALERTS) | {MAIL_TASK_FAILURES, 'telemetry check'})

TAG_RULES = {
    'service': lambda v: v in SERVICES,
    'request_id': lambda v: REQUEST_ID_PATTERN.fullmatch(v) is not None,
    'task_kind': lambda v: _IDENT.fullmatch(v) is not None,
    'attempt': lambda v: _DIGITS.fullmatch(v) is not None,
    'http_status': lambda v: _DIGITS.fullmatch(v) is not None,
    'error_type': lambda v: _IDENT.fullmatch(v) is not None,
    'alert': lambda v: _IDENT.fullmatch(v) is not None,
    'mail_reason': lambda v: v in MAIL_REASONS,
    'check': lambda v: v == 'canary',
}

# A second, key-based pass over anything that survives the allowlist (security baseline B2).
EXTRA_DENYLIST = [
    'new_password', 'current_password', 'confirm_password', 'password_confirmation', 'invite_code', 'code',
    'code_verifier', 'code_challenge', 'refresh_token', 'access_token', 'id_token', 'grant', 'grant_id', 'data_b64',
    'text', 'payload', 'posting', 'resume', 'content', 'prompt', 'message', 'note', 'notes', 'token', 'raw_token',
    'activation_token', 'reset_token', 'verify_token', 'email', 'sender', 'to', 'phone', 'handle', 'authorization',
    'cookie', 'x-csrf-token', 'x-ra-signature', 'x-ra-nonce', 'state', 'redirect_uri',
    # Admin page (schema 18): reset links, search text and the step-up time.
    'link', 'reset_link', 'q', 'query', 'password_at',
]

_state = {'service': None}


# Request IDs ---------------------------------------------------------------
def valid_request_id(value):
    return isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) is not None


def new_request_id():
    return uuid.uuid4().hex


def current_request_id():
    return REQUEST_ID.get()


# Sentry --------------------------------------------------------------------
def _setting(app, name):
    value = app.config.get(name)
    return value.strip() if isinstance(value, str) else value


def parse_frontend_dsn(dsn):
    """The browser DSN and its ingest origin, or None. Only Sentry's US ingest hosts (and the legacy host)."""
    if not isinstance(dsn, str) or not dsn or len(dsn) > 200:
        return None
    if re.search(r'[\s\x00-\x1f\x7f]', dsn):  # urlsplit silently drops tabs and newlines; refuse them instead
        return None
    try:
        parts = urlsplit(dsn)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != 'https' or parts.password is not None or port is not None or parts.query or parts.fragment
            or not parts.username or _PUBLIC_KEY.fullmatch(parts.username) is None
            or not parts.hostname or _INGEST_HOST.fullmatch(parts.hostname) is None
            or _PROJECT_PATH.fullmatch(parts.path) is None
            or parts.netloc != f'{parts.username}@{parts.hostname}'):
        return None
    return {'dsn': dsn, 'origin': f'https://{parts.hostname}'}


def _release():
    release = (os.environ.get('SENTRY_RELEASE') or '').strip()
    return release if _RELEASE.fullmatch(release) else None


def _environment(app):
    environment = (_setting(app, 'SENTRY_ENVIRONMENT') or app.config.get('APP_ENV') or 'production')
    return environment if _IDENT.fullmatch(environment) else 'production'


def init_sentry(app, service):
    """Start Sentry for this process when a DSN is configured. Returns True if telemetry is on."""
    import sentry_sdk
    dsn = _setting(app, 'SENTRY_DSN')
    if not dsn:
        return False
    transport = app.config.get('SENTRY_TRANSPORT')  # tests only
    if sentry_sdk.get_client().is_active() and transport is None and _state['service'] == service:
        return True
    from sentry_sdk.integrations.atexit import AtexitIntegration
    from sentry_sdk.integrations.dedupe import DedupeIntegration
    from sentry_sdk.integrations.excepthook import ExcepthookIntegration
    from sentry_sdk.integrations.flask import FlaskIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber
    _state['service'] = service
    options = dict(
        dsn=dsn, environment=_environment(app), release=_release(), server_name=service,
        send_default_pii=False, include_local_variables=False, include_source_context=True,
        max_request_body_size='never', max_breadcrumbs=0, attach_stacktrace=False,
        auto_session_tracking=False, traces_sample_rate=None, profiles_sample_rate=None,
        trace_propagation_targets=[], shutdown_timeout=2,
        default_integrations=False, auto_enabling_integrations=False,
        integrations=[DedupeIntegration(), ExcepthookIntegration(), AtexitIntegration(),
                      # No breadcrumbs and no events from log records.
                      LoggingIntegration(level=None, event_level=None),
                      FlaskIntegration(transaction_style='url')],
        event_scrubber=EventScrubber(denylist=DEFAULT_DENYLIST + EXTRA_DENYLIST, recursive=True),
        before_send=scrub_event, before_breadcrumb=lambda crumb, hint: None,
        before_send_transaction=lambda event, hint: None,
    )
    if transport is not None:
        options['transport'] = transport
    sentry_sdk.init(**options)
    return True


def telemetry_active():
    try:
        import sentry_sdk
        return sentry_sdk.get_client().is_active()
    except Exception:  # noqa: BLE001
        return False


_FRAME_KEYS = ('filename', 'abs_path', 'module', 'function', 'lineno', 'colno', 'in_app', 'context_line',
               'pre_context', 'post_context')
_DENY = None


def _denied(key):
    global _DENY
    if _DENY is None:
        from sentry_sdk.scrubber import DEFAULT_DENYLIST
        _DENY = {item.lower() for item in DEFAULT_DENYLIST + EXTRA_DENYLIST}
    return key.lower() in _DENY


def _frames(stacktrace):
    frames = []
    for frame in (stacktrace or {}).get('frames') or []:
        frames.append({key: frame[key] for key in _FRAME_KEYS if key in frame})
    return {'frames': frames}


def _exception(values):
    out = []
    for value in values or []:
        item = {'type': str(value.get('type') or 'Exception')[:120], 'value': '[redacted]'}
        if isinstance(value.get('module'), str):
            item['module'] = value['module'][:200]
        mechanism = value.get('mechanism') or {}
        if mechanism:
            item['mechanism'] = {k: mechanism[k] for k in ('type', 'handled') if k in mechanism}
        if value.get('stacktrace'):
            item['stacktrace'] = _frames(value['stacktrace'])
        out.append(item)
    return {'values': out}


def _tags(event):
    tags = dict(event.get('tags') or {})
    if _state['service']:
        tags['service'] = _state['service']
    request_id = REQUEST_ID.get()
    if request_id:
        tags['request_id'] = request_id
    kept = {}
    for key, value in tags.items():
        value = str(value)
        rule = TAG_RULES.get(key)
        if rule and rule(value) and not _denied(key):
            kept[key] = value
    return kept


def _user(event):
    user_id = None
    if has_request_context() and isinstance(g.get('user_id'), int):
        user_id = g.user_id
    elif isinstance((event.get('user') or {}).get('id'), (str, int)):
        user_id = (event.get('user') or {}).get('id')
    user_id = str(user_id) if user_id is not None else ''
    return {'id': user_id} if _DIGITS.fullmatch(user_id) else None


def _transaction(event):
    if has_request_context():
        rule = request.url_rule.rule if request.url_rule is not None else '<unmatched>'
        return rule
    if (event.get('transaction_info') or {}).get('source') == 'task':
        return event.get('transaction')
    return None


def _scrub_checkin(event):
    keep = {key: event[key] for key in ('type', 'event_id', 'timestamp', 'check_in_id', 'monitor_slug', 'status',
                                         'duration', 'environment', 'release', 'platform') if key in event}
    if isinstance(event.get('monitor_config'), dict):
        keep['monitor_config'] = event['monitor_config']
    return keep


def scrub_event(event, hint=None):
    """Rebuild the event from an allowlist (module docstring). Fails closed: any error drops the event."""
    try:
        if event.get('type') == 'check_in':
            return _scrub_checkin(event)
        out = {key: event[key] for key in ('event_id', 'timestamp', 'level', 'platform', 'release', 'environment',
                                           'server_name', 'logger', 'fingerprint') if key in event}
        if event.get('exception'):
            out['exception'] = _exception(event['exception'].get('values'))
        logentry = event.get('logentry') or {}
        template = logentry.get('message') if isinstance(logentry, dict) else None
        message = event.get('message')
        if template in MESSAGES:
            out['logentry'] = {'message': template}
        elif isinstance(message, str) and message in MESSAGES:
            out['message'] = message
        out['tags'] = _tags(event)
        user = _user(event)
        if user:
            out['user'] = user
        transaction = _transaction(event)
        if transaction:
            out['transaction'] = transaction
        method = ((event.get('request') or {}).get('method') or '').upper()
        if method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            out['request'] = {'method': method}
        contexts = event.get('contexts') or {}
        out['contexts'] = {name: {k: v for k, v in contexts[name].items() if k in ('name', 'version', 'build')}
                           for name in ('runtime', 'os') if isinstance(contexts.get(name), dict)}
        sdk = event.get('sdk') or {}
        out['sdk'] = {k: sdk[k] for k in ('name', 'version') if k in sdk}
        return out
    except Exception:  # noqa: BLE001 - never send an event we could not scrub
        return None


_throttle_lock = threading.Lock()
_last_sent = {}


def throttled_capture(key, exc=None, *, message=None, level='error', seconds=300, tags=None, user_id=None):
    """Capture at most one event per `key` every `seconds`. `message` must be one of MESSAGES."""
    if not telemetry_active():
        return None
    moment = time.monotonic()
    with _throttle_lock:
        if moment - _last_sent.get(key, -seconds - 1) < seconds:
            return None
        _last_sent[key] = moment
    import sentry_sdk
    with sentry_sdk.new_scope() as scope:
        for name, value in (tags or {}).items():
            scope.set_tag(name, value)
        if user_id is not None:
            scope.set_user({'id': str(user_id)})
        scope.level = level
        if exc is not None:
            return sentry_sdk.capture_exception(exc)
        if message in MESSAGES:
            return sentry_sdk.capture_message(message, level=level)
    return None


# Assembled at runtime, so the check event's own source lines (which Sentry shows) never contain them.
CANARIES = tuple(''.join(reversed(value)) for value in (
    'txet-emuser-YRANAC', 'moc.elpmaxe@yranac', '9910555551+', 'NEKOTYRANAC_tao_mn', 'lacol-YRANAC'))


def send_check_event():
    """A synthetic exception whose message, locals and tags carry canaries; returns the event id (or None)."""
    if not telemetry_active():
        return None
    import sentry_sdk

    def fail(resume_text):
        secret_local = CANARIES[4]  # noqa: F841 - must not be sent (include_local_variables=False)
        raise RuntimeError(f'{resume_text} {CANARIES[1]} {CANARIES[2]} Bearer {CANARIES[3]}')
    with sentry_sdk.new_scope() as scope:
        scope.set_tag('check', 'canary')
        scope.set_tag('email', CANARIES[1])
        scope.set_extra('resume', CANARIES[0])
        scope.set_context('payload', {'text': CANARIES[0]})
        try:
            fail(CANARIES[0])
        except RuntimeError as exc:
            event_id = sentry_sdk.capture_exception(exc)
    sentry_sdk.flush(timeout=5)
    return event_id


def reset_throttle():
    with _throttle_lock:
        _last_sent.clear()


def checkin(slug, ok=True, interval_minutes=5):
    """A Sentry Cron check-in: slug, status and a static schedule only."""
    if not telemetry_active():
        return None
    from sentry_sdk.crons import capture_checkin
    return capture_checkin(monitor_slug=slug, status='ok' if ok else 'error', monitor_config={
        'schedule': {'type': 'interval', 'value': interval_minutes, 'unit': 'minute'},
        'checkin_margin': 5, 'max_runtime': 10, 'failure_issue_threshold': 2, 'recovery_threshold': 1,
    })


# Mail alerts -----------------------------------------------------------------
class MailAlertHandler(logging.Handler):
    """Turns the mail logger's constant alert templates into Sentry events.

    Matches on the unformatted template (`record.msg`) only. The event carries the template and, for the two
    reason templates, the reason word if it is one of MAIL_REASONS. Nothing else about the record is read.
    """

    def __init__(self, seconds=600):
        super().__init__(level=logging.INFO)
        self.seconds = seconds

    def emit(self, record):
        try:
            alert = MAIL_ALERTS.get(record.msg) if isinstance(record.msg, str) else None
            if alert is None or not telemetry_active():
                return
            slug, level = alert
            tags = {'alert': slug}
            if record.msg in _MAIL_REASON_TEMPLATES and isinstance(record.args, tuple) and record.args \
                    and record.args[0] in MAIL_REASONS:
                tags['mail_reason'] = record.args[0]
                slug = f'{slug}:{record.args[0]}'
            throttled_capture(f'mail:{slug}', message=record.msg, level=level, seconds=self.seconds, tags=tags)
        except Exception:  # noqa: BLE001 - alerting must never break the caller's log call
            pass


class MailFailureWindow:
    """Backstop: an alert when 3 or more mail tasks fail for good within 15 minutes."""

    def __init__(self, threshold=3, window=900):
        self.threshold, self.window = threshold, window
        self._times = deque()
        self._lock = threading.Lock()

    def record(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._times.append(now)
            while self._times and now - self._times[0] > self.window:
                self._times.popleft()
            tripped = len(self._times) >= self.threshold
        if tripped:
            throttled_capture('mail:task_failures', message=MAIL_TASK_FAILURES, level='error', seconds=self.window,
                              tags={'alert': 'mail_task_failures', 'mail_reason': 'task_failures'})
        return tripped


mail_failures = MailFailureWindow()


def install_mail_alerts():
    logger = logging.getLogger(MAIL_LOGGER)
    if not any(isinstance(handler, MailAlertHandler) for handler in logger.handlers):
        logger.addHandler(MailAlertHandler())


# Flask wiring ------------------------------------------------------------------
client_config = Blueprint('client_config', __name__)


@client_config.get('/client-config')
def client_config_view():
    """Public: the browser's Sentry settings (the frontend project's public DSN), or null when off."""
    frontend = current_app.extensions['jobtracker_telemetry']['frontend']
    if frontend is None:
        return jsonify(sentry=None)
    # No release: the repositories are private, so no git SHA appears in public output.
    return jsonify(sentry={'dsn': frontend['dsn'], 'environment': _environment(current_app)})


def connect_src():
    frontend = current_app.extensions['jobtracker_telemetry']['frontend']
    return f"connect-src 'self' {frontend['origin']}" if frontend else None


def init_app(app):
    frontend_dsn = _setting(app, 'SENTRY_FRONTEND_DSN')
    frontend = parse_frontend_dsn(frontend_dsn) if frontend_dsn else None
    if frontend_dsn and frontend is None:
        log.warning('SENTRY_FRONTEND_DSN is not a valid Sentry US ingest DSN; browser telemetry stays off')
    service = 'jobtracker-worker' if app.config.get('QUEUE_WORKER') else 'jobtracker-web'
    app.extensions['jobtracker_telemetry'] = {'frontend': frontend, 'service': service,
                                              'backend': init_sentry(app, service)}
    if app.extensions['jobtracker_telemetry']['backend']:
        install_mail_alerts()
    app.register_blueprint(client_config, url_prefix='/api/v1')

    @app.cli.command('telemetry-check')
    def telemetry_check():
        """Send one synthetic error full of canary strings; confirm in Sentry that none of them arrived."""
        import click
        event_id = send_check_event()
        click.echo(f'Sent check event {event_id}. In Sentry, confirm it shows no "CANARY" text anywhere.'
                   if event_id else 'Telemetry is off (no SENTRY_DSN); nothing was sent.')

    @app.before_request
    def assign_request_id():
        supplied = request.headers.get('X-Request-ID')
        g.request_id = supplied if valid_request_id(supplied) else new_request_id()
        g.request_id_token = REQUEST_ID.set(g.request_id)

    @app.teardown_request
    def clear_request_id(exc=None):
        token = g.pop('request_id_token', None)
        if token is not None:
            REQUEST_ID.reset(token)

    @app.after_request
    def attach_request_id(response):
        request_id = g.get('request_id')
        if not request_id:
            return response
        response.headers['X-Request-ID'] = request_id
        # Every response carries the header; 5xx JSON bodies also name it, for the user to quote to support.
        if response.status_code >= 500 and response.is_json and not response.is_streamed \
                and not response.direct_passthrough:
            body = response.get_json(silent=True)
            if isinstance(body, dict) and 'jsonrpc' not in body and 'request_id' not in body:
                body['request_id'] = request_id
                response.set_data(app.json.response(body).get_data())  # same encoding as jsonify
        return response
