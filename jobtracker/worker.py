"""Queue worker process: `python -m jobtracker.worker` or `flask --app jobtracker worker`.

Single-threaded on purpose: one task at a time keeps resident memory near the
web worker's (~45 MiB) on the shared small Droplet. Run more processes, not
threads, if throughput is ever needed. SIGTERM/SIGINT finish the current task
and exit, so systemd restarts never abandon a claim mid-write (an abandoned
claim is still recovered when its lease expires).
"""
import logging
import os
import signal
import threading
import time

import click

from . import taskqueue

log = logging.getLogger('jobtracker.worker')
MAINTENANCE_SECONDS = 3600


def run(app, stop=None, poll_seconds=None):
    stop = stop or threading.Event()
    poll = float(poll_seconds or app.config.get('QUEUE_POLL_SECONDS') or 1)
    engine = app.extensions['jobtracker_db']['engine']
    log.info('Queue worker started (pid %s, poll %.1fs)', os.getpid(), poll)
    next_maintenance = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() >= next_maintenance:
                with app.app_context():
                    from .extraction_tasks import expire_tasks
                    with engine.begin() as connection:
                        expire_tasks(connection)
                    taskqueue.prune(engine)
                next_maintenance = time.monotonic() + MAINTENANCE_SECONDS
            if taskqueue.run_once(app) is None:
                stop.wait(poll)
        except Exception:  # noqa: BLE001 - e.g. a locked database; back off and continue
            log.exception('Queue worker iteration failed')
            stop.wait(min(30, poll * 5))
    log.info('Queue worker stopped')


def install_signal_handlers(stop):
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())


def init_app(app):
    @app.cli.command('worker')
    @click.option('--once', is_flag=True, help='Run until the queue is empty, then exit.')
    def worker_command(once):
        """Run the background task worker (separate from the web process)."""
        if once:
            click.echo(f'Processed {len(taskqueue.drain(app))} task(s)')
            return
        stop = threading.Event()
        install_signal_handlers(stop)
        run(app, stop)


def main():
    from . import create_app
    logging.basicConfig(level=os.environ.get('WORKER_LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # Wait for (never run) migrations: the web service owns schema upgrades.
    app = create_app({'QUEUE_WORKER': True})
    stop = threading.Event()
    install_signal_handlers(stop)
    run(app, stop)


if __name__ == '__main__':
    main()
