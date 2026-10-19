# Independent review — first manual demo

Reviewed October 19, 2026. Scope: Flask factory/auth/API, SQLite schema and service transactions, private resume storage/downloads, API example, and React source integration. This report distinguishes local-demo evidence from deployment gates. It is not a production security certification.

## Findings and resolution

1. **HTTPS proxy login origin mismatch (deployment blocker, fixed).** Login compares browser Origin with Flask's request scheme/host. Behind HTTPS termination with an HTTP upstream, the original app rejected legitimate logins. `TRUST_PROXY=1` now explicitly trusts one forwarded scheme, while forwarded host/IP/prefix remain untrusted. Independent regression tests exercise accepted HTTPS origin, rejected forged host, default distrust and Secure cookies. Enable only behind one private trusted reverse proxy; the deployment topology is still untested.
2. **Dependency reproducibility mismatch (pins reconciled).** Initial requirements pinned Flask 3.1.2, SQLAlchemy 2.0.44 and Gunicorn 23.0.0 while successful tests used Flask 3.1.3, SQLAlchemy 2.1.5 and Gunicorn 26.2.0. Release manager reconciled direct requirements and the transitive lock to tested versions. No claim is made here that an old version is vulnerable without advisory evidence; a clean lock-based install remains the relevant gate.
3. **Duplicate form control IDs (fixed).** Opportunity notes and contact notes on detail shared `field-notes`; the contact label can target the wrong textarea. Fields now use React useId, preserving unique label associations; a targeted browser regression verifies independent note fields.
4. **Asynchronous frontend selection race (fixed).** Detail fetches can resolve after navigation and overwrite current selected role. Request sequence guards now discard stale detail responses; navigation controls are blocked during mutations. Failed intake contacts populate the detail retry form, and failed uploads explicitly request reselection.

5. **Cookie configuration derived before app configuration overrides (fixed).** Production cookie security initially depended on the environment even when create_app configuration selected production. The factory now derives the default from the final APP_ENV and preserves an explicitly supplied cookie setting. The HTTPS regression verifies Secure cookies.

## Checked behavior

- All private API routes require authentication; authenticated browser writes require CSRF while bearer writes use explicit configured token.
- Login rejects cross-origin requests; password hash checks, session regeneration and process-local throttling exist. Signed cookies are HttpOnly, SameSite=Lax and Secure in production.
- Services reject unknown fields, invalid enums/dates/URLs, empty job inputs and bad contact data. Found-date default uses America/Los_Angeles; UTC timestamps are serialized explicitly. Date-only UI values avoid UTC conversion.
- Stage/outcome stay independent. Job updates and activity writes commit atomically; failed activity writes roll back. SQLAlchemy version predicates and optional API version values catch stale writes.
- SQLite enables foreign keys, WAL and a five-second busy timeout. Initial schema refuses unknown/unversioned databases rather than pretending create_all is a migration.
- Uploads have bounded request size, allowlisted document extensions/content checks, opaque exclusive-create storage paths, restricted permissions and authenticated attachment downloads. A rejected metadata transaction removes the written file; earlier resume versions remain intact.
- React renders descriptions/notes as text and constrains posting links to HTTP/S; no raw HTML rendering found. JSON field names align with services.
- Source Numbers file is not modified or treated as executable instructions. Import is not implemented.

## Test evidence

- Baseline backend suite: **25 passed** using installed dependencies. Final integrated Python suite including independent regressions and backup tests: **32 passed**.
- Independent `tests/test_review.py` adds proxy boundary, private-route caching/auth, upload rollback and UTC-midnight/Los Angeles date regressions; final integrated result is recorded below.
- Independent `npm run build`: passed with Vite 7.3.7. Service/site reports **4/4 integrated Chromium tests passed**, covering capture/reload/progress/history/contact/upload/download/table/board/mobile/logout, invalid inputs, partial intake retry, label independence and stale detail responses. Tests use disposable data; production proxy behavior is covered only through Flask tests.

## Remaining production gates

- No DigitalOcean deployment, TLS/reverse-proxy smoke test, public ingress isolation or infrastructure provisioning has been performed.
- Throttling is per process and source IP is deliberately not accepted from forwarded headers. Production needs a shared or proxy-enforced limiter and trusted ingress configuration; scoped/revocable agent credentials are deferred.
- Lock-based clean installation, dependency advisory review, production image checks, migration from the previous release and coordinated database/uploads backup/clean restore remain release responsibilities; consult OPERATIONS.md for their actual verification evidence.
- Filesystem writes and SQLite metadata are not one crash-atomic resource. Current exception cleanup handles failed metadata transactions; a process crash can leave an unreferenced private file. A reconciliation/maintenance procedure is needed before long-term hosting.
- Celery/Redis ingestion, secure remote fetching, extraction review, spreadsheet import/export and multi-user access are intentionally outside this manual-demo implementation.

## Demo use notes

Opportunity changes require the explicit Save changes action. Navigating away from unsaved edits currently discards them; no autosave or dirty-form confirmation is implemented. A conflict preserves the form until the user explicitly reloads saved details, allowing reconciliation.

## Review disposition

No unresolved blocking findings for the local manual demo after the above fixes. Deployment remains gated by the production checks above. Backend/API/upload coverage and frontend source/build review are independent; Chromium execution was performed by the frontend owner.
