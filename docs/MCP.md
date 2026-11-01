# Third-party agents: MCP, OAuth and the published API

People can use My Next Moov from the agent they already have (Claude, ChatGPT, Cursor, OpenClaw, or the Resume Agent) without copying tokens. There are two ways in, and both use the same accounts, permissions and services:

| Way in | URL | For |
| --- | --- | --- |
| MCP server | `{base}/mcp` | Agents that support MCP connectors. They discover the tools themselves. |
| REST API | `{base}/api/v1`, described at `{base}/api/v1/openapi.json` | Agents and scripts that call HTTP APIs. |

`{base}` is `PUBLIC_BASE_URL`. In production it is `https://mynextmoov.com`, so the MCP server is `https://mynextmoov.com/mcp`. The test server uses `https://nicolasmelo.com/nextmove` ([Deployments](DEPLOYMENTS.md)).

## Connecting an agent

1. Add `{base}/mcp` as a custom connector in the agent. In Claude this is Settings → Connectors → Add custom connector; in ChatGPT, use a connector or app in developer mode. Cursor, VS Code and other MCP clients take the URL in their MCP settings.
2. The agent opens a My Next Moov page. The user signs in (if they aren't already) and sees which agent is asking, where it will return to, and checkboxes for each scope.
3. The user approves. The agent receives tokens and lists the tools its scopes allow.

Connections appear under **Agent access → Connected apps**. Disconnecting one revokes all of its tokens and file links immediately. Approving the same app again updates its existing connection; it doesn't add a second one.

Agents that can only send a static header still use the existing managed tokens (`roles:search`, `roles:ingest`). Those tokens don't work on `/mcp`.

## Scopes

| Scope | Allows |
| --- | --- |
| `jobs:read` | List, read and search roles, history and contacts |
| `jobs:write` | Add roles, move stages, set outcomes, edit fields, add notes and contacts |
| `resumes:read` | List, read and download master and role resumes |
| `resumes:write` | Upload new master or role resume versions |

No scope allows deleting anything, changing the password, managing tokens or connections, or onboarding. Those routes accept browser sessions only, and OAuth tokens get 401 there.

## Tools

| Tool | Scope | Notes |
| --- | --- | --- |
| `list_jobs` | jobs:read | Filters: stage, outcome (default `active`, or `all`), query, limit |
| `get_job` | jobs:read | Posting, notes, contacts, and attached resumes when `resumes:read` was granted |
| `get_job_history` | jobs:read | Activity timeline |
| `add_job` | jobs:write | Starts at Needs review. With a URL and no description, the worker reads the posting |
| `move_job` | jobs:write | Stage, plus an optional dated note |
| `set_outcome` | jobs:write | Outcome only; the stage is unchanged |
| `add_note` | jobs:write | Appends `[YYYY-MM-DD · <agent name>] text` |
| `update_job` | jobs:write | Requires `version`; a stale version comes back as a tool error telling the agent to re-read |
| `add_contact` | jobs:write | Saving a contact does not mark outreach |
| `list_resumes` | resumes:read | Master versions, plus a role's attachments when given `job_id` |
| `read_resume` | resumes:read | DOCX/TXT as `{id, text, kind, style}` paragraphs; defaults to the latest master |
| `get_resume_download_link` | resumes:read | Signed URL, valid 5 minutes |
| `create_resume_upload_link` | resumes:write | Signed URL for a multipart `POST` with field `file`, valid 15 minutes |
| `upload_resume` | resumes:write | Base64 content up to 5 MB, for agents that can't make HTTP requests |

The server's `initialize` instructions tell agents the stage order, that uploading a resume is not applying, and to confirm stage or outcome changes unless the user asked for that exact change.

## Protocol details

- **MCP transport.** Streamable HTTP, stateless. `POST /mcp` takes one JSON-RPC message and returns JSON. Notifications get 202. `GET` and `DELETE` get 405, because there are no server-initiated messages or sessions. Supported protocol versions are 2025-11-25, 2025-06-18 and 2025-03-26. A browser `Origin` must be this site or listed in `MCP_ALLOWED_ORIGINS`.
- **Tool errors.** Failures such as validation errors, 404s and stale versions come back as results with `isError: true`, so the model can read them and recover. Tools a connection's scopes don't cover are left out of `tools/list`.
- **OAuth 2.1.** Authorization code with PKCE (S256 only), public clients only, and dynamic client registration at `POST /oauth/register` (10 per IP per 5 minutes).
  - **Redirect URIs** must be HTTPS, HTTP on loopback, or a private-use scheme such as `cursor://`. `javascript:`, `data:`, `file:` and similar schemes are refused.
  - **Codes** last 5 minutes and work once. Replaying one revokes the connection.
  - **Access tokens** (`nm_oat_`) last 1 hour.
  - **Refresh tokens** (`nm_ort_`) last 30 days and rotate on every use. Reusing an old one revokes the connection.
  - **Storage.** Only SHA-256 hashes of codes and tokens are stored.
  - **Revocation.** `POST /oauth/revoke` accepts either token type and disconnects the agent.
- **Discovery.**
  - `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource/mcp` are served by the app.
  - A 401 from `/mcp` names the metadata URL in `WWW-Authenticate`.
  - With `APP_ROOT_PATH`, nginx must also map the path-inserted URLs; see `deploy/nginx-nextmove.conf`.
- **REST.** The same tokens work on the job, contact, resume, search and master-resume routes listed in the OpenAPI document. A missing scope gets 403 with `error="insufficient_scope"`.
- **Consent page.** Rendered by the server. It can't be framed, and it requires the browser session's CSRF token. Signed-out users go to `/?signin&oauth_return=…`, and the dashboard returns them to the consent page after sign-in or sign-up. Only `oauth/authorize?…` is accepted as a return target.

## Security notes

- A client's name is self-declared. The consent page says so and shows the redirect host.
- Signed file links are bearer capabilities tied to a connection. They stop working when they expire, when the connection is revoked, or when the scope is removed. An upload link can be used more than once within its 15 minutes, and each use adds a version (an identical master upload is a no-op).
- `read_resume` reads `word/document.xml` with the standard library and refuses any DTD. PDFs are not parsed.
- The tenant isolation suite covers OAuth tokens on every route and every MCP tool.

## Next steps

- **Structured resumes and templates.** Resumes are still files. Before agents can safely edit them (not only upload new files), the service needs a structured resume model (sections, bullets, versions) and server-side templates for rendering. The intended tools are:
  - `get_resume_content` / `update_resume`: targeted edits with `expected_version`
  - `create_variant(base_resume_id, job_id)`
  - `list_templates`
  - `render_resume(id, template_id, format)`
  - `list_versions` / `restore_version`

  The Resume Agent's DOCX outline (`resume_agent/documents.py`) is a starting point; `read_resume` already uses the same `p<index>` paragraph-ID style.
- **Client ID Metadata Documents.** Newer MCP clients can identify themselves with an HTTPS URL instead of registering. Supporting that means fetching the document with the same SSRF protections as posting extraction.
- **Managed tokens with the new scopes.** For self-hosted agents that can't do OAuth.
- **Cleanup.** Clients that never complete an authorization are kept; add pruning if registrations grow.
