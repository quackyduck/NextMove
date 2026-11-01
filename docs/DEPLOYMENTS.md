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

Releases are unpacked into `/opt/next-move/releases/<sha>`, with `/opt/next-move/current` as a symlink, and use `deploy/next-move.service`, `deploy/next-move-worker.service` and the `deploy/nginx-nextmove.conf` snippet. `PUBLIC_BASE_URL` is `https://nicolasmelo.com/nextmove`. Because the app is mounted under `/nextmove`, nginx maps the path-inserted `/.well-known/…/nextmove` URLs. The memory margin is small, so don't load-test on this host.
