# NextMove demo deployment preparation

> **Test server.** This document describes the nicolasmelo.com/nextmove install, which is now the test server. Production is https://mynextmoov.com; see [Deployments](DEPLOYMENTS.md).

Prepared October 19, 2026 on `deploy/production-preparation`, based on
`fea2185caa8f2b0797240ebf00e178fad906fe3c`. Production credentials were created with explicit approval. A private loopback
preview was installed and then stopped when the user required main-only deployment;
no public nginx route or homepage link has been installed. No applicable `AGENTS.md`, `.agents`, or
`.codex` instructions were present in this checkout or its ancestors.

## Accepted scope and pending approval

The user selected **https://nicolasmelo.com/nextmove/**, requested a homepage
link, an **empty hosted database**, and persistent login throttling. They
explicitly deferred backup infrastructure and recovery testing for this demo.
These choices supersede the earlier production-plan backup/recovery requirements.
No local data is to be migrated. The existing local workspace stays intact.

Production password/hash and session-secret creation were explicitly approved
and completed privately. Retrieval is through the ignored mode-0600 local
`instance/deployment/nextmove-login.json`; never print or commit its contents.
Do not reuse the local demo password or `.env`. Agent access is disabled; Celery/Redis remain out of scope.
Do not install software, resize, open ports, or expand administrator access
without resolving any required approval. No new DNS or certificate is needed:
this path uses the existing nicolasmelo.com HTTPS virtual host and certificate.

The separate gainesville repository has a local homepage-link change; preserve
its existing links and all live homepage changes when applying the small patch.
The user now explicitly requires **all deployments from latest origin/main**.
Both changes are pushed in unmerged draft PRs. Do not deploy either branch.
After review/merge, fetch and verify each repository’s latest `origin/main`,
record its exact SHA, and package only that commit; never use a stale local main
or a branch artifact. Preserve live homepage changes when publishing its link.
Do not run the broad rsync --delete script for a single-link update.

## Current host and capacity

Read-only SSH inspection with strict host verification succeeded for
`root@68.183.132.238`:

- 458 MiB RAM total, 153 MiB available, no swap, 6.1 GiB disk free.
- nginx/systemd/Python 3.12.3 present; Docker/Caddy absent.
- BuyItAgain uses about 69 MiB cgroup memory; homepage service about 67 MiB.
- Existing nginx configuration validates. `/` and `/air-quality/` return 200;
  `/buyitagain/` redirects to its working login (200).

Use a Linux-native virtualenv, one preloaded Gunicorn worker/one thread, and
existing nginx. Build frontend assets locally; never build Node or add
Docker/Caddy on this small host for the demo. No Redis or extra proxy is needed.

This is the lowest-overhead architecture, but the 153 MiB margin has not been
shown sufficient on Linux. Startup, password hashing (tens of MiB transiently),
SQLite, and uploads could consume most of it. The unit's 192 MiB ceiling does
not reserve RAM or guarantee safety; do not infer feasibility from that limit.
Measure peak cgroup/PSS memory with disposable storage on an equivalent isolated
Linux host first, or use a tightly monitored private preview only after the
server-change approval. Avoid load/stress tests on the shared host. If it cannot
retain a safe margin, request a resize rather than risk the existing services.
A 1 GiB host is a more practical minimum; 2 GiB allows upgrade/recovery headroom.
No resize or global software installation was performed. Native application
dependencies were installed inside the isolated release virtualenv. The private
preview used about 54 MiB peak cgroup memory at idle, with no restarts, under an
additional runtime override of MemoryHigh=96M/MemoryMax=128M. It was stopped before
public login or load validation; this does not establish peak login capacity.

## Prepared files and prefix behavior

- `deploy/gunicorn.conf.py`: one preloaded worker/one thread, loopback default,
  30-second timeouts, no debug/reload or access logs. Gunicorn forwarded-scheme
  handling is disabled; the app's explicitly enabled ProxyFix is the only trust
  point for one proxy. Each forked worker replaces its inherited SQLAlchemy pool.
  Private record/download URLs stay out of access logs.
- `deploy/next-move.service`: native systemd template using a transient non-login
  DynamicUser identity, protected filesystem, private temp, resource bounds and
  failure restart. It requires a root-owned immutable Linux release under
  `/opt/next-move/current` containing `.venv`, built assets, and an existing
  `instance/` directory. `/var/lib/next-move` is persistent/writable, managed by
  StateDirectory. No new administrator account or SSH key is needed.
- `deploy/production.env.example`: nonsecret template for a separate root-owned
  mode-0600 `/etc/next-move/production.env`. Replace credential placeholders only
  after approval. Never source this file, print secrets, put hashes in shell
  arguments, or copy the local environment/data. Agent access is disabled by the
  unit even if a token accidentally appears in its environment file.
- `deploy/nginx-nextmove.conf`: include only in the current nicolasmelo.com HTTPS
  server. Redirect `/nextmove` to `/nextmove/` with 308, preserving query strings;
  proxy only `/nextmove/` to loopback port 5001, preserving the prefix. It does
  not alter `/api/`, `/air-quality/`, `/buyitagain/`, or `/`. Check port 5001 is
  still unused, save original nginx config, validate before graceful reload.

