"""Idempotent URL-to-role API, sharing the bounded extraction runner."""
from datetime import timedelta
import re
import threading

from flask import current_app, g
from sqlalchemy import func, select

from . import extraction_tasks as tasks
from . import services
from .extraction import extract_posting, validate_url
from .models import IngestionRequest, new_id, now

HOURLY_LIMIT = 30


def start_ingestion(url, key):
    validate_url(url)
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
        raise services.ServiceError('Idempotency-Key must contain 16–128 letters, digits, hyphens or underscores')
    url = url.strip()
    token_id = g.agent_token['id']
    app = current_app._get_current_object()
    engine = app.extensions['jobtracker_db']['engine']
    acquired = False
    try:
        with engine.begin() as connection:
            if engine.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            tasks.expire_tasks(connection)
            existing = connection.execute(select(IngestionRequest.__table__).where(
                IngestionRequest.idempotency_key == key)).mappings().first()
            if existing:
                if existing['token_id'] != token_id:
                    raise services.ServiceError('This retry key belongs to another token. Check its result in the app.', 409)
                if existing['url'] != url:
                    raise services.ServiceError('This Idempotency-Key was already used for a different URL', 409)
                return {'id': existing['id'], 'status': existing['status'], **existing['result']}
            recent = connection.scalar(select(func.count()).select_from(IngestionRequest).where(
                IngestionRequest.created_at >= now() - timedelta(hours=1)))
            if recent >= HOURLY_LIMIT:
                raise services.ServiceError('Ingestion limit reached (30 new requests per hour)', 429)
            acquired = tasks.SLOTS.acquire(blocking=False)
            if not acquired or tasks.active_tasks(connection) >= tasks.MAX_ACTIVE:
                raise services.ServiceError(tasks.BUSY, 429)
            identifier = new_id()
            connection.execute(IngestionRequest.__table__.insert().values(
                id=identifier, idempotency_key=key, token_id=token_id, url=url, status='running', result={}, created_at=now()))
        try:
            threading.Thread(target=run_ingestion, args=(app, identifier, url), daemon=True,
                             name='url-ingestion').start()
        except RuntimeError as exc:
            tasks.finish(engine, identifier, 'failed', {'error': 'Ingestion could not start. Try a new request.'}, IngestionRequest)
            raise services.ServiceError('Ingestion could not start', 503) from exc
    except Exception:
        if acquired:
            tasks.SLOTS.release()
        raise
    return {'id': identifier, 'status': 'running'}


def run_ingestion(app, identifier, url):
    try:
        result = extract_posting(url)
        with app.app_context():
            services.complete_ingestion(identifier, result['fields'], result['warnings'])
    except Exception as exc:
        message = str(exc) if isinstance(exc, services.ServiceError) else 'Ingestion failed. Try a new request or capture manually.'
        tasks.finish(app.extensions['jobtracker_db']['engine'], identifier, 'failed', {'error': message}, IngestionRequest)
    finally:
        tasks.SLOTS.release()
