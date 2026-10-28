"""Queued posting extraction with private, per-user previews and expiry.

The web request only validates and enqueues (BEGIN IMMEDIATE admission); the
separate worker process fetches the page. Each account may have two postings
being read at once, and the whole queue is bounded so one account cannot grow
it without limit. A preview not finished within TASK_SECONDS fails, so a
stopped worker surfaces as a clear, retryable error rather than a hang.
"""
from datetime import timedelta, timezone

from flask import current_app
from sqlalchemy import delete, func, select, update

from . import taskqueue
from .db import get_session
from .extraction import extract_posting, validate_url
from .models import ExtractionTask, IngestionRequest, new_id, now
from .services import ServiceError

TASK_SECONDS = 60
MAX_ACTIVE = 2
MAX_PENDING = 20
BUSY = 'Two postings are already being read. Wait a moment and try again.'
FULL = 'Posting extraction is busy. Try again in a minute.'
EXPIRED = {'error': 'Extraction timed out or was interrupted. Try again or enter details manually.'}
UNREADABLE = 'The posting could not be read. Try again or enter details manually.'


def expire_tasks(connection):
    for model in (ExtractionTask, IngestionRequest):
        connection.execute(update(model).where(
            model.status == 'running', model.created_at < now() - timedelta(seconds=TASK_SECONDS)
        ).values(status='failed', result=EXPIRED))
    connection.execute(delete(ExtractionTask).where(ExtractionTask.created_at < now() - timedelta(days=1)))


def active_tasks(connection, user_id):
    return sum(connection.scalar(select(func.count()).select_from(model).where(
        model.user_id == user_id, model.status == 'running')) for model in (ExtractionTask, IngestionRequest))


def admit(connection, user_id):
    """Raise 429 unless this account and the shared queue both have room."""
    if active_tasks(connection, user_id) >= MAX_ACTIVE:
        raise ServiceError(BUSY, 429)
    # Only tasks whose preview/receipt can still succeed count: when the worker
    # is down, stale rows must not keep every account at 429.
    if taskqueue.pending(connection, since=now() - timedelta(seconds=TASK_SECONDS)) >= MAX_PENDING:
        raise ServiceError(FULL, 429)


def start_extraction(user_id, url):
    validate_url(url)  # Syntax only: this request must never wait for a remote site.
    engine = current_app.extensions['jobtracker_db']['engine']
    identifier = new_id()
    with engine.begin() as connection:
        if engine.dialect.name == 'sqlite':
            # Atomic admission across web and worker processes.
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        expire_tasks(connection)
        admit(connection, user_id)
        connection.execute(ExtractionTask.__table__.insert().values(
            id=identifier, user_id=user_id, status='running', result={}, created_at=now()))
        taskqueue.enqueue(connection, user_id, 'extract_preview', {'task_id': identifier, 'url': url.strip()})
    return {'id': identifier, 'status': 'running'}


def finish(engine, identifier, status, result, model=ExtractionTask):
    with engine.begin() as connection:
        connection.execute(update(model).where(
            model.id == identifier, model.status == 'running'
        ).values(status=status, result=result))


def still_running(engine, model, identifier, user_id):
    """True when the record awaits a result and is within its deadline."""
    with engine.connect() as connection:
        return connection.scalar(select(model.id).where(
            model.id == identifier, model.user_id == user_id, model.status == 'running',
            model.created_at >= now() - timedelta(seconds=TASK_SECONDS))) is not None


def give_up_preview(engine, _user_id, payload):
    finish(engine, payload['task_id'], 'failed', EXPIRED)


# The lease outlives the whole TASK_SECONDS deadline (fetch_page's 20 s budget
# plus DNS for each redirect), so a live worker is never reclaimed mid-fetch; a
# reclaimed task finds its preview expired and stops.
LEASE_SECONDS = TASK_SECONDS + 15


@taskqueue.handler('extract_preview', give_up=give_up_preview, lease_seconds=LEASE_SECONDS)
def run_extraction(app, task):
    engine = app.extensions['jobtracker_db']['engine']
    identifier = task.payload['task_id']
    if not still_running(engine, ExtractionTask, identifier, task.user_id):
        return  # Expired or already finished: never fetch for a stale request.
    try:
        result = extract_posting(task.payload['url'])
        status = 'succeeded'
    except ServiceError as exc:
        status, result = 'failed', {'error': str(exc)}
    except Exception:
        # Never expose internal errors or page contents to the client/logs.
        status, result = 'failed', {'error': UNREADABLE}
    finish(engine, identifier, status, result)


def get_extraction(user_id, identifier, model=ExtractionTask, owner_id=None):
    session = get_session()
    task = session.scalar(select(model).where(model.id == identifier, model.user_id == user_id))
    if task is not None and model is IngestionRequest and task.token_id != owner_id:
        task = None
    if task is None or (model is ExtractionTask and task.created_at.replace(tzinfo=timezone.utc) < now() - timedelta(days=1)):
        raise ServiceError('Extraction preview expired or was not found. Extract the URL again.', 404)
    if task.status == 'running' and task.created_at.replace(tzinfo=timezone.utc) < now() - timedelta(seconds=TASK_SECONDS):
        session.execute(update(model).where(model.id == identifier, model.status == 'running')
                        .values(status='failed', result=EXPIRED))
        session.commit()
        session.refresh(task)
    return {'id': task.id, 'status': task.status, **task.result}
