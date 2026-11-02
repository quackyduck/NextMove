# Agent collaboration API

Base URL in development: `http://127.0.0.1:5001/api/v1`.
Managed tokens use `Authorization: Bearer <token>` and the permissions described below.
The legacy `AGENT_API_TOKEN`, if explicitly enabled, grants broad access to user #1's
workspace (never other accounts); it is disabled in the hosted deployment. Keep tokens out of source control, URLs and
logs. Production requests must use HTTPS.

Browser sessions use `/auth/signup`, `/auth/login`, `/auth/session`, `/auth/logout` and a CSRF token returned by signup/login/session. Send `X-CSRF-Token` on session-authenticated writes. Bearer clients do not need CSRF tokens.

## Accounts and tenancy (schema 9)

Every job, contact, resume (and its file), activity, agent token, extraction preview and ingestion receipt belongs to one account. Every request acts as exactly one account: the signed-in browser user, the user who created the managed token, or the migrated workspace owner (user #1, flagged `owns_legacy_tokens`; never a signed-up account) for the deprecated `AGENT_API_TOKEN` / `ROLE_SEARCH_TOKEN_SHA256`. Another account's IDs behave exactly like IDs that do not exist (404), and lists and search only include your own records. Nothing in the service sends email.

| Method | Path | Body | Responses |
| --- | --- | --- | --- |
| GET | /auth/session | — | 200 `{authenticated, csrf_token, user: {id, email, created_at} \| null, signup: {enabled, invite_required}}` |
| POST | /auth/signup | `{email, password, password_confirmation, invite_code}` | 201 `{authenticated: true, csrf_token, user}` and a session cookie; 400 `{error, field}` (`email`, `password`, `password_confirmation`); 403 `{error, field: "invite_code"}` for a missing, unknown, used or expired invite (or `{error}` when sign-up is closed); 409 `{error, field: "email"}`; 429 throttled |
| POST | /auth/login | `{email, password}` | 200 `{authenticated: true, csrf_token, user}`; 401 `Invalid email or password` for every failure; 429 throttled |
| POST | /auth/password | `{current_password, new_password, confirm_password}` | Changes only the signed-in account; see below |
| POST | /auth/logout | — | 200 `{authenticated: false, csrf_token: null}` |

- Emails are trimmed and lowercased; they are not verified. Passwords are 8–128 characters.
- Invites are single-use and expire (default 7 days). Consuming the invite and creating the user happen in one transaction. Bad input never uses up the invite; a duplicate email (409) does, so one code can answer at most one "is this email registered?" question. With `SIGNUP_REQUIRES_INVITE=0` the 409 inherently reveals registered emails (rate-limited per IP). `SIGNUP_REQUIRES_INVITE=0` opens sign-up without codes; `SIGNUP_ENABLED=0` closes it.
- Signup and login reject cross-origin `Origin` headers (403).
- Login attempt budgets per five minutes: 30 per client IP; 10 per account (by email, known or unknown, so 429 never reveals an account), after which a client that has made fewer than 3 attempts on that account may still try, up to 50 per account. An attacker hammering one email therefore cannot lock its owner out. There is no global budget. Sign-up is limited to 10 per IP. Password changes and token creation use a separate per-user budget (10), so login abuse cannot block a signed-in owner. Over budget returns 429 with `Retry-After: 300`. Unknown emails take the same time as wrong passwords.
- `/jobs/search` allows 120 requests per minute per account, plus 120 per client IP before authentication.
- A password change signs out that account's other sessions only.

Administrators manage accounts from the server shell (`flask --app jobtracker ...`); there is no admin API and no email:

| Command | Effect |
| --- | --- |
| `invite create [--count N] [--expires-days D]` | Prints N new single-use codes (only hashes are stored). Share them however you like. |
| `invite list` | Code prefix, status (unused / expired / used by user N) and expiry |
| `user list` | ID, email and creation time |
| `user reset-password EMAIL` | Prompts for a new password (never an argument) and signs that user out everywhere |
| `user set-email ID EMAIL` | Changes a sign-in email, e.g. user #1's migration placeholder `owner@localhost` |
| `user merge-messaging SOURCE TARGET` | Links a phone to an existing account: moves the harness grants of messaging account SOURCE (never activated, no data) to account TARGET, then deletes SOURCE |
| `reset-workspace-password` | Legacy alias for resetting user #1 |
| `worker [--once]` | Runs the queue worker (same as `python -m jobtracker.worker`) |

## Typical collaboration

1. `POST /jobs` with `title`, `company`, `url` and/or `description`. Capture the returned `id` and `version`.
2. `GET /jobs/<id>` to obtain the current description, notes and revision.
3. Tailor a resume outside this service.
4. `POST /jobs/<id>/resumes`, multipart field `file`, with a PDF, DOCX or TXT file. This creates a new attachment; it does not replace an earlier version.
5. `PATCH /jobs/<id>` with `{ "stage": "resume_updated", "version": <current version> }`. Fetch the latest job first if another operation changed its revision. A 409 means reread and reconcile, never blindly retry stale edits.
6. `GET /jobs/<id>/activities` for progress history.

Do not claim a role was applied to or a recruiter contacted merely because a resume was uploaded. Stage changes should represent actual work done.

## Routes

| Method | Path | Use |
| --- | --- | --- |
| GET | /jobs | List jobs in `{jobs: [...]}` |
| POST | /jobs | Create a job (201) |
| POST | /jobs/extract | Start background extraction (202); does not create a role |
| GET | /extractions/{id} | Poll preview status and extracted fields |
| GET | /jobs/{id} | Read current job |
| PATCH | /jobs/{id} | Update fields, optionally checking version |
| GET | /jobs/{id}/activities | Read `{activities: [...]}` |
| GET | /jobs/{id}/contacts | Read `{contacts: [...]}` |
| POST | /jobs/{id}/contacts | Add a name/email contact (201) |
| GET | /jobs/{id}/resumes | Read `{resumes: [...]}` |
| POST | /jobs/{id}/resumes | Upload multipart `file` (201) |
| GET | /resumes/{id}/download | Authorized original file download |

Errors use `{ "error": "message" }` and an appropriate HTTP status. Unknown IDs return 404. Bad input returns 400; authorization failures 401/403; conflicts 409; oversized uploads 413.

## Upload example

After saving a role, use an existing managed token with `roles:ingest` permission:

```sh
python3 scripts/agent_example.py '<job UUID>' '/absolute/path/revised-resume.pdf' \
  --token-file /path/to/private/token --base-url https://mynextmoov.com
```

The tool only uploads the file. It deliberately leaves progress changes to the caller. Resume files are private and only served by authenticated download routes. Exact server-enforced limits are documented in the application configuration.

## Not implemented yet

Spreadsheet import remains future work. The OpenAPI document is published at `/api/v1/openapi.json` (see below). URL extraction and review are available asynchronously through `/jobs/extract`; `POST /jobs` itself does not fetch URLs.

## Outreach badge

Job JSON includes `outreach_completed` (a JSON boolean, default `false`), accepted by job POST/PATCH. Creating a role at Outreach or transitioning into Outreach sets it to `true`. Other stage changes preserve it. An explicit PATCH can mark past outreach or correct an accidental mark without changing stage; a transition into Outreach takes precedence and marks outreach completed. Changes are recorded atomically in the activity timeline and respect optimistic versions.

Contacts are people you may contact; adding one does not prove contact was made and does not automatically mark outreach. A later pipeline stage also does not imply outreach occurred. The badge appears on table rows, board cards and job details while this field is true.

## Account password (browser session only)

`POST /auth/password` accepts exactly `{ "current_password": "...", "new_password": "...", "confirm_password": "..." }`. A valid browser session and its `X-CSRF-Token` are required; even valid bearer tokens receive 403. New passwords must contain 8–128 characters and match confirmation. Wrong current password/invalid input returns 400; the persistent shared sign-in attempt budget also applies to password verification (429/Retry-After).

Success returns `{ "authenticated": true, "csrf_token": "...", "message": "Password updated. Other sessions have been signed out." }`. Replace the client's CSRF token with this value. Other browser cookies are invalidated by a stored session generation; the current session is rotated and retained. A concurrent change returns 409 and clears the stale session; unavailable credential storage returns 503 and never falls back to the environment verifier. No password/hash/session generation is exposed in responses or job history. Agent token authority is unchanged by browser password changes.

## URL extraction preview

`POST /jobs/extract` accepts only `{ "url": "https://company.example/careers/role" }` with normal session/CSRF or bearer authorization. It returns **202** with `{ "id": "...", "status": "running" }`, a `Location` status URL, and `Retry-After: 1`, before any DNS, fetch, or parsing work: the request only queues a task for the separate worker process (`python -m jobtracker.worker`). Invalid syntax returns 400; 429 means this account already has two postings being read, or the shared queue is full (20 pending tasks). If no worker is running, the preview fails after 60 seconds with a retryable error.

Authenticated `GET /extractions/{id}` returns the task's `id` and `status`:

- `running`: extraction is in progress.
- `succeeded`: includes `fields` (`title`, `company`, `url`, `salary`, `notes`, `description`) and a `warnings` array. Missing fields are empty strings. Qualifications go into notes; the submitted URL is retained through redirects.
- `failed`: includes a user-facing `error`. Private destinations, fetch/parse errors, and ambiguous multi-role pages fail the task. Task failures return HTTP 200; missing/expired previews return 404.

Review/edit `fields`, then `POST /jobs` to save. Extraction never creates a pipeline role. The UI polls every 750 ms, pauses for sign-in recovery, and offers retry after a status-request failure.

Fetching uses public HTTP(S) destinations on standard ports, validates all DNS answers and redirects, pins connections to validated IPs, and verifies TLS against the original hostname. It does not execute JavaScript or authenticate to job boards. Extraction prefers Schema.org JobPosting JSON-LD, with best-effort HTML fallback. See [operations](OPERATIONS.md#quick-capture-release--schema-4) for concurrency, fetch limits, expiry, and restart behavior.


## URL ingestion and managed tokens

[The proposal](URL_INGESTION_PROPOSAL.md) covers onboarding, agent setup, retries, and rollout. `POST /ingestions` accepts `{ "url": "..." }` and a required `Idempotency-Key`, returning 202 and a status `Location`. `GET /ingestions/{id}` returns running, success with `job_id`/warnings, or failure. Successful extraction automatically saves a Needs review role, with qualifications in notes. Keys are unique within an account: another of your tokens cannot reuse an existing key or read its receipt, and keys used by other accounts are invisible (reusing one simply creates your own receipt). The 30-per-hour creation limit is per account. Extraction runs on the queue worker.

Only unexpired, unrevoked managed tokens with `roles:ingest` permission authenticate ingestion calls. `roles:search` grants `/jobs/search`; either permission allows connection testing. Browser sessions and broad agent tokens do not. `GET /agent-connection` returns connected status and safe token metadata, updates last-used time, and creates no role or ingestion. The client supports `--test-connection` without a posting URL.

Browser-session-only management (CSRF required on POST):

| Method | Path | Behavior |
| --- | --- | --- |
| GET | /agent-tokens | List metadata; no secrets or hashes |
| POST | /agent-tokens | Accept `name`, `current_password`, optional `expires_in_days` (1–365, default 90); return `token` once and `agent_token` metadata (201) |
| POST | /agent-tokens/{id}/revoke | Revoke immediately for future requests; idempotent |

Names are trimmed and limited to 80 characters. Creation requires current-password verification, shares the sign-in throttle, and permits at most 20 active tokens. Metadata includes `id`, `name`, `prefix`, `permissions`, `status`, `created_at`, `expires_at`, `last_used_at`, and `revoked_at`. Status is active, expired, or revoked. Missing token IDs return 404; malformed creation input or wrong passwords return 400; stale password verification or the active-token limit returns 409; throttling returns 429. APIs use no-store responses, including the one-time creation response.

No token environment setting is needed. Schema 7 is required; earlier draft environment credentials are not migrated or accepted. Existing unowned receipts remain stored but inaccessible to new tokens. PR #6 is ready for review; merging and deployment are separate steps.

Token creation accepts optional `permissions`: a nonempty array of `roles:search` and/or `roles:ingest` (default ingestion only). See [agent setup](AGENT_SETUP.md). Manual creation defaults to `interesting`; ingestion always starts `needs_review`.

## Resume uploads with managed tokens

Existing, unexpired/unrevoked `roles:ingest` (Add roles) tokens may also call
`POST /jobs/{id}/resumes` with multipart field `file`. No token replacement or
permission migration is required. This is an explicit expansion of Add roles
authority: tokens can append resume attachments to **any saved role** in this
single-user workspace, including manually entered roles. Search-only tokens
and the legacy search-only verifier remain read-only. The connection test reports
the same `roles:ingest` scope; no new scope or secret is introduced.

For URL intake, wait for the ingestion receipt to succeed and use its `job_id`.
For manual API entry, save the role first and use its returned `id`. Creating a
role and attaching a file are separate requests; the upload does not participate
in the ingestion transaction and cannot attach to a still-running receipt.

PDF, DOCX, and UTF-8 TXT are accepted. Keep files at 10 decimal MB or less: the
10 MiB server request limit includes multipart overhead. A successful upload
returns 201 with metadata for the new attachment only and adds a `resume_added`
activity. Previous files remain intact; role fields, stage, outcome, and version
are unchanged. Upload is **not idempotent**: retain the response; check in the
browser before retrying an uncertain request, because another request creates
another attachment version. No new list/download, notes, contact, role-editing,
manual-creation, or token-management permissions are granted.

Send bearer credentials over HTTPS; no CSRF header is needed for managed tokens.
Browser uploads retain session/CSRF behavior, and explicitly configured legacy
broad tokens retain their prior access. Revocation/expiry applies on the next
request; an already authorized upload may finish. Existing validation and private
storage remain in place: invalid/missing files return 400, unknown roles 404,
oversized requests 413, and invalid/expired/revoked/wrong-scope tokens 401.

## Not Interested outcome

Job creation and updates accept `outcome: "not_interested"`, displayed as
**Not Interested** in the outcome selector, role summaries, and pipeline filter.
All existing outcome values remain valid: `active`, `rejected`, `withdrawn`,
`closed`, and `accepted`. New roles still default to `active`.

Changing an outcome does not change stage, outreach completion, contacts, or
attachments. Normal version checks and activity history apply. Not Interested
roles are excluded from the default Active filter and active-role counts; choose
Not Interested or All outcomes to see them. Search summaries return the new value
like other outcomes. This requires schema 8; see the operations migration notes.

## Onboarding and master resume (browser session only)

These routes require a signed-in browser session (and `X-CSRF-Token` on POST).
Agent tokens and the legacy bearer token receive 403/401: profile data is personal.
Schema 10 adds the `onboarding_steps` and `master_resumes` tables.

- `GET /onboarding` → `{steps: [{id, version, title, status, guided, skippable}], guided_active}`.
  `status` is `pending`, `done` or `skipped`. Registered steps: `create_account`
  (always done), `upload_resume` and `connect_agent` (optional). `guided_active`
  is true only for accounts created through sign-up that have not yet acted on
  every guided step; migrated and admin-created accounts get the checklist only.
- `POST /onboarding/steps/<id>` `{status: "skipped" | "done"}` → the same shape.
  Steps with a completion fact are done when the fact holds: `upload_resume` once a
  master resume exists, `connect_agent` once any of the account's tokens has made a
  successful request (for example the connection test). Claiming `done` before
  that returns 409; skipping a completed step changes nothing. Unknown steps 404.
  A new registered step appears for existing users as `pending`; bumping a step's
  version re-offers it to users who skipped an older version.
- `POST /profile/resume` multipart `file` (PDF, DOCX or UTF-8 TXT, 5 MB max) →
  201 `{resume, created: true, steps, guided_active}`, or 200 with `created: false`
  when the bytes match the latest version. Type is decided by content (magic bytes),
  not the name or MIME type, and must agree with the extension. DOCX files that are
  encrypted, macro-enabled, ZIP bombs, polyglots or contain unsafe member names are
  rejected with 400; oversize requests 413; more than 20 versions 409. Files are
  streamed to `<uploads>/<user_id>/master/` under generated names; the client
  filename is only kept for display and download. Nothing is parsed.
- `GET /profile/resume` → `{resume: latest | null, versions: [...], template}` with
  `id, template_id, version, original_filename, media_type, size, sha256, created_at`,
  for the default resume template (see "Resume templates" below); `template` is null until the first upload.
- `GET /profile/resume/download[?version=N]` → the file as an attachment (404 when none).

## Resume Agent harness and messaging-first accounts (schema 11)

The Resume Agent harness acts on JobTracker strictly as one user. These routes are internal: the public proxy refuses `/api/v1/harness/` and `/api/v1/me/`, and the harness calls JobTracker over the private container network. `HARNESS_ALLOW_HTTP=1` waives the HTTPS check for that network only. Schema 11 makes `users.email` and `users.password_hash` nullable, and adds `harness_grants`, `activation_tokens` and `tailored_drafts`.

**Account creation.** `POST /harness/accounts` is signed with `HARNESS_SERVICE_SECRET` (or `HARNESS_SERVICE_SECRET_FILE`), using the headers `X-RA-Timestamp`, `X-RA-Nonce` and `X-RA-Signature`. The signature is the HMAC-SHA256 of `timestamp\nnonce\nMETHOD\npath\nsha256(body)`. Requests older than 5 minutes and replayed nonces get 401, and so does a server with no secret configured. The route returns 201 `{user_id, grant}`. The account has no email or password, and therefore no browser sign-in, until activation. It is never merged with another account by phone number or email. Only a SHA-256 of the grant persists.

**Grant routes.** Every `/me` route takes `Authorization: Bearer nm_harness_...` and is scoped to the grant's owner: another account's IDs are 404. Grants are not browser, agent or legacy credentials, and those credentials are 401 on `/me`.

- `POST /me/master-resumes` multipart `file`: the same checks as `POST /profile/resume`. Returns 201 `{id, version, created}`, or 200 when the bytes match the latest version.
- `GET /me/master-resumes/<id>/file`: the file, with `X-Original-Filename` (percent-encoded).
- `POST /me/jobs` `{url}`: creates the role at `needs_review` and queues posting extraction on the worker. It uses the same per-account admission as previews; when the queue is busy, `extraction_id` is null. Returns 202 `{id, url, title, company, stage, posting, extraction_id}`.
- `GET /me/extractions/<id>`: `{id, status, error?}`. On success, the role's title, company, salary, notes and description are filled.
- `GET /me/jobs/<id>`: the role. `posting` is its description, or null.
- `PUT /me/jobs/<id>/posting` `{text}`: a pasted description, for postings that can't be read.
- `POST /me/drafts` multipart: `draft` (JSON with `request_id`, `revision`, `job_id`, `master_resume_version_id`, and optionally `file_stem`: letters, digits, `_` or `-`, at most 120 characters), a `docx` file, and an optional `pdf` file. The job and master resume must belong to the grant owner. A repeated `(request_id, revision)` returns the first draft with 200; a new one returns 201 `{id, approved, has_pdf, draft}`.
- `GET /me/drafts/<id>`: the same shape. There is no download route for drafts.
- `POST /me/drafts/<id>/approve`: the only way a draft is exported. It files the draft's DOCX (and PDF) as resume versions on its job, named `<file_stem>.docx` (default `tailored-resume-r<revision>`), and records activity. It never changes the job's stage. Repeating it is a no-op.
- `POST /me/activation-links`: 201 `{url, activated: false, expires_at}`, a single-use link valid for 24 hours, built from `PUBLIC_BASE_URL`. An already activated account gets 200 `{url, activated: true}` with the dashboard URL.

**Activation (browser).**

- `GET /auth/activation?token=` → `{valid}`. Reading never consumes the link, so previews and scanners are harmless.
- `POST /auth/activate` `{token, email, password, password_confirmation}`. This sets the email and password, consumes the link, and signs in, all in one transaction. A used, expired or unknown link gets 410. An email already in use gets 409. The route shares the signup throttle. The dashboard opens activation from `/?activate=<token>`.

## Third-party agents: OAuth, MCP and OpenAPI (schema 12)

Agents connect with OAuth 2.1 and use either the MCP server at `/mcp` or this API with the same access tokens. The contract is published at `GET /openapi.json` (no authentication). It covers the routes OAuth tokens may call, with the scope each one requires. See [MCP.md](MCP.md) for the consent flow, tools, token lifetimes and security model.

| Route | Credentials | Scope |
| --- | --- | --- |
| `GET /jobs`, `GET /jobs/{id}`, `/activities`, `GET /contacts`, `GET /extractions/{id}`, `GET /jobs/search` | session, legacy, OAuth | `jobs:read` |
| `POST /jobs`, `PATCH /jobs/{id}`, `POST /jobs/extract`, `POST /contacts` | session, legacy, OAuth | `jobs:write` |
| `GET /jobs/{id}/resumes`, `GET /resumes/{id}/download`, `GET /profile/resume`, `GET /profile/resume/download` | session (and legacy for job resumes), OAuth | `resumes:read` |
| `POST /jobs/{id}/resumes`, `POST /profile/resume` | session (plus legacy and `roles:ingest` for job resumes), OAuth | `resumes:write` |
| `GET /templates`, `GET /templates/{id}`, `GET /templates/{id}/download` | session, OAuth | `resumes:read` |
| `POST /templates`, `PATCH /templates/{id}`, `POST /templates/{id}/versions` | session, OAuth | `resumes:write` |
| `GET /oauth/connections`, `POST /oauth/connections/{id}/revoke` | browser session only | — |
| `GET /files/{token}`, `POST /uploads/{token}` | the signed link itself | issued by MCP tools |

Schema 12 adds `oauth_clients`, `oauth_grants`, `oauth_codes` and `oauth_tokens`. Set `PUBLIC_BASE_URL` in production: it is the OAuth issuer and the base URL of MCP links.

## Resume templates (schema 13)

A resume template is a named master resume for one kind of role ("Senior product manager", "Engineering manager"), with its own version history numbered 1, 2, ... Names are free text (100 characters, NFC-normalized, no control or formatting characters such as NUL or bidi overrides), unique per account ignoring case; descriptions are optional (2,000 characters; newlines and tabs allowed). An account keeps up to 10 templates and 20 versions of each. Nothing is ever deleted.

Exactly one template is the **default**: it is what "the master resume" means for `/profile/resume`, the harness `/me/master-resumes` routes and MCP `list_resumes` / `read_resume` without an ID. The default is never empty: new templates start without one, and the first template to receive a version becomes the default. An upload that names no template (for example `/profile/resume` or onboarding) goes to the default. Before there is one, it goes to the account's only template if it has exactly one; otherwise to the template named "Main resume" (created if needed, which needs a free slot: with 10 templates and no default it is refused with 409). The default changes only by promoting another template that has at least one version. Signed `master` upload links resolve the default when they are used, not when they are issued. Schema 13 adds `resume_templates` and `master_resumes.template_id`, and moves each account's existing versions into a default "Main resume" with their numbers unchanged; accounts with no master resume get no template.

All routes take a browser session or an OAuth token (never legacy or managed agent tokens); another account's template IDs return the same 404 as unknown ones.

- `GET /templates` → `{templates: [{id, name, description, is_default, created_at, updated_at, version_count, latest}]}`, default first, then by name.
- `POST /templates` `{name, description?}` → 201, the new (empty, non-default) template with `versions: []`. 409 for a taken name or an 11th template.
- `GET /templates/<id>` → the template with `versions` (newest first; each version carries `template_id`).
- `PATCH /templates/<id>` `{name?, description?, is_default?: true}` → the template, without `versions`. `is_default: false` is refused (400): promote another template instead. Promoting a template with no versions is refused (409).
- `POST /templates/<id>/versions` multipart `file` → the same checks and responses as `POST /profile/resume`, without the onboarding fields.
- `GET /templates/<id>/download[?version=N]` → the file (latest by default). `version` is at most 9 digits (400 otherwise).
- Uploading a version does not change the template's `updated_at`, which tracks name, description and default changes.
- `GET /profile/resume` also returns `template`: the default template, or null until the account's first upload.
