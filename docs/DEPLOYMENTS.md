# Deployments

Next Move runs in two places. They have separate databases, accounts and tokens; nothing is shared or synced between them.

| | Production | Test |
| --- | --- | --- |
| URL | **https://mynextmoov.com** | https://nicolasmelo.com/nextmove |
| MCP server | `https://mynextmoov.com/mcp` | `https://nicolasmelo.com/nextmove/mcp` |
| Host | DigitalOcean Droplet, 4 GiB | DigitalOcean Droplet, 458 MiB, shared with BuyItAgain and the homepage |
| Runs as | Docker Compose from the NextMoveAgent repository: Caddy, `jobtracker-web`, `jobtracker-worker`, plus the Resume Agent gateway and harness | systemd (`next-move`, `next-move-worker`) behind nginx, mounted at `/nextmove` |
| Setup docs | NextMoveAgent `docs/Deployment.md` | [Production preparation](PRODUCTION_PREPARATION.md), [Operations](OPERATIONS.md), `deploy/` in this repository |
| Data | `jobtracker-data` volume (`/data` in the container) | `/var/lib/private/next-move` |

Point users, agents and links at **production**. Use the test server to try a release before it goes live.

## Release order

1. Merge to `main`. Deploy only the verified `origin/main` SHA.
2. Optionally, deploy to the test server first and try the change there.
3. Deploy to production.

Every release that changes the schema needs a snapshot first. Both servers migrate the schema when the app starts, and an older release must never be started against an upgraded database.

## Production (mynextmoov.com)

The checkouts live side by side in `/opt/nextmove/NextMove` and `/opt/nextmove/NextMoveAgent`. The `jobtracker` image is built from the NextMove checkout.

1. **Update and build.** Fast-forward `/opt/nextmove/NextMove` to `origin/main`. Tag the running image (`docker tag nextmove/jobtracker:local nextmove/jobtracker:rollback-<old sha>`), then build with `docker compose build jobtracker-web`. The running containers are unaffected until they're recreated.
2. **Rehearse a schema change** by running the new image against a copy of the database: `--network none`, the production env file and a temporary `/data`. Compare row hashes before and after.
3. **Cut over.** Run `docker compose stop jobtracker-worker jobtracker-web`. Copy the volume with `cp -a`, which keeps the app's UID 10002; a plain copy leaves root-owned files the app can't open. Then run `docker compose up -d --no-deps jobtracker-web`, check health, and start `jobtracker-worker`.
4. **Check health** inside the container with `Host: jobtracker-web`. `APP_ALLOWED_HOSTS` is `mynextmoov.com,jobtracker-web`, so a request to `127.0.0.1` gets a 400 even when the app is healthy.
5. **Smoke-test from outside:** `/health`, `/`, both `/.well-known/oauth-*` documents, `POST /mcp` (401 naming the metadata URL), and 404 for `/api/v1/harness/` and `/api/v1/me/`.

To roll back, stop both services, restore the snapshot with `cp -a`, tag the rollback image as `nextmove/jobtracker:local`, and start them again. Each release's record is in `/opt/nextmove/deployment-record-*.json`.

`PUBLIC_BASE_URL` comes from `SITE_ADDRESS` in Compose. The app is served at the root path, so the OAuth discovery URLs need no proxy mapping.

## Test (nicolasmelo.com/nextmove)

Logs go to the `nextmove` journal namespace and are kept about 7 days (`deploy/journald-nextmove.conf`; see [Operations](OPERATIONS.md), "Logs are kept about 7 days"). Read them with `journalctl --namespace=nextmove -u next-move`. Releases are unpacked into `/opt/next-move/releases/<sha>`, with `/opt/next-move/current` as a symlink, and use `deploy/next-move.service`, `deploy/next-move-worker.service` and the `deploy/nginx-nextmove.conf` snippet. `PUBLIC_BASE_URL` is `https://nicolasmelo.com/nextmove`. Because the app is mounted under `/nextmove`, nginx maps the path-inserted `/.well-known/…/nextmove` URLs. The memory margin is small, so don't load-test on this host.

## Schema 15: email verification, the mailer and the privacy page

This release requires email for sign-up and changes the dependency install.

**Before building**
- Dependencies are now installed with `pip install --require-hashes -r requirements.lock.txt` (every line carries hashes; `dnspython` is new). NextMoveAgent's `deploy/jobtracker.Dockerfile` must use `--require-hashes` too. Regenerate the lock with hashes whenever a pin changes.
- Set the privacy page's "Last updated" date: `LAST_UPDATED` in `frontend/src/Privacy.jsx` (it ships as `[date of publishing]`).

**Production environment** (NextMoveAgent): in `deploy/jobtracker.env` (web and worker) set `MAIL_BACKEND=resend`, `MAIL_FROM=My Next Moov <no-reply@mail.mynextmoov.com>`, `MAIL_REPLY_TO=support@mynextmoov.com`, `MAIL_DAILY_LIMIT=90`, `MAIL_LIMIT_SIGNUP=50`, `MAIL_LIMIT_RESET=25`, `MAIL_MONTHLY_LIMIT=3000` and `POW_BITS=18`. On `jobtracker-worker` only, add the `MAIL_API_KEY_FILE` secret and **`PUBLIC_BASE_URL: https://${SITE_ADDRESS}`**, which the worker needs to build links. Production refuses to start without `MAIL_BACKEND`, which the migration rehearsal with the production env file will show.

