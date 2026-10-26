# Agent collaboration API

Base URL in development: `http://127.0.0.1:5001/api/v1`.
Managed tokens use `Authorization: Bearer <token>` and the permissions described below.
The legacy `AGENT_API_TOKEN`, if explicitly enabled, grants broad workspace access;
it is disabled in the hosted deployment. Keep tokens out of source control, URLs and
logs. Production requests must use HTTPS.

Browser sessions use `/auth/login`, `/auth/session`, `/auth/logout` and a CSRF token returned by login/session. Send `X-CSRF-Token` on session-authenticated writes. Bearer clients do not need CSRF tokens.

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
  --token-file /path/to/private/token --base-url https://nicolasmelo.com/nextmove
```

The tool only uploads the file. It deliberately leaves progress changes to the caller. Resume files are private and only served by authenticated download routes. Exact server-enforced limits are documented in the application configuration.

## Not implemented in the first manual demo

Queued extraction, OpenAPI generation and spreadsheet import remain future work. URL extraction and review are available asynchronously through `/jobs/extract`; `POST /jobs` itself does not fetch URLs.

## Outreach badge

Job JSON includes `outreach_completed` (a JSON boolean, default `false`), accepted by job POST/PATCH. Creating a role at Outreach or transitioning into Outreach sets it to `true`. Other stage changes preserve it. An explicit PATCH can mark past outreach or correct an accidental mark without changing stage; a transition into Outreach takes precedence and marks outreach completed. Changes are recorded atomically in the activity timeline and respect optimistic versions.

Contacts are people you may contact; adding one does not prove contact was made and does not automatically mark outreach. A later pipeline stage also does not imply outreach occurred. The badge appears on table rows, board cards and job details while this field is true.

## Workspace password (browser session only)

`POST /auth/password` accepts exactly `{ "current_password": "...", "new_password": "...", "confirm_password": "..." }`. A valid browser session and its `X-CSRF-Token` are required; even valid bearer tokens receive 403. New passwords must contain 8–128 characters and match confirmation. Wrong current password/invalid input returns 400; the persistent shared sign-in attempt budget also applies to password verification (429/Retry-After).

Success returns `{ "authenticated": true, "csrf_token": "...", "message": "Password updated. Other sessions have been signed out." }`. Replace the client's CSRF token with this value. Other browser cookies are invalidated by a stored session generation; the current session is rotated and retained. A concurrent change returns 409 and clears the stale session; unavailable credential storage returns 503 and never falls back to the environment verifier. No password/hash/session generation is exposed in responses or job history. Agent token authority is unchanged by browser password changes.

## URL extraction preview

`POST /jobs/extract` accepts only `{ "url": "https://company.example/careers/role" }` with normal session/CSRF or bearer authorization. It returns **202** with `{ "id": "...", "status": "running" }`, a `Location` status URL, and `Retry-After: 1`, before any DNS, fetch, or parsing work. Invalid syntax returns 400; capacity exhaustion returns 429.

Authenticated `GET /extractions/{id}` returns the task's `id` and `status`:

- `running`: extraction is in progress.
- `succeeded`: includes `fields` (`title`, `company`, `url`, `salary`, `notes`, `description`) and a `warnings` array. Missing fields are empty strings. Qualifications go into notes; the submitted URL is retained through redirects.
- `failed`: includes a user-facing `error`. Private destinations, fetch/parse errors, and ambiguous multi-role pages fail the task. Task failures return HTTP 200; missing/expired previews return 404.

Review/edit `fields`, then `POST /jobs` to save. Extraction never creates a pipeline role. The UI polls every 750 ms, pauses for sign-in recovery, and offers retry after a status-request failure.

Fetching uses public HTTP(S) destinations on standard ports, validates all DNS answers and redirects, pins connections to validated IPs, and verifies TLS against the original hostname. It does not execute JavaScript or authenticate to job boards. Extraction prefers Schema.org JobPosting JSON-LD, with best-effort HTML fallback. See [operations](OPERATIONS.md#quick-capture-release--schema-4) for concurrency, fetch limits, expiry, and restart behavior.


## URL ingestion and managed tokens

[The proposal](URL_INGESTION_PROPOSAL.md) covers onboarding, agent setup, retries, and rollout. `POST /ingestions` accepts `{ "url": "..." }` and a required `Idempotency-Key`, returning 202 and a status `Location`. `GET /ingestions/{id}` returns running, success with `job_id`/warnings, or failure. Successful extraction automatically saves a Needs review role, with qualifications in notes. Keys remain unique across tokens; another token cannot reuse an existing key or read its receipt.

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
