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

Anonymous tasks (schema 15): `user_id` may be NULL, but only for kinds whose
handler is declared `anonymous=True` (mail about a pending sign-up, which has
no account yet). `enqueue` refuses NULL for any other kind and `claim` fails
such a row without running it, so an anonymous task never reaches tenant code.

`Retry(count_attempt=False)` defers without using up an attempt (mail quota);
such a task still fails once it is older than its handler's `max_deferral`.
`delete_on_success` removes the row on success, inside the claim-token fence,
for payloads that hold an address.
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
# Failed rows of delete_on_success kinds hold an address; keep them only briefly.
SHORT_RETENTION = timedelta(days=1)
MAX_DEFERRAL = timedelta(hours=48)


class Retry(Exception):
    """Transient failure: try again after `delay` seconds if attempts remain.

    With count_attempt=False (e.g. a mail quota deferral) the attempt is refunded,
    bounded by the handler's max_deferral since the task was created.
    """
    def __init__(self, message='', delay=5, count_attempt=True):
        super().__init__(message)
        self.delay = delay
        self.count_attempt = count_attempt


@dataclass
class Handler:
    run: Callable
    lease_seconds: int = 30
    max_attempts: int = 2
    give_up: Callable | None = None
    anonymous: bool = False
    delete_on_success: bool = False
    max_deferral: timedelta = MAX_DEFERRAL


HANDLERS: dict[str, Handler] = {}


def handler(kind, *, lease_seconds=30, max_attempts=2, give_up=None, anonymous=False, delete_on_success=False,
            max_deferral=MAX_DEFERRAL):
    def register(function):
        HANDLERS[kind] = Handler(function, lease_seconds, max_attempts, give_up, anonymous, delete_on_success,
                                 max_deferral)
        return function
    return register


@dataclass
class Claim:
    id: str
    user_id: int | None
    kind: str
    payload: dict
    attempts: int
    max_attempts: int
    claim_token: str
    created_at: object = None
    extra: dict = field(default_factory=dict)


def enqueue(connection, user_id, kind, payload, *, delay=0):
    """Insert within the caller's transaction; returns the task id.

    user_id may be None only for kinds declared anonymous=True.
    """
    spec = HANDLERS[kind]
    if user_id is None and not spec.anonymous:
        raise ValueError(f'Queue task kind {kind!r} needs an owning user')
    identifier = new_id()
    connection.execute(insert(QueuedTask).values(
        id=identifier, user_id=user_id, kind=kind, payload=payload, status='queued', attempts=0,
        max_attempts=spec.max_attempts, run_after=now() + timedelta(seconds=delay), lease_expires_at=None,
        claim_token=None, last_error='', created_at=now(), updated_at=now()))
    return identifier


def pending(connection, since=None, kinds=None):
    """Unfinished tasks, optionally only those created after `since` and of the given kinds."""
    query = select(func.count()).select_from(QueuedTask).where(QueuedTask.status.in_(('queued', 'running')))
    if since is not None:
        query = query.where(QueuedTask.created_at >= since)
    if kinds is not None:
        query = query.where(QueuedTask.kind.in_(tuple(kinds)))
    return connection.scalar(query)


def _claimable(moment):
    return or_(and_(QueuedTask.status == 'queued', QueuedTask.run_after <= moment),
               and_(QueuedTask.status == 'running', QueuedTask.lease_expires_at < moment))


def load_handlers():
    # Handler modules register on import; a worker must know every kind before
    # it may fail a task as unknown. Add new handler modules here.
    from . import confirmations, deletion, extraction_tasks, harness, ingestion, password_reset  # noqa: F401


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
            known = row['kind'] in HANDLERS
            refused = row['user_id'] is None and not (known and HANDLERS[row['kind']].anonymous)
            if refused or row['attempts'] >= row['max_attempts'] or not known:
                # A lease expired on the last attempt (worker crash/timeout), the kind
                # is unknown to this release, or an ownerless row is not an anonymous
                # kind: fail instead of running or looping forever.
                reason = ('Anonymous task refused' if refused else
                          'Lease expired on final attempt' if known else 'Unknown task kind')
                connection.execute(update(QueuedTask).where(QueuedTask.id == row['id']).values(
                    status='failed', lease_expires_at=None, claim_token=None, last_error=reason, updated_at=moment))
                if not refused:
                    exhausted.append(dict(row))
                continue
            token = new_id()
            spec = HANDLERS[row['kind']]
            connection.execute(update(QueuedTask).where(QueuedTask.id == row['id']).values(
                status='running', attempts=row['attempts'] + 1, claim_token=token, updated_at=moment,
                lease_expires_at=moment + timedelta(seconds=spec.lease_seconds)))
            claimed = Claim(row['id'], row['user_id'], row['kind'], row['payload'], row['attempts'] + 1,
                            row['max_attempts'], token, row['created_at'])
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


def _fence(task):
    # Only the worker still holding this claim may finish (or delete) the task.
    return (QueuedTask.id == task.id, QueuedTask.claim_token == task.claim_token, QueuedTask.status == 'running')


def _finish(engine, task, **values):
    with engine.begin() as connection:
        return connection.execute(update(QueuedTask).where(*_fence(task)).values(
            lease_expires_at=None, claim_token=None, updated_at=now(), **values)).rowcount == 1


def _delete(engine, task):
    with engine.begin() as connection:
        return connection.execute(delete(QueuedTask).where(*_fence(task))).rowcount == 1


def _deferral_expired(spec, task):
    created = task.created_at
    if created is None:
        return False
    created = created.replace(tzinfo=created.tzinfo or now().tzinfo)
    return created < now() - spec.max_deferral


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
        if isinstance(exc, Retry) and not exc.count_attempt:
            if not _deferral_expired(spec, task):
                # Deferred (e.g. mail quota): refund the attempt this claim took.
                log.info('Queue task %s (%s) deferred', task.id, task.kind)
                _finish(engine, task, status='queued', run_after=now() + timedelta(seconds=delay),
                        attempts=task.attempts - 1, last_error=summary)
                return 'deferred'
            summary = 'Deferred past max_deferral'
            if _finish(engine, task, status='failed', last_error=summary):
                _give_up(engine, spec, task.user_id, task.payload, task.id)
            return 'failed'
        log.warning('Queue task %s (%s) attempt %s failed: %s', task.id, task.kind, task.attempts, summary)
        log.debug(''.join(traceback.format_exception(exc)))
        if task.attempts < task.max_attempts:
            _finish(engine, task, status='queued', run_after=now() + timedelta(seconds=delay), last_error=summary)
            return 'retry'
        if _finish(engine, task, status='failed', last_error=summary):
            _give_up(engine, spec, task.user_id, task.payload, task.id)
        return 'failed'
    if spec.delete_on_success:
        return 'succeeded' if _delete(engine, task) else 'lost'
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
    load_handlers()
    short = [kind for kind, spec in HANDLERS.items() if spec.delete_on_success]
    with engine.begin() as connection:
        removed = connection.execute(delete(QueuedTask).where(
            QueuedTask.status.in_(('succeeded', 'failed')), QueuedTask.updated_at < now() - RETENTION)).rowcount
        if short:
            removed += connection.execute(delete(QueuedTask).where(
                QueuedTask.kind.in_(short), QueuedTask.status.in_(('succeeded', 'failed')),
                QueuedTask.updated_at < now() - SHORT_RETENTION)).rowcount
        return removed
