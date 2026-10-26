# Set up your job intake agent

This walkthrough describes draft PR #6. Use it after the PR is reviewed, merged,
and deployed. PR #7 supplies the duplicate search; this draft connects it to
self-service tokens. No MCP server is required: a scoped token and this client
provide the workflow. MCP can be added later for agents that require tool discovery.

## 1. Create access on the site

Sign in, open **Agent access**, name the token for the agent, choose an expiry,
and keep **Search saved roles for duplicates** and **Add roles from URLs for
review** checked. Enter the current workspace password and select **Create token**.
Copy the secret once, save it, then select **Test connection**. This browser test
creates no role. **I've saved my token** hides it; leaving/reloading also discards
it. A lost secret cannot be recovered: revoke it and create a replacement.

Search-only tokens are available by unchecking Add roles. Tokens cannot read
private notes or resumes, edit existing roles, or administer access.

## 2. Store the token on the agent's machine

For a local agent, create a private file interactively; the token is not placed in
shell history or printed. Run this yourself, outside the agent conversation:

```sh
python3 - <<'PY'
from pathlib import Path
import getpass, os
folder = Path.home() / '.config' / 'nextmove'
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
fd = os.open(folder / 'agent-token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as file:
    file.write(getpass.getpass('Paste your NextMove token: ').strip() + '\n')
PY
```

The command refuses to overwrite an existing file. For a hosted agent, use its
secret manager to inject `NEXTMOVE_INGEST_TOKEN` into the client process. Give
the agent only the file path or variable name. Never paste the value into chat,
URLs, source code, screenshots, or command arguments. The client reads it internally
and refuses redirects and non-HTTPS sites (except loopback development).

## 3. Test from the agent's machine

Check out the reviewed version of this repository and run from that folder:

```sh
python3 scripts/ingest_url.py --token-file "$HOME/.config/nextmove/agent-token" \
  --base-url https://nicolasmelo.com/nextmove --test-connection
```

Expect `connected: true`, the token name, and both permissions. A 401 means an
invalid, expired, or revoked token; a 403 can mean HTTPS is required. This verifies
connectivity and authentication without creating a role. For hosted agents omit
`--token-file` to use the injected environment secret. Use the exact deployed base
URL; the client appends `/api/v1` itself.

## 4. Enable the skill

The repository includes `.agents/skills/nextmove-url-intake/SKILL.md`. Open this
checkout as your agent's working project. Codex supports project-local skills in
`.agents/skills`; see [official skill setup](https://learn.chatgpt.com/docs/build-skills).
Start a fresh task if the skill is not discovered. For another agent, register
this SKILL.md using its skill mechanism, keep this checkout available, and configure
its working directory to this repository. The skill depends on the client and
this guide; copying only SKILL.md to another location is insufficient.

Give the agent these nonsecret settings: checkout location, base URL, and token
file path (or environment variable name). Allow running the Python client and
HTTPS access to your site. No database, server login, or workspace password is needed.

Example request:

> Use $nextmove-url-intake to check this posting and add it if it is new:
> https://careers.example/jobs/123. Use https://nicolasmelo.com/nextmove and
> the private token file at ~/.config/nextmove/agent-token. Pause on possible
> duplicates and leave new roles for my review.

## 5. Search, submit, and review

The agent first searches using the URL and any known company/title:

```sh
python3 scripts/ingest_url.py 'https://careers.example/jobs/123?jobId=123' \
  --search --company 'Example' --title 'Backend Engineer' \
  --token-file "$HOME/.config/nextmove/agent-token"
```

The client checks every available result page. Duplicates are skipped. Possible
matches require your decision; a failed/incomplete search must not silently proceed.
Job identifiers and unknown query parameters stay significant; known tracking
parameters do not. If there is no match, submit:

```sh
python3 scripts/ingest_url.py 'https://careers.example/jobs/123?jobId=123' \
  --token-file "$HOME/.config/nextmove/agent-token" --wait
```

Keep the printed Idempotency-Key. If the response is lost, repeat with
`--idempotency-key THE_SAME_KEY`; a fresh key is a new submission. Extraction is
asynchronous; the client waits up to 70 seconds and reports failures/warnings.
Search and submission are separate operations; they do not enforce uniqueness
against concurrent writers.

Agent submissions appear in the pipeline's **Needs review** column. Open the role,
check extracted details/notes and warnings, choose **Interesting** in the stage
selector, then save. Manual entries and Quick capture saves start **Interesting**.
Existing roles retain their current stages; no roles are reclassified during upgrade.

## 6. Revoke or rotate access

In **Agent access**, find the named token and select Revoke, then Confirm revoke.
New searches, connection checks, submissions and receipt reads fail immediately.
Already accepted work may finish; saved roles remain. Remove the local secret or
hosted secret configuration. To rotate, create/test a replacement, configure the
agent with it, then revoke the old token. Old ingestion receipts belong to the old
token; do not replay unresolved submissions using a replacement and a fresh key.
Password changes revoke browser sessions but do not revoke agent tokens.
