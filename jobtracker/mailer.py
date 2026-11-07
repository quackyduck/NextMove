"""Outbound email: one interface, several backends, a shared daily quota and alerts.

Only the queue worker sends. The web process enqueues work and never needs the
mail credential; it only validates configuration. Never put a token or a link
in a queue payload (payloads are kept for days): enqueue a row id and mint the
token in the worker right before calling `send_email`, storing only its SHA-256.

Backends (MAIL_BACKEND):

* ``resend``: Resend's HTTPS API over port 443 with stdlib urllib (DigitalOcean
  blocks outbound SMTP ports). Recommended in production.
* ``smtp``: stdlib smtplib, STARTTLS or implicit TLS, any port.
* ``console``: development only; logs the masked recipient and subject (and
  the body, which holds links, only in development). Refused in production.
* ``file``: development/tests only; one 0600 .eml per message in MAIL_FILE_DIR.
* ``memory``: tests only; messages are appended to `outbox`.
* ``none``: email is off. Production refuses it while sign-up is enabled.

Every send names a category. The hard cap MAIL_DAILY_LIMIT (rolling 24 h) is
shared; ``signup`` and ``reset`` have their own limits inside it and whatever
is left is reserved for ``notice`` (password changed) and ``test``. The signup
limit keeps LEGACY_RESERVE sends for legacy accounts' verification links
(kind="existing"), so new sign-ups pause before those run out. The month is
capped too (MAIL_MONTHLY_LIMIT, from the mail_usage table).

Logging: production logs only kinds, categories, ids, the provider message id
and exception classes; never addresses, subjects, bodies, links, tokens or
keys. Alert lines use the constant templates in TEMPLATES (the telemetry alert
handler pins them), with numeric or fixed-word arguments only.
"""
from datetime import datetime, timedelta, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
import json
import logging
import os
from pathlib import Path
import re
import smtplib
import socket
import ssl
import time
from typing import NamedTuple
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit
import uuid

from flask import current_app

from . import throttle

log = logging.getLogger('jobtracker.mail')

CATEGORIES = ('signup', 'reset', 'notice', 'test')
BACKENDS = ('resend', 'smtp', 'console', 'file', 'memory', 'none')
DAY = 86400
LEGACY_RESERVE = 5
TIMEOUT_MAX = 20
DEFAULT_API_URL = 'https://api.resend.com/emails'
DEVELOPMENT_FROM = 'My Next Moov <no-reply@mynextmoov.invalid>'
LOCAL_LINK_BASE = 'http://127.0.0.1:5001'
RESPONSE_LIMIT = 4096

# Alert and status lines. Constant %-style templates: pass arguments, never pre-format.
TEMPLATES = {
    'daily_80': 'mail daily usage at 80%% (%d of %d)',
    'daily_limit': 'mail daily limit reached (%d of %d); sends deferred',
    'monthly_80': 'mail monthly usage at 80%% (%d of %d)',
    'monthly_limit': 'mail monthly limit reached (%d of %d); sends deferred',
    'share_80': 'mail %s share at 80%% (%d of %d)',
    'signup_paused': 'signup paused: %s',
    'degraded': 'mail degraded: %s',
    'recovered': 'mail recovered',
    'credentials': 'mail provider rejected credentials (HTTP %d)',
    'skipped': 'mail recipient skipped: %s',
}

# Recipient-domain caps for sign-up mail (per rolling 24 h).
DOMAIN_LIMIT = 10
BIG_DOMAIN_LIMIT = 40
MX_LIMIT = 15
BIG_DOMAINS = frozenset(('gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'live.com', 'icloud.com',
                         'me.com', 'yahoo.com', 'proton.me', 'protonmail.com'))
BIG_MX_SUFFIXES = ('google.com', 'googlemail.com', 'outlook.com', 'icloud.com', 'yahoodns.net', 'protonmail.ch')
DNS_LIFETIME = 3.0
DNS_CACHE_SECONDS = 3600
_dns_cache: dict = {}

outbox: list = []


class MailError(Exception):
    """Base class. Messages never contain bodies, recipients, provider responses or credentials."""


class PermanentMailError(MailError):
    """Do not retry: refused recipient, invalid request, rejected credentials."""


class TransientMailError(MailError):
    """Retry later: timeouts, connection and TLS errors, 4xx SMTP, 429/5xx API."""
    delay = 60


class MailQuotaExceeded(TransientMailError):
    """A daily, category or monthly limit is spent; defer (does not count as an attempt)."""
    delay = 3600


