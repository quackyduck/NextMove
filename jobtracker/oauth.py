"""OAuth 2.1 authorization server for third-party agents (MCP clients and API callers).

Public clients register themselves (RFC 7591), then send the user here to sign in
and approve scopes. Codes require PKCE S256 and are single-use; refresh tokens
rotate, and presenting a used one revokes the whole grant (replay defense). Only
SHA-256 hashes of codes and tokens persist. A grant is one user's consent to one
client: the connection they see under Agent access and can revoke at any time.

Metadata lives at the app root (RFC 8414 / RFC 9728). With APP_ROOT_PATH, the
path-inserted well-known URLs need the proxy mapping in deploy/nginx-nextmove.conf.
"""
from base64 import urlsafe_b64encode
from datetime import timedelta, timezone
from functools import wraps
import hashlib
import hmac
from html import escape
import re
import secrets
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from flask import Blueprint, current_app, g, jsonify, redirect, request, session
from sqlalchemy import delete, insert, select, update

from . import throttle
from .models import OAUTH_SCOPES, OAuthClient, OAuthCode, OAuthGrant, OAuthToken, User, new_id, now

oauth = Blueprint('oauth', __name__)
ACCESS_PREFIX = 'nm_oat_'
REFRESH_PREFIX = 'nm_ort_'
CODE_SECONDS = 300
ACCESS_SECONDS = 3600
REFRESH_DAYS = 30
REGISTRATION_ATTEMPTS = 10
SCOPE_LABELS = {
    'jobs:read': 'See your saved roles, notes, contacts and history',
    'jobs:write': 'Add roles, move them between stages, and add notes and contacts',
    'resumes:read': 'Read and download your resumes',
    'resumes:write': 'Upload new resume versions and create, rename or choose the default resume template '
                     '(nothing is ever deleted)',
}
# Schemes that could run code or read local files if used as a redirect.
UNSAFE_SCHEMES = {'javascript', 'data', 'file', 'vbscript', 'about', 'blob', 'filesystem'}
LOOPBACK = {'localhost', '127.0.0.1', '::1'}
VERIFIER = re.compile(r'[A-Za-z0-9._~-]{43,128}')
CHALLENGE = re.compile(r'[A-Za-z0-9_-]{43}')
CORS_PATHS = ('/.well-known/', '/oauth/register', '/oauth/token', '/oauth/revoke')


def _engine():
    return current_app.extensions['jobtracker_db']['engine']