**Before cut-over:** on a copy of production, `flask user list --unverified --undeliverable`; fix undeliverable addresses with `flask user set-email ID EMAIL --verified`. From the worker container, check `api.resend.com:443` and MX lookups (`python -c "import dns.resolver; print(dns.resolver.resolve('gmail.com','MX')[0])"`).

**After cut-over:** `flask mail test support@mynextmoov.com` (worker); `flask user verify <owner email>`; sign up a throwaway address on a phone and note the proof-of-work time; in the received message's raw source check SPF, DKIM and DMARC and that the link is exactly `https://mynextmoov.com/verify-email#token=…` (no tracking redirect); sign up again with the same address (you get the "already have an account" email); `flask mail status` daily for the first week.

**Test server:** `MAIL_BACKEND=none` and `SIGNUP_ENABLED=0`. After migrating, run `flask user verify EMAIL` for each account listed by `flask user list --unverified`, or they cannot sign in. Create accounts with `flask user create EMAIL`.

### Rolling back schema 15

Older releases refuse to start on a schema-15 database, and there is no down-migration. So a code rollback is always a data rollback to the snapshot taken just before the cut-over.

1. **Decide whether you need a rollback at all.** Most problems have a switch that needs no rollback:
   - mail abuse or bounces: `SIGNUP_REQUIRES_INVITE=1`, then restart web;
   - email broken: `MAIL_BACKEND=none` with `SIGNUP_ENABLED=0`. Legacy unverified users then can't sign in until email returns, or until you run `flask user verify` for them; verified users are unaffected.
2. **Know what you lose.** Everything written after the snapshot: accounts created by confirmed sign-ups, roles, resumes, templates, OAuth connections and tokens issued, and verification state (accounts verified since then return to unverified). Count first, with `SELECT COUNT(*) FROM users WHERE created_at > '<snapshot time>'` and the same for `jobs`, `resumes` and `master_resumes`, so you can tell those users.
3. **Stop both services:** `docker compose stop jobtracker-worker jobtracker-web`. The worker must stop first, because it would otherwise keep sending mail from the queue.
4. **Restore the snapshot** with `cp -a`, which keeps the app's UID 10002. Restore the throttle database too if it was snapshotted, or leave it as it is: it holds only HMAC keys and is harmless either way.
5. **Restore the previous image:** `docker tag nextmove/jobtracker:rollback-<old sha> nextmove/jobtracker:local`.
6. **Remove the schema-15 environment** from `deploy/jobtracker.env` and the worker's secret mount: the `MAIL_*`, `POW_*` and worker `PUBLIC_BASE_URL` lines. Older code ignores them, but leaving the mail key mounted for no reason is avoidable exposure.
7. **Start web, check health, then start the worker.** Smoke-test sign-in for the owner.
8. **Clean up in Resend.** Pending confirmation emails already sent contain links that now lead nowhere; they expire on their own. If the rollback is for a security reason, revoke the Resend API key.
9. **Before trying again,** take a fresh snapshot. Redeploying runs the 14→15 migration again from scratch.

Without a rollback, `SIGNUP_REQUIRES_INVITE=1` makes sign-up invite-only at once.

## Schema 16: self-service password reset

**Before cut-over:** take the snapshot. Older releases refuse a schema-16 database, so a code rollback means restoring that snapshot, exactly as for schema 15.

**Ship it switched off:** set `PASSWORD_RESET_ENABLED=0` in `deploy/jobtracker.env` (web and worker) for the first deploy.

**After cut-over:**
1. Run `flask user reset-link <your email>` in the worker container, open the link, set a password and sign in. Check that the audit line `operator reset link minted user_id=…` is in the logs, and that no line contains the link.
2. Set `PASSWORD_RESET_ENABLED=1` and restart web and worker.
3. Run one real request from the sign-in screen. In the received email's raw source, check that the link is exactly `https://mynextmoov.com/reset-password#token=…`, with no tracking redirect.
4. Check `flask mail status` for the reset share.

**Test server:** `MAIL_BACKEND=none` keeps self-service reset off there; use `flask user reset-link`.

## Schema 17: data deletion and export

Take the dated snapshot first (`snapshots/YYYY-MM-DD-<sha>`) and delete snapshots older than 28 days. **At the cut-over, with web and worker stopped, run a one-time `flask --app jobtracker storage vacuum`** in a one-off container. It vacuums the main and throttle databases at their configured paths. `secure_delete` only overwrites pages freed from now on; the VACUUM rewrites pages left by earlier deletions, such as the schema 15 and 16 cleanups. Older releases refuse schema 17; rolling back means restoring the snapshot **and then replaying the deletion ledger** (OPERATIONS.md, "Backups, snapshots and restores"). Deploy NextMoveAgent's `resume-agent tenant delete` (branch `feat/tenant-delete`) first or alongside. After cut-over:
1. Delete a throwaway role and template in the app.
2. Run `flask user delete --id <test account>` and the printed agent step.
3. Run `flask storage sweep` (a dry run) and check it reports nothing unexpected.