class MailNotConfigured(PermanentMailError):
    """MAIL_BACKEND=none, or the sending process has no credential."""


class Recipient(NamedTuple):
    """Result of recipient_ok: 'ok', 'dns', 'domain_cap' or 'mx_cap', plus the cap budgets a send reserves."""
    status: str
    budgets: tuple = ()


# Configuration -------------------------------------------------------------
def _env(name, default=None):
    return os.environ.get(name, default)


def _secret(name):
    path = os.environ.get(f'{name}_FILE')
    if path:
        return Path(path).read_text().strip()
    return os.environ.get(name)


SETTINGS = {
    'MAIL_BACKEND': lambda: _env('MAIL_BACKEND'),
    'MAIL_FROM': lambda: _env('MAIL_FROM'),
    'MAIL_REPLY_TO': lambda: _env('MAIL_REPLY_TO'),
    'MAIL_TIMEOUT_SECONDS': lambda: float(_env('MAIL_TIMEOUT_SECONDS', '15')),
    'MAIL_DAILY_LIMIT': lambda: int(_env('MAIL_DAILY_LIMIT', '90')),
    'MAIL_LIMIT_SIGNUP': lambda: int(_env('MAIL_LIMIT_SIGNUP', '50')),
    'MAIL_LIMIT_RESET': lambda: int(_env('MAIL_LIMIT_RESET', '25')),
    'MAIL_MONTHLY_LIMIT': lambda: int(_env('MAIL_MONTHLY_LIMIT', '3000')),
    'MAIL_API_URL': lambda: _env('MAIL_API_URL', DEFAULT_API_URL),
    'MAIL_API_KEY': lambda: _secret('MAIL_API_KEY'),
    'MAIL_HOST': lambda: _env('MAIL_HOST'),
    'MAIL_PORT': lambda: int(_env('MAIL_PORT', '587')),
    'MAIL_SECURITY': lambda: _env('MAIL_SECURITY', 'starttls'),
    'MAIL_USERNAME': lambda: _env('MAIL_USERNAME'),
    'MAIL_PASSWORD': lambda: _secret('MAIL_PASSWORD'),
    'MAIL_FILE_DIR': lambda: _env('MAIL_FILE_DIR'),
    # Recipient DNS checks: always on in production; opt-in elsewhere (test domains have no MX).
    'MAIL_CHECK_RECIPIENT_DNS': lambda: _env('MAIL_CHECK_RECIPIENT_DNS'),
}


