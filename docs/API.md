# Agent collaboration API

Base URL in development: `http://127.0.0.1:5001/api/v1`.
Configure `AGENT_API_TOKEN` on the server and send `Authorization: Bearer <token>`.
The initial token grants access to this single-user workspace; scoped/revocable tokens are a later milestone. Keep it out of source control, URLs and logs. Production requests must use HTTPS.

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

After creating a job, use the standard-library example:

```sh
JOBTRACKER_AGENT_TOKEN='<your configured AGENT_API_TOKEN>' \
  .venv/bin/python scripts/agent_example.py '<job UUID>' '/absolute/path/revised-resume.pdf'
```

The tool only uploads the file. It deliberately leaves progress changes to the caller. Resume files are private and only served by authenticated download routes. Exact server-enforced limits are documented in the application configuration.

## Not implemented in the first manual demo

Queued extraction, scoped token management, OpenAPI generation and spreadsheet import remain future work. URL extraction and review are available asynchronously through `/jobs/extract`; `POST /jobs` itself does not fetch URLs.

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


## Proposed URL ingestion API (draft; unavailable in production)

See [the URL ingestion proposal](URL_INGESTION_PROPOSAL.md) for the security model, contract, retry semantics, setup, and rollout decisions. The draft adds `POST /ingestions` with `{ "url": "..." }` and a required `Idempotency-Key`, returning 202 and a status `Location`. `GET /ingestions/{id}` returns `running`, `succeeded` with `job_id`/warnings, or `failed` with an error. Successful extraction automatically saves an Interesting role, with qualifications in notes.

These routes accept only a dedicated bearer token whose SHA-256 matches `INGEST_API_TOKEN_SHA256`; they reject browser sessions and the broad agent token. The credential does not grant other API access. It is disabled by default. Use `scripts/ingest_url.py --help` for the secure client example. Schema 5 is required; the implementation and activation remain subject to review.
