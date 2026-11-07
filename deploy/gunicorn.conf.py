"""One preloaded worker; bounded concurrency for a small single-user host."""
import os

bind = os.environ.get('GUNICORN_BIND', '127.0.0.1:5001')
workers = 1
threads = 1
preload_app = True
timeout = 30
graceful_timeout = 30
keepalive = 2
worker_tmp_dir = '/tmp'
# No access log: resume URLs and private record identifiers stay out of logs.
accesslog = None
errorlog = '-'
# JSON lines on stderr (jobtracker/logs.py): type and stack for errors, never exception messages.
# LOG_FORMAT=text (or APP_ENV=development) keeps Gunicorn's plain format.
if os.environ.get('LOG_FORMAT', 'text' if os.environ.get('APP_ENV') == 'development' else 'json') == 'json':
    from jobtracker.logs import gunicorn_logconfig
    logconfig_dict = gunicorn_logconfig('jobtracker-web')
if os.environ.get('APP_ENV', 'production') != 'development':
    import logging
    logging.raiseExceptions = False  # a failing handler must not print the record (message and args) to stderr
capture_output = False
# ProxyFix is the sole scheme interpreter, controlled by TRUST_PROXY. Prevent
# Gunicorn from independently accepting forwarded scheme headers.
secure_scheme_headers = {}
forwarded_allow_ips = ''
umask = 0o077


def post_fork(server, worker):
    # Preloading shares code pages, but each child must own its DB pool. See
    # SQLAlchemy's multiprocessing guidance; do not close the parent's pool.
    application = worker.app.wsgi()
    application.extensions['jobtracker_db']['engine'].dispose(close=False)
