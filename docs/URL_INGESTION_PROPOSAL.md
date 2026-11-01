# Proposal: secure URL ingestion API

**Ready for review. Not merged or deployed. No production token or configuration has been changed.**

## Recommendation

Create a named, expiring **256-bit bearer token** in **Agent access** for each personal integration. Send the posting URL in a JSON body over HTTPS, with the token in the `Authorization` header. Keep this credential separate from your browser password and the existing broad agent token.

The feature includes self-service token creation, setup instructions, connection testing, expiry, last-used metadata, and revocation. Choose **search saved roles** (`roles:search`), **add roles from URLs** (`roles:ingest`), or both. The UI defaults to both; API creation defaults to ingestion only. See the [step-by-step agent setup and reusable skill](AGENT_SETUP.md). HTTPS and header-based credentials follow [OWASP REST security guidance](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html) and the [bearer-token header convention](https://www.rfc-editor.org/rfc/rfc6750.html#section-2.1). This is an opaque API key, not an OAuth authorization server.

## What one request does

1. Authenticate, validate the URL/key, and record an ingestion request.
2. Return **202 Accepted** immediately, before DNS or page fetching.
3. Extract company, title, URL, salary, description, and qualifications in the background.
4. Save one **Needs review** role, with qualifications in notes, then persist success and its `job_id` in the same database transaction.

Unlike mobile Quick capture, this endpoint **automatically saves** after successful extraction. It does not apply, mark outreach, or contact anyone. Missing fields stay blank, and extraction warnings are returned. Extraction failure creates no role. HTML extraction remains best effort: a readable page is not proof that the URL represents a genuine job. Review saved roles in the app.

## API contract

Proposed production endpoint: `https://mynextmoov.com/api/v1/ingestions`

```http
POST /nextmove/api/v1/ingestions
Authorization: Bearer <dedicated-secret-token>
Idempotency-Key: 914b9df9-4236-45ab-a8e1-b5606922e638
Content-Type: application/json

{"url":"https://company.example/careers/role"}
```

The initial response contains `{ "id": "...", "status": "running" }`, a `Location` status path, and `Retry-After: 1`. Poll that path with the same bearer token:

```json
{"id":"...","status":"succeeded","job_id":"...","warnings":[]}
```

A failed task returns `{ "id": "...", "status": "failed", "error": "..." }` with HTTP 200. Authentication fails with 401, insecure production transport with 403, bad input with 400, a reused key/different URL with 409, and exhausted capacity/rate limits with 429. An unknown status ID returns 404.

The ingestion permission permits submission, reading its own ingestion status, and appending resume versions to any saved role (including manual entries). Existing Add roles tokens gain upload support without replacement; this extension does not grant resume reads or edits to role fields. See [resume upload contract](API.md#resume-uploads-with-managed-tokens). Optional search permission additionally exposes minimal matching role summaries. It cannot list your pipeline, read existing notes/resumes, alter role fields, or change your password. Responses expose ingestion IDs, saved-role IDs, warnings and errors rather than general workspace records. Browser sessions and the broad agent token do not authenticate these new routes. Each token can read only its own ingestion receipts; other tokens receive 404. Browser sessions manage tokens but do not authenticate ingestion calls.

## Retry and duplicate behavior

Use a fresh UUID for each intended submission. **Retry an uncertain request with the same key and exact trimmed URL.** Concurrent retries share the same task; completed retries return HTTP 200 with the original result. Reusing the key for a different URL returns 409. Keys accept 16–128 letters, digits, hyphens, or underscores. A retry key used by a different token returns 409 without exposing the receipt. After replacing a lost/revoked token, check the saved role in the app before deciding to submit a new key.

Receipts and keys persist across restarts and are not pruned with one-day previews. Different keys may intentionally create separate roles for the same URL; this is not semantic URL deduplication. Failed receipts remain failed. Submit a new key only after checking the failure and deciding to retry. The role, activity entry, and successful receipt commit together, so a lost response after saving does not require creating another role.

## User onboarding

1. Sign in to NextMove and open **Agent access** in the navigation.
2. Enter a token name (for example, “My job-search agent”), select an expiry (7, 30, 90 days, or one year), and confirm your current workspace password. The default is 90 days. Creation shares the existing sign-in verification budget; a concurrent password change invalidates the creation attempt.
3. Select **Create token**. Save the secret in the agent's secret manager or a private token file. It is displayed only in this creation response/screen and cannot be retrieved later. **Copy token** copies it; **I’ve saved my token**, navigation, sign-out, or reload removes it from the screen. It is not persisted in browser storage. Creation and revocation are restricted to authenticated browser sessions, with CSRF protection on writes; agents cannot manage credentials.
4. **Test connection** checks the newly created token from the browser. A successful result identifies the agent and confirms that no role was created. There is also a password-style field to test an existing token; it clears after the attempt. Invalid tokens do not sign the user out of the browser.
5. Configure the actual agent and run the client-side connection test below. This verifies the credential and URL from the agent’s own environment, which the in-app test cannot prove.

The screen lists each token's name, short prefix, permission, status, creation/expiry dates, and last-used time. **Revoke** requires confirmation and immediately rejects future authenticated requests for that token. Other tokens and saved roles remain intact. Already accepted background work may finish. To rotate, create and configure a replacement, test it, then revoke the old token. Lost secrets require replacement; the app cannot recover them. Changing the workspace password does not revoke agent tokens.

## Agent setup and secret handling

The included standard-library client reads a private token file or `NEXTMOVE_INGEST_TOKEN`, refuses redirects, and never prints the secret. Test it from the agent's machine:

```sh
python3 scripts/ingest_url.py --token-file ~/.config/nextmove/ingest-token \
  --test-connection
```

Use `--search` with the URL and any known `--company`/`--title` first; skip duplicates and ask before possible matches. Then submit a new posting:

```sh
python3 scripts/ingest_url.py 'https://company.example/careers/role' \
  --token-file ~/.config/nextmove/ingest-token \
  --idempotency-key 914b9df9-4236-45ab-a8e1-b5606922e638 --wait
```

Restrict local token files with `chmod 600`. For hosted agents, put the token in the platform's secret settings and inject it as `NEXTMOVE_INGEST_TOKEN`. Give the agent the API instructions and secret reference rather than pasting the secret into a conversation. A process with unrestricted filesystem/environment access can read its own credentials; use a separate tool backend if stronger isolation is needed. Do not put tokens in links, Git, browser storage, logs, or shared shortcut exports.

`GET /api/v1/agent-connection`, authenticated with the token, returns `{ "connected": true, "agent_token": { ...metadata... } }`. It updates last-used time but creates no ingestion or role. The submit client's default API base includes `/nextmove`; setup instructions derive their URL from the actual app location. HTTPS is required outside local development.

The server stores only the token's SHA-256 digest, a short display prefix, permissions, and lifecycle metadata in SQLite. The full secret is returned once, in the no-store creation response, and remains only in the current screen's memory until dismissed. No environment-file edit or server restart is needed to create or revoke tokens. Production management requires HTTPS; browser and CLI connection tests refuse redirects. Invalid credentials avoid database writer locks; successful authentication rechecks revocation atomically. The former draft `INGEST_API_TOKEN_SHA256` setting is no longer used, and broad agent access stays disabled. No managed tokens exist until a signed-in user creates one.

## Limits and rollout

The implementation shares the existing two-extraction capacity limit and adds a SQLite-backed limit of **30 new ingestion requests per rolling hour**, across the workspace and token rotations. Replays do not consume another submission. Existing public-address checks, IP pinning, redirect validation, TLS verification, page-size and network-time limits remain in effect; the token is never forwarded to posting sites.

The lightweight background runner still does not resume interrupted tasks after a process restart. Status checks or new submissions mark stale requests failed after 60 seconds; late workers cannot save expired tasks. Use a dedicated durable queue if automatic recovery becomes necessary. Receipt storage grows over time; future retention must preserve idempotency tombstones.

Schema **5** adds ingestion receipts; schema **6** adds managed tokens and receipt ownership; schema **7** adds the Needs review stage while preserving existing stages and child records. Existing records and previews are preserved. Any pre-onboarding draft receipts remain stored with no token owner and cannot be read by newly created tokens; their saved roles are unaffected. Deployment would require the normal stopped-writer checkpoint, migration, and rollback plan. An older binary cannot use the upgraded database. No additional service, paid dependency, or new public port is proposed.

Before merging, review the defaults: automatic saving, separate named tokens with 90-day expiry, a maximum of 20 active tokens, 30 new requests/hour, and permanent retry receipts. Then choose the first trusted caller. Production continues running the existing release until a separate merge/deployment decision.

## Validation

200 Python tests and 20 Chromium browser tests pass using disposable storage. The four onboarding/review browser tests also pass under `/nextmove/`; root and prefixed builds and the isolated production Gunicorn smoke pass. Tests cover search-to-ingestion, independent token scopes, one-time secrets, revocation, manual defaults, review promotion, and migration with existing contacts, resumes and history. No production records or credentials were used.
