"""Durable task queue: claims, leases and fencing, retries, admission and the worker.

Most tests register a throwaway `test_kind` handler and drive taskqueue
directly; the last one runs the real `python -m jobtracker.worker` process.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update
from werkzeug.security import generate_password_hash

from jobtracker import create_app, extraction_tasks, taskqueue, worker
from jobtracker.models import QueuedTask, new_id, now

from accounts import add_user, sign_in
from legacy import create_legacy_db

KIND = 'test_kind'
ROOT = Path(__file__).resolve().parent.parent
PASSWORD = 'test-password'


@pytest.fixture
def config(tmp_path):
    return {
        'TESTING': True, 'APP_ENV': 'development', 'SECRET_KEY': 'queue-test-secret',
        'APP_PASSWORD_HASH': generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000'),
        'DATABASE_URL': f'sqlite:///{tmp_path / "queue.sqlite3"}', 'UPLOAD_FOLDER': str(tmp_path / 'uploads'),
        'LOGIN_THROTTLE_DATABASE': str(tmp_path / 'throttle.sqlite3'), 'SESSION_COOKIE_SECURE': False,
    }


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def engine(app):
    return app.extensions['jobtracker_db']['engine']


@pytest.fixture
def register():
    """Register `test_kind`; `behave` runs inside the handler. Returns call records."""
    def add(behave=None, give_up_error=None, **options):
        calls = SimpleNamespace(runs=[], give_ups=[], lock=threading.Lock())

        def run(app, task):
            with calls.lock:
                calls.runs.append(task.id)
            if behave:
                behave(app, task)

        def give_up(engine, user_id, payload):
            calls.give_ups.append((user_id, payload))
            if give_up_error:
                raise give_up_error

        taskqueue.handler(KIND, give_up=give_up, **options)(run)
        return calls

    yield add
    taskqueue.HANDLERS.pop(KIND, None)


def enqueue(engine, payload=None, user_id=1, **options):
    with engine.begin() as connection:
        return taskqueue.enqueue(connection, user_id, KIND, payload or {}, **options)


def row(engine, task_id):
    with engine.connect() as connection:
        return connection.execute(select(QueuedTask.__table__).where(QueuedTask.id == task_id)).mappings().one()


def set_fields(engine, task_id=None, **values):
    with engine.begin() as connection:
        statement = update(QueuedTask).values(**values)
        connection.execute(statement.where(QueuedTask.id == task_id) if task_id else statement)


def utcnow():
    return now().replace(tzinfo=None)


def failing(exc):
    def behave(app, task):
        raise exc
    return behave


def test_enqueued_task_runs_once_and_succeeds(app, engine, register):
    calls = register()
    task_id = enqueue(engine, {'n': 1})
    assert taskqueue.run_once(app) == 'succeeded'
    assert calls.runs == [task_id]
    task = row(engine, task_id)
    assert (task['status'], task['attempts'], task['claim_token'], task['lease_expires_at'], task['last_error']) == (
        'succeeded', 1, None, None, '')
    assert taskqueue.run_once(app) is None


def test_idle_polling_never_takes_the_write_lock(app, engine, register, config):
    register()
    future = enqueue(engine, delay=60)
    blocker = sqlite3.connect(config['DATABASE_URL'].removeprefix('sqlite:///'), isolation_level=None)
    try:
        blocker.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        assert taskqueue.run_once(app) is None
        assert time.monotonic() - started < 1  # busy_timeout is 5 s: a write attempt would wait
    finally:
        blocker.rollback()
        blocker.close()
    assert row(engine, future)['status'] == 'queued'


def test_retry_requeues_with_backoff_and_is_not_claimable_early(app, engine, register):
    outcomes = iter([taskqueue.Retry('flaky', delay=60), None])

    def behave(app, task):
        exc = next(outcomes)
        if exc:
            raise exc

    calls = register(behave, max_attempts=3)
    task_id = enqueue(engine)
    assert taskqueue.run_once(app) == 'retry'
    task = row(engine, task_id)
    assert task['status'] == 'queued' and task['attempts'] == 1 and task['last_error'] == 'Retry'
    assert task['run_after'] > utcnow() + timedelta(seconds=50)
    assert taskqueue.claim(engine) is None and taskqueue.run_once(app) is None
    set_fields(engine, task_id, run_after=now() - timedelta(seconds=1))
    assert taskqueue.run_once(app) == 'succeeded'
    assert calls.runs == [task_id, task_id] and row(engine, task_id)['attempts'] == 2


def test_exhausted_retries_fail_and_give_up_once(app, engine, register):
    calls = register(failing(taskqueue.Retry('still down')), max_attempts=2)
    task_id = enqueue(engine, {'record': 'r1'})
    assert taskqueue.run_once(app) == 'retry'
    set_fields(engine, task_id, run_after=now() - timedelta(seconds=1))
    assert taskqueue.run_once(app) == 'failed'
    assert taskqueue.run_once(app) is None
    assert row(engine, task_id)['status'] == 'failed' and row(engine, task_id)['attempts'] == 2
    assert calls.give_ups == [(1, {'record': 'r1'})]


def test_unexpected_errors_are_retried_and_store_only_the_type(app, engine, register):
    calls = register(failing(ValueError('private page content: salary 123')), max_attempts=2)
    task_id = enqueue(engine)
    assert taskqueue.run_once(app) == 'retry'
    task = row(engine, task_id)
    assert task['status'] == 'queued' and task['last_error'] == 'ValueError'
    assert task['run_after'] > utcnow()
    set_fields(engine, task_id, run_after=now() - timedelta(seconds=1))
    assert taskqueue.run_once(app) == 'failed'
    assert row(engine, task_id)['last_error'] == 'ValueError' and len(calls.give_ups) == 1


def test_expired_lease_is_reclaimed_and_the_stale_holder_is_fenced(app, engine, register):
    calls = register(max_attempts=3)
    task_id = enqueue(engine)
    stale = taskqueue.claim(engine)
    assert stale.id == task_id and taskqueue.claim(engine) is None  # the lease is still live
    set_fields(engine, task_id, lease_expires_at=now() - timedelta(seconds=1))
    fresh = taskqueue.claim(engine)
    assert fresh.id == task_id and fresh.claim_token != stale.claim_token and fresh.attempts == 2
    assert taskqueue.execute(app, stale) == 'lost'  # finishes late: must not overwrite the new claim
    task = row(engine, task_id)
    assert (task['status'], task['claim_token']) == ('running', fresh.claim_token)
    assert taskqueue.execute(app, fresh) == 'succeeded'
    assert row(engine, task_id)['status'] == 'succeeded' and calls.runs == [task_id, task_id]


def test_expired_lease_on_the_final_attempt_fails_and_gives_up(app, engine, register):
    calls = register(max_attempts=1)
    task_id = enqueue(engine, {'record': 'r2'})
    assert taskqueue.claim(engine).id == task_id
    set_fields(engine, task_id, lease_expires_at=now() - timedelta(seconds=1))
    assert taskqueue.run_once(app) is None
    task = row(engine, task_id)
    assert (task['status'], task['last_error'], task['claim_token']) == ('failed', 'Lease expired on final attempt', None)
    assert calls.give_ups == [(1, {'record': 'r2'})] and calls.runs == []


def test_a_failing_give_up_hook_does_not_lose_the_claim(app, engine, register):
    register(give_up_error=RuntimeError('hook broke'), max_attempts=1)
    dead = enqueue(engine)
    taskqueue.claim(engine)
    set_fields(engine, dead, lease_expires_at=now() - timedelta(seconds=1), run_after=now() - timedelta(minutes=5))
    waiting = enqueue(engine)
    claimed = taskqueue.claim(engine)
    assert claimed is not None and claimed.id == waiting
    assert row(engine, waiting)['status'] == 'running' and row(engine, waiting)['claim_token'] == claimed.claim_token
    assert row(engine, dead)['status'] == 'failed'


def test_unknown_kinds_fail_instead_of_looping(app, engine):
    task_id = new_id()
    with engine.begin() as connection:
        connection.execute(insert(QueuedTask).values(
            id=task_id, user_id=1, kind='from_a_newer_release', payload={}, status='queued', attempts=0,
            max_attempts=2, run_after=now(), last_error='', created_at=now(), updated_at=now()))
    assert taskqueue.run_once(app) is None
    task = row(engine, task_id)
    assert (task['status'], task['last_error']) == ('failed', 'Unknown task kind')


def test_concurrent_claims_are_exclusive(config, register):
    apps = [create_app(config), create_app(config)]
    calls = register(lambda app, task: time.sleep(0.005))
    engine = apps[0].extensions['jobtracker_db']['engine']
    ids = [enqueue(engine, {'n': n}) for n in range(20)]

    def drain(index):
        while taskqueue.run_once(apps[index % 2]) is not None:
            pass

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(drain, range(4)))
    assert sorted(calls.runs) == sorted(ids)
    assert {(row(engine, task_id)['status'], row(engine, task_id)['attempts']) for task_id in ids} == {('succeeded', 1)}


def test_prune_deletes_only_old_finished_tasks(app, engine, register):
    register()
    old, recent = now() - timedelta(days=8), now() - timedelta(days=6)
    cases = {('succeeded', old): False, ('failed', old): False, ('succeeded', recent): True,
             ('queued', old): True, ('running', old): True}
    ids = {}
    for (status, moment), _ in cases.items():
        ids[status, moment] = enqueue(engine)
        set_fields(engine, ids[status, moment], status=status, updated_at=moment)
    assert taskqueue.prune(engine) == 2
    with engine.connect() as connection:
        remaining = set(connection.scalars(select(QueuedTask.id)))
    assert remaining == {ids[key] for key, kept in cases.items() if kept}


def test_a_full_queue_refuses_new_previews_until_its_tasks_go_stale(app, engine, register):
    register()
    client = app.test_client()
    headers = sign_in(client, password=PASSWORD)
    ids = [enqueue(engine) for _ in range(extraction_tasks.MAX_PENDING)]
    response = client.post('/api/v1/jobs/extract', json={'url': 'https://example.com/job'}, headers=headers)
    assert response.status_code == 429 and response.json['error'] == extraction_tasks.FULL
    # A dead worker leaves rows behind; once they can no longer succeed they stop counting.
    for task_id in ids:
        set_fields(engine, task_id, created_at=now() - timedelta(seconds=extraction_tasks.TASK_SECONDS + 1))
    assert client.post('/api/v1/jobs/extract', json={'url': 'https://example.com/job'}, headers=headers).status_code == 202


def test_pending_since_ignores_stale_rows(app, engine, register):
    register()
    fresh, stale = enqueue(engine), enqueue(engine)
    set_fields(engine, stale, created_at=now() - timedelta(minutes=10))
    with engine.connect() as connection:
        assert taskqueue.pending(connection) == 2
        assert taskqueue.pending(connection, since=now() - timedelta(minutes=1)) == 1
    set_fields(engine, fresh, status='succeeded')
    with engine.connect() as connection:
        assert taskqueue.pending(connection, since=now() - timedelta(minutes=1)) == 0


def test_preview_admission_is_per_user(app):
    add_user(app, 'second@example.com', 'second-password')
    first, second = app.test_client(), app.test_client()
    first_headers = sign_in(first, password=PASSWORD)
    second_headers = sign_in(second, 'second@example.com', 'second-password')
    for _ in range(extraction_tasks.MAX_ACTIVE):
        assert first.post('/api/v1/jobs/extract', json={'url': 'https://example.com/a'},
                          headers=first_headers).status_code == 202
    busy = first.post('/api/v1/jobs/extract', json={'url': 'https://example.com/a'}, headers=first_headers)
    assert busy.status_code == 429 and busy.json['error'] == extraction_tasks.BUSY
    assert second.post('/api/v1/jobs/extract', json={'url': 'https://example.com/b'},
                       headers=second_headers).status_code == 202


def test_worker_run_processes_tasks_and_stops_on_request(app, engine, register):
    calls = register()
    ids = [enqueue(engine) for _ in range(3)]
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(app, stop), kwargs={'poll_seconds': 0.05})
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while len(calls.runs) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
    assert sorted(calls.runs) == sorted(ids)
    assert {row(engine, task_id)['status'] for task_id in ids} == {'succeeded'}


def test_queue_worker_never_migrates_an_old_schema(config, tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    create_legacy_db(path, 8, generate_password_hash(PASSWORD, method='pbkdf2:sha256:1000'))
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='start the web service'):
        create_app({**config, 'DATABASE_URL': f'sqlite:///{path}', 'QUEUE_WORKER': True, 'SCHEMA_WAIT_SECONDS': 0.2})
    assert time.monotonic() - started < 5
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT version FROM schema_version').fetchall() == [(8,)]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='users'").fetchall() == []


def test_worker_process_runs_a_preview_and_exits_cleanly_on_sigterm(config, tmp_path):
    app = create_app(config)  # The web process owns migrations: create the schema first.
    client = app.test_client()
    headers = sign_in(client, password=PASSWORD)
    env = {**os.environ, 'APP_ENV': 'development', 'SECRET_KEY': config['SECRET_KEY'], 'APP_PASSWORD': PASSWORD,
           'DATABASE_URL': config['DATABASE_URL'], 'UPLOAD_FOLDER': config['UPLOAD_FOLDER'],
           'LOGIN_THROTTLE_DATABASE': config['LOGIN_THROTTLE_DATABASE'], 'QUEUE_POLL_SECONDS': '0.1',
           'PYTHONPATH': str(ROOT)}
    for name in ('APP_PASSWORD_HASH', 'BOOTSTRAP_ADMIN_EMAIL', 'QUEUE_WORKER'):
        env.pop(name, None)
    log = (tmp_path / 'worker.log').open('w')
    process = subprocess.Popen([sys.executable, '-m', 'jobtracker.worker'], cwd=ROOT, env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    try:
        # Rejected by the worker's address check: no network is ever used.
        response = client.post('/api/v1/jobs/extract', json={'url': 'http://127.0.0.1/x'}, headers=headers)
        assert response.status_code == 202
        deadline = time.monotonic() + 10
        while (result := client.get(f"/api/v1/extractions/{response.json['id']}").json)['status'] == 'running':
            assert time.monotonic() < deadline, (tmp_path / 'worker.log').read_text()
            time.sleep(0.1)
        assert result['status'] == 'failed' and 'private addresses are unavailable' in result['error']
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0, (tmp_path / 'worker.log').read_text()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        log.close()
