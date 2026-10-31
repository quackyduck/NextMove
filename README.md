# Next Move

Private, single-user job search tracker: Flask + SQLAlchemy/SQLite API, React + Bootstrap interface, password session login, and bearer-token agent access. The first demo supports manual intake, spreadsheet/board views, stage/outcome updates, notes and next actions, contacts, versioned resume uploads, and activity history.

Use **Quick capture** to paste a posting URL, review the extracted company, title, salary, and qualifications, and save a potential role. Key qualifications are saved in notes, and the extracted description is retained. The compact view works on phones; manual intake remains available. Celery/Redis and Numbers import are future milestones.

## Run the local demo

Prerequisites: Python 3.11+ (tested with 3.14), Node.js 22.12+ and npm. From the repository root:

```sh
./scripts/setup.sh
./scripts/run.sh
```

Open [http://127.0.0.1:5001](http://127.0.0.1:5001) and sign in as `owner@localhost` (or `BOOTSTRAP_ADMIN_EMAIL`). On first setup, the local development password is `local-demo-only` unless `APP_PASSWORD` or `APP_PASSWORD_HASH` is configured. `scripts/run.sh` also starts the queue worker that reads posting URLs. More accounts sign up with an invite code from `flask --app jobtracker invite create`; see [accounts](docs/API.md#accounts-and-tenancy-schema-9). After setup, the database holds the active password; environment changes do not reset it. No debug server or reloader is enabled. Keep the local server bound to loopback.

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

## Connect any agent (MCP and OAuth)

Assistants that support MCP connectors (Claude, ChatGPT, Cursor and others) connect at `/mcp`: the user signs in, picks what the agent may do, and can disconnect it under **Agent access**. Other agents use the same OAuth tokens on the [published OpenAPI contract](docs/API.md#third-party-agents-oauth-mcp-and-openapi-schema-12) at `/api/v1/openapi.json`. See [MCP.md](docs/MCP.md) for the tools, scopes and security model.

## Operations and next steps

[Expansion roadmap](docs/ROADMAP.md) maps the current system and specifies onboarding, the agent API, resume tailoring, job crawl, the apply agent, independent deployment and donations, with eval and test plans for each.

[Implementation plan and milestone checklist](docs/PLAN.md) describes the parallel agent roles and incremental demos. [Operations and deployment runbook](docs/OPERATIONS.md) includes verified local backup/restore and the gates for a future private DigitalOcean deployment. Nothing has been provisioned or deployed.

[DigitalOcean deployment plan](docs/DEPLOYMENT_PLAN.md) provides the phased checklist for a private preview, production preparation, HTTPS cutover, recovery, and later Celery rollout.

[Production preparation](docs/PRODUCTION_PREPARATION.md) records the inspected existing host, pending user decisions, local runtime templates, persistent login defense, and remaining validation. These templates have not been installed or deployed.

Outreach is both a stage and a lasting fact: entering Outreach marks **Outreach made**, which stays visible on the table and board after you progress. The checkbox in intake/details lets you record past outreach or correct it; saving a contact alone does not mark it. The schema-2 upgrade preserves evidenced outreach from existing stage history. See the [operations notes](docs/OPERATIONS.md#outreach-badge-release--schema-2) before upgrading an existing workspace.

## Expired sign-in sessions

If your session expires or another tab changes it, a sign-in dialog opens over the current workspace. Unsaved form values and selected files remain in memory in that tab; refreshing or closing it still discards them. After signing back in, retry your action explicitly. The app never automatically repeats a save, job creation or upload. Session lifetime and server-side authentication/CSRF protection are unchanged.

The browser test launcher gives each spec file separate disposable storage and a fresh persistent login budget. This lets recovery tests exercise real failed/repeated logins without exhausting the unrelated workflow suite's limiter; production limits remain enabled.

## Change your password

Select **Change password** in the sidebar. Enter your current password, a new password of 8–128 characters, and its confirmation. The change keeps this device signed in and signs out other browser sessions. Only a verifier hash is stored, in the workspace database; the change survives restarts and redeployments. Agent tokens cannot change this password and are managed separately.

The configured `APP_PASSWORD_HASH` is bootstrap-only after the credential is initialized. For administrator recovery, run `.venv/bin/flask --app jobtracker user reset-password EMAIL` (or the legacy `reset-workspace-password` for user #1) in the intended runtime with its configuration/data access; it prompts without echo and revokes all browser sessions. Do not pass a password as a shell argument or delete the database to reset access. Production recovery must use the service's data ownership/configuration, not the local development `.env`.

## Quick capture from a URL

Select **Quick capture**, paste a posting URL, and choose **Extract role details**. Review/edit the company, title, URL, salary, and qualifications, then **Save potential role**. Saving creates an **Interesting** role; qualifications go into notes and the description is retained. Extraction alone creates no pipeline record.

Extraction runs in the background. You can navigate away and return; previews and edits survive navigation and sign-in recovery, but not a page refresh. It prefers [Schema.org JobPosting](https://schema.org/JobPosting) data, with an HTML fallback. Missing fields stay blank with a notice. Review the result, and use a direct posting URL for pages containing multiple roles.

Sites requiring sign-in, JavaScript, or bot challenges may not import. **Enter details manually** preserves the URL for manual intake. No new dependency, API key, or Redis service is required. See the [API contract](docs/API.md#url-extraction-preview) for extraction details and [schema-4 operations notes](docs/OPERATIONS.md#quick-capture-release--schema-4) before upgrading.


## URL ingestion integration

[Review the secure ingestion proposal](docs/URL_INGESTION_PROPOSAL.md) for an API that accepts one URL and saves the extracted role in the background. The feature includes an **Agent access** screen for creating named, expiring tokens, one-time secret display/copy, setup instructions, connection testing, last-used metadata, and revocation. `scripts/ingest_url.py --test-connection` tests a token from the agent’s machine without saving a role. No environment edit or restart is needed; no token exists until you create one. PR #6 is ready for review and has not been merged or deployed.
### Read-only role search

`GET /api/v1/jobs/search` finds explainable duplicate candidates using company, title, and/or URL. It supports an optional, separately scoped hashed bearer credential. See [the contract, secure calling examples, and matching tradeoffs](docs/saved-role-search-api.md). With the configured path mount, call `/nextmove/api/v1/jobs/search`.

Follow the [agent setup walkthrough](docs/AGENT_SETUP.md) for secure token storage, the reusable intake skill, duplicate checking and submission. API submissions start **Needs review**; manual entries and Quick capture start **Interesting**.