def init_app(app):
    """Read MAIL_* settings (test_config wins over the environment), validate them and pick the backend."""
    for name, read in SETTINGS.items():
        if name not in app.config:
            app.config[name] = read()
    production = app.config['APP_ENV'] != 'development' and not app.testing
    development = app.config['APP_ENV'] == 'development'
    backend = app.config['MAIL_BACKEND']
    if not backend:
        if production:
            raise RuntimeError('Production requires MAIL_BACKEND (resend, smtp or none)')
        backend = 'memory' if app.testing else 'console'
    backend = backend.strip().lower()
    if backend not in BACKENDS:
        raise RuntimeError('MAIL_BACKEND must be one of ' + ', '.join(BACKENDS))
    if backend in ('console', 'file') and not (development or app.testing):
        raise RuntimeError(f'MAIL_BACKEND={backend} is for development only; it would expose links')
    if backend == 'memory' and not app.testing:
        raise RuntimeError('MAIL_BACKEND=memory is for tests only')
    app.config['MAIL_BACKEND'] = backend
    if backend == 'none' and production and app.config.get('SIGNUP_ENABLED', True):
        raise RuntimeError('Sign-up needs email: set MAIL_BACKEND or SIGNUP_ENABLED=0')
    timeout = app.config['MAIL_TIMEOUT_SECONDS']
    if not 1 <= timeout <= TIMEOUT_MAX:
        raise RuntimeError(f'MAIL_TIMEOUT_SECONDS must be between 1 and {TIMEOUT_MAX}')
    hard, signup, reset = (app.config[key] for key in ('MAIL_DAILY_LIMIT', 'MAIL_LIMIT_SIGNUP', 'MAIL_LIMIT_RESET'))
    if hard < 1 or signup < 1 or reset < 1 or app.config['MAIL_MONTHLY_LIMIT'] < 1:
        raise RuntimeError('Mail limits must be positive')
    if signup <= LEGACY_RESERVE:
        raise RuntimeError(f'MAIL_LIMIT_SIGNUP must exceed the {LEGACY_RESERVE}-send legacy reserve')
    if hard - signup - reset < max(1, hard // 10):
        raise RuntimeError('Mail limits must leave at least 10% of MAIL_DAILY_LIMIT for notices')
    if backend in ('console', 'file', 'memory', 'none') and not app.config['MAIL_FROM']:
        app.config['MAIL_FROM'] = DEVELOPMENT_FROM  # Local backends only; real ones need an explicit sender.
    if backend != 'none':
        _validate_sender(app.config)
    if backend in ('resend', 'smtp'):
        _validate_base_url(app.config)
    # Local backends (development, tests) without PUBLIC_BASE_URL link to the loopback dev server.
    app.config['MAIL_LINK_BASE'] = ((app.config.get('PUBLIC_BASE_URL') or '').rstrip('/') or
                                    (LOCAL_LINK_BASE + (app.config.get('APP_ROOT_PATH') or '').rstrip('/')
                                     if backend in ('console', 'file', 'memory') else ''))
    if backend == 'resend':
        url = urlsplit(app.config['MAIL_API_URL'] or '')
        if url.scheme != 'https' or not url.hostname:
            raise RuntimeError('MAIL_API_URL must be an https URL')
        if app.config.get('QUEUE_WORKER') and not app.config['MAIL_API_KEY']:
            raise RuntimeError('The worker sends mail: set MAIL_API_KEY_FILE')
    if backend == 'smtp':
        if not app.config['MAIL_HOST'] or re.search(r'\s', app.config['MAIL_HOST']):
            raise RuntimeError('MAIL_HOST is required for MAIL_BACKEND=smtp')
        if not 1 <= app.config['MAIL_PORT'] <= 65535:
            raise RuntimeError('MAIL_PORT must be between 1 and 65535')
        security = (app.config['MAIL_SECURITY'] or '').lower()
        if security not in ('starttls', 'ssl', 'none') or (security == 'none' and not development):
            raise RuntimeError('MAIL_SECURITY must be starttls or ssl (none only in development)')
        app.config['MAIL_SECURITY'] = security
        if app.config.get('QUEUE_WORKER') and app.config['MAIL_USERNAME'] and not app.config['MAIL_PASSWORD']:
            raise RuntimeError('The worker sends mail: set MAIL_PASSWORD_FILE')
    if backend == 'file':
        if not app.config['MAIL_FILE_DIR']:
            raise RuntimeError('MAIL_BACKEND=file needs MAIL_FILE_DIR')
        Path(app.config['MAIL_FILE_DIR']).mkdir(parents=True, exist_ok=True, mode=0o700)
    check = app.config['MAIL_CHECK_RECIPIENT_DNS']
    if production:
        if check is not None and str(check).strip().lower() in ('0', 'false', 'off'):
            raise RuntimeError('MAIL_CHECK_RECIPIENT_DNS cannot be disabled in production')
        app.config['MAIL_CHECK_RECIPIENT_DNS'] = True
    else:
        app.config['MAIL_CHECK_RECIPIENT_DNS'] = check is True or str(check).strip().lower() in ('1', 'true', 'on')
    if production and app.config.get('QUEUE_WORKER') and os.environ.get('WORKER_LOG_LEVEL', '').upper() == 'DEBUG':
        # Allowed (operator's choice) but flagged: DEBUG tracebacks can include exception message text.
        logging.getLogger('jobtracker.worker').warning(
            'WORKER_LOG_LEVEL=DEBUG in production: debug tracebacks may include message text; use INFO')
    app.config.setdefault('MAIL_STATUS_CACHE_SECONDS', 0 if app.testing else 30)
    app.extensions['mailer'] = {'backend': backend}
    _register_cli(app)


def _validate_sender(config):
    for key in ('MAIL_FROM', 'MAIL_REPLY_TO'):
        value = config.get(key)
        if not value:
            if key == 'MAIL_FROM':
                raise RuntimeError('MAIL_FROM is required')
            continue
        if '\r' in value or '\n' in value:
            raise RuntimeError(f'{key} must be a single line')
        _, address = parseaddr(value)
        if deliverable(address) is None:
            raise RuntimeError(f'{key} must contain a valid address')


def _validate_base_url(config):
    base = (config.get('PUBLIC_BASE_URL') or '').rstrip('/')
    parts = urlsplit(base)
    if parts.scheme != 'https' or not parts.hostname or parts.query or parts.fragment:
        raise RuntimeError('Sending mail needs PUBLIC_BASE_URL set to the public https URL')
    root = (config.get('APP_ROOT_PATH') or '').rstrip('/')
    if root and not parts.path.rstrip('/').endswith(root):
        raise RuntimeError('PUBLIC_BASE_URL must include APP_ROOT_PATH (it is never appended)')


def _config(name):
    return current_app.config[name]


def backend_name():
    return current_app.extensions['mailer']['backend']


def enabled():
    """False when email is off (MAIL_BACKEND=none)."""
    return backend_name() != 'none'


# Addresses and links --------------------------------------------------------
_LOCAL = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')


def deliverable(addr):
    """The canonical address when mail can be sent to it, else None.

    Lowercased and trimmed. Local part: RFC 5322 dot-atom only (no quotes, spaces,
    <>()[],;:\\ or doubled dots). Domain: ASCII letters, digits and hyphens, at
    least two labels, a TLD that is not all digits, no IP literals or localhost.
    Non-ASCII is refused (type the xn-- form): stdlib IDNA 2003 and IDNA 2008 differ.
    """
    if not isinstance(addr, str):
        return None
    value = addr.strip().lower()
    if len(value) > 254 or value.count('@') != 1:
        return None
    local, domain = value.split('@')
    if not 1 <= len(local) <= 64 or not _LOCAL.fullmatch(local):
        return None
    labels = domain.split('.')
    if len(labels) < 2 or len(domain) > 253 or not all(_LABEL.fullmatch(label) for label in labels):
        return None
    if labels[-1].isdigit() or domain == 'localhost' or domain.endswith('.localhost'):
        return None
    try:
        if Address(addr_spec=value).addr_spec != value:
            return None
    except (ValueError, IndexError):
        return None
    return value


def public_url(path, fragment=None):
    """PUBLIC_BASE_URL + path, optionally with a #fragment. Never from the Host header; APP_ROOT_PATH is not appended."""
    base = (current_app.config.get('PUBLIC_BASE_URL') or '').rstrip('/') or current_app.config.get('MAIL_LINK_BASE')
    if not base:
        raise MailNotConfigured('PUBLIC_BASE_URL is not configured')
    url = base + '/' + path.lstrip('/')
    if fragment:
        url += '#' + urlencode(fragment)
    return url


def mask_address(email):
    """'n•••@gmail.com' for screens and logs that must not show the whole address."""
    if not isinstance(email, str) or '@' not in email:
        return ''
    local, _, domain = email.rpartition('@')
    return f'{local[:1]}•••@{domain}'


# Quota ---------------------------------------------------------------------
def _key(name):
    return throttle.key('mail', name)


def _category_limit(category, kind=None):
    if category == 'signup':
        limit = _config('MAIL_LIMIT_SIGNUP')
        return limit if kind == 'existing' else limit - LEGACY_RESERVE
    if category == 'reset':
        return _config('MAIL_LIMIT_RESET')
    return None


def _budgets(category, kind=None):
    budgets = [(_key('global'), _config('MAIL_DAILY_LIMIT'), DAY)]
    limit = _category_limit(category, kind)
    if limit is not None:
        if category == 'signup' and kind != 'existing':
            # New sign-ups count in their own key too, so they stop LEGACY_RESERVE before the share.
            budgets.append((_key('signup-new'), limit, DAY))
            budgets.append((_key('signup'), _config('MAIL_LIMIT_SIGNUP'), DAY))
        else:
            budgets.append((_key(category), limit if category != 'signup' else _config('MAIL_LIMIT_SIGNUP'), DAY))
    return budgets


def sent_today(category=None):
    return throttle.used(_key(category or 'global'), DAY)


def _engine():
    return current_app.extensions['jobtracker_db']['engine']


def sent_this_month():
    from sqlalchemy import func, select
    from .models import MailUsage
    first = datetime.now(timezone.utc).date().replace(day=1)
    with _engine().connect() as connection:
        return connection.scalar(select(func.coalesce(func.sum(MailUsage.sent), 0)).where(MailUsage.day >= first))


def _record_usage():
    from sqlalchemy import text
    today = datetime.now(timezone.utc).date()
    with _engine().begin() as connection:
        connection.execute(text('INSERT INTO mail_usage (day, sent) VALUES (:day, 1) '
                                'ON CONFLICT (day) DO UPDATE SET sent = sent + 1'), {'day': today.isoformat()})


def has_room(category, kind=None):
    """Non-binding: whether a send in this category could be reserved now (check before minting a token)."""
    if category not in CATEGORIES:
        raise ValueError('Unknown mail category')
    try:
        if sent_this_month() >= _config('MAIL_MONTHLY_LIMIT'):
            return False
        return throttle.remaining(_budgets(category, kind))
    except throttle.Unavailable:
        return False


def _reserve(category, kind, extra=()):
    """Reserve the hard cap, the category share and any recipient-cap budgets in one atomic step."""
    hard = _config('MAIL_DAILY_LIMIT')
    month_limit = _config('MAIL_MONTHLY_LIMIT')
    if sent_this_month() >= month_limit:
        raise MailQuotaExceeded('Monthly mail limit reached')
    try:
        if not throttle.reserve(_budgets(category, kind) + list(extra)):
            if sent_today() >= hard:
                _alert('daily_limit', 'error', DAY, sent_today(), hard)
            raise MailQuotaExceeded('Daily mail limit reached')
    except throttle.Unavailable as exc:
        raise TransientMailError('Mail quota unavailable') from exc


def _after_send(category):
    """Record an accepted send; fire edge-triggered usage alerts."""
    _record_usage()
    hard, month_limit = _config('MAIL_DAILY_LIMIT'), _config('MAIL_MONTHLY_LIMIT')
    today, month = sent_today(), sent_this_month()
    if today >= hard * 0.8:
        _alert('daily_80', 'warning', DAY, today, hard)
    if month == -(-month_limit * 8 // 10):  # Exactly when the month crosses 80%: once a month.
        log.warning(TEMPLATES['monthly_80'], month, month_limit)
    if month == month_limit:
        log.error(TEMPLATES['monthly_limit'], month, month_limit)
    limit = _category_limit(category, 'existing')
    if limit is not None:
        used = sent_today(category)
        if used >= limit * 0.8:
            _alert('share_80:' + category, 'warning', DAY, category, used, limit, template='share_80')


def _alert(name, level, window, *args, template=None):
    """Log one alert line at most once per `window` seconds (deduplicated in the throttle database)."""
    try:
        if throttle.reserve([(_key('alert-' + name), 1, window)]):
            getattr(log, level)(TEMPLATES[template or name.split(':')[0]], *args)
    except throttle.Unavailable:
        pass


def _note(name):
    try:
        throttle.transaction(lambda count, record: record([_key(name)]) or True)
    except throttle.Unavailable:
        pass


def _seen(name, window):
    try:
        return throttle.used(_key(name), window)
    except throttle.Unavailable:
        return 0


def _cached(name, compute, cached):
    """Per-process cache for service-wide states read on every page load (/auth/session, challenges).

    Computing them takes several throttle-database transactions (and, while paused or degraded, an
    alert dedup write), so read-only paths reuse a value for MAIL_STATUS_CACHE_SECONDS (30 s; 0 in
    tests). Paths that change state (sending, the signup and activate POSTs) pass cached=False.
    """
    ttl = current_app.config.get('MAIL_STATUS_CACHE_SECONDS', 0)
    if not cached or ttl <= 0:
        return compute()
    store = current_app.extensions['mailer'].setdefault('cache', {})
    moment = time.monotonic()
    hit = store.get(name)
    if hit and hit[0] > moment:
        return hit[1]
    value = compute()
    store[name] = (moment + ttl, value)
    return value


def signup_paused(cached=False):
    """True when new sign-ups cannot get mail today: share, daily, monthly limit, or rejected credentials."""
    def compute():
        reason = _pause_reason()
        if reason:
            _alert('signup_paused:' + reason, 'error', 6 * 3600, reason, template='signup_paused')
        return reason is not None
    return _cached('paused', compute, cached)


def _pause_reason():
    if not enabled():
        return None
    try:
        if sent_this_month() >= _config('MAIL_MONTHLY_LIMIT'):
            return 'monthly_limit'
        if sent_today() >= _config('MAIL_DAILY_LIMIT'):
            return 'daily_limit'
        if sent_today('signup-new') >= _category_limit('signup'):
            return 'signup_share'
    except throttle.Unavailable:
        return None
    if _seen('credentials-rejected', 6 * 3600) and not _seen('ok', 600):
        return 'credentials'
    return None


def status(cached=False):
    """'off', 'degraded' or 'ok' for the whole service (never about one address)."""
    if not enabled():
        return 'off'
    return _cached('status', _status, cached)


def _status():
    reason = None
    pause = _pause_reason()
    if pause in ('monthly_limit', 'daily_limit', 'signup_share'):
        reason = 'quota'
    elif pause == 'credentials':
        reason = 'credentials'
    elif _seen('fail', 600) >= 3 and not _seen('ok', 600):
        reason = 'provider'
    elif _seen('dns-timeout', 600) >= 20:
        reason = 'dns'
    if reason:
        _alert('degraded:' + reason, 'error', 6 * 3600, reason, template='degraded')
        return 'degraded'
    return 'ok'


# Recipient checks (worker only) ---------------------------------------------
def _lookup(domain):
    """(status, primary MX host) for a domain: 'ok', 'dns' (cannot receive mail). Raises TransientMailError."""
    import dns.exception
    import dns.name
    import dns.resolver
    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_LIFETIME
    try:
        answer = resolver.resolve(domain, 'MX', raise_on_no_answer=False)
    except dns.resolver.NXDOMAIN:
        return 'dns', None
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        raise TransientMailError('DNS lookup failed') from exc
    except dns.exception.DNSException:
        return 'dns', None
    records = list(answer.rrset or [])
    if records:
        if len(records) == 1 and records[0].exchange == dns.name.root:
            return 'dns', None  # Null MX (RFC 7505): the domain accepts no mail.
        best = min(records, key=lambda record: record.preference)
        host = best.exchange.to_text(omit_final_dot=True).lower()
        return ('dns', None) if not host or host == '.' else ('ok', host)
    for rdtype in ('A', 'AAAA'):
        try:
            if resolver.resolve(domain, rdtype, raise_on_no_answer=False).rrset:
                return 'ok', domain
        except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
            raise TransientMailError('DNS lookup failed') from exc
        except dns.exception.DNSException:
            continue
    return 'dns', None


def _resolve(domain):
    cached = _dns_cache.get(domain)
    moment = time.monotonic()
    if cached and cached[0] > moment:
        return cached[1], cached[2]
    try:
        result = _lookup(domain)
    except TransientMailError:
        _note('dns-timeout')
        raise
    _dns_cache[domain] = (moment + DNS_CACHE_SECONDS, *result)
    return result


def recipient_ok(email, category):
    """Can this address receive mail, and is it within the per-domain and per-MX caps?

    Worker only, before reserving quota or minting a token. Returns a Recipient:
    status 'ok', 'dns' (no MX/A/AAAA, NXDOMAIN or null MX), 'domain_cap' or
    'mx_cap', and the cap budgets that `send_email(..., recipient=...)` reserves
    atomically with the quota. Reset mail checks DNS but skips the caps. Raises
    TransientMailError on a DNS timeout (retry, never skip). Without
    MAIL_CHECK_RECIPIENT_DNS (development, tests) DNS is not consulted.
    """
    address = deliverable(email)
    if address is None:
        return Recipient('dns')
    domain = address.rpartition('@')[2]
    host = domain
    if current_app.config.get('MAIL_CHECK_RECIPIENT_DNS'):
        state, host = _resolve(domain)
        if state != 'ok':
            _alert('skipped:dns', 'warning', 3600, 'dns', template='skipped')
            return Recipient('dns')
    if category != 'signup':
        return Recipient('ok')
    domain_budget = (_key('dom:' + domain), BIG_DOMAIN_LIMIT if domain in BIG_DOMAINS else DOMAIN_LIMIT, DAY)
    budgets = [domain_budget]
    big_mx = any(host == suffix or host.endswith('.' + suffix) for suffix in BIG_MX_SUFFIXES)
    if host and not big_mx:
        budgets.append((_key('mx:' + host), MX_LIMIT, DAY))
    try:
        if not throttle.remaining([domain_budget]):
            _alert('skipped:domain_cap', 'warning', 3600, 'domain_cap', template='skipped')
            return Recipient('domain_cap')
        if len(budgets) > 1 and not throttle.remaining(budgets[1:]):
            _alert('skipped:mx_cap', 'warning', 3600, 'mx_cap', template='skipped')
            return Recipient('mx_cap')
    except throttle.Unavailable as exc:
        raise TransientMailError('Mail quota unavailable') from exc
    return Recipient('ok', tuple(budgets))


# Sending ---------------------------------------------------------------------
def _single_line(value, name):
    if not isinstance(value, str) or '\r' in value or '\n' in value:
        raise PermanentMailError(f'Invalid {name}')
    return value


def build_message(to, subject, text, html=None):
    sender = _config('MAIL_FROM')
    message = EmailMessage()
    message['From'] = _single_line(sender, 'sender')
    message['To'] = to
    message['Subject'] = _single_line(subject, 'subject')
    reply_to = _config('MAIL_REPLY_TO')
    if reply_to:
        message['Reply-To'] = _single_line(reply_to, 'reply-to')
    message['Date'] = formatdate(usegmt=True)
    message['Message-ID'] = make_msgid(domain=parseaddr(sender)[1].rpartition('@')[2] or None)
    message['Auto-Submitted'] = 'auto-generated'
    message.set_content(text)
    if html is not None:
        message.add_alternative(html, subtype='html')
    return message


def send_email(to, subject, text, html=None, *, category, kind=None, recipient=None):
    """Send one message now (call from the worker). Raises PermanentMailError or TransientMailError.

    category: 'signup', 'reset', 'notice' or 'test'. kind='existing' lets legacy
    verification use the sign-up reserve. recipient: the Recipient from
    recipient_ok, whose cap budgets are reserved together with the quota.
    """
    if category not in CATEGORIES:
        raise ValueError('Unknown mail category')
    if not enabled():
        raise MailNotConfigured('Email is turned off')
    address = deliverable(to)
    if address is None:
        raise PermanentMailError('Undeliverable address')
    if recipient is not None and recipient.status != 'ok':
        raise PermanentMailError('Recipient refused')
    message = build_message(address, subject, text, html)
    _reserve(category, kind, recipient.budgets if recipient else ())
    try:
        provider_id = BACKEND_SENDERS[backend_name()](message, address)
    except TransientMailError:
        _note('fail')
        raise
    recovering = _seen('fail', 600) >= 3 and not _seen('ok', 600)
    _note('ok')
    if recovering:
        log.info(TEMPLATES['recovered'])
    log.info('mail sent category=%s kind=%s provider_id=%s', category, kind or '-', provider_id or '-')
    _after_send(category)


def _send_console(message, address):
    development = current_app.config['APP_ENV'] == 'development'
    log.info('mail to=%s subject=%s', mask_address(address), message['Subject'])
    if development:
        log.info('mail body (development only):\n%s', message.get_body(('plain',)).get_content())
    return None


def _send_file(message, address):
    folder = Path(_config('MAIL_FILE_DIR'))
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f'{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}-{uuid.uuid4().hex}.eml'
    target = folder / name
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(message.as_bytes())
    log.info('mail written to=%s', mask_address(address))
    return name


def _send_memory(message, address):
    outbox.append(message)
    return None


def _send_none(message, address):
    raise MailNotConfigured('Email is turned off')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never follow: the bearer key must not be re-sent elsewhere.


def _opener():
    return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def _send_resend(message, address):
    key = _config('MAIL_API_KEY')
    if not key:
        raise MailNotConfigured('MAIL_API_KEY is not configured')
    body = {'from': message['From'], 'to': [address], 'subject': message['Subject'],
            'text': message.get_body(('plain',)).get_content(),
            'headers': {'Auto-Submitted': 'auto-generated'}}
    html = message.get_body(('html',))
    if html is not None:
        body['html'] = html.get_content()
    if message['Reply-To']:
        body['reply_to'] = message['Reply-To']
    request = urllib.request.Request(_config('MAIL_API_URL'), data=json.dumps(body).encode(), method='POST', headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'User-Agent': 'mynextmoov-mailer'})
    try:
        with _opener().open(request, timeout=_config('MAIL_TIMEOUT_SECONDS')) as response:
            data = response.read(RESPONSE_LIMIT)
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        exc.close()  # The body may echo the recipient: never read or log it.
        if status_code in (401, 403):
            _note('credentials-rejected')
            _alert('credentials', 'error', 6 * 3600, status_code)
            raise PermanentMailError(f'Provider refused the credential (HTTP {status_code})') from None
        if status_code == 429 or status_code >= 500 or 300 <= status_code < 400:
            raise TransientMailError(f'Provider unavailable (HTTP {status_code})') from None
        raise PermanentMailError(f'Provider rejected the message (HTTP {status_code})') from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
        raise TransientMailError(f'Provider unreachable ({type(exc).__name__})') from None
    try:
        provider_id = json.loads(data or b'{}').get('id')
    except (ValueError, AttributeError):
        provider_id = None
    return provider_id if isinstance(provider_id, str) and len(provider_id) <= 100 else None


def _send_smtp(message, address):
    host, port = _config('MAIL_HOST'), _config('MAIL_PORT')
    security, timeout = _config('MAIL_SECURITY'), _config('MAIL_TIMEOUT_SECONDS')
    context = ssl.create_default_context()
    sender = parseaddr(message['From'])[1]
    try:
        if security == 'ssl':
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        with client:
            client.ehlo()
            if security == 'starttls':
                client.starttls(context=context)  # Raises if not offered: never fall back to plaintext.
                client.ehlo()
            if _config('MAIL_USERNAME'):
                client.login(_config('MAIL_USERNAME'), _config('MAIL_PASSWORD') or '')
            client.send_message(message, from_addr=sender, to_addrs=[address])
    except smtplib.SMTPAuthenticationError as exc:
        _note('credentials-rejected')
        _alert('credentials', 'error', 6 * 3600, exc.smtp_code)
        raise PermanentMailError('SMTP server refused the credential') from None
    except smtplib.SMTPRecipientsRefused:
        raise PermanentMailError('SMTP server refused the recipient') from None
    except smtplib.SMTPResponseException as exc:
        if 400 <= exc.smtp_code < 500:
            raise TransientMailError(f'SMTP temporary failure ({exc.smtp_code})') from None
        raise PermanentMailError(f'SMTP permanent failure ({exc.smtp_code})') from None
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise TransientMailError(f'SMTP unavailable ({type(exc).__name__})') from None
    return None


BACKEND_SENDERS = {'resend': _send_resend, 'smtp': _send_smtp, 'console': _send_console, 'file': _send_file,
                   'memory': _send_memory, 'none': _send_none}


def enqueue_notice(connection, user_id, kind, payload):
    """Queue a mail task whose payload carries no secret (e.g. a password-changed notice). Never a link with a token."""
    from . import taskqueue
    return taskqueue.enqueue(connection, user_id, kind, payload)


# CLI ---------------------------------------------------------------------------
def _register_cli(app):
    import click
    from flask.cli import AppGroup
    if 'mail' in app.cli.commands:
        return
    mail = AppGroup('mail', help='Outbound email (admin).')
    app.cli.add_command(mail)

    @mail.command('test')
    @click.argument('to')
    def mail_test(to):
        """Send a fixed message with no link through the configured backend (counts against the daily limit)."""
        text = ('This is a test message from My Next Moov. It confirms that outbound email works.\n'
                'No action is needed.\n')
        try:
            send_email(to, 'My Next Moov test message', text, category='test')
        except MailError as exc:
            raise click.ClickException(f'not sent: {type(exc).__name__}') from None
        click.echo('sent')

    @mail.command('status')
    def mail_status():
        """Counts against the limits, the current status, and queued, deferred and failed mail tasks."""
        from sqlalchemy import func, select
        from .models import EmailConfirmation, QueuedTask
        hard = _config('MAIL_DAILY_LIMIT')
        click.echo(f'backend: {backend_name()}')
        click.echo(f'status: {status()}')
        click.echo(f'signup paused: {"yes (" + _pause_reason() + ")" if _pause_reason() else "no"}')
        click.echo(f'last 24 h: {sent_today()} of {hard}')
        click.echo(f'  signup: {sent_today("signup")} of {_config("MAIL_LIMIT_SIGNUP")} '
                   f'(new sign-ups {sent_today("signup-new")} of {_category_limit("signup")})')
        click.echo(f'  reset: {sent_today("reset")} of {_config("MAIL_LIMIT_RESET")}')
        click.echo(f'this month (UTC): {sent_this_month()} of {_config("MAIL_MONTHLY_LIMIT")}')
        since = datetime.now(timezone.utc) - timedelta(days=1)
        with _engine().connect() as connection:
            for reason, count in connection.execute(
                    select(EmailConfirmation.skip_reason, func.count()).where(
                        EmailConfirmation.delivery == 'skipped', EmailConfirmation.created_at >= since)
                    .group_by(EmailConfirmation.skip_reason)):
                click.echo(f'skipped ({reason}): {count}')
            kinds = ('send_confirmation', 'resend_confirmation', 'password_reset_email', 'password_reset_request',
                     'password_changed_email')
            for state, count in connection.execute(
                    select(QueuedTask.status, func.count()).where(QueuedTask.kind.in_(kinds))
                    .group_by(QueuedTask.status)):
                click.echo(f'mail tasks {state}: {count}')
