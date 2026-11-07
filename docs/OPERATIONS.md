# Operations and deployment runbook

> **Test server.** This document describes the nicolasmelo.com/nextmove install, which is now the test server. Production is https://mynextmoov.com; see [Deployments](DEPLOYMENTS.md).

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


## API onboarding — schema 7

The ingestion API adds schema 5 receipts and schema 6 managed `agent_tokens` plus nullable receipt ownership. Use the standard stopped-writer database/uploads checkpoint before any eventual deployment; an older binary requires the matching checkpoint for rollback. Existing roles, credentials, previews and receipts remain intact. Pre-onboarding receipts have no owner, so new tokens cannot read them. Stored keys still prevent accidental duplicate creation.

Tokens are created/revoked through browser-only, CSRF-protected Agent access; creation re-verifies the current workspace password with the shared persistent attempt budget. Only hashes and safe lifecycle metadata persist. Managed tokens do not use `INGEST_API_TOKEN_SHA256`; remove obsolete draft settings when deploying, and leave the unrelated broad agent token disabled. No tokens are seeded during migration, and no configuration restart is needed for later creation/revocation. The single-user API becomes usable when the owner creates the first token after deployment.

Revocation/expiry rejects future calls while leaving other tokens and roles intact; accepted work may finish. Workspace password changes do not revoke tokens. SQLite snapshots now include verifier hashes, token lifecycle metadata and receipt ownership; treat them as private. For replacement, create a fresh token, configure/test the agent, then revoke the original. Keep the original idempotency key when retrying with the original token; a new token cannot claim the old token's key or receipt. Client-side connection checks and UI checks are non-mutating except for last-used metadata.

Schema 7 rebuilds the jobs table transactionally to add the `needs_review` CHECK value. It preserves IDs, stages, versions and child records, defers foreign keys only during replacement, explicitly validates all references before commit, and retains FK enforcement. Stop writers and take the usual matching database/uploads backup before upgrading.

## Not Interested outcome — schema 8

Schema 7 → 8 expands the jobs outcome CHECK constraint to accept
`not_interested`. SQLite requires a transactional rebuild of the jobs table;
existing rows (including outcome, stage, version, timestamps, and notes), child
records, and attachments remain intact. Foreign keys are deferred during the
rebuild and checked before commit. No existing roles are reclassified. A failed
migration rolls back the table, rows, constraint, and schema version.

Before deployment, stop all writers and retain the normal coordinated database
and uploads checkpoint. Deploy only after merge from latest `origin/main`. This
feature does not require credentials, services, or dependencies. A schema-7 binary
refuses schema 8: rollback requires the matching pre-upgrade checkpoint in new
storage and can discard later changes. Do not remove the outcome from live data
or relabel the schema version to force a downgrade.

Tests cover migration preservation/rollback, database and API validation, outcome
history, unchanged stage, restart persistence, and filtering in table/board views
with disposable storage. Building this feature does not modify hosted records.

## Accounts, tenancy and queue worker — schema 9

