"""Persistent, atomic attempt budgets keyed per account and per client IP.

Reserve before password hashing, including successful/malformed attempts. Do not
clear on success: concurrent successes must not reset another worker's budget.
No password, token, email or client address is stored: budget keys are HMACs
under SECRET_KEY. A separate SQLite file survives application restarts and is
shared by every web worker.

There is deliberately no global budget: in a multi-tenant app it would let a
few addresses lock every account out. Login budgets per five minutes:

* 30 per client IP (bounds one client's hashing work and password spraying);
* 10 per account (email, known or not, so 429 never reveals an account) from
  any address; once spent, a client that has made fewer than 3 attempts on
  that account in the window may still try, up to 50 per account in total. An
  attacker hammering a known email therefore cannot lock its owner out from
  their own network, while a distributed guesser needs a fresh address per 3
  guesses and stops at 50 per window.

Signed-in re-verification (password change, token creation) uses a per-user
budget only, so login abuse against an email cannot block its owner's session.

Budgets may use their own window, up to MAX_WINDOW_SECONDS (24 h): `reserve`
takes (key, limit) pairs at the default 5-minute window, or (key, limit,
window) triples. `transaction` is the only place attempts are pruned, at the
longest window, so short and long budgets share one table safely.

Client addresses are grouped before keying: IPv6 by /64 (one host's prefix is
one client), IPv4-mapped IPv6 as its IPv4 address. `address_key` gives the
canonical form of an email for per-address budgets only (never identity).
"""
from contextlib import closing
import hashlib
import hmac
import ipaddress
from pathlib import Path
import sqlite3
import time

from flask import current_app, request

WINDOW_SECONDS = 300
MAX_ATTEMPTS = 10
MAX_CLEAN_CLIENT_ATTEMPTS = 50
CLEAN_CLIENT_TRIES = 3
MAX_IP_ATTEMPTS = 30
SEARCH_WINDOW_SECONDS = 60
SEARCH_ATTEMPTS = 120
MAX_WINDOW_SECONDS = 86400
DAY_SECONDS = 86400
# One budget across every unauthenticated request that sends mail (signup, activation, resend).
MAIL_TRIGGER_IP_ATTEMPTS = 5
MAIL_TRIGGER_IP_DAILY = 8
# Gmail ignores dots in the local part and treats googlemail.com as gmail.com.
GMAIL_DOMAINS = ('gmail.com', 'googlemail.com')


class Unavailable(Exception):
    pass


def init_app(app):
    configured = app.config.get('LOGIN_THROTTLE_DATABASE')
    path = Path(configured) if configured else Path(app.config['UPLOAD_FOLDER']).parent / 'login-throttle.sqlite3'
    if str(path) == ':memory:':
        raise RuntimeError('Login throttle requires a persistent SQLite file')
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation gives a new file private permissions before SQLite opens
    # it. Existing files are retained, including their active login budget.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        # attempts/search_attempts are pre-schema-9 tables, kept so older builds still start.
        connection.execute('CREATE TABLE IF NOT EXISTS attempts (timestamp REAL NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS search_attempts (timestamp REAL NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS keyed_attempts (key TEXT NOT NULL, timestamp REAL NOT NULL)')
        connection.execute('CREATE INDEX IF NOT EXISTS keyed_attempts_key ON keyed_attempts (key, timestamp)')
        connection.commit()
    app.extensions['login_throttle_path'] = path


def key(kind, value):
    secret = current_app.config['SECRET_KEY'].encode()
    return kind + ':' + hmac.new(secret, f'{kind}\0{value}'.encode(), hashlib.sha256).hexdigest()[:32]


def group_address(value):
    """One client per IPv4 address or IPv6 /64; IPv4-mapped IPv6 counts as its IPv4 address.

    A missing or malformed value (never expected behind Caddy and ProxyFix, where remote_addr is always
    an IP) is keyed by the raw value itself, capped at 64 characters, under a 'raw:' prefix that no real
    address can produce. So misparsed clients never pool into one shared budget that one of them could
    exhaust for all, and a fixed bad value still gets exactly one budget of its own.
    """
    try:
        address = ipaddress.ip_address((value or '').split('%', 1)[0])
    except ValueError:
        return 'raw:' + (value or 'none')[:64]
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network(f'{address}/64', strict=False))
    return str(address)


def client_address():
    # With TRUST_PROXY, ProxyFix sets remote_addr from the single trusted proxy.
    return group_address(request.remote_addr)


def address_key(email):
    """Canonical address for per-address budgets: lowercase, no +tag, and no dots for Gmail.

    Only for throttling: `a.b+x@googlemail.com` and `ab@gmail.com` share a budget,
    while account identity and the address mail goes to stay exactly as normalized.
    """
    value = (email or '').strip().lower()
    local, at, domain = value.rpartition('@')
    if not at:
        return value
    local = local.split('+', 1)[0]
    if domain in GMAIL_DOMAINS:
        local, domain = local.replace('.', ''), 'gmail.com'
    return f'{local}@{domain}'


