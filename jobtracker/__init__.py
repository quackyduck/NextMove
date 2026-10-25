"""Application factory for the private Job Tracker."""
import os
import re
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, static_folder=None, instance_relative_config=True)
    development = os.getenv('APP_ENV') == 'development'
    app.config.from_mapping(
        APP_ENV=os.getenv('APP_ENV', 'production'),
        SECRET_KEY=os.getenv('SECRET_KEY'),
        APP_PASSWORD_HASH=os.getenv('APP_PASSWORD_HASH'),
        AGENT_API_TOKEN=os.getenv('AGENT_API_TOKEN'),
        INGEST_API_TOKEN_SHA256=os.getenv('INGEST_API_TOKEN_SHA256'),
        DATABASE_URL=os.getenv('DATABASE_URL', f'sqlite:///{Path(app.instance_path) / "jobtracker.sqlite3"}'),
        UPLOAD_FOLDER=os.getenv('UPLOAD_FOLDER', str(Path(app.instance_path) / 'resumes')),
        FRONTEND_DIST=os.getenv('FRONTEND_DIST', str(Path(__file__).resolve().parent.parent / 'frontend' / 'dist')),
        TRUST_PROXY=os.getenv('TRUST_PROXY') == '1',
        LOGIN_THROTTLE_DATABASE=os.getenv('LOGIN_THROTTLE_DATABASE'),
        TRUSTED_HOSTS=[host.strip() for host in os.getenv('APP_ALLOWED_HOSTS', '').split(',') if host.strip()] or None,
        APP_ROOT_PATH=os.getenv('APP_ROOT_PATH', ''),
        SESSION_COOKIE_NAME='next_move_session',
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
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)
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
    from . import throttle
    throttle.init_app(app)
    from . import db
    from .api import api
    from .auth import auth
    db.init_app(app)
    from . import credentials
    credentials.init_app(app)
    app.register_blueprint(api, url_prefix='/api/v1')
    app.register_blueprint(auth, url_prefix='/api/v1')

    @app.get('/health')
    @app.get('/api/v1/health')
    def health():
        from sqlalchemy import text
        db.get_session().execute(text('SELECT 1'))
        return jsonify(status='ok')

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

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        if request_is_private():
            response.headers['Cache-Control'] = 'no-store'
        return response
    if prefix:
        from .prefix import PrefixMount
        app.wsgi_app = PrefixMount(app.wsgi_app, prefix)
    return app


def request_is_private():
    from flask import request
    return request.path.startswith('/api/')