Schema 8 → 9 turns the single workspace into user #1 of a multi-user app. It
adds `users`, `invite_codes` and `queued_tasks`, rebuilds `jobs`, `contacts`,
`resumes`, `activities`, `agent_tokens`, `extraction_tasks` and
`ingestion_requests` with a NOT NULL `user_id` (children reference
`jobs(id, user_id)`, so a contact or resume can never point at another
account's role), and drops `workspace_credentials`. Every row keeps its ID,
version and timestamps. User #1 (flagged as the owner the deprecated environment tokens act for)
keeps the existing password hash and session generation, so current sessions must sign in once more (the cookie now carries
the account) but the password is unchanged. Its email comes from
`BOOTSTRAP_ADMIN_EMAIL` at migration time, else the placeholder
`owner@localhost`; change it with `flask --app jobtracker user set-email 1 you@example.com`.
The whole schema change is one transaction: any failure, including a foreign-key
check failure, leaves schema 8 untouched. A database without a workspace
credential seeds user #1 from `APP_PASSWORD_HASH`, or refuses to upgrade, so the
first sign-up can never become user #1. Account IDs are `AUTOINCREMENT` and are
never reused.

Resume files move from `UPLOAD_FOLDER/<file>` to `UPLOAD_FOLDER/<user_id>/<file>`
(directories mode 0700) after the schema transaction commits. The move runs at
every startup, is a same-filesystem `rename`, skips files already in place, and
finishes on the next start if interrupted. Downloads only look inside the
requesting account's directory.

### Upgrade steps

1. Stop the web service, the worker (if an older build has one) and every agent writer.
2. Take the coordinated snapshot (`scripts/backup.py --maintenance backup ...`) and keep it.
3. Add `BOOTSTRAP_ADMIN_EMAIL` (and optionally `SIGNUP_ENABLED`, `SIGNUP_REQUIRES_INVITE`,
   `QUEUE_POLL_SECONDS`) to the environment file; see `deploy/production.env.example`.
4. Start the web service once; it migrates and relocates files (the worker never
   migrates: it waits up to two minutes for the web service, then exits for systemd to retry). Check
   `flask --app jobtracker user list` shows user #1 with the expected email.
5. Install and start `deploy/next-move-worker.service` (below).
6. Smoke test: sign in with email + password, open a known role, download a resume,
   extract a posting URL, and confirm a second account (signed up with
   `flask --app jobtracker invite create`) sees none of user #1's data.

### Rollback

A schema-8 binary refuses schema 9. Roll back by stopping all services, restoring
the step-2 snapshot into a new directory (`scripts/backup.py --maintenance restore`)
and pointing the previous release at it; the snapshot holds the flat upload layout
the old code expects. Changes made after the snapshot (including new accounts) are
lost. Do not hand-edit `schema_version` or move files back by hand.

### Queue worker

URL extraction (`/jobs/extract`) and agent ingestion (`/ingestions`) no longer
start threads in Gunicorn. The web request inserts the preview/receipt and a
`queued_tasks` row in one transaction; `python -m jobtracker.worker` (unit
`deploy/next-move-worker.service`, or `flask --app jobtracker worker`) claims one
task at a time. Claims are leases (75 s, longer than the 60 s preview
deadline) with a per-claim token, so a live worker is never reclaimed mid-fetch,
a crashed worker's task is failed cleanly, and a stale worker cannot overwrite a
newer result. Finished tasks are pruned after 7 days. If the worker
is down, previews fail after 60 seconds with a retryable message; nothing else
in the app depends on it. Admission: two running previews/ingestions per account
and 20 pending tasks younger than 60 s overall (429 beyond that); stale rows left
by a stopped worker do not count.

Install beside the web unit (same code, environment file, data directory and
dynamic user):

```sh
sudo install -m 0644 deploy/next-move-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now next-move-worker
journalctl --namespace=nextmove -u next-move-worker -f   # "Queue worker started"
```

`SIGTERM` lets the current task finish (network reads are capped at about 20 s)
and exits 0; `TimeoutStopSec=45` covers it. The worker needs outbound HTTP(S);
the web unit no longer does. Memory, measured on macOS with production settings (Linux
is usually a little lower): the worker's resident set is about 68 MiB idle and
stays flat after 20 tasks; for comparison the preloaded Gunicorn master is about
100 MiB plus about 36 MiB for its worker. The unit caps the worker at
`MemoryHigh=80M`/`MemoryMax=112M`, so web + worker stay well under 300 MiB on the
458 MiB Droplet. Idle polling is one indexed read per second (about 1.5 ms CPU,
no writes); raise `QUEUE_POLL_SECONDS` to trade latency for even less.

### Accounts administration

Since schema 15, accounts must verify their email; see "Outbound email and email verification" below. Share invite codes yourself (`flask --app jobtracker invite create --count 1 --expires-days 7`;
`invite list` shows status without the code). Forgotten passwords use
`flask --app jobtracker user reset-password EMAIL` (prompts, signs that user out
everywhere). Run these as the service's data identity with its environment, as
for the existing password recovery. `SIGNUP_REQUIRES_INVITE=0` opens sign-up;
`SIGNUP_ENABLED=0` closes it. Login attempt budgets (per account and per IP via
`TRUST_PROXY=1` and nginx's overwritten `X-Forwarded-For`; no global budget, so
no client can lock every account out) persist in the throttle database as HMACs,
never raw emails or addresses. Without `TRUST_PROXY=1` every request appears to
come from nginx and shares one IP budget.

## Production release — schema 10, October 28, 2026

`main` at `de129f0` (PRs #13, #12, #11) took the live instance from schema 8 to
10 in one start. The release followed the upgrade steps above, plus:

- The migration was rehearsed on a copy of production first, locally and then on
  the Droplet with the release's own Python 3.12 environment; the full pytest
  suite also ran on that environment. Every tenant row (ignoring the new
  `user_id`), every resume file and the password hash matched before and after.
- `BOOTSTRAP_ADMIN_EMAIL`, `SIGNUP_ENABLED=1`, `SIGNUP_REQUIRES_INVITE=1` and
  `QUEUE_POLL_SECONDS=1` were added to the environment file; `TRUST_PROXY=1` was
  already set. The nginx snippet now overwrites `X-Forwarded-For`, which makes
  per-IP login budgets effective (before this they saw an empty address).
- A 1 GiB `/swapfile` (`vm.swappiness=10`) was added because the host had about
  85 MiB available and no swap. Measured after release: web about 64 MiB, worker
  about 44 MiB, about 140 MiB available.
- The rollback checkpoint (schema-8 snapshot, previous environment file and nginx
  snippet) is `/opt/next-move/rollback/phase01-de129f0`.
- Live checks used an in-memory admin session only: all roles listed, a resume
  download matched its stored hash, anonymous API calls were refused, and posting
  URLs extracted through the worker in under two seconds. No test account was
  created, because accounts cannot be deleted yet.

Run admin commands through `deploy/next-move-admin.sh` (installed as
`/usr/local/sbin/next-move-admin`). It runs `flask` as the service's dynamic
user with its environment, so database files and resume directories keep their
owner and 0700/0600 modes; running `flask` directly as root can leave files the
service cannot write.

```sh
sudo install -m 0755 deploy/next-move-admin.sh /usr/local/sbin/next-move-admin
sudo next-move-admin invite create --count 1 --expires-days 7
sudo next-move-admin user reset-password you@example.com
```


## Outbound email and email verification — schema 15

Sign-up, account activation and older accounts' first sign-in all depend on email. Only the queue worker sends; the web process enqueues and never holds the mail key. API.md ("Email confirmation") has the account rules; this section covers running it.

### Configuration

| Setting | Production value | Notes |
| --- | --- | --- |
| `MAIL_BACKEND` | `resend` | Required in production. `resend` uses Resend's HTTPS API (port 443; DigitalOcean blocks outbound SMTP on 25, 465 and 587). `smtp` works with any provider on any port (Resend 2587 STARTTLS / 2465 TLS, Postmark 2525, SES 2587). `none` turns email off and requires `SIGNUP_ENABLED=0`. `console`, `file` and `memory` are refused in production. |
| `MAIL_FROM` | `My Next Moov <no-reply@mail.mynextmoov.com>` | The domain must be verified at the provider. |
| `MAIL_REPLY_TO` | `support@mynextmoov.com` | |
| `MAIL_API_KEY_FILE` | `/run/secrets/mail_api_key` | **Worker only.** A Resend key with "Sending access" only, restricted to `mail.mynextmoov.com`. |
| `MAIL_TIMEOUT_SECONDS` | `15` | 1–20. |
| `MAIL_DAILY_LIMIT` | `90` | Rolling 24 h, all email. Resend's free tier allows about 100 a day. |
| `MAIL_LIMIT_SIGNUP` / `MAIL_LIMIT_RESET` | `50` / `25` | Sign-up email (5 of the 50 reserved for older accounts' verification links) and reset email; the remainder (15) is kept for "password changed" notices. Startup refuses limits that leave under 10% for notices. |
| `MAIL_MONTHLY_LIMIT` | `3000` | UTC calendar month, counted from `mail_usage`. |
| `POW_BITS` | `18` | Proof-of-work difficulty for sign-up, activation, resend and reset (+2 while email is degraded). |
| `PUBLIC_BASE_URL` | `https://mynextmoov.com` | Required in **both** web and worker: the worker builds the links. |

Startup checks (the service refuses to start otherwise): a known backend allowed in this environment; a valid single-line `MAIL_FROM`; an https `PUBLIC_BASE_URL` that ends with `APP_ROOT_PATH` (it is never appended); an https API URL; the key present in the worker; recipient DNS checks not disabled. A production worker started with `WORKER_LOG_LEVEL=DEBUG` logs a warning (debug tracebacks can include message text); keep it at INFO.

Scaling up after upgrading the Resend plan: raise the four limits together, for example `MAIL_DAILY_LIMIT=950`, `MAIL_LIMIT_SIGNUP=525`, `MAIL_LIMIT_RESET=265`, `MAIL_MONTHLY_LIMIT=30000`, then restart web and worker.

### Watching it

- `flask --app jobtracker mail status` (in the worker container) prints the 24 h counts against each limit, the month-to-date total, the current status (`ok`, `degraded`, `off`), whether sign-up is paused and why, skipped recipients by reason, and queued, deferred and failed email tasks.
- `flask --app jobtracker mail test ADDRESS` sends a fixed message with no link and prints `sent` or the error type.
- `flask --app jobtracker signup pending [--email E]` shows pending confirmations (addresses masked).
- Alert log lines (logger `jobtracker.mail`; they go to Sentry and email the owner): `mail daily usage at 80% …`, `mail daily limit reached …`, `mail monthly usage at 80% …`, `mail monthly limit reached …`, `mail <kind> share at 80% …`, `signup paused: <reason>`, `mail degraded: <reason>`, `mail provider rejected credentials (HTTP n)`, `mail recipient skipped: <reason>`. **An 80% warning means: upgrade the Resend plan.** Logs never contain addresses, subjects, bodies, links, tokens or keys.
- Turn on Resend's own account notifications and send them to a mailbox outside Resend.

### When email is degraded or the quota is spent

Requests never wait on email. While the provider fails or a limit is spent, sign-up still answers "Check your email", the auth screens say email is delayed, and the worker retries. Quota deferrals are not counted as failed attempts. A link that cannot be sent within 24 hours is cancelled. When sign-up's daily share is spent, sign-up and activation pause for the day with an honest message; password reset and notices keep their own shares.

### Bounce and complaint runbook

Triggers: a Resend notification; a bounce rate above 2% over the last 24 hours in the Resend dashboard; **any** spam complaint; or a surge of `mail recipient skipped` warnings.

1. Set `SIGNUP_REQUIRES_INVITE=1` and restart web. Sign-up becomes invite-only immediately; reset and notices are unaffected.
2. Check `mail status` and the skipped reasons; look at `signup pending` for a pattern (one domain, one client network).
3. Raise `POW_BITS` by 2 (about four times the work) and/or lower the per-domain caps in `mailer.py`.
4. Answer Resend support if the account was paused.
5. Reopen sign-up once the bounce rate stays under 1% for 48 hours.

If abuse continues despite proof-of-work, the next step is Cloudflare Turnstile (a CSP and privacy-policy change). The first follow-up after launch is Resend's signed bounce and complaint webhook, so hard-bounced addresses are never sent to again.

### Rotating the mail key

Create a new send-only, domain-restricted key in Resend; update the worker's secret file; `docker compose up -d --no-deps jobtracker-worker`; run `mail test`; delete the old key. Rotate after any suspected worker compromise, and yearly otherwise.

### Accounts from before schema 15

They keep working through their agents (OAuth connections, agent tokens, the legacy token and harness grants are not suspended), but their browser sessions end and the next sign-in asks them to confirm their email. While unverified they **cannot revoke their own connections in the dashboard**. After confirming the owner's identity out of band, support revokes them with `flask user revoke-agents ID [--harness]`. Fix addresses that cannot receive mail (`flask user list --unverified --undeliverable`) with `flask user set-email ID EMAIL --verified`. On a server with email off (the test server), run `flask user verify EMAIL` for each account, and create new accounts with `flask user create EMAIL`.

### Backups and pending sign-ups

`email_confirmations` holds pending sign-ups' email addresses and password hashes for about two days in the live database. **Snapshots contain them for as long as the snapshot is kept.** Treat snapshots as sensitive as the live database, and do not keep them longer than recovery needs (the privacy page promises at most 4 weeks).

## Password reset — schema 16

Additive migration: `password_reset_tokens` (hashes only) and `password_reset_requests` (per-account history, no email or IP, kept 30 days). Back up and stop writers as for any schema change. Older releases refuse schema 16, so rolling back the code means restoring the pre-migration backup. To switch the feature off without a rollback, set `PASSWORD_RESET_ENABLED=0` and restart.

- **The queue worker must run.** It looks the account up, mints the link and sends it. It needs `PUBLIC_BASE_URL`.
- **Mail.** Reset links use the `reset` share of `MAIL_DAILY_LIMIT` (`MAIL_LIMIT_RESET`, default 25), and the "password changed" notice uses what's left. `flask mail status` shows both. A deferred task's row holds the address for up to 48 hours.
- **First production deploy.** Ship with `PASSWORD_RESET_ENABLED=0`. Smoke-test with `user reset-link`, then turn it on and run one real request end to end. Check the raw link in the received email: there must be no click-tracking redirect, and the `#token=` fragment must be intact.
- **Test server** (`MAIL_BACKEND=none`): self-service reset is off. Use `flask user verify EMAIL` and then `flask user reset-link EMAIL`.

### Support runbook

- `flask --app jobtracker user reset-link EMAIL` (or `--id ID`, `--minutes N` from 5 to 1440) prints the link to stdout only. It's a credential: give it only to the account holder, over a channel you trust. Never paste it into a ticket, and never run the command in a recorded or logged session. A newer link replaces older ones. The link doesn't verify the address, so for an unverified account also run `flask user verify EMAIL` once you've confirmed the person another way.
- `flask --app jobtracker user reset-password EMAIL` prompts for the password. It revokes sessions, OAuth connections, agent tokens and reset links, plus chat links when the email is unverified (override with `--keep-harness` or `--revoke-harness`), and emails the notice.
- To revoke agents only, use `flask user revoke-agents ID [--harness]`.
- Audit lines (logger `jobtracker.accounts`, never a token, link or email): `operator reset link minted user_id=N expires=… minutes=M operator=cli`, `operator password reset user_id=N operator=cli`, and `revoked user_id=N oauth=… agent=… harness=… via=reset|operator|cli`.

## Data deletion and export — schema 17

### Deletion and export requests (identity first)
1. Find the account (`flask user list`) and reply with a one-time code from `flask support code --id N --for delete` (or `--for export`). The code works only for that account and that action; only its hash is stored, and it expires in 72 h.
2. Act only on a reply **from the account's address that contains the code**, and whose raw headers ("Show original") show **SPF and DKIM `pass`** for the sender's domain. `flask support check CODE` shows which account and action it authorizes, without using it up.
3. **Verified account:** run `user delete --id N --code CODE` or `user export --id N --code CODE`. The code is used up by that command, inside the deletion's own transaction, so a failed deletion leaves it usable.
4. **Legacy unverified account:** a mailbox isn't proof of ownership. The requester must also sign in (proving the password, which verifies the address) or complete a password reset. Otherwise, treat it as "this address on someone else's account": move the address off it with `user set-email ID <placeholder> --verified`, then `user unverify`. The account is locked, and its data isn't deleted.
5. **Messaging-only account (no email):** issue the code for that account id and send it in the chat. The user texts it back from their phone; you see it arrive in that conversation in Messages on the bridge Mac. Then run `user delete --id N --yes --code CODE`.
6. **After `user delete`:**
   - run the printed `resume-agent tenant delete --jobtracker-user N` in the gateway container;
   - delete the conversation in Messages on the bridge Mac (with Messages in iCloud on, that removes it from iCloud too);
   - delete its files under `~/Library/Messages/Attachments`;
   - note the date in the support log.

   This is all within 14 days of the request.
7. **Exports:** send the printed link in the verified thread only, never as an attachment. Opening it shows a Download button and uses up nothing; each download counts, 3 in 48 h.

Verified accounts get an "account deleted" email automatically; unverified ones don't, so reply in the thread.

### Backups, snapshots and restores
- **Release snapshots** go in dated directories (`snapshots/YYYY-MM-DD-<sha>`) and are **deleted after at most 28 days**. That's a release-checklist step: `find <snapshots> -mindepth 1 -maxdepth 1 -mtime +28 -exec rm -rf {} +`. The same rule covers copies of production data anywhere: the test server, scratch folders, laptops.
- **Never restore a backup older than 35 days** (the ledger window). `deletions replay` refuses a database whose newest data is older than that.
- **`--force` only after checking the backup's real date** from its snapshot directory name (`snapshots/YYYY-MM-DD-<sha>`). For example, a quiet account with no writes for 35 days can trip the check even on a recent backup. Never force a snapshot older than 35 days.
- **Restore checklist,** in this order:
  1. Stop `jobtracker-web` and `jobtracker-worker`, plus the gateway and harness if their volumes are restored too.
  2. `flask deletions export > /secure/deletions-<date>.jsonl`, from the live database.
  3. Restore.
  4. `flask deletions replay /secure/deletions-<date>.jsonl` in a one-off container, with nothing serving.
  5. For every `account` row in that file: `resume-agent tenant delete --jobtracker-user N`.
  6. Start the services.
  7. Delete the export file.
- Deleted rows are overwritten in place (`PRAGMA secure_delete=ON`), and account deletion truncates the WAL. Old data therefore doesn't linger in later backups beyond their own window.

### Storage sweep
The worker runs a bounded sweep every 6 h: orphan files older than an hour, deleted accounts' folders (only those the ledger names) and expired exports. It refuses with an ERROR log line on an empty or unmigrated database, or a pass larger than 50 files or 1 folder. Run `flask storage sweep` for a dry run, and add `--apply` (plus `--force` after checking) to act. Unknown folders are only reported.

### Logs
Audit lines (logger `jobtracker.accounts`) carry ids and counts only:
- `job deleted …`, `resume deleted …`, `template version deleted …`, `template deleted …`;
- `account deleted user_id=N rows=R files=F operator=cli`;
- `export created …`, `export downloaded …`;
- `deletions replay …`.

The bridge Mac's own logs (`~/Library/Logs/NextMove`) are redacted, and fall under the same log-retention statement.

### Branded emails

Every email has the existing plain-text part plus a branded HTML part, rendered by `mailer.render(kind, ...)` from `jobtracker/templates/email/layout.html` (Jinja autoescape) and the fixed wording in `jobtracker/email_content.py`. The only values filled in are the masked address, the server-built link and fixed facts (a time, minutes, a date). Token links appear both as the button and as copyable text.

The logo is served publicly at `PUBLIC_BASE_URL/static/email/mascot-96-v1.png` (allowlisted, cached as immutable); it must stay reachable for emails already delivered. A new image gets a new versioned name (add it to `EMAIL_ASSETS` in `jobtracker/__init__.py`) rather than replacing this file. There are no tracking pixels or redirected links; keep Resend open and click tracking off.

Before launch, send each kind to Gmail (web and iOS), Apple Mail (macOS and iOS) and Outlook (365 web and Windows) and check light, dark and images-off rendering. Gmail ignores the dark-mode styles and inverts the light design; Outlook for Windows shows square corners.

## Telemetry and alerting

Errors go to Sentry (SaaS, US region) only when a DSN is configured. With no DSN, `sentry_sdk.init` is never
called: there's no client, no background thread and no network traffic. The test server (nicolasmelo.com/nextmove)
runs without Sentry.

**Settings** (`deploy/production.env.example`):

- `SENTRY_DSN_FILE` (preferred) or `SENTRY_DSN`: the backend project, for web and worker.
- `SENTRY_FRONTEND_DSN`: the browser project's public DSN. It must be on `o<digits>.ingest.us.sentry.io` (or the
  legacy `o<digits>.ingest.sentry.io`). That exact host, and nothing else, is added to the CSP `connect-src`. An
  invalid value logs a warning and leaves browser telemetry off.
- `SENTRY_ENVIRONMENT`.
- `SENTRY_RELEASE`: set from the image build. It is sent only to Sentry, never in public output.
- `LOG_FORMAT`: `json` by default, `text` in development.

**What is sent.**

Backend events (`jobtracker/telemetry.py`) contain:
- the exception type and code frames, with the message always `[redacted]` and no local variables;
- the route template (for example `/api/v1/files/<token>`) and the HTTP method;
- tags `service`, `request_id`, `task_kind`, `alert`, `mail_reason`;
- the numeric account ID only.

Events never contain request bodies, query strings, headers, cookies, breadcrumbs, tracing data, `sys.argv`,
emails or IP addresses. Every event is rebuilt from an allowlist, and a key denylist runs over the result. If
scrubbing fails, the event is dropped.

Browser events (`frontend/src/telemetry.js`) carry only:
- the exception type and frames, without query strings;
- the message, if it matches a short list of safe shapes (otherwise `[redacted]`);
- the page name and the numeric account ID.

The browser SDK is a lazily loaded chunk from npm, with no Replay, Feedback, console capture or tracing.
`/api/v1/client-config` hands the browser its DSN and environment, never the release.

**Request IDs.** Caddy overwrites `X-Request-ID` on every request, and nginx on the test server sets
`$request_id`. Flask checks it again (`[A-Za-z0-9-]{8,64}`) or generates one. Every response carries it as a
header, and 5xx JSON bodies carry it as `request_id`. The frontend shows "(reference: …)" on server failures, and
the crash screen shows the Sentry event ID. Ask users for that reference, then search logs and Sentry for it. Queued
worker tasks don't carry the web request's ID yet.

**Logs.** Logs are one JSON object per line on stderr: `ts`, `level`, `logger`, `service`, `msg`, `request_id`,
and for errors `exc_type` and `stack` (code lines only). The exception message is never logged. Unhandled exceptions
log the route template, not the path. Token-bearing paths (`/api/v1/files/`, `/api/v1/uploads/`,
`/api/v1/exports/`, activation, email-verification and password-reset links) are also masked in any message. The Gunicorn access log stays off. Messages pass through a redaction safety
net covering emails, phone numbers, bearer and Next Move tokens, token query or fragment parameters, and signed-link
paths. `WORKER_LOG_LEVEL=DEBUG` in production logs a startup warning.

**Logs are kept about 7 days.**
- **Production (mynextmoov.com):** every container logs to the host journal through Docker's `journald` driver,
  and `/etc/systemd/journald.conf.d/nextmove.conf` keeps it about 7 days (`MaxRetentionSec=7day`,
  `MaxFileSec=1day`). It's installed by NextMoveAgent's `deploy/host/setup-droplet.sh`; see NextMoveAgent
  `docs/Deployment.md`, "Logs are kept about 7 days".
- **Test server (nicolasmelo.com/nextmove):** the units set `LogNamespace=nextmove`, so their logs go to a
  separate journal with its own 7-day limit, and the other apps on that shared host keep their own retention.
  Install the limit once, then restart the units:

  ```sh
  sudo install -m 0644 deploy/journald-nextmove.conf /etc/systemd/journald@nextmove.conf
  sudo install -m 0644 deploy/next-move.service deploy/next-move-worker.service /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl restart next-move next-move-worker
  journalctl --namespace=nextmove -u next-move -f
  ```

  The nginx snippet turns the access log off for every `/nextmove` location. nginx's own error log and the other
  sites' access logs follow that host's logrotate settings (Ubuntu's default keeps 14 days).
- **Check it:**
  - Size: `journalctl --disk-usage` (production) or `journalctl --namespace=nextmove --disk-usage` (test).
  - Oldest entry: `journalctl [--namespace=nextmove] -o short-iso | head -2`. It should be no more than about 8 days
    old.
  - Settings in effect: `systemd-analyze cat-config systemd/journald.conf` (production) or
    `cat /etc/systemd/journald@nextmove.conf` (test).
- **Elsewhere:** Gunicorn's access log is off and its error log goes to the journal. Droplet backups and snapshots
  include the journal and follow DigitalOcean's schedule, which is longer than 7 days.

**Check scrubbing end to end** after enabling a DSN: run `flask --app jobtracker telemetry-check` in the service's
runtime. In Docker that's `docker compose exec jobtracker-web flask --app jobtracker telemetry-check`. It sends one
synthetic error whose message, locals, tags, extras and context are full of `CANARY` strings. Open the event in
Sentry and confirm that none of them is visible.

**Health and crons.**
- `/health` checks the database and is the external uptime target: `https://mynextmoov.com/health`, every minute,
  expecting 200 and `"status":"ok"`.
- The worker sends a Sentry Cron check-in `jobtracker-worker` every 5 minutes.

**Alerts to configure in Sentry** (all email the operator):

| Alert | Condition |
| --- | --- |
| New issue | Any new issue in the backend or browser project |
| Error burst | More than 10 events in 5 minutes |
| Mail (sign-up depends on it: Resend free tier, verification required) | Any event tagged `alert:mail_*` or `alert:signup_paused`. These are the `jobtracker.mail` logger's constant templates: daily or monthly usage at 80%, limit reached, category share at 80%, `signup paused: %s`, `mail degraded: %s`, provider rejected credentials. There is also `mail tasks failing`, sent when 3 or more mail tasks fail permanently within 15 minutes. Mail tasks are the queue handlers registered with `taskqueue.handler(..., mail=True)`; their individual failures never page. Any other kind, including an unregistered one, does. **Email on every occurrence, action interval 30 minutes, no muting**, plus an email when the issue resolves. Arguments are never sent, only the template and an allowlisted reason word. |
| Worker silent | Cron `jobtracker-worker` missed twice (5-minute interval) |
| Uptime | `/health` failing from two regions; TLS expiry |

**Sentry project settings**:
- Data Scrubber and Default Scrubbers on.
- **Prevent Storing of IP Addresses** on.
- Sensitive fields: the `EXTRA_DENYLIST` names.
- Allowed Domains `mynextmoov.com` on the browser project.
- Key rate limits (about 60/min for the browser key, 300/hour for the backend key).
- Minimum retention.

Sentry is a subprocessor; the `/privacy` page names it.
