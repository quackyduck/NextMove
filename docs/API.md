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

`POST /jobs/extract` accepts exactly `{ "url": "https://company.example/careers/role" }`, with normal session/CSRF or bearer authorization. It performs syntax validation and task admission only, returning **202** with `{ "id": "...", "status": "running" }`, a `Location` status URL, and `Retry-After: 1`. DNS, fetching, and parsing happen in a bounded background thread. No record is created in the job pipeline during extraction.

`GET /extractions/{id}` requires authentication and returns `{ "id": "...", "status": "running" }` until done. Success returns `{ "id": "...", "status": "succeeded", "fields": { "title": "...", "company": "...", "url": "...", "salary": "...", "notes": "...", "description": "..." }, "warnings": ["..."] }`. Failure returns HTTP 200 with `{ "id": "...", "status": "failed", "error": "..." }`. Review/edit `fields`, then submit to `POST /jobs` to persist the role. Qualifications go into `notes`; the original submitted URL is retained even if the page redirects. Unavailable fields are empty strings, with warnings. The UI polls every 750 ms while the extraction runs, pauses during sign-in recovery, and supports retrying a failed status request.

Only public HTTP(S) destinations on standard ports are allowed, with all DNS answers and every redirect checked and connections pinned to validated addresses. Invalid URL syntax returns 400 immediately. Private destinations, unreadable, oversized, non-HTML, ambiguous multi-role pages and fetch failures become failed tasks. Fetching is bounded to 2 MiB and four redirects, with a 20-second network-read budget and five-second socket timeouts. Schema.org JobPosting JSON-LD is preferred; plain HTML extraction is best effort and does not run JavaScript or authenticate to job boards.

Schema 4 stores preview results in SQLite so any web process can serve polling requests. Two tasks may run at once (429 if full); a per-process semaphore also bounds threads if DNS stalls past task expiry. Tasks abandoned during process restart fail after 60 seconds; they are not automatically retried. Preview results expire after one day (404), and expired rows are removed on subsequent submissions. This single-user background runner does not require Redis/Celery. For larger workloads or execution that must survive worker restarts, use a dedicated durable queue worker.
