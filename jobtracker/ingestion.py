"""Idempotent URL-to-role API, run by the queue worker like previews."""
from datetime import timedelta
import re

from flask import current_app, g
from sqlalchemy import func, select

from . import extraction_tasks as tasks
from . import services, taskqueue
from .extraction import extract_posting, validate_url
from .models import IngestionRequest, new_id, now

HOURLY_LIMIT = 30
FAILED = 'Ingestion failed. Try a new request or capture manually.'


def start_ingestion(user_id, url, key):
    validate_url(url)
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
        raise services.ServiceError('Idempotency-Key must contain 16–128 letters, digits, hyphens or underscores')
    url = url.strip()
    token_id = g.agent_token['id']
    engine = current_app.extensions['jobtracker_db']['engine']
    with engine.begin() as connection:
        if engine.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        tasks.expire_tasks(connection)
        # Keys are unique per account; another account's keys are invisible.
        existing = connection.execute(select(IngestionRequest.__table__).where(
            IngestionRequest.user_id == user_id, IngestionRequest.idempotency_key == key)).mappings().first()
        if existing:
            if existing['token_id'] != token_id:
                raise services.ServiceError('This retry key belongs to another token. Check its result in the app.', 409)
            if existing['url'] != url:
                raise services.ServiceError('This Idempotency-Key was already used for a different URL', 409)
            return {'id': existing['id'], 'status': existing['status'], **existing['result']}
        recent = connection.scalar(select(func.count()).select_from(IngestionRequest).where(
            IngestionRequest.user_id == user_id, IngestionRequest.created_at >= now() - timedelta(hours=1)))
        if recent >= HOURLY_LIMIT:
            raise services.ServiceError('Ingestion limit reached (30 new requests per hour)', 429)
        tasks.admit(connection, user_id)
        identifier = new_id()
        connection.execute(IngestionRequest.__table__.insert().values(
            id=identifier, user_id=user_id, idempotency_key=key, token_id=token_id, url=url, status='running',
            result={}, created_at=now()))
        taskqueue.enqueue(connection, user_id, 'ingest_url', {'request_id': identifier, 'url': url})
    return {'id': identifier, 'status': 'running'}


def give_up_ingestion(engine, _user_id, payload):
    tasks.finish(engine, payload['request_id'], 'failed', {'error': FAILED}, IngestionRequest)


@taskqueue.handler('ingest_url', give_up=give_up_ingestion, lease_seconds=tasks.LEASE_SECONDS)
def run_ingestion(app, task):
    engine = app.extensions['jobtracker_db']['engine']
    identifier = task.payload['request_id']
    if not tasks.still_running(engine, IngestionRequest, identifier, task.user_id):
        return
    try:
        result = extract_posting(task.payload['url'])
        services.complete_ingestion(task.user_id, identifier, result['fields'], result['warnings'])
    except Exception as exc:
        message = str(exc) if isinstance(exc, services.ServiceError) else FAILED
        tasks.finish(engine, identifier, 'failed', {'error': message}, IngestionRequest)