def transaction(decide):
    """Run decide(count, record) atomically; count(key, window) and record(keys).

    The single pruning path: attempts older than MAX_WINDOW_SECONDS are deleted;
    count() still only sees each budget's own window.
    """
    try:
        with closing(sqlite3.connect(current_app.extensions['login_throttle_path'], timeout=5)) as connection:
            connection.execute('PRAGMA secure_delete=ON')  # Schema 17: pruned attempts don't linger in free pages.
            # Serialize pruning, counting, and reservation across processes. The
            # password hash runs AFTER commit, so slow hashes never hold a lock.
            connection.execute('BEGIN IMMEDIATE')
            now = time.time()
            # Future timestamps also count: clock rollback must not reset budget.
            connection.execute('DELETE FROM keyed_attempts WHERE timestamp <= ?', (now - MAX_WINDOW_SECONDS,))

            def count(name, window=WINDOW_SECONDS):
                _check_window(window)
                return connection.execute('SELECT COUNT(*) FROM keyed_attempts WHERE key = ? AND timestamp > ?',
                                          (name, now - window)).fetchone()[0]

            def record(names):
                connection.executemany('INSERT INTO keyed_attempts (key, timestamp) VALUES (?, ?)',
                                       [(name, now) for name in names])
            allowed = decide(count, record)
            connection.commit()
            return allowed
    except sqlite3.Error as exc:
        # Fail closed; do not include paths or database diagnostics in responses.
        raise Unavailable() from exc


_transaction = transaction  # Older name, kept for callers outside this module.


def _check_window(window):
    if not 0 < window <= MAX_WINDOW_SECONDS:
        raise ValueError(f'Throttle windows must be between 1 and {MAX_WINDOW_SECONDS} seconds')


def _budgets(budgets, window):
    """Normalize (key, limit) and (key, limit, window) entries to triples."""
    result = []
    for budget in budgets:
        name, limit, *rest = budget
        span = rest[0] if rest else window
        _check_window(span)
        result.append((name, limit, span))
    return result


def reserve(budgets, window=WINDOW_SECONDS):
    """Reserve one attempt in every budget atomically, or none.

    Each budget is (key, limit), using `window`, or (key, limit, window).
    """
    budgets = _budgets(budgets, window)

    def decide(count, record):
        allowed = all(count(name, span) < limit for name, limit, span in budgets)
        if allowed:
            record([name for name, _, _ in budgets])
        return allowed
    return transaction(decide)


def remaining(budgets, window=WINDOW_SECONDS):
    """Whether every budget still has room, without recording an attempt (non-binding)."""
    budgets = _budgets(budgets, window)
    return transaction(lambda count, record: all(count(name, span) < limit for name, limit, span in budgets))


def used(name, window=WINDOW_SECONDS):
    """Attempts recorded against one key within `window` (for status reports)."""
    _check_window(window)
    return transaction(lambda count, record: count(name, window))


def reserve_login(email):
    """Login budget for a normalized email (None when malformed) from this client."""
    ip = key('ip', client_address())
    account = key('account', email) if email is not None else None
    pair = key('account-ip', f'{email}\0{client_address()}') if email is not None else None

    def decide(count, record):
        if count(ip) >= MAX_IP_ATTEMPTS:
            return False
        if account is not None:
            used = count(account)
            clean_client = count(pair) < CLEAN_CLIENT_TRIES
            if used >= MAX_CLEAN_CLIENT_ATTEMPTS or (used >= MAX_ATTEMPTS and not clean_client):
                return False
        record([name for name in (ip, account, pair) if name])
        return True
    return transaction(decide)


def reserve_verification(user_id):
    """Re-verifying a signed-in user's password (password change, token creation)."""
    return reserve([(key('verify', user_id), MAX_ATTEMPTS)])


def reserve_signup():
    """Signup and activation: the shared per-IP mail-trigger budget plus the general IP budget."""
    return reserve(mail_trigger_budgets() + [(key('ip', client_address()), MAX_IP_ATTEMPTS)])


def mail_trigger_budgets():
    """Signup, activation and resend share one per-IP budget: 5 per 5 minutes and 8 per day."""
    name = key('mail-trigger-ip', client_address())
    return [(name, MAIL_TRIGGER_IP_ATTEMPTS, WINDOW_SECONDS), (name + ':day', MAIL_TRIGGER_IP_DAILY, DAY_SECONDS)]


def reserve_mail_trigger():
    """Resend: the shared per-IP mail-trigger budget only (no account lookup happens)."""
    return reserve(mail_trigger_budgets())


def reserve_per_address(kind, email, limit=3, clean_limit=6, clean_tries=2, window=DAY_SECONDS):
    """Per-address budget on the canonical address, with a clean-client allowance.

    `limit` attempts from any client per window; once spent, a client that has
    made fewer than `clean_tries` attempts on that address may still go, up to
    `clean_limit`. So a stranger hammering an address cannot stop its owner.
    Returns True when allowed (and records it), False otherwise. Callers keep
    their response identical either way.
    """
    canonical = address_key(email)
    address = key(kind, canonical)
    pair = key(kind + '-client', f'{canonical}\0{client_address()}')

    def decide(count, record):
        used_count = count(address, window)
        clean = count(pair, window) < clean_tries
        if used_count >= clean_limit or (used_count >= limit and not clean):
            return False
        record([address, pair])
        return True
    return transaction(decide)


def reserve_search(user_id=None):
    """Search work per account after authentication, or per IP before it."""
    name = key('search-user', user_id) if user_id is not None else key('search-ip', client_address())
    return reserve([(name, SEARCH_ATTEMPTS)], SEARCH_WINDOW_SECONDS)
