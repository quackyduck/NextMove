# Operations and deployment runbook

Status: local manual tracker only. No infrastructure has been provisioned and no deployment is authorized by these instructions. Celery/Redis and URL extraction are not implemented; pasted descriptions keep intake useful.

Demo scope update, October 19: the user selected `/nextmove/` on the existing
host, an empty hosted workspace, a homepage link, and persistent login throttling.
They explicitly deferred backups/recovery testing. [Production preparation](PRODUCTION_PREPARATION.md)
records this superseding scope and the uninstalled nginx/systemd configuration.
Docker/Caddy are not required for this demo. Statements below about absent
manifests or process-local throttling describe the original baseline. Production
credentials, Linux capacity, and public acceptance remain pending.

## Local health and configuration

Run `./scripts/run.sh` and check `curl --fail http://127.0.0.1:5001/health`. The health response checks database connectivity. It does not prove resume-file integrity or a functioning queue. The same Flask process serves the built frontend and API. Rebuild after frontend changes.

`APP_ENV=development` is the precise development value. `.env` stays private; do not source it into a shell because password hashes may contain shell metacharacters. Load settings through Flask/python-dotenv. Agent examples use `JOBTRACKER_AGENT_TOKEN`; the server uses `AGENT_API_TOKEN` with the same secret value. Python dependencies are pinned in `requirements.lock.txt`; frontend versions are locked by `frontend/package-lock.json`.

## Coordinated backup and restore

Stop the web server and every agent/worker writer before taking a coordinated database + uploads snapshot. SQLite's online backup API makes a consistent database copy, but the database and files need this maintenance window together. `--maintenance` records your confirmation; it does not stop processes or enforce a lock. The commands require explicit paths and never load `.env` or infer a live database.

```sh
.venv/bin/python scripts/backup.py --maintenance backup \
  --database "$PWD/instance/jobtracker.sqlite3" \
  --uploads "$PWD/instance/resumes" \
  --destination /absolute/private/backups/jobtracker-2026-10-19

.venv/bin/python scripts/backup.py --maintenance restore \
  --snapshot /absolute/private/backups/jobtracker-2026-10-19 \
  --destination /absolute/private/restored-jobtracker
```

Both destinations must be new directories. Keep snapshots outside uploads. Backup records checksums for every file and verifies SQLite integrity/foreign keys. Restore validates checksums before copying and refuses to overwrite an existing directory. Failed operations may leave a partial destination: inspect or remove that disposable destination before retrying. A checksum manifest detects accidental corruption; it does not authenticate a backup from an untrusted source.

To inspect a restoration, keep the original app stopped or use a different port and set `DATABASE_URL=sqlite:////absolute/private/restored-jobtracker/database.sqlite3` and `UPLOAD_FOLDER=/absolute/private/restored-jobtracker/uploads` in that app's environment. Verify job counts, activity, and a resume download before changing the active installation. Never replace a database while any process has it open; retained `-wal`/`-shm` sidecars can invalidate ad hoc replacements. A full restore uses a new directory and explicit configuration.

`tests/test_backup.py` verifies disposable SQLite/WAL + file round-trip, checksum corruption detection, non-overwrite behavior, nested manifest handling, and rejection of recursive destinations. No actual user records are used. Future production backups must be encrypted off-host, access restricted, retained on a documented schedule, and restored periodically. Retention, off-host destination, and scheduling remain deployment decisions.

## Candidate DigitalOcean topology

One Droplet with durable local disk, Caddy TLS reverse proxy, Gunicorn serving the React build, SQLite, private resume files, and later Redis/Celery. One host initially; do not place SQLite on ephemeral filesystem or share it between hosts. The app is currently validated using local Flask only; no Docker image, Compose deployment, real queue, TLS proxy, or off-host backup has been verified.

Production requires `APP_ENV=production`, a fresh random `SECRET_KEY` of at least 32 characters, and `APP_PASSWORD_HASH` generated with Werkzeug. Keep secrets outside Git and images, and use a separate agent token. Never deploy the local password. Gunicorn must listen on a private socket/network behind Caddy. Enable `TRUST_PROXY=1` only with exactly one trusted reverse proxy and no public/direct access to Gunicorn; it trusts forwarded protocol for HTTPS origin checks. Keep the original Host header and reject unknown hosts at the proxy. HTTPS cookies require real HTTPS in production.

