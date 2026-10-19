# Next Move — DigitalOcean deployment plan

Prepared October 19, 2026. This is an execution checklist for a future deployment; creating this document does not provision infrastructure or publish the app.

Demo scope update, October 19: the user selected `/nextmove/` on the existing
host, an empty hosted workspace, a homepage link, and persistent login throttling.
They explicitly deferred backups/recovery testing. [Production preparation](PRODUCTION_PREPARATION.md)
records this superseding scope and the uninstalled nginx/systemd configuration.
Docker/Caddy are not required for this demo. Statements below about absent
manifests or process-local throttling describe the original baseline. Production
credentials, Linux capacity, and public acceptance remain pending.

## Target and release scope

Deploy the existing manual tracker first: password login, job intake, table/board, stages, contacts, activity, next actions, and private resume versions. Manual posting descriptions remain sufficient. Add Celery/Redis extraction in a subsequent independently tested release; it is not a prerequisite for the manual tracker.

Use a private server preview before enabling public HTTPS. Keep the current local demo available until the hosted copy passes acceptance checks. Never use test fixtures as personal data or silently move local records into production.

Current evidence: 32 Python tests, four Chromium browser tests, frontend build, fresh Python lock installation, and disposable backup/restore checks passed locally. Linux CI is configured but has not yet run remotely. Docker, TLS, DigitalOcean provisioning, and off-host recovery have not been verified. Initial database schema is version 1; there is no upgrade/downgrade migration system yet.

## Intended architecture

```text
Browser / authorized agent
          |
       HTTPS :443
          |
        Caddy              Public ingress; TLS; request/rate limits
          |
     private network       Exactly one trusted proxy
          |
       Gunicorn            Flask API + built React assets
          |
     local durable disk
     /data/jobtracker.sqlite3
     /data/resumes/

Later: Flask -> private Redis -> Celery worker -> same service layer/storage
Backups: coordinated database + files -> encrypted off-host destination
```

One Docker-enabled DigitalOcean Droplet, one application host, SQLite on durable local storage, and Caddy as the reverse proxy. Start with one Gunicorn worker and a small bounded thread count; measure before increasing concurrency. Build frontend assets before shipping the image. Avoid a Node/Vite development server in production.

Do not use ephemeral storage for SQLite or resume files. Do not share a WAL database across hosts or network filesystems. A future multi-host deployment should first migrate to PostgreSQL and shared object storage. SQLite's WAL constraints are documented in its [official WAL guidance](https://www.sqlite.org/wal.html).

## Decisions to record before spending or provisioning

- [ ] Confirm DigitalOcean account/project and an approved monthly budget, including compute, backup storage, and any registry costs. Check current prices in the account rather than relying on a fixed estimate in this plan.
- [ ] Choose a region and supported Linux image. Candidate starting capacity: 1–2 vCPU and 2 GiB RAM, subject to load checks and approved budget; this is a sizing proposal, not a quoted service plan.
- [ ] Choose an existing domain/subdomain, DNS administrator, and SSH administrator key. A private SSH preview can proceed without a domain.
- [ ] Choose a container registry or an authenticated image-transfer method and confirm whether any remote Git repository should be used. Local commits are not automatically pushed anywhere.
- [ ] Choose an off-host encrypted backup destination and who holds the recovery key.
- [ ] Decide whether to migrate current local records or begin with an empty hosted workspace. Record the source checkpoint and expected counts for a migration.
- [ ] Confirm the first release is manual intake; keep URL extraction visibly marked as pending.

Do not place account credentials, private SSH keys, production passwords, resume contents, or bearer tokens in this document, Git, image layers, CI artifacts, or command output.

## Phase 1 — Prepare a reproducible release

1. [ ] Select a clean Git commit and record its full SHA in the release record. Run the existing Linux CI workflow against that commit when a remote is configured.
2. [ ] Add a multi-stage Dockerfile: locked Node build of `frontend/dist`, then a supported Python runtime installing `requirements.lock.txt`. Copy only runtime code and built assets; use a non-root runtime user with an explicit writable data directory.
3. [ ] Add `compose.yaml` with an app service and Caddy, health checks, restart policies, persistent app/Caddy volumes, and pinned image versions/digests. Publish only Caddy's ports in the public configuration. Do not mount the source tree or bake `.env`/`instance` into images.
4. [ ] Provide a private-preview configuration publishing the app only on the Droplet's loopback interface, with SSH tunneling for access. Keep it distinct from the public Caddy configuration.
5. [ ] Run Gunicorn against `jobtracker:create_app()` with no debug/reload. Document worker/thread/timeouts and graceful stop behavior. Keep uploads bounded to the app's 10 MiB request limit, including multipart overhead; explain the effective file limit in the UI.
6. [ ] Make health probes use `/health`. This proves process/database connectivity only; authenticated workflow and resume-integrity checks are separate gates.
7. [ ] Test a fresh image, clean storage bootstrap, and container recreation on Linux. Confirm the image architecture matches the Droplet; local Apple Silicon builds alone do not establish Linux/x86 compatibility.

