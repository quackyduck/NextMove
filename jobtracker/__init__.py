"""Application factory for the private Job Tracker."""
import os
import re
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException, InternalServerError
from werkzeug.security import generate_password_hash

from .strict_json import JSONProvider


def secret_setting(name):
    """NAME, or the contents of the file named by NAME_FILE (container secrets)."""
    path = os.getenv(f'{name}_FILE')
    return Path(path).read_text().strip() if path else os.getenv(name)


def _optional_secret(name):
    """Like secret_setting, but a missing or unreadable file means "unset" (used for optional telemetry)."""
    try:
        return secret_setting(name)
    except OSError:
        import logging
        logging.getLogger(__name__).warning('%s_FILE could not be read; treating %s as unset', name, name)
        return None


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, static_folder=None, instance_relative_config=True)
    app.json = JSONProvider(app)
    development = os.getenv('APP_ENV') == 'development'
    app.config.from_mapping(
        APP_ENV=os.getenv('APP_ENV', 'production'),
        SECRET_KEY=os.getenv('SECRET_KEY'),
        APP_PASSWORD_HASH=os.getenv('APP_PASSWORD_HASH'),
        AGENT_API_TOKEN=os.getenv('AGENT_API_TOKEN'),
        ROLE_SEARCH_TOKEN_SHA256=os.getenv('ROLE_SEARCH_TOKEN_SHA256'),
        DATABASE_URL=os.getenv('DATABASE_URL', f'sqlite:///{Path(app.instance_path) / "jobtracker.sqlite3"}'),
        UPLOAD_FOLDER=os.getenv('UPLOAD_FOLDER', str(Path(app.instance_path) / 'resumes')),
        FRONTEND_DIST=os.getenv('FRONTEND_DIST', str(Path(__file__).resolve().parent.parent / 'frontend' / 'dist')),
        TRUST_PROXY=os.getenv('TRUST_PROXY') == '1',
        LOGIN_THROTTLE_DATABASE=os.getenv('LOGIN_THROTTLE_DATABASE'),
        # Accounts: sign-up is invite-only by default. Email verification is required (schema 15).
        SIGNUP_ENABLED=os.getenv('SIGNUP_ENABLED', '1') != '0',
        SIGNUP_REQUIRES_INVITE=os.getenv('SIGNUP_REQUIRES_INVITE', '1') != '0',
        BOOTSTRAP_ADMIN_EMAIL=os.getenv('BOOTSTRAP_ADMIN_EMAIL'),
        QUEUE_POLL_SECONDS=float(os.getenv('QUEUE_POLL_SECONDS', '1')),
        # Resume Agent harness (jobtracker/harness.py). Unset secret = account creation refused.
        HARNESS_SERVICE_SECRET=secret_setting('HARNESS_SERVICE_SECRET'),
        # Only for the private container network; the public proxy never routes harness paths.
        HARNESS_ALLOW_HTTP=os.getenv('HARNESS_ALLOW_HTTP') == '1',
        # Absolute dashboard URL for activation links sent over chat.
        PUBLIC_BASE_URL=os.getenv('PUBLIC_BASE_URL', ''),
        # Browser origins (besides this one) allowed to call /mcp, e.g. a local MCP inspector.
        MCP_ALLOWED_ORIGINS=[origin.strip().rstrip('/') for origin in os.getenv('MCP_ALLOWED_ORIGINS', '').split(',')
                             if origin.strip()],
        TRUSTED_HOSTS=[host.strip() for host in os.getenv('APP_ALLOWED_HOSTS', '').split(',') if host.strip()] or None,
        APP_ROOT_PATH=os.getenv('APP_ROOT_PATH', ''),
        SESSION_COOKIE_NAME='next_move_session',
        # Error telemetry (jobtracker/telemetry.py). Unset DSN = telemetry fully off.
        SENTRY_DSN=_optional_secret('SENTRY_DSN'),
        SENTRY_FRONTEND_DSN=os.getenv('SENTRY_FRONTEND_DSN', ''),
        SENTRY_ENVIRONMENT=os.getenv('SENTRY_ENVIRONMENT', ''),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=not development,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    if test_config:
        app.config.update(test_config)
    prefix = app.config['APP_ROOT_PATH'].rstrip('/')
    if prefix and not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)+', prefix):
        raise RuntimeError('APP_ROOT_PATH must be empty or an absolute path of simple segments')
    app.config['APPLICATION_ROOT'] = prefix or '/'
    app.config['SESSION_COOKIE_PATH'] = prefix + '/'
    if app.config['TRUST_PROXY']:
        # Only enable behind exactly one reverse proxy on a private connection.
        # Trust its scheme, but keep the original Host for origin validation.
        from werkzeug.middleware.proxy_fix import ProxyFix
        # x_for=1 takes the client address the proxy appended, for per-IP login budgets.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)
    development = app.config['APP_ENV'] == 'development'
    if not test_config or 'SESSION_COOKIE_SECURE' not in test_config:
        app.config['SESSION_COOKIE_SECURE'] = not development
    if development:
        app.config['SECRET_KEY'] = app.config.get('SECRET_KEY') or 'local-development-key-do-not-deploy'
        app.config['APP_PASSWORD_HASH'] = app.config.get('APP_PASSWORD_HASH') or generate_password_hash(os.getenv('APP_PASSWORD', 'local-demo-only'))
    elif not app.config.get('SECRET_KEY') or not app.config.get('APP_PASSWORD_HASH'):
        raise RuntimeError('Production requires SECRET_KEY and APP_PASSWORD_HASH')
    if not development and (len(app.config['SECRET_KEY']) < 32 or app.config['SECRET_KEY'] == 'local-development-key-do-not-deploy'):
        raise RuntimeError('Production SECRET_KEY must contain at least 32 characters')
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config['UPLOAD_FOLDER']).mkdir(parents=True, exist_ok=True, mode=0o700)
    # First, so request IDs and error capture cover every later hook and blueprint.
    from . import telemetry
    telemetry.init_app(app)
    from . import throttle
    throttle.init_app(app)
    from . import db
    from .api import api
    from .auth import auth
    db.init_app(app)
    from . import credentials, worker
    credentials.init_app(app)
    worker.init_app(app)
    # Email verification (schema 15): outbound mail, proof-of-work and the confirmation routes.
    from . import mailer, pow
    mailer.init_app(app)
    pow.init_app(app)
    from .confirmations import confirmations
    app.register_blueprint(confirmations, url_prefix='/api/v1')
    # Self-service password reset (schema 16): its routes, worker handlers and `user reset-link`.
    from . import password_reset
    password_reset.init_app(app)
    # Data deletion and export (schema 17): browser-only delete routes, the export link, and the operator CLI.
    from . import deletion
    deletion.init_app(app)
    # Admin page (schema 18): browser-only, admins only, and `flask user set-admin`.
    from . import admin
    admin.init_app(app)
    app.register_blueprint(api, url_prefix='/api/v1')
    app.register_blueprint(auth, url_prefix='/api/v1')
    from .profile import profile
    app.register_blueprint(profile, url_prefix='/api/v1')
    from .resume_templates import templates
    app.register_blueprint(templates, url_prefix='/api/v1')
    from .harness import harness
    app.register_blueprint(harness, url_prefix='/api/v1')
    # Third-party agents: OAuth 2.1 (root, for well-known discovery), MCP and signed file links.
    from .oauth import oauth
    app.register_blueprint(oauth)
    from .mcp import mcp
    app.register_blueprint(mcp)
    from .resume_files import files
    app.register_blueprint(files, url_prefix='/api/v1')

    @app.get('/health')
    @app.get('/api/v1/health')
    def health():
        from sqlalchemy import text
        db.get_session().execute(text('SELECT 1'))
        return jsonify(status='ok')

    # Images for branded emails (mailer.render links them with public_url). Public, immutable, allowlisted:
    # registered before the SPA catch-all, which would otherwise answer with index.html.
    email_assets = Path(__file__).resolve().parent / 'static' / 'email'

    @app.get('/static/email/<name>')
    def email_asset(name):
        if name not in EMAIL_ASSETS:
            return jsonify(error='Not found'), 404
        response = send_from_directory(email_assets, name, mimetype='image/png', max_age=31536000)
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response

    @app.get('/')
    @app.get('/<path:asset_path>')
    def frontend(asset_path='index.html'):
        if asset_path.startswith('api/'):
            return jsonify(error='Not found'), 404
        dist = Path(app.config['FRONTEND_DIST'])
        if not (dist / 'index.html').is_file():
            return jsonify(service='Job Tracker', status='frontend not built', api='/api/v1')
        if (dist / asset_path).is_file():
            return send_from_directory(dist, asset_path)
        return send_from_directory(dist, 'index.html')

    @app.before_request
    def utf8_query_string():
        # Werkzeug decodes request.args strictly, so a raw non-UTF-8 byte would be a 500 wherever it is read.
        try:
            request.query_string.decode('utf-8')
        except UnicodeDecodeError:
            abort(400, 'The query string must be valid UTF-8')

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    # On InternalServerError, not Exception: an unhandled exception still goes through Flask's
    # handle_exception, which sends got_request_exception (Sentry) and calls log_exception before this runs.
    # A handler for Exception would swallow both. The after_request hook adds request_id to the body.
    @app.errorhandler(InternalServerError)
    def internal_error(exc):
        return jsonify(error='Something went wrong on our side.'), 500

    def log_exception(exc_info):
        # The route template, never request.path: paths carry signed tokens (/api/v1/files/<token>).
        rule = request.url_rule.rule if request.url_rule is not None else '<unmatched>'
        app.logger.error('Unhandled exception on %s %s', request.method, rule, exc_info=exc_info)
    app.log_exception = log_exception

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        policy = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        connect = telemetry.connect_src()  # only the exact Sentry ingest host, and only when browser telemetry is on
        response.headers['Content-Security-Policy'] = f'{policy}; {connect}' if connect else policy
        if request_is_private():
            response.headers['Cache-Control'] = 'no-store'
        return response
    if prefix:
        from .prefix import PrefixMount
        app.wsgi_app = PrefixMount(app.wsgi_app, prefix)
    return app


# The only files served from /static/email/ (a new image gets a new versioned name).
EMAIL_ASSETS = frozenset({'mascot-96-v1.png'})


def request_is_private():
    from flask import request
    return request.path.startswith('/api/')
