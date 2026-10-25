"""Bounded background extraction with shared previews and interrupted-task expiry."""
from datetime import timedelta, timezone
import threading

from flask import current_app
from sqlalchemy import delete, func, select, update

from .db import get_session
from .extraction import extract_posting, validate_url
from .models import ExtractionTask, new_id, now
from .services import ServiceError

TASK_SECONDS = 60
MAX_ACTIVE = 2
SLOTS = threading.BoundedSemaphore(MAX_ACTIVE)
BUSY = 'Two postings are already being read. Wait a moment and try again.'
EXPIRED = {'error': 'Extraction timed out or was interrupted. Try again or enter details manually.'}


def expire_tasks(connection):
    connection.execute(update(ExtractionTask).where(
        ExtractionTask.status == 'running', ExtractionTask.created_at < now() - timedelta(seconds=TASK_SECONDS)
    ).values(status='failed', result=EXPIRED))
    connection.execute(delete(ExtractionTask).where(ExtractionTask.created_at < now() - timedelta(days=1)))


def start_extraction(url):
    if not SLOTS.acquire(blocking=False):
        raise ServiceError(BUSY, 429)
    try:
        validate_url(url)  # Syntax only: this request must never wait for a remote site.
        engine = current_app.extensions['jobtracker_db']['engine']
        identifier = new_id()
        with engine.begin() as connection:
            if engine.dialect.name == 'sqlite':
                # Atomic admission across worker processes; no unbounded thread queue.
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            expire_tasks(connection)
            active = connection.scalar(select(func.count()).select_from(ExtractionTask).where(ExtractionTask.status == 'running'))
            if active >= MAX_ACTIVE:
                raise ServiceError(BUSY, 429)
            connection.execute(ExtractionTask.__table__.insert().values(
                id=identifier, status='running', result={}, created_at=now()))
        try:
            threading.Thread(target=run_extraction, args=(engine, identifier, url.strip()), daemon=True,
                             name='posting-extraction').start()
        except RuntimeError as exc:
            finish(engine, identifier, 'failed', {'error': 'Extraction could not start. Please try again.'})
            raise ServiceError('Extraction could not start. Please try again.', 503) from exc
        return {'id': identifier, 'status': 'running'}
    except Exception:
        SLOTS.release()
        raise


def finish(engine, identifier, status, result):
    with engine.begin() as connection:
        connection.execute(update(ExtractionTask).where(
            ExtractionTask.id == identifier, ExtractionTask.status == 'running'
        ).values(status=status, result=result))


def run_extraction(engine, identifier, url):
    try:
        result = extract_posting(url)
        status = 'succeeded'
    except ServiceError as exc:
        status, result = 'failed', {'error': str(exc)}
    except Exception:
        # Never expose internal errors or page contents to the client/logs.
        status, result = 'failed', {'error': 'The posting could not be read. Try again or enter details manually.'}
    try:
        finish(engine, identifier, status, result)
    finally:
        SLOTS.release()


def get_extraction(identifier):
    session = get_session()
    task = session.get(ExtractionTask, identifier)
    if task is None or task.created_at.replace(tzinfo=timezone.utc) < now() - timedelta(days=1):
        raise ServiceError('Extraction preview expired or was not found. Extract the URL again.', 404)
    if task.status == 'running' and task.created_at.replace(tzinfo=timezone.utc) < now() - timedelta(seconds=TASK_SECONDS):
        task.status, task.result = 'failed', EXPIRED
        session.commit()
    return {'id': task.id, 'status': task.status, **task.result}