## Gates before deployment

- [ ] Approve domain, account access, infrastructure cost, and backup destination.
- [ ] Add and test Docker/Compose and Caddy configuration; non-root process, persistent volumes, health checks, and no debug server.
- [ ] Verify password/login throttling, secure cookies, forwarded protocol, origin/CSRF checks, upload limits, security headers, and bearer-token handling through the proxy.
- [ ] Add scoped/revocable agent credentials and stronger persistent/shared login throttling before public exposure; current token is one global bearer secret and throttling is process-local.
- [ ] Implement and verify Redis/Celery extraction with real worker success/failure/restart tests and SSRF defenses, or explicitly approve a manual-only release.
- [ ] Run Python tests, UI tests, production image browser flow, resume download, migration from prior release, restart persistence, and an encrypted off-host backup/restore drill.
- [ ] Record release commit/image digest, expected schema version, config locations, storage ownership, and logs with private values redacted.
- [ ] Present release evidence and remaining limitations for the deployment decision.

## Rollback policy

Keep the prior image and a coordinated pre-upgrade snapshot. Stop writers before any migration; current schema is version 3. Startup transactionally upgrades versions 1/2 to 3 and rejects unsupported/unversioned schemas. There is no downgrade command. Never run an older image against a newer schema unless compatibility has been explicitly tested. Roll back application code alone only for a compatible schema; otherwise restore the coordinated snapshot into a new directory and point the prior release at that restored data. Snapshot rollback loses changes made after the snapshot, so review that loss before switching. Smoke-test login, a known job/activity, and a resume before reopening writes.

## Local release evidence — October 19, 2026

- `./scripts/test.sh --ui`: **32 Python tests passed**, Vite production build passed, **4 Chromium browser tests passed**. Browser coverage includes login failures, manual capture/reload/stage/history, contact and resume upload/download, board/mobile view, partial intake recovery, distinct field labels, and stale detail navigation responses.
- Browser tests used `scripts/test_ui.py` with a dynamically selected loopback port and disposable database/uploads; the launcher stopped the test server and removed its files. Personal demo storage was not used.
- A fresh temporary virtualenv installed `requirements.lock.txt`, `pip check` reported no broken requirements, and the same **32 Python tests passed**. Verified on macOS/Python 3.14; GitHub's Linux workflow is prepared but has not run.
- The backup tests included a disposable coordinated SQLite/WAL + uploads restore and corruption/non-overwrite checks. No personal data was backed up or restored during validation.
- Shell scripts passed `bash -n`. Production image, external TLS proxy, real queue, DigitalOcean provisioning, off-host encryption/retention, and schema upgrades beyond initial version 1 remain unverified/deferred.

## Outreach badge release — schema 2

This release adds a persistent `outreach_completed` boolean. Version 1 databases upgrade at startup under an explicit SQLite write transaction, including the column addition, evidence-based backfill, and version change. Stop all old app/worker writers before starting the new release. Do not restart the current local demo or deploy a feature-branch artifact before the PR merges.

Backfill marks only jobs currently at Outreach or whose recorded stage history includes entering/leaving Outreach. Applied/interview/offer jobs without such evidence remain unmarked; users can mark them manually. It preserves existing job IDs, versions, contacts, resume metadata/files and activity. Subsequent restarts do not repeat backfill or undo a user's correction. A migration error rolls back the schema change and leaves version 1 intact.

Before upgrading a workspace with data, retain a private coordinated pre-upgrade database/files checkpoint with writers stopped. This is a release rollback checkpoint, not new scheduled/off-host backup infrastructure. Existing hosted-demo decisions deferring backup infrastructure still apply. A version-1 binary refuses version 2; code-only rollback is not supported. For rollback restore the matching pre-upgrade data checkpoint into new storage and use the previous binary, explicitly accounting for post-checkpoint writes. Do not downgrade by deleting the new column in a live database.

Validation for this change is recorded in its PR. Future upgrades must add explicit numbered migrations rather than use `create_all` to alter an existing schema.

## Password settings release — schema 3

