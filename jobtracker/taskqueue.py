"""Durable SQLite-backed task queue: claim with a lease, fence completion, retry.

Producers call `enqueue` inside their own write transaction, so a task exists
exactly when the record it serves does. A separate worker process (worker.py)
claims one task at a time:

* Claim is a compare-and-set under `BEGIN IMMEDIATE`: status -> running,
  attempts + 1, a fresh `claim_token` and `lease_expires_at`.
* Completion only applies while the caller still holds that claim token, so a
  worker whose lease expired cannot overwrite the reclaiming worker's result.
* A crashed worker's task becomes claimable again when its lease expires; once
  `max_attempts` are used up it fails and the handler's `give_up` hook runs.
* A handler raises `Retry` for transient failures (backoff); any other
  exception is also retried until attempts are exhausted.

Idle polling is a single indexed read; the write lock is only taken when work
is visible. Safe with one Gunicorn worker plus any number of queue workers.
"""
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import traceback
from typing import Callable

from sqlalchemy import and_, delete, func, insert, or_, select, update

from .models import QueuedTask, new_id, now

log = logging.getLogger(__name__)
RETENTION = timedelta(days=7)


class Retry(Exception):
    """Transient failure: try again after `delay` seconds if attempts remain."""
    def __init__(self, message='', delay=5):
        super().__init__(message)
        self.delay = delay


@dataclass
class Handler:
    run: Callable
    lease_seconds: int = 30
    max_attempts: int = 2
    give_up: Callable | None = None


HANDLERS: dict[str, Handler] = {}


def handler(kind, *, lease_seconds=30, max_attempts=2, give_up=None):
    def register(function):
        HANDLERS[kind] = Handler(function, lease_seconds, max_attempts, give_up)
        return function
    return register


@dataclass
class Claim:
    id: str
    user_id: int
    kind: str
    payload: dict
    attempts: int
    max_attempts: int
    claim_token: str
    extra: dict = field(default_factory=dict)


def enqueue(connection, user_id, kind, payload, *, delay=0):
    """Insert within the caller's transaction; returns the task id."""
    spec = HANDLERS[kind]
    identifier = new_id()
    connection.execute(insert(QueuedTask).values(
        id=identifier, user_id=user_id, kind=kind, payload=payload, status='queued', attempts=0,
        max_attempts=spec.max_attempts, run_after=now() + timedelta(seconds=delay), lease_expires_at=None,
        claim_token=None, last_error='', created_at=now(), updated_at=now()))
    return identifier


def pending(connection, since=None):
    """Unfinished tasks, optionally only those created after `since`."""
    query = select(func.count()).select_from(QueuedTask).where(QueuedTask.status.in_(('queued', 'running')))
    if since is not None:
        query = query.where(QueuedTask.created_at >= since)
    return connection.scalar(query)


def _claimable(moment):
    return or_(and_(QueuedTask.status == 'queued', QueuedTask.run_after <= moment),
               and_(QueuedTask.status == 'running', QueuedTask.lease_expires_at < moment))


def load_handlers():
    # Handler modules register on import; a worker must know every kind before
    # it may fail a task as unknown. Add new handler modules here.
    from . import extraction_tasks, harness, ingestion  # noqa: F401


def claim(engine):
    """Claim the oldest runnable task, or return None. Never blocks on an idle queue."""
    load_handlers()
    moment = now()
    with engine.connect() as connection:
        if connection.scalar(select(QueuedTask.id).where(_claimable(moment)).limit(1)) is None:
            return None
    exhausted, claimed = [], None
    with engine.begin() as connection:
        if engine.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        moment = now()
        while claimed is None:
            row = connection.execute(select(QueuedTask.__table__).where(_claimable(moment))
                                     .order_by(QueuedTask.run_after, QueuedTask.created_at).limit(1)).mappings().first()
            if row is None:
                break
            if row['attempts'] >= row['max_attempts'] or row['kind'] not in HANDLERS:
                # A lease expired on the last attempt (worker crash/timeout) or the
                # kind is unknown to this release: fail instead of looping forever.
                reason = 'Lease expired on final attempt' if row['kind'] in HANDLERS else 'Unknown task kind'
                connection.execute(update(QueuedTask).where(QueuedTask.id == row['id']).values(
                    status='failed', lease_expires_at=None, claim_token=None, last_error=reason, updated_at=moment))
                exhausted.append(dict(row))
                continue
            token = new_id()
            spec = HANDLERS[row['kind']]
            connection.execute(update(QueuedTask).where(QueuedTask.id == row['id']).values(
                status='running', attempts=row['attempts'] + 1, claim_token=token, updated_at=moment,
                lease_expires_at=moment + timedelta(seconds=spec.lease_seconds)))
            claimed = Claim(row['id'], row['user_id'], row['kind'], row['payload'], row['attempts'] + 1,
                            row['max_attempts'], token)
    # Only after the commit: a rolled-back claim must not report a failure.
    for row in exhausted:
        _give_up(engine, HANDLERS.get(row['kind']), row['user_id'], row['payload'], row['id'])
    return claimed


def _give_up(engine, spec, user_id, payload, task_id):
    if spec is None or spec.give_up is None:
        return
    try:
        spec.give_up(engine, user_id, payload)
    except Exception as exc:  # noqa: BLE001 - never lose a claim to a failing hook
        log.warning('give_up hook for task %s failed: %s', task_id, type(exc).__name__)


def _finish(engine, task, **values):
    with engine.begin() as connection:
        return connection.execute(update(QueuedTask).where(
            QueuedTask.id == task.id, QueuedTask.claim_token == task.claim_token, QueuedTask.status == 'running',
        ).values(lease_expires_at=None, claim_token=None, updated_at=now(), **values)).rowcount == 1


def execute(app, task):
    """Run one claimed task; record success, retry, or final failure."""
    engine = app.extensions['jobtracker_db']['engine']
    spec = HANDLERS[task.kind]
    try:
        with app.app_context():
            spec.run(app, task)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, never raised
        delay = exc.delay if isinstance(exc, Retry) else min(60, 5 * task.attempts)
        # Store only the exception type; messages may echo private page content.
        summary = type(exc).__name__
        log.warning('Queue task %s (%s) attempt %s failed: %s', task.id, task.kind, task.attempts, summary)
        log.debug(''.join(traceback.format_exception(exc)))
        if task.attempts < task.max_attempts:
            _finish(engine, task, status='queued', run_after=now() + timedelta(seconds=delay), last_error=summary)
            return 'retry'
        if _finish(engine, task, status='failed', last_error=summary):
            _give_up(engine, spec, task.user_id, task.payload, task.id)
        return 'failed'
    if not _finish(engine, task, status='succeeded', last_error=''):
        return 'lost'  # Lease was taken over; the newer claim owns the outcome.
    return 'succeeded'


def run_once(app):
    """Claim and run at most one task. Returns its outcome, or None when idle."""
    task = claim(app.extensions['jobtracker_db']['engine'])
    return execute(app, task) if task else None


def drain(app, limit=100):
    """Run until idle (tests and one-shot maintenance). Returns outcomes."""
    outcomes = []
    while len(outcomes) < limit and (outcome := run_once(app)) is not None:
        outcomes.append(outcome)
    return outcomes


def prune(engine):
    with engine.begin() as connection:
        return connection.execute(delete(QueuedTask).where(
            QueuedTask.status.in_(('succeeded', 'failed')), QueuedTask.updated_at < now() - RETENTION)).rowcount
