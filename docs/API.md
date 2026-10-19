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
