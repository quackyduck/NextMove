# Proposal: secure URL ingestion API

**Draft for review. Not merged, deployed, or enabled. No production token has been generated.**

## Recommendation

Use a dedicated, random **256-bit bearer token** for a small personal integration. Send the posting URL in a JSON body over HTTPS, with the token in the `Authorization` header. Keep this credential separate from your browser password and the existing broad agent token.

This is simpler than OAuth for one trusted caller. The draft implements that option; per-device credentials, expiry, and an in-app token-management screen can follow if you connect multiple clients. HTTPS and header-based credentials follow [OWASP REST security guidance](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html) and the [bearer-token header convention](https://www.rfc-editor.org/rfc/rfc6750.html#section-2.1). This is an opaque API key, not an OAuth authorization server.

## What one request does

1. Authenticate, validate the URL/key, and record an ingestion request.
2. Return **202 Accepted** immediately, before DNS or page fetching.
3. Extract company, title, URL, salary, description, and qualifications in the background.
4. Save one **Interesting** role, with qualifications in notes, then persist success and its `job_id` in the same database transaction.

Unlike mobile Quick capture, this endpoint **automatically saves** after successful extraction. It does not apply, mark outreach, or contact anyone. Missing fields stay blank, and extraction warnings are returned. Extraction failure creates no role. HTML extraction remains best effort: a readable page is not proof that the URL represents a genuine job. Review saved roles in the app.

## API contract

Proposed production endpoint: `https://nicolasmelo.com/nextmove/api/v1/ingestions`

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

The token permits only submission and reading ingestion status. It cannot list your pipeline, read existing notes/resumes, alter roles, or change your password. Responses expose ingestion IDs, saved-role IDs, warnings and errors rather than general workspace records. Browser sessions and the broad agent token do not authenticate these new routes. A rotated token can read the workspace's earlier ingestion receipts if it knows their IDs.

## Retry and duplicate behavior

Use a fresh UUID for each intended submission. **Retry an uncertain request with the same key and exact trimmed URL.** Concurrent retries share the same task; completed retries return HTTP 200 with the original result. Reusing the key for a different URL returns 409. Keys accept 16–128 letters, digits, hyphens, or underscores.

Receipts and keys persist across restarts and are not pruned with one-day previews. Different keys may intentionally create separate roles for the same URL; this is not semantic URL deduplication. Failed receipts remain failed. Submit a new key only after checking the failure and deciding to retry. The role, activity entry, and successful receipt commit together, so a lost response after saving does not require creating another role.

## Keeping the token secure

The server stores only `INGEST_API_TOKEN_SHA256`, the SHA-256 digest of a randomly generated token, in its existing private configuration. Unconfigured or malformed verifiers disable access. This hash scheme is appropriate for high-entropy random tokens; never substitute a memorable password. No raw token is stored in SQLite or source control.

Keep the raw token in an OS secret manager, or a mode-0600 file on a trusted device. Do not put it in links, query strings, browser JavaScript, shared shortcut exports, screenshots, Git, or command-line arguments. A personal phone shortcut can send the JSON/header directly; treat its stored secret as a credential. For a distributed app or browser extension, use a trusted backend with the secret instead.

The included standard-library client reads a private file or `NEXTMOVE_INGEST_TOKEN`, refuses redirects, and never prints the token:

```sh
python3 scripts/ingest_url.py 'https://company.example/careers/role' \
  --token-file ~/.config/nextmove/ingest-token \
  --idempotency-key 914b9df9-4236-45ab-a8e1-b5606922e638 --wait
```

It prints the retry key before sending. Without `--wait`, it returns the initial receipt. The base URL defaults to the production path and can be overridden for testing; HTTP is allowed only on loopback. Keep normal TLS certificate verification enabled.

After approval, provision locally without printing the secret:

```python
from pathlib import Path
import hashlib, os, secrets
folder = Path.home() / '.config' / 'nextmove'
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
token = secrets.token_urlsafe(32)
with (folder / 'ingest-token').open('x') as stream:
    os.fchmod(stream.fileno(), 0o600)
    stream.write(token + '\n')
print('INGEST_API_TOKEN_SHA256=' + hashlib.sha256(token.encode()).hexdigest())
```

Only the verifier is printed. An existing token file is never overwritten. Provision the verifier in the server's private environment file, keep `AGENT_API_TOKEN` disabled, and restart the service. Rotation replaces the verifier and updates the caller's secret; clearing it disables both ingestion routes. Accepted background work may finish after rotation. No credential generation/configuration change is part of this draft.

## Limits and rollout

The implementation shares the existing two-extraction capacity limit and adds a SQLite-backed limit of **30 new ingestion requests per rolling hour**, across the workspace and token rotations. Replays do not consume another submission. Existing public-address checks, IP pinning, redirect validation, TLS verification, page-size and network-time limits remain in effect; the token is never forwarded to posting sites.

The lightweight background runner still does not resume interrupted tasks after a process restart. Status checks or new submissions mark stale requests failed after 60 seconds; late workers cannot save expired tasks. Use a dedicated durable queue if automatic recovery becomes necessary. Receipt storage grows over time; future retention must preserve idempotency tombstones.

Schema **5** adds `ingestion_requests`, preserving existing data and preview tasks. Deployment would require the normal stopped-writer checkpoint, migration, and rollback plan. An older binary cannot use the upgraded database. No additional service, paid dependency, or new public port is proposed.

Before merging, review the defaults: automatic saving, one limited token, 30 new requests/hour, and permanent retry receipts. Then choose the first caller (for example, a personal phone shortcut or trusted script) and provision its credential privately. Until approval, production continues running the existing release with this API unavailable.
