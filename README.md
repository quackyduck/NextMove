# Next Move

Private, single-user job search tracker: Flask + SQLAlchemy/SQLite API, React + Bootstrap interface, password session login, and bearer-token agent access. The first demo supports manual intake, spreadsheet/board views, stage/outcome updates, notes and next actions, contacts, versioned resume uploads, and activity history.

URL extraction, Celery/Redis, Numbers import, and DigitalOcean deployment are future milestones. A posting URL is stored as a reference today; paste the description during intake.

## Run the local demo

Prerequisites: Python 3.11+ (tested with 3.14), Node.js 22.12+ and npm. From the repository root:

```sh
./scripts/setup.sh
./scripts/run.sh
```

Open [http://127.0.0.1:5001](http://127.0.0.1:5001). The local development password is `local-demo-only` unless `APP_PASSWORD` or `APP_PASSWORD_HASH` is configured. No debug server or reloader is enabled. Keep the local server bound to loopback.

Setup installs the tested Python versions from `requirements.lock.txt`, installs the frontend with `npm ci`, and builds the UI that Flask serves. It creates a private `.env` with a random session secret only when `.env` does not already exist. Existing settings and personal records are preserved. `requirements.txt` lists direct dependencies; refresh both files deliberately after testing upgrades.

Data persists in `instance/jobtracker.sqlite3`; resume files are in `instance/resumes`. These paths and `.env` are ignored by Git. Stop the server with Ctrl-C. Restarting the server preserves your data. Do not delete `instance` to reset a demo containing personal records.

## Tests

```sh
./scripts/test.sh
cd frontend
npx playwright install chromium
cd ..
.venv/bin/python scripts/test_ui.py
```

Python tests create temporary databases/uploads. The UI test launcher starts its own loopback server on an available port with temporary database/uploads, then stops it and removes its test data. Do not point browser tests at your personal demo: tests create records. `E2E_BASE_URL`/`E2E_PASSWORD` are intended only for a separately isolated test deployment. `./scripts/test.sh --ui` runs build and browser tests together after browser installation.

## Agent API

Configure `AGENT_API_TOKEN` in your private `.env` and restart the server. Requests use `Authorization: Bearer <token>`; never put the token in a URL. Browser sessions use CSRF tokens returned by login. See [API contract](docs/API.md) and `scripts/agent_example.py --help` for a create-job/contact/resume workflow. The example reads `JOBTRACKER_AGENT_TOKEN` from its environment; use the same value as the server's `AGENT_API_TOKEN`.

## Operations and next steps

[Implementation plan and milestone checklist](docs/PLAN.md) describes the parallel agent roles and incremental demos. [Operations and deployment runbook](docs/OPERATIONS.md) includes verified local backup/restore and the gates for a future private DigitalOcean deployment. Nothing has been provisioned or deployed.

[DigitalOcean deployment plan](docs/DEPLOYMENT_PLAN.md) provides the phased checklist for a private preview, production preparation, HTTPS cutover, recovery, and later Celery rollout.

[Production preparation](docs/PRODUCTION_PREPARATION.md) records the inspected existing host, pending user decisions, local runtime templates, persistent login defense, and remaining validation. These templates have not been installed or deployed.
