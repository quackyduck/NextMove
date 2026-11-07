# Agent collaboration API

Base URL in development: `http://127.0.0.1:5001/api/v1`.
Managed tokens use `Authorization: Bearer <token>` and the permissions described below.
The legacy `AGENT_API_TOKEN`, if explicitly enabled, grants broad access to user #1's
workspace (never other accounts); it is disabled in the hosted deployment. Keep tokens out of source control, URLs and
logs. Production requests must use HTTPS.

Browser sessions use `/auth/signup`, `/auth/login`, `/auth/session`, `/auth/logout` and a CSRF token returned by signup/login/session. Send `X-CSRF-Token` on session-authenticated writes. Bearer clients do not need CSRF tokens.

## Accounts and tenancy (schema 9; email verification required since schema 15)

Every job, contact, resume (and its file), activity, agent token, extraction preview and ingestion receipt belongs to one account. Every request acts as exactly one account: the signed-in browser user, the user who created the managed token, or the migrated workspace owner (user #1, flagged `owns_legacy_tokens`; never a signed-up account) for the deprecated `AGENT_API_TOKEN` / `ROLE_SEARCH_TOKEN_SHA256`. Another account's IDs behave exactly like IDs that do not exist (404), and lists and search only include your own records.

An account can only be used once its email is verified (schema 15, see [Email confirmation](#email-confirmation-schema-15)). Sign-up never creates a session: the emailed link does.

| Method | Path | Body | Responses |
| --- | --- | --- | --- |
| GET | /auth/session | — | 200 `{authenticated, csrf_token, user: {id, email, created_at} \| null, signup: {enabled, invite_required, paused, paused_message}, mail_status}` |
| GET | /auth/challenge?purpose= | — | 200 `{challenge, bits, expires_at}` for `signup`, `activate`, `resend` or `reset`; 400 other purposes; 429 after 30 per client per 5 minutes |
| POST | /auth/signup | `{email, password, password_confirmation, invite_code, proof}` | **202** `{message}` and a confirmation cookie, for every address; 400 `{error, field}` (`email`, `password`, `password_confirmation`), or 400 `{code: "proof_invalid"}`; 403 `{error, field: "invite_code"}` for a missing, unknown, used or expired invite (or `{error}` when sign-up is closed or email is off); 503 `{code: "signup_paused"}`; 429 throttled. **There is no 409.** |
| POST | /auth/login | `{email, password}` | 200 `{authenticated: true, csrf_token, user}`; 401 `Invalid email or password` for every failure; 403 `{code: "verification_required", email_hint}` or `{code: "verification_unavailable"}` only after a **correct** password on an account that has not verified its email (see below); 429 throttled |
| POST | /auth/password | `{current_password, new_password, confirm_password}` | Changes only the signed-in account; see below |
| POST | /auth/logout | — | 200 `{authenticated: false, csrf_token: null}` |
| POST | /auth/email-verification/check | `{token}` | 200 `{valid: true, kind, email_hint, needs_password, signed_in_as_other}` or `{valid: false}`; never uses the link |
| POST | /auth/email-verification/confirm | `{token, password?}` | 200 `{authenticated: true, csrf_token, user}` and a new session; 400 `{code: "password_required" \| "wrong_password", field: "password"}`; 409 `{code: "account_exists"}`; 410 for every other failure; 429; 503 |
| POST | /auth/email-verification/resend | `{email, proof}` | **202** `{message}` always (malformed input too); 400 `{code: "proof_invalid"}`; 429 per client |

- Emails are trimmed and lowercased. New addresses (sign-up, activation, `set-email`) must also be deliverable: an RFC 5322 dot-atom local part and an ASCII domain with at least two labels, no IP literals, no `localhost`, no non-ASCII characters (type the `xn--` form of an internationalized domain). Sign-in still looks up older addresses leniently. Passwords are 8–128 characters.
- **No enumeration on sign-up.** The request never looks the address up: it always returns the same 202 and stores a pending sign-up. The queue worker decides what to send. A new address gets a confirmation link; an address that already has an account gets "You already have an account: sign in or reset your password", with no link, and the pending row is deleted. Login reveals that an account still needs verification only to someone who typed its correct password.
- Invites are single-use and expire (default 7 days). An invite is checked at sign-up and consumed only when the sign-up is confirmed, in the same transaction that creates the account (403 `{code: "invite_used"}` if another sign-up confirmed it first). `SIGNUP_REQUIRES_INVITE=0` opens sign-up without codes; `SIGNUP_ENABLED=0` closes it. Sign-up is also closed while email is off (`MAIL_BACKEND=none`).
- Sign-up, activation, resend and the password-reset request each need a solved proof-of-work: `GET /auth/challenge?purpose=…` returns a signed challenge, and the browser finds a nonce such that SHA-256(`challenge:nonce`) starts with `bits` zero bits (18 normally, 20 while email is degraded; about a second on a phone). Each challenge works once and expires after 10 minutes.
- Signup, login, activation and the confirmation routes reject cross-origin `Origin` headers (403).
- Client addresses are throttled as IPv4 addresses or IPv6 /64 prefixes. Login attempt budgets per five minutes: 30 per client; 10 per account (by email, known or unknown, so 429 never reveals an account), after which a client that has made fewer than 3 attempts on that account may still try, up to 50 per account. An attacker hammering one email therefore cannot lock its owner out. There is no global login budget. Sign-up, activation and resend share **one** budget per client: 5 per 5 minutes and 8 per day (429 with `Retry-After`). Per address (ignoring `+tags`, and dots for Gmail), at most 3 such emails a day, or 6 when the extra requests come from clients that have not asked for that address before; over that, the response is the same 202 and nothing is sent. Password changes and token creation use a separate per-user budget (10), so login abuse cannot block a signed-in owner. Unknown emails take the same time as wrong passwords.
- `/jobs/search` allows 120 requests per minute per account, plus 120 per client before authentication.
- A password change signs out that account's other sessions only.

Administrators manage accounts from the server shell (`flask --app jobtracker ...`); there is no admin API:

| Command | Effect |
| --- | --- |
| `invite create [--count N] [--expires-days D]` | Prints N new single-use codes (only hashes are stored). Share them however you like. |
| `invite list` | Code prefix, status (unused / expired / used by user N) and expiry |
| `user list [--unverified] [--undeliverable]` | ID, email, verified or unverified, deliverable or not, and creation time |
| `user create EMAIL [--unverified] [--allow-placeholder]` | Creates an account (verified unless `--unverified`); prompts for the password. For servers where sign-up is off. |
| `user verify EMAIL` / `user unverify EMAIL` | Marks the email verified (support confirmed it), or clears that and signs the account out |
| `user send-verification EMAIL` / `--all-legacy` | Queues a verification link; confirming it needs the account's password. Never prints a link. |
| `user reset-password EMAIL [--keep-harness \| --revoke-harness]` | Prompts for a new password (never an argument), signs that user out everywhere, revokes their OAuth connections, agent tokens and reset links (chat links too when the email is unverified, unless overridden), and emails the "password changed" notice. See [Password reset](#password-reset-schema-16) |
| `user reset-link EMAIL \| --id ID [--minutes N]` | Prints a one-time reset link (5–1440 minutes, default 60). Sends no email and never verifies the address |
| `user set-email ID EMAIL [--verified] [--send-verification]` | Changes a sign-in email in one transaction: the address must be deliverable; verification is cleared unless `--verified`; pending confirmations are deleted; every browser session of that account is signed out |
| `user revoke-agents ID [--harness]` | Revokes the account's OAuth connections and agent tokens (and chat grants with `--harness`); prints the counts |
| `user merge-messaging SOURCE TARGET` | Links a phone to an existing account: moves the harness grants of messaging account SOURCE (never activated, no data) to account TARGET, then deletes SOURCE |
| `signup pending [--email E]` | Counts of pending confirmations by kind and delivery state (addresses masked) |
| `mail test TO` / `mail status` | Sends a fixed test message / shows usage against the daily, per-kind and monthly limits |
| `reset-workspace-password` | Legacy alias for resetting user #1 |
| `worker [--once]` | Runs the queue worker (same as `python -m jobtracker.worker`), which also sends email |

### Email confirmation (schema 15)

Confirmations live in `email_confirmations`, never in `users`. A row is one of three kinds:

- `signup`: a sign-up that is not an account yet. It holds the chosen password's hash. Confirming creates the account, already verified, and starts guided onboarding.
- `activation`: a messaging account's chosen email and password (see the harness section). The account stays messaging-only until the link is confirmed.
- `existing`: an account created before verification was required. It verifies at its next sign-in.

The worker mints the token right before sending (only its SHA-256 is stored), so no token is ever in the queue. The link is `PUBLIC_BASE_URL/verify-email#token=…`. The token is in the URL fragment, which is never sent to a server; the page moves it into memory and clears the address bar before any other script runs. Links work once and expire 24 hours after they are sent. A link not sent within 24 hours (email delayed) is cancelled. Pending rows, with their password hashes, are deleted about two days after they are created at the latest, and right away when the address turns out to have an account.

**Confirming needs the link and proof of the password:** either the confirmation cookie (`next_move_confirm`: HttpOnly, 48 hours, set in the browser that chose the password; one click), or typing that password again (5 tries per link; then the link stops working). So someone who receives a link for a sign-up they did not make cannot create, activate or verify an account that someone else controls. Checking a link never uses it. Confirming signs the browser in, replacing any other session (`check` reports `signed_in_as_other` so the page can warn first). Signing up again from the same browser replaces that browser's pending password and link. From another browser it adds a separate pending sign-up, and the first one confirmed wins.

**Resend while signed out** (`/auth/email-verification/resend`) always answers 202 and looks nothing up while handling the request. The worker re-sends the newest pending link for that address, or a verification link for an unverified older account, and sends nothing for any other address.

**Older accounts** keep `email_verified_at` NULL after the upgrade. Their browser sessions end. The next sign-in with the correct password returns 403 `verification_required` and emails a link (at most one a minute and 5 a day). An address that cannot receive mail (e.g. `owner@localhost`) returns `verification_unavailable`: an operator fixes it with `user set-email ID EMAIL --verified`. Until verified, their existing OAuth connections, agent tokens, the legacy token and harness grants keep working, but they cannot sign in to the dashboard, approve new connections or revoke tokens there. Support can revoke them with `user revoke-agents`. A completed emailed password reset also verifies the address.

**Email limits.** All email shares a daily limit (`MAIL_DAILY_LIMIT`, default 90, rolling 24 h) and a monthly one (`MAIL_MONTHLY_LIMIT`, 3,000). Within the day, sign-up email (links, notices, resends) may use 50, of which the last 5 are reserved for older accounts' verification links. Password-reset email may use 25, and the rest (15) is kept for "password changed" notices. Before sending, the worker checks the recipient's domain in DNS (MX, else A/AAAA; no mail to domains that cannot receive it) and caps sign-up email per domain (10 a day, 40 for large providers) and per mail server (15). When sign-up email is used up for the day, `/auth/session` reports `signup.paused` with "Sign-up is paused for today. Try again tomorrow." and signup and activation return 503 `signup_paused`. Deferred email is retried later and is never counted as a failed attempt. `mail_status` is `degraded` while a limit is spent or the provider is failing, and `off` when email is turned off. It describes the whole service, never one address. Verification shows a masked `email_hint`. The password-reset status shows the full address, because that form needs it.

## Typical collaboration

1. `POST /jobs` with `title`, `company`, `url` and/or `description`. Capture the returned `id` and `version`.
2. `GET /jobs/<id>` to obtain the current description, notes and revision.
3. Tailor a resume outside this service.
4. `POST /jobs/<id>/resumes`, multipart field `file`, with a PDF, DOCX or TXT file. This creates a new attachment; it does not replace an earlier version.
5. `PATCH /jobs/<id>` with `{ "stage": "resume_updated", "version": <current version> }`. Fetch the latest job first if another operation changed its revision. A 409 means reread and reconcile, never blindly retry stale edits.
6. `GET /jobs/<id>/activities` for progress history.

Do not claim a role was applied to or a recruiter contacted merely because a resume was uploaded. Stage changes should represent actual work done.

## Routes

Every response has an `X-Request-ID` header. A valid incoming value (`[A-Za-z0-9-]{8,64}`) is kept; in production
Caddy always replaces it. 5xx JSON bodies are `{"error": "...", "request_id": "..."}`; quote that ID to My Next Moov
support. `GET /api/v1/client-config` (public) returns the browser telemetry settings: `{"sentry": null}` or
`{"sentry": {"dsn": "...", "environment": "..."}}`.


JSON bodies must be valid Unicode: a string or key holding an unpaired UTF-16 surrogate escape (such as `"\ud800"`) is treated as malformed JSON and refused with the route's usual 400. Escaped surrogate pairs (`"\ud83d\ude00"`, an emoji) are fine. Raw bytes that are not valid UTF-8 in a JSON body are refused the same way. MCP answers either with JSON-RPC `-32700 Parse error`, and JSON nested more than 64 levels deep is treated as malformed too. On every route, a query string carrying raw (not percent-encoded) bytes that are not valid UTF-8 returns 400 `{"error": "The query string must be valid UTF-8"}`; percent-encoded UTF-8 such as `?q=%F0%9F%98%80` works as usual.

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
| POST | /jobs/{id}/resumes | Upload multipart `file`, optionally `source_resume_id` (201) |
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
- `GET /me/templates`: the same `{templates: [...]}` as `GET /templates`, default first. Each `latest.id` is a version the file route above downloads, so the harness can choose the template closest to a posting. Read-only (`resumes:read`): creating, renaming and promoting templates stay with the user.
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
- `POST /auth/activate` `{token, email, password, password_confirmation, proof}` consumes the activation link and returns **202** with a confirmation cookie. It creates no session, and gives no 409 for an address already in use. The chosen email is **not** bound yet: the account stays messaging-only (no email, no password, the harness keeps working) until the emailed confirmation link is opened. That link needs the activating browser's cookie or the chosen password, so a phone holder cannot claim someone else's address. An address that already belongs to another account gets an email saying so instead of a link. Confirming sets the email, password and verification, and signs in. A used, expired or unknown activation link gets 410. The route shares the sign-up budgets and pauses with sign-up. The dashboard opens activation from `/?activate=<token>`. Accounts activated before schema 15 verify at their next sign-in like other older accounts. A messaging account that never verified loses its chat link if its password is reset (the reset cannot tell the phone holder from the mailbox owner); verifying first keeps it.

## Third-party agents: OAuth, MCP and OpenAPI (schema 12)

Agents connect with OAuth 2.1 and use either the MCP server at `/mcp` or this API with the same access tokens. The contract is published at `GET /openapi.json` (no authentication). It covers the routes OAuth tokens may call, with the scope each one requires. See [MCP.md](MCP.md) for the consent flow, tools, token lifetimes and security model.

| Route | Credentials | Scope |
| --- | --- | --- |
| `GET /jobs`, `GET /jobs/{id}`, `/activities`, `GET /contacts`, `GET /extractions/{id}`, `GET /jobs/search` | session, legacy, OAuth | `jobs:read` |
| `POST /jobs`, `PATCH /jobs/{id}`, `POST /jobs/extract`, `POST /contacts` | session, legacy, OAuth | `jobs:write` |
| `GET /jobs/{id}/resumes`, `GET /resumes/{id}/download`, `GET /profile/resume`, `GET /profile/resume/download` | session (and legacy for job resumes), OAuth | `resumes:read` |
| `POST /jobs/{id}/resumes`, `POST /profile/resume` | session (plus legacy and `roles:ingest` for job resumes), OAuth | `resumes:write` |
| `GET /templates`, `GET /templates/{id}`, `GET /templates/{id}/download`, `GET /resume-diff` | session, OAuth | `resumes:read` |
| `POST /templates`, `PATCH /templates/{id}`, `POST /templates/{id}/versions` | session, OAuth | `resumes:write` |
| `GET /oauth/connections`, `POST /oauth/connections/{id}/revoke` | browser session only | — |
| `GET /files/{token}`, `POST /uploads/{token}` | the signed link itself | issued by MCP tools |

Schema 12 adds `oauth_clients`, `oauth_grants`, `oauth_codes` and `oauth_tokens`. Set `PUBLIC_BASE_URL` in production: it is the OAuth issuer and the base URL of MCP links.

## Resume templates (schema 13)

A resume template is a named master resume for one kind of role ("Senior product manager", "Engineering manager"), with its own version history numbered 1, 2, ... Names are free text (100 characters, NFC-normalized, no control or formatting characters such as NUL or bidi overrides), unique per account ignoring case, runs of whitespace and NFKC compatibility forms such as full-width letters; descriptions are optional (2,000 characters; newlines and tabs allowed). An account keeps up to 10 templates and 20 versions of each. Agents can never delete; you can delete versions and templates in the app (see [Deleting data](#deleting-data-schema-17)).

Exactly one template is the **default**: it is what "the master resume" means for `/profile/resume`, the harness `/me/master-resumes` routes and MCP `list_resumes` / `read_resume` without an ID. The default is never empty: new templates start without one, and the first template to receive a version becomes the default. An upload that names no template (for example `/profile/resume` or onboarding) goes to the default. Before there is one, it goes to the account's only template if it has exactly one; otherwise to the template named "Main resume" (created if needed, which needs a free slot: with 10 templates and no default it is refused with 409). The default changes only by promoting another template that has at least one version. Signed `master` upload links (MCP `create_resume_upload_link`) are pinned to the default at issue time when there is one; issued before any default, they resolve it when used. Schema 13 adds `resume_templates` and `master_resumes.template_id`, and moves each account's existing versions into a default "Main resume" with their numbers unchanged; accounts with no master resume get no template.

All routes take a browser session or an OAuth token (never legacy or managed agent tokens); another account's template IDs return the same 404 as unknown ones.

**Connected apps never choose the default.** OAuth tokens follow the same policy here as over MCP. The 404 for unknown and other accounts' IDs always comes first.

- `POST /templates` needs an existing default (403 before the user's first upload) and is refused with 409 once the account has 5 templates; the other 5 are the user's.
- `PATCH /templates/<id>` is refused with 403: renaming, editing the description and promoting stay with the user.
- `POST /templates/<id>/versions` is refused with 403 until the account has a default. `POST /profile/resume` stays open to OAuth tokens and follows the no-default rule above.

- `GET /templates` → `{templates: [{id, name, description, is_default, created_at, updated_at, version_count, latest}]}`, default first, then by name.
- `POST /templates` `{name, description?}` → 201, the new (empty, non-default) template with `versions: []`. 409 for a taken name or an 11th template.
- `GET /templates/<id>` → the template with `versions` (newest first; each version carries `template_id`).
- `PATCH /templates/<id>` `{name?, description?, is_default?: true}` → the template, without `versions`. `is_default: false` is refused (400): promote another template instead. Promoting a template with no versions is refused (409).
- `POST /templates/<id>/versions` multipart `file` → the same checks and responses as `POST /profile/resume`, without the onboarding fields.
- `GET /templates/<id>/download[?version=N]` → the file (latest by default). `version` is at most 9 digits (400 otherwise).
- Uploading a version does not change the template's `updated_at`, which tracks name, description and default changes.
- `GET /profile/resume` also returns `template`: the default template, or null until the account's first upload.

### Which template a role's resume came from (schema 14)

A resume attached to a role can record the template version it was tailored from. `POST /jobs/<id>/resumes` takes an optional multipart field `source_resume_id`: the ID of any of the caller's template versions, in any template and not only the latest (from `GET /templates/<id>` or `/profile/resume`). An unknown ID, another account's, or a role resume's ID all get the same 404 `{"error": "Source resume version not found"}`, before the file is stored. Approving a harness draft records the version the draft was made from. The source is set once and cannot be changed.

Role resumes (`GET /jobs/<id>/resumes` and the upload response) carry `source`: `{resume_id, template_id, template_name, version}`, or null. `template_name` is the template's current name, so a rename shows up here. Callers that can't read resumes (managed `roles:ingest` tokens, and OAuth connections or upload links without `resumes:read`) get only `source: {resume_id}` in the upload response. The `resume_added` activity stores only `details.source_resume_id`; `GET /jobs/<id>/activities` and MCP `get_job_history` add the resolved `details.source` only for callers that can read resumes. Schema 14 adds `resumes.source_master_resume_id`. A composite foreign key to `master_resumes (id, user_id)`, through the new unique index `uq_master_resumes_id_user`, keeps it inside the same account. Existing resumes record no source.

### Comparing resumes

`GET /resume-diff?from=<id>&to=<id>` compares two resumes paragraph by paragraph. Each ID is a template (meaning its latest version) or any resume version the caller owns: a template version or a resume attached to a role, whichever template it was tailored from. It returns `{from, to, identical, summary, outline, changes, truncated}`:

- `from` / `to`: the version's metadata with `kind` (`master` or `job`) and `template: {id, name}` (null for a role's resume). Stored file names are never returned.
- `summary`: `{added, removed, changed, unchanged}` paragraph counts.
- `outline`: `{from: [...], to: [...]}`, each side's headings in order.
- `changes`: `[{op, from_section, to_section, section, from, to}]` in document order, where `op` is `added`, `removed` or `changed`; `from_section` / `to_section` are the heading the paragraph sits under on that side (`""` before the first heading, null when that side is absent); `section` is `to_section`, or `from_section` for a removal; and `from` / `to` are `{id, text, kind}` or null. A replaced run of paragraphs is paired in order as `changed`; any surplus is `added` or `removed`. Moved paragraphs show as a removal and an addition. At most 500 changes are listed; `truncated` says whether there were more (the summary still counts them all).
- Only text is compared, after Unicode NFC normalization: a paragraph whose kind (heading, bullet, body) or formatting alone changed is not reported, so a re-saved file can return `identical: false` with no changes. Headings come from Word `Heading*`/`Title` styles; TXT files have none.
- Identical files (same SHA-256) return `identical: true` and no changes; the file is read once.

Errors: 400 without `from` or `to`; 404 `"<from|to>: template or resume not found"` for an unknown ID or another account's (same body); 409 for a template with no version yet; 422 for a PDF (no text is extracted; upload a DOCX or TXT version), or a document over 2,000 paragraphs or 100,000 characters of text. An ID is looked up as a template first, then as a resume version.

## Password reset (schema 16)

Self-service reset for accounts with a password: `verified` accounts and `legacy_unverified` ones. Pending sign-ups and activations aren't accounts, and messaging-only accounts have no email, so they never receive a reset link. All three routes are signed-out, same-origin, JSON-only and HTTPS-only outside development.

| Method | Path | Body | Responses |
| --- | --- | --- | --- |
| POST | /auth/password-reset/request | `{email, proof}` | 202 `{message}`, identical for every address; 400 `{code: "proof_invalid"}` (solve a new `/auth/challenge?purpose=reset`), 400 `{field: "email"}`; 429 per IP; 503 `{code: "reset_unavailable"}` when reset is off |
| POST | /auth/password-reset/status | `{token}` | 200 `{valid: true, email}` or `{valid: false}`. Never consumes the link |
| POST | /auth/password-reset/complete | `{token, password, password_confirmation}` | 200 `{message, email, disconnected: {sessions, agents, chat}, review_data}`; 400 `{field}` (the link stays valid); 410 for any unusable link; 429; 503 |

- **Request.** The route checks the proof-of-work, then the throttles, then enqueues one anonymous task holding the address. It never reads `users`, so the response and the work are the same for every address. The worker looks the account up, checks that the address can receive mail, and only then mints the link. Throttles: 5 per 5 minutes and 8 per day per IP (IPv6 by /64); 3 per hour and 5 per day per address, using the canonical form (`+tags` and Gmail dots count as one address). Over the per-address limit the 202 is unchanged and nothing is sent.
- **Link.** `{PUBLIC_BASE_URL}/reset-password#token=nmr_…`. It works once, expires after 60 minutes (`PASSWORD_RESET_MINUTES`) and is bound to the account's session generation and email, so any password or email change, or a newer link, kills it. Only a SHA-256 of the token is stored. The fragment never reaches a server.
- **Status** returns the full email so password managers can fill it in; only someone holding a valid link sees it. Email verification masks its hint instead.
- **Complete.** One transaction sets the password and signs out every browser session. It revokes OAuth connections (and their tokens and signed links) and managed agent tokens. When the email was still unverified it also revokes harness (chat) grants, since that email was never proven to belong to the phone's owner. An emailed link marks the address verified; an operator link doesn't. Completing never signs in. A "password changed" notice is queued, and self-service `POST /auth/password` sends it too.
- `GET /auth/session` includes `password_reset: {enabled}`. It's true when `PASSWORD_RESET_ENABLED` isn't `0`, mail is on and `PUBLIC_BASE_URL` is set.
- Mail: reset links use the `reset` share of the daily cap (`MAIL_LIMIT_RESET`, 25); the notice uses what's left (at least 15). A send deferred by the quota waits without using up attempts, for at most 48 hours.

## Deleting data (schema 17)

Only the signed-in owner can delete, in the app: a browser session plus `X-CSRF-Token`, and HTTPS outside development. OAuth and MCP tokens, managed agent tokens, harness grants and the legacy `AGENT_API_TOKEN` are refused (401/403). Agents can add and edit, but never delete. None of these routes is in the OpenAPI contract, and there are no delete tools.

| Method | Path | Body | Result |
| --- | --- | --- | --- |
| GET | /jobs/{id}/deletion | — | `{contacts, activities, resumes, drafts}`, the counts the confirmation shows |
| DELETE | /jobs/{id} | `{version}` | Deletes the role with its contacts, notes, activity, resumes and drafts, and their files. 409 when `version` is stale. Queued fill tasks for the role are cancelled. Ingestion receipts are kept but scrubbed (`url` emptied), so replaying the same Idempotency-Key answers `{"status": "deleted"}` and never re-creates the role. |
| DELETE | /resumes/{id} | — | Deletes one job resume and its file. Signed links for it stop working. |
| GET | /templates/{id}/deletion | — | `{name, versions, tailored_resumes_unlinked, drafts, is_default, other_templates, deletable}` |
| DELETE | /templates/{id} | `{confirm_name}` | Deletes the template and all its versions. The name must match exactly (400). The default can't be deleted while other templates exist (409); the only template can. |
| DELETE | /templates/{id}/versions/{version_id} | — | Deletes one version. The version must belong to that template (404 otherwise). Numbers are never reused or renumbered. Refused (409) when it's the default template's last version and another template has versions. |

**What happens to related data:**
- Tailored resumes made from a deleted version keep their content and lose their link to the source. The role's history records why.
- Harness drafts made from it are deleted.
- Approving a draft now removes the draft's own file copies, so no hidden copy outlives the job resume.

Every deletion is final; there's no undo or trash. Each one writes an id-only row to the deletion ledger (`deletions`, kept 35 days), which lets an operator re-apply deletions after restoring a backup (OPERATIONS.md).

**Export download.** The operator's `flask user export` link is `{PUBLIC_BASE_URL}/api/v1/exports/nme_…`. It needs no session and is valid 48 h.
- `GET` shows a small page with a **Download my data (ZIP)** button and uses up nothing, so mail scanners and link previews are harmless.
- The button sends a same-origin `POST` to the same URL; a cross-origin POST gets 403. Each POST counts, and 3 are allowed.
- Unknown, expired or used-up links get a 404 page.

Administrators (`flask --app jobtracker …`):

| Command | Effect |
| --- | --- |
| `user delete EMAIL \| --id N --code CODE [--yes] [--allow-owner] [--pending-only]` | Deletes the account and everything in it after a typed confirmation. The support code must have been issued for this account and `delete`; it's consumed in the deletion transaction, and `--yes` never waives it. `--yes` needs `--id`. Refuses the workspace owner without `--allow-owner`. Verified accounts get an "account deleted" email. Prints the NextMoveAgent step. |
| `user export EMAIL \| --id N --code CODE` | Builds a ZIP of the account's data and prints a 48-hour download link. The code must have been issued for this account and `export`; it's consumed with the export. |
| `support code --id N --for delete\|export` / `support check CODE` | Issues a one-time code bound to one account and one action (72 h), and shows what a returned code authorizes without using it up |
| `deletions export` / `deletions replay FILE [--force]` | Ledger export before a restore, and replay after it (never sends mail) |
| `storage sweep [--apply] [--force]` | Reports, or with `--apply` removes, orphan files and deleted accounts' folders. Guarded. |