`APP_ROOT_PATH=/nextmove` mounts Flask through PrefixMount, which rejects requests
outside this path and never trusts a client-supplied prefix header. Build with
`NEXT_MOVE_BASE_PATH=/nextmove/` so JS/CSS, API calls, and resume-download links use
the same prefix. Both variables default to root for local development. Flask's
`next_move_session` cookie is restricted to `/nextmove/`, with Secure, HttpOnly,
and SameSite=Lax production flags; it does not reuse other apps' session names.
`APP_ALLOWED_HOSTS=nicolasmelo.com,127.0.0.1` enforces the hostname. Enable
`TRUST_PROXY=1` only behind the private nginx proxy. Request bodies are capped at
10 MiB including multipart overhead; keep actual resume files at 10 decimal MB
or less. Private uploads remain outside the static build.

## Persistent login defense

`jobtracker/throttle.py` stores only timestamps in a separate SQLite file,
explicitly `/var/lib/next-move/login-throttle.sqlite3` in the service template.
It uses atomic transactions to allow ten attempts per rolling five minutes
across all clients/processes/restarts, before password hashing. Malformed and
successful attempts also consume budget; successful login cannot reset another
worker's budget. Forged IP headers cannot bypass it; it is workspace-wide.
Exhaustion returns 429 with Retry-After; storage failures return 503/fail closed.
Existing authenticated sessions continue working. Clock rollback conservatively
extends the active window. An attacker can exhaust the shared budget and deny
new logins temporarily; this is the single-workspace tradeoff.

No personal-data schema change is needed. Keep the throttle file during code
rollback/restart. User-deferred backups mean hosted demo data currently has no
off-host recovery guarantee. Do not silently create backup infrastructure or
migrate data while deploying this agreed demo.

## Validation

Run both root and prefix browser workflows against disposable databases:

```sh
./scripts/test.sh --ui
(cd frontend && NEXT_MOVE_BASE_PATH=/nextmove/ npm run build -- --outDir /tmp/nextmove-dist)
APP_ROOT_PATH=/nextmove FRONTEND_DIST=/tmp/nextmove-dist .venv/bin/python scripts/test_ui.py
FRONTEND_DIST=/tmp/nextmove-dist .venv/bin/python scripts/test_production.py
```

The final script starts real Gunicorn in production mode with disposable test
configuration, emulates the trusted HTTPS proxy over loopback, and checks prefix,
asset paths, cookie flags, host/origin rejection, authenticated write, restart
persistence, and throttle persistence. It does not prove real TLS or Linux memory.
Python tests also cover atomic concurrent reservations and prefixed private resume
upload/download/logout. Linux systemd/nginx validation and real public acceptance
remain required before calling the demo deployed.

Local evidence: **40 Python tests passed**, both root and `/nextmove/` production
asset builds passed, **four Chromium workflows passed at each path**, and the
real production-mode Gunicorn restart/persistence/throttle smoke passed. The
homepage HTML review confirms all three links: air quality, BuyItAgain, NextMove.
The ordinary root-path frontend build is retained in `frontend/dist`; the tested
prefixed build is separately retained in `/tmp/nextmove-release-preview/frontend-dist`
so the existing local demo is not broken by release assets.

Before transfer, fetch latest `origin/main` after the reviewed PR is merged and
record that exact SHA and artifact checksum. Archive that commit, never branch
HEAD, plus its frontend build, without local
`.env`, data, or macOS `.venv`. Build the Python virtualenv on Linux with the
lockfile. Start the empty app privately, measure capacity, then add only the nginx
include and homepage link when authorized. Check real public login, private APIs,
resume handling, restart/throttle persistence, and every pre-existing endpoint.
Do not leave disposable acceptance records in the user's empty workspace; use a
separate test instance/storage for mutating tests, followed by read-only checks
of the actual empty instance.

## Reversible rollback

Retain the old nginx configuration and homepage index before making their small
edits; this is configuration rollback, not deferred backup infrastructure.
Retain immutable releases. First-install rollback stops/disables only NextMove,
removes only its nginx include, restores only the homepage link patch, validates
and reloads nginx, and retains NextMove's data/configuration for investigation.
Existing services remain running. Never overwrite their databases or broadly
rsync --delete the homepage. No rollback has been performed during preparation.

Configuration references: [nginx upstream headers](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header)
and [SQLAlchemy process pools](https://docs.sqlalchemy.org/en/20/core/pooling.html#using-connection-pools-with-multiprocessing-or-os-fork).

## Paused server state after main-only instruction

The installed private preview points `/opt/next-move/current` to release
`dc361520413a2ab4eadaa17bb12433a19a618743`. Its isolated Linux virtualenv,
root-private `/etc/next-move/production.env`, unit/drop-in, and empty durable
workspace remain on disk. `next-move.service` is **inactive and disabled**;
port 5001 has no listener. No nginx snippet/include or public homepage link was
installed. `/nextmove/` still returns the original homepage fallback, not NextMove.
The existing nginx, BuyItAgain, and homepage services remained active. No data
was migrated, no backup infrastructure was created, and nothing was deleted.

Linux validation before stopping: pip check, all 40 Python tests, and real
production-Gunicorn smoke checks passed. NextMove PR CI passed on both push and
pull-request events; the homepage repository reported no checks. Public login
and final publication remain pending main merges and exact-main revalidation.
