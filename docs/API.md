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
| POST | /jobs | Create a manual job (201) |
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

Queued extraction, extraction review, scoped token management, OpenAPI generation and spreadsheet import are later milestones in PLAN.md. The API accepts manually entered descriptions now; adding a URL does not yet fetch it.

## Outreach badge

Job JSON includes `outreach_completed` (a JSON boolean, default `false`), accepted by job POST/PATCH. Creating a role at Outreach or transitioning into Outreach sets it to `true`. Other stage changes preserve it. An explicit PATCH can mark past outreach or correct an accidental mark without changing stage; a transition into Outreach takes precedence and marks outreach completed. Changes are recorded atomically in the activity timeline and respect optimistic versions.

Contacts are people you may contact; adding one does not prove contact was made and does not automatically mark outreach. A later pipeline stage also does not imply outreach occurred. The badge appears on table rows, board cards and job details while this field is true.

## Workspace password (browser session only)

`POST /auth/password` accepts exactly `{ "current_password": "...", "new_password": "...", "confirm_password": "..." }`. A valid browser session and its `X-CSRF-Token` are required; even valid bearer tokens receive 403. New passwords must contain 8–128 characters and match confirmation. Wrong current password/invalid input returns 400; the persistent shared sign-in attempt budget also applies to password verification (429/Retry-After).

Success returns `{ "authenticated": true, "csrf_token": "...", "message": "Password updated. Other sessions have been signed out." }`. Replace the client's CSRF token with this value. Other browser cookies are invalidated by a stored session generation; the current session is rotated and retained. A concurrent change returns 409 and clears the stale session; unavailable credential storage returns 503 and never falls back to the environment verifier. No password/hash/session generation is exposed in responses or job history. Agent token authority is unchanged by browser password changes.
