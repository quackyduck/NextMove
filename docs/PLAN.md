# Job Tracker implementation plan

Created October 19, 2026. Local development first; deploy only after demos and release checks.

## Product decisions

Single-user, private job-search workspace. Flask application factory, SQLite via SQLAlchemy, React/Vite with Bootstrap, versioned JSON API, Celery with Redis for asynchronous ingestion, Git source control. Use a modular monolith with explicit service interfaces: it is easy to run and back up while keeping UI, storage, API, extraction, and document handling replaceable. No paid services or LLM provider required for initial demos.

The source Numbers file remains unchanged. Its embedded preview shows pipeline summaries, role lanes, priority, resume tracking, next steps, and contact follow-ups. Preview inspection is not a row-level import. Treat spreadsheet text and fetched job descriptions as untrusted source data, never executable instructions.

## Workflow and domain

Default stages, in order: Interesting → Resume updated → Outreach → Applied → Recruiter interview → Hiring manager interview → Full panel interview → Offer. Separate outcome (active, rejected, withdrawn, closed, accepted) from stage, so a rejection does not erase progress. Allow stages to be skipped or revisited; show every change in an activity timeline. Resume preparation and outreach may happen in either order; stage is a convenient current position, not proof all earlier actions occurred.

Job fields: UUID, title, company, source URL, found date (defaults to today's America/Los_Angeles date), description, location, salary text, lane/category, priority, stage, outcome, notes, next action, next-action date, created/updated timestamps and revision. Intake permits URL-only or pasted-text-only records with a placeholder title editable later. Contact records belong to jobs initially; name/email optional. Versioned resume records retain original filename, opaque stored filename, media type, size, SHA-256 and timestamp; attached to a job and downloadable by authorized user/agent. Files live outside public static assets. Activity records are append-only. Future extraction attempts retain status, error and proposed fields.

## Boundaries and contracts

- `jobtracker/db.py`, `models.py`, `services.py`: database owner. Own schema, migrations, serialization, atomic transactions, constraints and history; no HTTP concerns.
- `jobtracker/api.py`, `auth.py`, `__init__.py`: API owner. Own factory, authentication, validation, stable errors and file access; call services instead of duplicating SQL.
- `frontend/`: service/site owner. React renders API data; no direct database access. Own accessibility, loading/error/empty states, forms, table, board and details.
- `jobtracker/ingestion/`: extraction owner in later wave. Fetching and parsing independent from task queue. Extraction proposes values, never overwrites user edits silently.
- `tests/`, `frontend/tests/`: tests belong to modules; reviewer independently checks integration and regressions.
- `scripts/`, deployment manifests, README: release manager. Own reproducible commands, health checks, backup/restore instructions and release evidence.

### First-demo shared API contract

Prefix `/api/v1`. JSON errors `{ "error": "message" }`. Job objects use snake_case and include `id`, `title`, `company`, `url`, `found_date`, `description`, `location`, `salary`, `lane`, `priority`, `stage`, `outcome`, `notes`, `next_action`, `next_action_date`, `created_at`, `updated_at`, `version`. Stage values: `interesting`, `resume_updated`, `outreach`, `applied`, `recruiter_interview`, `hiring_manager_interview`, `panel_interview`, `offer`. Outcome values: `active`, `rejected`, `withdrawn`, `closed`, `accepted`. Priority: `low`, `medium`, `high`.

- `GET /health` public readiness, no private data.
- `POST /auth/login` `{password}`; `GET /auth/session`; `POST /auth/logout`.
- Session auth for browser; configured bearer token for agents. CSRF protection for session mutations using a returned token and `X-CSRF-Token`; bearer requests exempt.
- `GET /jobs` → `{jobs: [...]}`; filters can be applied in first-demo client.
- `POST /jobs` → job, 201; `GET /jobs/<id>` → job.
- `PATCH /jobs/<id>` → job; supplied version supports optimistic conflict detection (409).
- `GET /jobs/<id>/activities` → `{activities: [...]}`.
- `GET/POST /jobs/<id>/contacts` → `{contacts: [...]}` / contact.
- `GET/POST /jobs/<id>/resumes` → `{resumes: [...]}` / resume. Upload multipart field `file`; filename/name metadata never controls filesystem paths.
- `GET /resumes/<id>/download` authorized download.
- Later: extraction submit/status/review, CSV import preview/export, scoped token issuance/revocation, OpenAPI schema and agent examples.

## Parallel agents and integration rules

Create five distinct roles: Service/site, Database, API layer, Code reviews, Release manager. Three worker slots are available concurrently; run Service/site + Database + API first, then Code reviews + Release manager as slots free. Primary agent coordinates contracts, integration and local demos. All share this checkout, so ownership boundaries matter. Never reset another agent's work or run concurrent Git commits. Primary agent serializes commits. Communicate contract changes before using them; reviewers produce findings and can patch owned tests after coordination.

## Milestone 0 — Foundation and agreements

- [x] Inspect repository and available Numbers reference preview.
- [x] Choose stack and document module/API ownership.
- [x] Define stages, outcomes, dates, document handling and fallback behavior.
- [x] Initialize Git, ignore secrets/DB/uploads/build artifacts.
- [x] Create isolated Python environment and reproducible frontend dependencies.
- [x] Establish documented local run/test commands.

Exit: each module owner can work against the contract independently.

## Milestone 1 — Useful manual tracker (first local demo)

- [x] 1. Database agent creates SQLAlchemy models, repository/service layer, initial migration strategy and isolated tests. Enable foreign keys, WAL, bounded busy timeout; use short transactions. Stage updates and activity writes must commit atomically. Use durable IDs, ISO dates and UTC timestamps.
- [x] 2. API agent creates Flask factory, password sessions, bearer auth, CSRF, validation, job CRUD, contacts and resume upload/download. Enforce upload size/type limits, private paths, safe downloads and consistent errors. Configuration comes from environment; demo credentials are development-only and production must fail closed.
- [x] 3. Service/site agent creates responsive dashboard with summary counts, table-first view, board toggle, intake form, job details, editable progress, notes, next actions, contacts, resume upload and activity timeline. Provide accessible labels, keyboard stage controls, pending state, failures and retry.
- [x] 4. Integrate real persistent backend. Keep frontend demo/fixtures separate from actual user data.
- [x] 5. Test create → reload → edit → stage change → history, auth, invalid input, dates, upload/download and API collaboration.
- [x] 6. Start local servers bound to loopback, run browser tests, report exact demo URL, credentials/setup command and limitations.

Exit: a job can be captured manually, tracked across stages and revisited after reload without Redis or extraction. User can run this demo immediately while later modules evolve.

## Milestone 2 — Review and release foundation

- [x] 1. Independent code-review agent checks authorization, CSRF, upload traversal, validation, transaction behavior, user-visible errors, data loss and frontend integration.
- [x] 2. Release-manager agent creates repeatable setup/run/test scripts, environment template, CI, health checks and a deployment runbook. Add initial migration and backup/restore verification against disposable data.
- [x] 3. Primary agent resolves high-impact findings, runs Python + frontend + browser tests and records actual evidence.
- [x] 4. Add API documentation and a copyable agent example: create job, append contact, upload modified resume, read activity. Preserve earlier resume versions.

Exit: reproducible manual demo with documented limitations and no unresolved blocking findings.

## Milestone 3 — Asynchronous ingestion, always optional

- [ ] 1. Add Redis/Celery worker with task status persisted in SQLite: queued, running, needs_review, failed, completed. Web process never waits for extraction.
- [ ] 2. Separate URL validation, secure HTTP fetch, parsing, proposal persistence and user approval. Allow only HTTP/S, reject credentials and private/loopback/link-local/metadata destinations; revalidate DNS and redirects, pin connections or enforce egress policy against DNS rebinding. Limit timeout, redirects, response size and content type. Treat HTML as inert input.
- [ ] 3. Extract schema.org JobPosting JSON-LD first, then common HTML metadata; retain source and extraction timestamp. Do not require LLMs. JS-only/blocked sites surface a helpful manual-paste fallback.
- [ ] 4. Review extracted company/title/location/salary/description before applying; never overwrite edits made after task start. Add idempotency, bounded retries for transient failures, job cancellation/deletion behavior and redacted errors.
- [ ] 5. Unit-test parsing fixtures and SSRF defenses; integration-test real Redis/worker success, retry, failure, restart and concurrent manual edit. Eager task tests alone do not prove queue integration.

Exit demo: paste URL, keep working, review extraction or paste description when extraction fails.

## Milestone 4 — Personal workflow and migration

- [ ] 1. Extract Numbers data read-only using a supported parser or exported CSV; do not infer rows from preview. Produce an import preview with field mapping and duplicate report before committing rows.
- [ ] 2. Map spreadsheet statuses to stages/outcomes, preserve unknown values and original notes/resume references. Distinguish workbook summary cells from actual job rows.
- [ ] 3. Add CSV export, import rollback/batch IDs, search/filter/sort persistence, overdue next actions and contact follow-ups.
- [ ] 4. Add resume version labels, links between jobs and chosen versions, and optional resume-generated activity. Keep automated outreach/submission out of scope unless requested.

Exit: imported counts and representative rows reconciled to source; full export and restore verified.

## Milestone 5 — DigitalOcean deployment decision

Suggested topology: one Docker-enabled Droplet, Caddy HTTPS reverse proxy, Gunicorn Flask serving built React, worker, Redis, persistent SQLite and uploads volumes. SQLite requires durable local disk and a single app host; use one worker at first. Migrate to PostgreSQL if multi-host scaling/write contention becomes real. Do not place SQLite on ephemeral App Platform storage.

- [ ] 1. Release manager prepares Docker/Compose, non-root processes, pinned dependencies, health checks, production env validation, domain/TLS instructions and rollback procedure.
- [ ] 2. Password hash and random secret outside source control; HTTPS-only cookies, login throttling, scoped/revocable agent tokens, no broad CORS, request size limits and secure headers. No demo credentials/sample private data in production.
- [ ] 3. Back up SQLite consistently with its online backup API and include uploads in a coordinated maintenance snapshot; encrypt off-host backups. Restore into a clean environment and verify job/file integrity. Define retention and rotate secrets.
- [ ] 4. Verify production image locally: auth, browser flow, real queue, worker restart, persistence across recreation, migrations from prior release, backup/restore and rollback compatibility.
- [ ] 5. Present evidence plus remaining blockers. Obtain user decision on domain, Droplet/account access and spending before provisioning. Deployment remains a separate decision; do not publish automatically.
- [ ] 6. On approval, provision, deploy, smoke-test HTTPS/auth/agent API/queue and verify backup operation. Document deployed version and rollback command.

Exit: user-approved private deployment with recoverable data and verified operations.

## Testing strategy and release gates

- Python pytest: domain defaults/transitions/history, SQL constraints, migrations, rollback, persistence, optimistic conflicts, validation, login/logout, unauthorized access, CSRF, bearer access, contact and file authorization, upload/download integrity.
- React component tests where behavior merits them; typecheck/build always. Playwright real-browser tests for login, manual intake, reload persistence, table/board, stage changes/history, contacts, resume upload/download, errors and narrow viewport.
- Ingestion fixture tests plus real broker/worker integration; no tests depend on external job websites.
- Isolated temporary SQLite/uploads and separate test credentials; never point tests at personal data.
- Per milestone run affected checks; full suite at integrated demo/release. Report actual pass/fail and untested boundaries. Keep screenshots/traces for diagnosing failures.
- No deployment gate passes with known authentication bypass, data loss, public resumes, broken migration, unverified restore or failing critical workflow.

## Implementation sources

Official architecture/testing references: https://flask.palletsprojects.com/en/stable/patterns/appfactories/ and https://flask.palletsprojects.com/en/stable/testing/ . Task design: https://docs.celeryq.dev/en/stable/userguide/tasks.html . DigitalOcean Docker baseline: https://docs.digitalocean.com/products/marketplace/catalog/docker/ . Pin installed versions in project lockfiles and verify locally rather than assuming version compatibility.

## Deferred decisions

No decision needed to start: use table as default, optional board, local files for resumes, manual intake first. Before deployment only: domain, approved infrastructure spend/account access, backup destination. Later product choices: reminders, email/calendar integration, LLM extraction provider and multi-user support.

## Current checkpoint — first local demo

Milestones 0–2 are complete for the local manual tracker. Five distinct agent roles participated in two waves. Verified: 32 Python tests, 4 real Chromium browser tests, frontend build, fresh lock-based Python install, disposable coordinated backup/restore, and live bearer-authenticated agent resume upload. CI is configured but has not run remotely. See REVIEW.md and OPERATIONS.md for evidence and remaining deployment gates. Local user demo: http://127.0.0.1:5001 . Celery extraction, Numbers row import and DigitalOcean deployment remain unchecked future milestones.