**Exit:** immutable image, tested configuration, and a release record tied to a Git commit. These manifests do not exist yet; implement and test them before using deployment commands.

## Phase 2 — Production configuration and access

Use a separate root-owned production configuration outside the checkout, for example `/etc/next-move/production.env`, readable only by the service administrator. Do not reuse the local `.env`.

| Setting | Production requirement |
| --- | --- |
| `APP_ENV` | `production` |
| `SECRET_KEY` | Fresh random secret, at least 32 characters; rotation invalidates existing sessions |
| `APP_PASSWORD_HASH` | Werkzeug hash of a new strong workspace password; collect plaintext privately, never as a shell argument/history entry |
| `APP_PASSWORD` | Unset; local demo password must not be deployed |
| `DATABASE_URL` | `sqlite:////data/jobtracker.sqlite3` inside the container |
| `UPLOAD_FOLDER` | `/data/resumes`, on the same persistent application volume |
| `TRUST_PROXY` | `1` only for one trusted Caddy proxy with direct app access blocked; otherwise leave disabled |
| `AGENT_API_TOKEN` | Leave unset for the initial public release until scoped/revocable credentials are implemented and tested |

1. [ ] Verify startup fails without required production secrets; cookies are Secure, HttpOnly, and SameSite, and login/logout/CSRF work through the real proxy.
2. [ ] Add persistent/shared login throttling before public exposure. The current in-process limiter resets on restart and is insufficient as the only internet-facing defense.
3. [ ] Enforce the intended hostname at ingress. Preserve Host and the external HTTPS scheme; reject direct access and forged forwarding headers. The current opt-in proxy handling trusts only one forwarded scheme, not forwarded host/client IP.
4. [ ] Test upload type/size rejection, private authenticated downloads, unauthorized API requests, safe error responses, and no-store handling.
5. [ ] If agent access is needed earlier, restrict it to the private preview; otherwise implement token scopes, expiration/revocation, and audit attribution before enabling it publicly.
6. [ ] Keep HTML job descriptions as inert text. No outbound fetching is needed in this release.

Caddy documents its upstream and forwarded-header behavior in the [reverse proxy reference](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy). Verify the exact pinned version/configuration rather than assuming proxy defaults.

**Exit:** public-access gates pass or the deployment stays private behind SSH access.

## Phase 3 — Provision and run a private preview

1. [ ] After the deployment decision, create the selected Droplet with SSH public-key access and record its region, identifier, image, and approved size. Follow DigitalOcean's [production setup guidance](https://docs.digitalocean.com/products/droplets/getting-started/recommended-droplet-setup/).
2. [ ] Create a non-root administrator, apply OS updates, configure Docker/Compose, and validate administrator access before changing SSH settings.
3. [ ] Configure a DigitalOcean Cloud Firewall: SSH only from approved administrator addresses; allow HTTP/HTTPS only when ready for public ingress. Never expose the application, Redis, or database ports publicly. Verify actual exposure externally, including Docker-published ports.
4. [ ] Create persistent directories/volumes with ownership matching the application UID. The runtime user needs write access for SQLite database, WAL/SHM files, and resumes.
5. [ ] Install the production configuration separately from code/images. Transfer the selected image through the approved authenticated method and verify its digest.
6. [ ] Start the loopback-only preview and access it through an SSH tunnel. If using production Secure cookies without TLS in the preview, test through a TLS endpoint or a production-equivalent browser environment; do not weaken public cookie settings for convenience.
7. [ ] Validate password login, manual intake, stage/history, contact creation, two resume versions, authenticated download, and data persistence after container recreation. Keep fixtures in a separate preview data volume.
8. [ ] Test denial paths: wrong password, missing auth, missing/incorrect CSRF, oversized/invalid uploads, stale revisions, unknown IDs, and inaccessible private app ports.

**Exit:** the production image works on the actual host using disposable records; the personal dataset is still untouched.

## Phase 4 — Backups and optional data migration

Proposed recovery targets for this small single-user app: at most 24 hours of data loss and restoration within four hours. These are targets to verify in a recovery drill, not current guarantees. Run coordinated backups daily and immediately before releases, initially retaining seven daily and four weekly snapshots; adjust to budget and storage needs.