def _digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _iso(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat().replace('+00:00', 'Z') if value else None


def base_url():
    """Issuer and resource base: PUBLIC_BASE_URL, else this request's origin and mount."""
    configured = current_app.config.get('PUBLIC_BASE_URL', '').rstrip('/')
    return configured or request.host_url.rstrip('/') + request.script_root


def resource_metadata_url():
    return base_url() + '/.well-known/oauth-protected-resource/mcp'


def challenge_header(error=None, scope=None):
    parts = [f'resource_metadata="{resource_metadata_url()}"']
    if error:
        parts.append(f'error="{error}"')
    parts.append(f'scope="{scope or " ".join(OAUTH_SCOPES)}"')
    return 'Bearer ' + ', '.join(parts)


def _transport_allowed():
    return current_app.config['APP_ENV'] == 'development' or request.is_secure


def _oauth_error(error, description, status=400):
    response = jsonify(error=error, error_description=description)
    response.status_code = status
    return response


def parse_scope(value):
    """A sorted scope list, or None when any requested scope is unknown."""
    requested = set((value or '').split())
    if not requested:
        return list(OAUTH_SCOPES)
    return sorted(requested) if requested <= set(OAUTH_SCOPES) else None


def _resource_allowed(value):
    if not value:
        return True
    base = base_url()
    return value.rstrip('/') in {base, base + '/mcp', base + '/api/v1'}


# Discovery ----------------------------------------------------------------
@oauth.get('/.well-known/oauth-authorization-server')
def authorization_server_metadata():
    base = base_url()
    return jsonify(
        issuer=base,
        authorization_endpoint=base + '/oauth/authorize',
        token_endpoint=base + '/oauth/token',
        registration_endpoint=base + '/oauth/register',
        revocation_endpoint=base + '/oauth/revoke',
        response_types_supported=['code'],
        grant_types_supported=['authorization_code', 'refresh_token'],
        code_challenge_methods_supported=['S256'],
        token_endpoint_auth_methods_supported=['none'],
        revocation_endpoint_auth_methods_supported=['none'],
        scopes_supported=list(OAUTH_SCOPES),
        authorization_response_iss_parameter_supported=True,
        service_documentation=base + '/api/v1/openapi.json',
    )


@oauth.get('/.well-known/oauth-protected-resource')
@oauth.get('/.well-known/oauth-protected-resource/mcp')
@oauth.get('/.well-known/oauth-protected-resource/api/v1')
def protected_resource_metadata():
    base = base_url()
    suffix = request.path.removeprefix('/.well-known/oauth-protected-resource')
    return jsonify(
        resource=base + suffix,
        authorization_servers=[base],
        scopes_supported=list(OAUTH_SCOPES),
        bearer_methods_supported=['header'],
        resource_name='My Next Moov',
        resource_documentation=base + '/api/v1/openapi.json',
    )


# Dynamic client registration (RFC 7591) ----------------------------------
def _redirect_problem(uri):
    if not isinstance(uri, str) or not 1 <= len(uri) <= 2000 or re.search(r'[\x00-\x20\x7f]', uri):
        return 'Each redirect URI must be a URL without spaces'
    try:
        parts = urlsplit(uri)
    except ValueError:
        return 'Invalid redirect URI'
    scheme = parts.scheme.lower()
    if parts.fragment or '#' in uri:
        return 'Redirect URIs must not contain a fragment'
    if scheme == 'https':
        return None if parts.hostname else 'Invalid redirect URI'
    if scheme == 'http':
        # RFC 8252: native apps may listen on loopback with any port.
        return None if parts.hostname in LOOPBACK else 'HTTP redirect URIs must use a loopback host'
    if not scheme or scheme in UNSAFE_SCHEMES or not re.fullmatch(r'[a-z][a-z0-9+.-]*', scheme):
        return 'Unsupported redirect URI scheme'
    return None  # A private-use scheme for a native app, e.g. cursor://


@oauth.post('/oauth/register')
def register():
    try:
        if not throttle.reserve([(throttle.key('oauth-register-ip', throttle.client_address()), REGISTRATION_ATTEMPTS)]):
            response = _oauth_error('invalid_request', 'Too many registrations; try again later', 429)
            response.headers['Retry-After'] = str(throttle.WINDOW_SECONDS)
            return response
    except throttle.Unavailable:
        return _oauth_error('temporarily_unavailable', 'Registration temporarily unavailable', 503)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _oauth_error('invalid_client_metadata', 'A JSON object is required')
    uris = data.get('redirect_uris')
    if not isinstance(uris, list) or not 1 <= len(uris) <= 5:
        return _oauth_error('invalid_redirect_uri', 'Provide 1–5 redirect_uris')
    for uri in uris:
        problem = _redirect_problem(uri)
        if problem:
            return _oauth_error('invalid_redirect_uri', problem)
    grant_types = data.get('grant_types', ['authorization_code', 'refresh_token'])
    if not isinstance(grant_types, list) or not set(grant_types) <= {'authorization_code', 'refresh_token'}:
        return _oauth_error('invalid_client_metadata', 'Only authorization_code and refresh_token grants are supported')
    if data.get('response_types', ['code']) != ['code']:
        return _oauth_error('invalid_client_metadata', 'Only the code response type is supported')
    name = data.get('client_name', '')
    if not isinstance(name, str):
        return _oauth_error('invalid_client_metadata', 'client_name must be text')
    name = ''.join(char for char in name if char.isprintable()).strip()[:200] or 'Unnamed agent'
    client_id = 'nmc_' + secrets.token_urlsafe(24)
    created = now()
    with _engine().begin() as connection:
        connection.execute(insert(OAuthClient).values(id=client_id, name=name, redirect_uris=uris, created_at=created))
    # Every client is public: a requested secret-based method is overridden (RFC 7591 §3.2.1).
    return jsonify(client_id=client_id, client_id_issued_at=int(created.timestamp()), client_name=name,
                   redirect_uris=uris, grant_types=['authorization_code', 'refresh_token'],
                   response_types=['code'], token_endpoint_auth_method='none'), 201


# Authorization ------------------------------------------------------------
def _client(client_id):
    if not isinstance(client_id, str) or not client_id:
        return None
    with _engine().connect() as connection:
        return connection.execute(select(OAuthClient.__table__).where(OAuthClient.id == client_id)).mappings().first()


def _authorization_request(params):
    """(request, None) when valid; (None, response) when the user must see an error or be redirected."""
    client = _client(params.get('client_id'))
    if client is None:
        return None, _page('This app is not registered', '<p>The link that brought you here is invalid. '
                           'Start the connection again from your agent.</p>', 400)
    redirect_uri = params.get('redirect_uri') or (client['redirect_uris'][0] if len(client['redirect_uris']) == 1 else '')
    if redirect_uri not in client['redirect_uris']:
        return None, _page('Redirect address mismatch', '<p>This request does not match the app\'s registered '
                           'address, so it was stopped. Start the connection again from your agent.</p>', 400)
    state = params.get('state')

    def fail(error, description):
        query = {'error': error, 'error_description': description, 'iss': base_url()}
        if state:
            query['state'] = state
        return None, redirect(_append(redirect_uri, query))
    if params.get('response_type') != 'code':
        return fail('unsupported_response_type', 'Only response_type=code is supported')
    if params.get('code_challenge_method') != 'S256' or not CHALLENGE.fullmatch(params.get('code_challenge') or ''):
        return fail('invalid_request', 'PKCE with code_challenge_method=S256 is required')
    scopes = parse_scope(params.get('scope'))
    if scopes is None:
        return fail('invalid_scope', 'Unknown scope requested')
    if not _resource_allowed(params.get('resource')):
        return fail('invalid_target', 'Unknown resource')
    return SimpleNamespace(client=client, redirect_uri=redirect_uri, state=state, scopes=scopes,
                           code_challenge=params['code_challenge']), None


def _append(uri, query):
    return uri + ('&' if '?' in uri else '?') + urlencode(query)


def _signed_in():
    from .auth import browser_authenticated
    from .credentials import Unavailable
    try:
        return browser_authenticated()
    except Unavailable:
        return False


@oauth.get('/oauth/authorize')
def authorize():
    if not _transport_allowed():
        return _page('HTTPS required', '<p>Open this page over HTTPS.</p>', 403)
    params = request.args.to_dict()
    pending, failure = _authorization_request(params)
    if failure is not None:
        return failure
    if not _signed_in():
        # The dashboard signs in, then returns here (see oauthReturn in main.jsx).
        back = 'oauth/authorize?' + request.query_string.decode()
        return redirect(f'{request.script_root}/?' + urlencode({'signin': '', 'oauth_return': back}))
    return _consent(pending, params)


@oauth.post('/oauth/authorize')
def authorize_decision():
    if not _transport_allowed():
        return _page('HTTPS required', '<p>Open this page over HTTPS.</p>', 403)
    from .auth import same_origin
    if not same_origin() or not _signed_in():
        return _page('Sign in again', '<p>Your session ended. Start the connection again from your agent.</p>', 403)
    supplied, expected = request.form.get('csrf_token', ''), session.get('csrf_token', '')
    if not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        return _page('Sign in again', '<p>This form expired. Start the connection again from your agent.</p>', 403)
    params = {key: request.form.get(key, '') for key in
              ('client_id', 'redirect_uri', 'response_type', 'code_challenge', 'code_challenge_method',
               'scope', 'state', 'resource')}
    pending, failure = _authorization_request(params)
    if failure is not None:
        return failure
    response = {'iss': base_url()}
    if pending.state:
        response['state'] = pending.state
    chosen = sorted(set(request.form.getlist('scopes')) & set(pending.scopes))
    if request.form.get('decision') != 'allow' or not chosen:
        return redirect(_append(pending.redirect_uri, {**response, 'error': 'access_denied'}))
    code = secrets.token_urlsafe(32)
    with _engine().begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        connection.execute(delete(OAuthCode).where(OAuthCode.expires_at < now() - timedelta(days=1)))
        grants = OAuthGrant.__table__
        # One live connection per user and client; approving again replaces its scopes.
        grant_id = connection.scalar(select(grants.c.id).where(
            grants.c.user_id == g.user_id, grants.c.client_id == pending.client['id'], grants.c.revoked_at.is_(None)))
        if grant_id:
            connection.execute(update(grants).where(grants.c.id == grant_id).values(scopes=chosen))
        else:
            grant_id = new_id()
            connection.execute(insert(grants).values(id=grant_id, user_id=g.user_id, client_id=pending.client['id'],
                                                     scopes=chosen, created_at=now()))
        connection.execute(insert(OAuthCode).values(
            code_hash=_digest(code), grant_id=grant_id, redirect_uri=pending.redirect_uri,
            code_challenge=pending.code_challenge, expires_at=now() + timedelta(seconds=CODE_SECONDS)))
    return redirect(_append(pending.redirect_uri, {**response, 'code': code}))


def _consent(pending, params):
    destination = urlsplit(pending.redirect_uri)
    where = destination.hostname or destination.scheme + ':'
    boxes = ''.join(
        f'<label class="scope"><input type="checkbox" name="scopes" value="{escape(scope)}" checked> '
        f'<span><strong>{escape(scope)}</strong><br>{escape(SCOPE_LABELS[scope])}</span></label>'
        for scope in pending.scopes)
    hidden = ''.join(f'<input type="hidden" name="{escape(key)}" value="{escape(params.get(key, ""))}">'
                     for key in ('client_id', 'response_type', 'code_challenge', 'code_challenge_method',
                                 'scope', 'state', 'resource'))
    hidden += f'<input type="hidden" name="redirect_uri" value="{escape(pending.redirect_uri)}">'
    hidden += f'<input type="hidden" name="csrf_token" value="{escape(session["csrf_token"])}">'
    email = escape(g.credential.email or '')
    body = (f'<p><strong>{escape(pending.client["name"])}</strong> wants to connect to your My Next Moov '
            f'account <strong>{email}</strong>.</p>'
            f'<p class="muted">The app named itself; My Next Moov has not verified it. After you approve, '
            f'you return to <strong>{escape(where)}</strong>.</p>'
            f'<form method="post">{hidden}<fieldset><legend>Allow it to:</legend>{boxes}</fieldset>'
            f'<p class="muted">It can never delete anything, change your password or create tokens. '
            f'Revoke access anytime under Agent access.</p>'
            f'<div class="actions"><button name="decision" value="deny" class="secondary">Deny</button>'
            f'<button name="decision" value="allow">Allow</button></div></form>')
    return _page('Connect an agent', body)


def _page(title, body, status=200):
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} · My Next Moov</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;background:#f6f5f2;color:#1d1d1b;margin:0;padding:16px}}
main{{max-width:440px;margin:48px auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 3px #0002}}
h1{{font-size:1.3rem;margin-top:0}} .muted{{color:#5f5f5a;font-size:.9rem}}
fieldset{{border:0;padding:0;margin:16px 0}} legend{{font-weight:600;margin-bottom:8px}}
.scope{{display:flex;gap:10px;align-items:flex-start;padding:8px 0}} .scope input{{margin-top:4px}}
.actions{{display:flex;gap:12px;justify-content:flex-end}}
button{{font:inherit;padding:8px 18px;border-radius:8px;border:1px solid #1d1d1b;background:#1d1d1b;color:#fff;cursor:pointer}}
button.secondary{{background:#fff;color:#1d1d1b}}
@media (prefers-color-scheme:dark){{body{{background:#161615;color:#ecebe6}}main{{background:#232321;box-shadow:none}}
.muted{{color:#a9a8a1}}button{{background:#ecebe6;color:#161615;border-color:#ecebe6}}button.secondary{{background:#232321;color:#ecebe6}}}}
</style></head><body><main><h1>{escape(title)}</h1>{body}</main></body></html>'''
    return html, status, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'}


# Tokens -------------------------------------------------------------------
def _issue(connection, grant_id, scopes):
    access, refresh = ACCESS_PREFIX + secrets.token_urlsafe(32), REFRESH_PREFIX + secrets.token_urlsafe(32)
    issued = now()
    connection.execute(delete(OAuthToken).where(OAuthToken.expires_at < issued - timedelta(days=1)))
    connection.execute(insert(OAuthToken), [
        dict(id=new_id(), grant_id=grant_id, kind='access', token_hash=_digest(access), created_at=issued,
             expires_at=issued + timedelta(seconds=ACCESS_SECONDS)),
        dict(id=new_id(), grant_id=grant_id, kind='refresh', token_hash=_digest(refresh), created_at=issued,
             expires_at=issued + timedelta(days=REFRESH_DAYS)),
    ])
    return {'access_token': access, 'token_type': 'Bearer', 'expires_in': ACCESS_SECONDS,
            'refresh_token': refresh, 'scope': ' '.join(scopes)}


def _revoke_grant(connection, grant_id):
    connection.execute(update(OAuthGrant).where(OAuthGrant.id == grant_id, OAuthGrant.revoked_at.is_(None))
                       .values(revoked_at=now()))


@oauth.post('/oauth/token')
def token():
    if not _transport_allowed():
        return _oauth_error('invalid_request', 'HTTPS required', 403)
    form = request.form
    kind, client_id = form.get('grant_type'), form.get('client_id', '')
    if kind == 'authorization_code':
        result = _exchange_code(form, client_id)
    elif kind == 'refresh_token':
        result = _refresh(form, client_id)
    else:
        return _oauth_error('unsupported_grant_type', 'Use authorization_code or refresh_token')
    if isinstance(result, tuple):
        return _oauth_error(*result)
    response = jsonify(result)
    response.headers['Pragma'] = 'no-cache'
    return response


def _exchange_code(form, client_id):
    code, verifier = form.get('code', ''), form.get('code_verifier', '')
    if not code or not VERIFIER.fullmatch(verifier):
        return 'invalid_request', 'code and a valid code_verifier are required'
    if not _resource_allowed(form.get('resource')):
        return 'invalid_target', 'Unknown resource'
    grants, codes = OAuthGrant.__table__, OAuthCode.__table__
    with _engine().begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        row = connection.execute(select(codes, grants.c.client_id, grants.c.scopes, grants.c.revoked_at)
                                 .join(grants, grants.c.id == codes.c.grant_id)
                                 .where(codes.c.code_hash == _digest(code))).mappings().first()
        if row is None or row['client_id'] != client_id:
            return 'invalid_grant', 'Unknown authorization code'
        if row['used_at'] is not None:
            # A replayed code means it leaked: everything issued from this consent is revoked.
            _revoke_grant(connection, row['grant_id'])
            return 'invalid_grant', 'Authorization code already used'
        connection.execute(update(codes).where(codes.c.code_hash == row['code_hash']).values(used_at=now()))
        if row['expires_at'].replace(tzinfo=timezone.utc) <= now() or row['revoked_at'] is not None:
            return 'invalid_grant', 'Authorization code expired'
        if form.get('redirect_uri', row['redirect_uri']) != row['redirect_uri']:
            return 'invalid_grant', 'redirect_uri does not match the authorization request'
        expected = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        if not hmac.compare_digest(expected, row['code_challenge']):
            return 'invalid_grant', 'PKCE verification failed'
        return _issue(connection, row['grant_id'], row['scopes'])


def _refresh(form, client_id):
    raw = form.get('refresh_token', '')
    if not raw.startswith(REFRESH_PREFIX):
        return 'invalid_grant', 'Unknown refresh token'
    grants, tokens = OAuthGrant.__table__, OAuthToken.__table__
    with _engine().begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        row = connection.execute(select(tokens, grants.c.client_id, grants.c.scopes, grants.c.revoked_at)
                                 .join(grants, grants.c.id == tokens.c.grant_id)
                                 .where(tokens.c.token_hash == _digest(raw), tokens.c.kind == 'refresh')).mappings().first()
        if row is None or row['client_id'] != client_id or row['revoked_at'] is not None:
            return 'invalid_grant', 'Unknown refresh token'
        if row['used_at'] is not None:
            _revoke_grant(connection, row['grant_id'])  # Rotation replay: assume theft.
            return 'invalid_grant', 'Refresh token already used; reconnect the agent'
        if row['expires_at'].replace(tzinfo=timezone.utc) <= now():
            return 'invalid_grant', 'Refresh token expired; reconnect the agent'
        scopes = row['scopes']
        if form.get('scope'):
            narrowed = parse_scope(form['scope'])
            if narrowed is None or not set(narrowed) <= set(scopes):
                return 'invalid_scope', 'A refresh cannot add scopes'
        connection.execute(update(tokens).where(tokens.c.id == row['id']).values(used_at=now()))
        return _issue(connection, row['grant_id'], scopes)


@oauth.post('/oauth/revoke')
def revoke():
    raw, client_id = request.form.get('token', ''), request.form.get('client_id', '')
    grants, tokens = OAuthGrant.__table__, OAuthToken.__table__
    with _engine().begin() as connection:
        row = connection.execute(select(tokens.c.grant_id, grants.c.client_id)
                                 .join(grants, grants.c.id == tokens.c.grant_id)
                                 .where(tokens.c.token_hash == _digest(raw))).first()
        # Revoking either token disconnects the agent. Unknown tokens still get 200 (RFC 7009).
        if row is not None and row.client_id == client_id:
            _revoke_grant(connection, row.grant_id)
    return '', 200


# Resource-server side -----------------------------------------------------
def authenticate_access(raw):
    """SimpleNamespace(user_id, grant_id, scopes, client_name) for a live access token, else None."""
    if not isinstance(raw, str) or not raw.startswith(ACCESS_PREFIX) or not 40 <= len(raw) <= 256:
        return None
    grants, tokens, clients = OAuthGrant.__table__, OAuthToken.__table__, OAuthClient.__table__
    live = (tokens.c.token_hash == _digest(raw), tokens.c.kind == 'access', tokens.c.expires_at > now(),
            grants.c.revoked_at.is_(None))
    with _engine().connect() as connection:
        row = connection.execute(
            select(grants.c.id, grants.c.user_id, grants.c.scopes, grants.c.last_used_at, clients.c.name)
            .select_from(tokens.join(grants, grants.c.id == tokens.c.grant_id).join(clients, clients.c.id == grants.c.client_id)
                         .join(User.__table__, User.__table__.c.id == grants.c.user_id))
            .where(*live)).first()
    if row is None:
        return None
    if row.last_used_at is None or row.last_used_at.replace(tzinfo=timezone.utc) < now() - timedelta(seconds=60):
        with _engine().begin() as connection:
            connection.execute(update(grants).where(grants.c.id == row.id).values(last_used_at=now()))
    return SimpleNamespace(user_id=row.user_id, grant_id=row.id, scopes=set(row.scopes), client_name=row.name)


def grant_scopes(grant_id):
    """(user_id, scopes) of a live grant, else None; for signed links issued under a grant."""
    with _engine().connect() as connection:
        row = connection.execute(select(OAuthGrant.user_id, OAuthGrant.scopes).where(
            OAuthGrant.id == grant_id, OAuthGrant.revoked_at.is_(None))).first()
    return (row.user_id, set(row.scopes)) if row else None


def bearer_failure(status, error, description, scope=None):
    response = jsonify(error=description)
    response.status_code = status
    response.headers['WWW-Authenticate'] = challenge_header(error, scope)
    return response


def require_oauth(scope):
    """Accept only an OAuth access token carrying `scope`; sets g.user_id and g.oauth."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not _transport_allowed():
                return jsonify(error='HTTPS required'), 403
            scheme, _, raw = request.headers.get('Authorization', '').partition(' ')
            access = authenticate_access(raw) if scheme.lower() == 'bearer' else None
            if access is None:
                return bearer_failure(401, 'invalid_token' if raw else None, 'Authentication required')
            if scope is not None and scope not in access.scopes:
                return bearer_failure(403, 'insufficient_scope', f'This connection was not granted {scope}', scope)
            g.user_id, g.auth_type, g.oauth = access.user_id, 'oauth', access
            return view(*args, **kwargs)
        return wrapped
    return decorator


# Connections (browser session only; routes in api.py) ---------------------
def list_connections(user_id):
    grants, clients = OAuthGrant.__table__, OAuthClient.__table__
    with _engine().connect() as connection:
        rows = connection.execute(select(grants, clients.c.name).join(clients, clients.c.id == grants.c.client_id)
                                  .where(grants.c.user_id == user_id, grants.c.revoked_at.is_(None))
                                  .order_by(grants.c.created_at.desc())).mappings().all()
    return [{'id': row['id'], 'client_name': row['name'], 'scopes': row['scopes'],
             'created_at': _iso(row['created_at']), 'last_used_at': _iso(row['last_used_at'])} for row in rows]


def disconnect(user_id, grant_id):
    """Revoke a connection and every token issued under it; idempotent. False when not this user's."""
    with _engine().begin() as connection:
        found = connection.scalar(select(OAuthGrant.id).where(OAuthGrant.id == grant_id, OAuthGrant.user_id == user_id))
        if found is None:
            return False
        _revoke_grant(connection, grant_id)
    return True


@oauth.after_app_request
def cors(response):
    """Browser-based MCP clients discover and exchange tokens cross-origin. No cookies are honored there."""
    path = request.path
    if path.startswith('/oauth/'):
        response.headers['Cache-Control'] = 'no-store'
    if path.startswith(CORS_PATHS) or path == '/mcp' or path == '/api/v1/openapi.json':
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, MCP-Protocol-Version, Mcp-Session-Id'
        response.headers['Access-Control-Expose-Headers'] = 'WWW-Authenticate, Mcp-Session-Id'
        response.headers['Access-Control-Max-Age'] = '600'
    return response