Schema 2 → 3 adds the singleton `workspace_credentials` table without modifying job/contact/resume/history records. The existing configured password hash seeds it once. Subsequent starts use the database credential and do not overwrite a UI change with an older environment value. Keep the deployment configuration/secret key intact; the app still requires production bootstrap configuration and fails closed when credentials cannot be read. SQLite workspace files are explicitly mode 0600.

Existing pre-upgrade cookies have no session generation and are signed out once on deployment. Password changes rotate the current cookie/CSRF and atomically increment the stored generation, revoking other browser sessions including copied cookies. Concurrent password verification cannot overwrite a newer credential or acquire its generation. Bearer access cannot change the password.

Existing coordinated SQLite + uploads snapshots include the verifier/session generation because they are part of the main database. Treat those snapshots as private credentials. Stop writers and retain a rollback checkpoint before migration. A schema-2 binary cannot open schema 3; rollback requires the matching database checkpoint and can lose later writes/password changes. A restored snapshot may reinstate an older credential/session generation: rotate `SECRET_KEY` during a full restore to invalidate all pre-restore cookies, and verify or reset the intended password before reopening access. This does not create new backup infrastructure.

Administrator recovery uses the hidden interactive `flask --app jobtracker reset-workspace-password` command in the actual service runtime/configuration, under the correct data identity. Do not source the environment file into a shell, print secrets, pass a new password in command arguments, or run the command against a different database. Recovery is compare-and-swap protected and revokes all sessions. UI changes deliberately do not rewrite the root-owned environment file or any saved local password record; those older records are no longer authoritative once the user changes their password.

This PR is independent of the session-expiry recovery PR (#3). Merge/rebase as needed; do not deploy a feature-branch artifact. No hosted password, local personal database, or production configuration was changed while implementing/testing it.


## Quick capture release — schema 4

Stop writers and take the usual database/uploads backup before upgrading. Schema 4 adds `extraction_tasks` and its creation-time index; existing records are preserved. Rollback to a schema-3 binary requires its matching database checkpoint. Migration tests verify preservation of existing role notes.

The web worker runs at most two extractions in daemon threads, with private previews shared through SQLite. Limits: 2 MiB pages, four redirects, five-second socket timeouts, and a 20-second network-read budget. DNS uses the OS resolver; per-process thread slots stay bounded if it stalls. Interrupted/stale tasks fail after 60 seconds on polling or submission. Retry is explicit. Previews expire after one day and are pruned on subsequent submissions.

No Redis/Celery service, dependency, or environment variable is required. The existing preloaded Gunicorn worker starts threads only after fork, on submission. Use a dedicated durable queue if execution must survive process restarts or scale beyond this single-user workspace. Previews contain page text and retain the workspace's private storage and authenticated/no-store API protections.


## Draft API onboarding — schema 6 (not deployed)

The ingestion draft adds schema 5 receipts and schema 6 managed `agent_tokens` plus nullable receipt ownership. Use the standard stopped-writer database/uploads checkpoint before any eventual deployment; an older binary requires the matching checkpoint for rollback. Existing roles, credentials, previews and receipts remain intact. Pre-onboarding receipts have no owner, so new tokens cannot read them. Stored keys still prevent accidental duplicate creation.

Tokens are created/revoked through browser-only, CSRF-protected Agent access; creation re-verifies the current workspace password with the shared persistent attempt budget. Only hashes and safe lifecycle metadata persist. Managed tokens do not use `INGEST_API_TOKEN_SHA256`; remove obsolete draft settings when deploying, and leave the unrelated broad agent token disabled. No tokens are seeded during migration, and no configuration restart is needed for later creation/revocation. The single-user API becomes usable when the owner creates the first token after deployment.

Revocation/expiry rejects future calls while leaving other tokens and roles intact; accepted work may finish. Workspace password changes do not revoke tokens. SQLite snapshots now include verifier hashes, token lifecycle metadata and receipt ownership; treat them as private. For replacement, create a fresh token, configure/test the agent, then revoke the original. Keep the original idempotency key when retrying with the original token; a new token cannot claim the old token's key or receipt. Client-side connection checks and UI checks are non-mutating except for last-used metadata.