1. [ ] Stop every writer during a snapshot: web service, authorized agent integrations, and any future worker. Display a maintenance response rather than permitting writes.
2. [ ] Use `scripts/backup.py --maintenance backup` against explicit database/uploads paths. It uses SQLite's [online backup API](https://www.sqlite.org/backup.html); do not improvise a live copy of just the `.sqlite3` file.
3. [ ] Encrypt the coordinated snapshot before off-host transfer. Restrict access, keep the decryption key separately, and confirm failed backup/transfer alerts reach the operator. Never log credentials or private records.
4. [ ] Restore into a new directory/volume with `scripts/backup.py --maintenance restore`. Verify checksums, SQLite integrity/foreign keys, record counts, history, and resume download/hash against the source. Do not overwrite an open database or reuse stale WAL/SHM files.
5. [ ] Complete an actual recovery drill on disposable hosted storage, including retrieval and decryption of the off-host backup. Local backup tests alone do not satisfy this gate.
6. [ ] If migrating personal local data, take a fresh coordinated snapshot, transfer it securely, restore into the designated production volume, verify the counts/files privately, and keep the original local dataset intact.
7. [ ] Pick one authoritative workspace after cutover; stop writing to the older copy to avoid divergent SQLite datasets. A later return to local use requires an explicit export/restore decision.

The exact current CLI examples and maintenance requirements are in [OPERATIONS.md](OPERATIONS.md). Resume files and database metadata are separate resources; review orphaned files after crashes before long-term hosting.

**Exit:** hosted data is recoverable and any chosen migration is reconciled.

## Phase 5 — HTTPS cutover and acceptance

1. [ ] Point the selected DNS record to the Droplet. Add IPv6 only if IPv6 ingress/firewall is configured and tested.
2. [ ] Start Caddy for the selected hostname with persistent certificate storage. Allow the ports required by the chosen certificate challenge. Verify certificate issuance/renewal behavior and HTTP-to-HTTPS redirects using [Caddy's HTTPS guidance](https://caddyserver.com/docs/automatic-https).
3. [ ] Enable `TRUST_PROXY=1` only with the final single-proxy topology. Check legitimate HTTPS login succeeds and a forged Origin or forwarded host is rejected.
4. [ ] Run browser acceptance through the public HTTPS URL against designated disposable data: login, create/reload/edit, table/board, mobile layout, contacts, upload/download/version preservation, and logout.
5. [ ] Confirm production data survives a controlled service restart and host reboot. Test failed-service restart and ensure it does not erase or recreate the data volume.
6. [ ] Verify denied direct access, agent access policy, rate limits, upload limits, private file serving, redacted logging, disk alerts, external uptime checks, and backup alerts.
7. [ ] Record acceptance evidence and any limitations, then open the hosted workspace for regular use. Keep the prior release and pre-cutover snapshot available.

**Exit:** password-protected HTTPS tracker with demonstrated recovery and monitoring. Publishing is complete only after these checks, not merely when a page loads.

## Phase 6 — Release and rollback procedure

For each release:

1. [ ] Record Git SHA/image digest, config version (never secret values), schema version, tests, backup timestamp/location, operator, and release time.
2. [ ] Build/test before maintenance. Stop writers, take and verify a coordinated backup, and retain the previous image.
3. [ ] For schema version 1, verify expected schema and start the selected image. Future schema changes require explicit tested migrations before rollout; `create_all` is not an upgrade strategy.
4. [ ] Run authenticated smoke checks immediately after startup. If a critical check fails, keep maintenance enabled and follow the appropriate rollback path.
5. [ ] For schema-compatible releases, roll back to the previous image and retest. If data/schema changed incompatibly, restore the pre-release snapshot into new storage and point the prior image at it. State the loss of any post-snapshot writes before using snapshot rollback.
6. [ ] Re-enable writes only after data/file integrity and critical workflows pass. Record rollback evidence if used.

## Later release — Celery extraction

- [ ] Add private Redis and one Celery worker using the established service layer. Keep manual intake independent of queue availability.
- [ ] Persist task status, source/proposed fields, bounded retries, and errors. Review proposals before changing user-maintained fields.
- [ ] Restrict HTTP/S fetching, destinations/DNS/redirects, timeouts, response size and content type; test metadata/private-IP access and DNS-rebinding defenses.
- [ ] Test real broker/worker success, failure, restart, retry/idempotency, and concurrent manual edits on Linux. Queue-eager tests do not establish production integration.
- [ ] Include worker shutdown in backup/migration maintenance; monitor queue failures and protect logs.
- [ ] Reassess memory, disk, worker concurrency, backup retention and approved hosting cost using observed load.

## Final go/no-go checklist

- [ ] Infrastructure/domain/spend and data-migration choices recorded.
- [ ] Linux production image and pinned configuration tested.
- [ ] Required secrets configured; local credentials absent.
- [ ] HTTPS, ingress isolation, sessions, CSRF and persistent throttling verified.
- [ ] Agent access disabled or scoped/revocable access verified.
- [ ] Personal data and resumes persist across restart and recreation.
- [ ] Off-host encrypted backup and actual restore verified.
- [ ] Authenticated browser acceptance and negative cases passed.
- [ ] Monitoring/alerts operational; previous release and rollback snapshot available.
- [ ] Release record written and deployment decision obtained.
