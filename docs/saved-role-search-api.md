# Saved role search API (draft proposal)

## Purpose and scope

Let a trusted user or agent check existing roles before deciding whether to ingest
a URL. This PR is independent of ingestion PR #6 and based on merged main. It
adds no fetches, mutations, automatic upload suppression, infrastructure, or
dependencies. A duplicate classification is evidence for human/agent review, not
a database uniqueness guarantee. No production credential or live configuration
is created or changed by this PR.

## Contract

`GET /nextmove/api/v1/jobs/search?company=Acme&title=Backend%20Engineer&limit=20`

The mount prefix follows `APP_ROOT_PATH`; omit `/nextmove` for a root deployment.
Supply any nonempty combination of `company`, `title`, and `url`. Supplied fields
are candidate signals, not an AND filter: a URL match can win despite conflicting
metadata, and company-only/title-only candidates remain discoverable.

Company/title accept at most 500 characters each, URL 4096. Unknown, repeated,
empty, punctuation-only text, malformed URL, and invalid pagination parameters
return 400. `limit` defaults to 20 (1–100); `offset` defaults to 0 (0–1000).
No request body is required or consumed.

Example response (abbreviated to one candidate):

```json
{
  "matches": [{
    "id": "role-uuid", "title": "Backend Engineer", "company": "Acme",
    "url": "https://careers.example/jobs/123", "stage": "interesting",
    "outcome": "active", "score": 90, "classification": "duplicate",
    "reasons": ["company_exact", "title_exact", "company_and_specific_title_exact"],
    "evidence": {"company_similarity": 1.0, "title_similarity": 1.0}
  }],
  "total": 1, "limit": 20, "offset": 0, "next_offset": null,
  "pagination_capped": false
}
```

Results sort by descending score, then role ID. `total` counts all qualifying
candidates, including those outside the page. Follow `next_offset` while non-null.
`pagination_capped: true` means additional candidates exceed the offset budget;
refine the supplied signals. Offset pages are not snapshots: concurrent edits can
shift results. No private notes, descriptions, contacts, resumes, activity, dates,
or general job serialization are loaded or returned. The existing source URL is
returned verbatim (unless over 4096 characters) and may itself contain sensitive
query data; callers should treat all results as private. Responses use the existing `Cache-Control: no-store`.

## Authentication and operations

`ROLE_SEARCH_TOKEN_SHA256` is an optional lowercase SHA-256 hex verifier of a
high-entropy bearer token (at least 32 characters; recommend 32 random bytes).
Scoped bearer access fails closed when unset or malformed. SHA-256 is appropriate
here for random machine secrets, not human passwords. Only the verifier is stored server-side.
Provision a distinct secret through the existing private environment/secret
management process; rotate by replacing the verifier and restarting workers.
There is no token creation endpoint. This PR does not enable any production token.

To compute a verifier locally without putting the token in shell history:

```sh
python3 -c 'import getpass,hashlib; print(hashlib.sha256(getpass.getpass("Random search token: ").encode()).hexdigest())'
```

Call over HTTPS with `Authorization: Bearer <search-token>`, never a query token.
For example, this interactive client keeps the token out of command arguments:

```python
import getpass, json, urllib.parse, urllib.request

query = urllib.parse.urlencode({
    "company": "Acme", "title": "Backend Engineer",
    "url": "https://careers.example/jobs/123?utm_source=email",
})
request = urllib.request.Request(
    "https://YOUR_HOST/nextmove/api/v1/jobs/search?" + query,
    headers={"Authorization": "Bearer " + getpass.getpass("Search token: ")},
)
with urllib.request.urlopen(request, timeout=15) as response:
    print(json.load(response))
```

The scoped secret authorizes only this GET/HEAD endpoint. It cannot list full
jobs, access a job detail, download a resume, or write anything. Browser sessions
and the existing explicitly configured broad agent token retain their existing
behavior; browser mutations still need CSRF. In the integrated PR #6 draft, named managed tokens explicitly granted `roles:search` also authenticate this endpoint. Create, test, expire and revoke them in Agent access; ingestion-only tokens remain rejected. The optional legacy environment search verifier remains supported independently. See [agent setup](AGENT_SETUP.md).

A persistent workspace-wide budget of 120 search requests/minute applies before
authentication, including invalid credentials and HEAD requests, across workers
and restarts. It uses a separate table in the existing private throttle SQLite
file, without consuming browser login attempts or altering the personal-data
schema. Returns 429 with `Retry-After: 60`; throttle failures return 503. A global
budget deliberately favors simplicity over per-user fairness; an attacker can
temporarily exhaust it, so retain existing reverse-proxy request/header limits.

Each search projects only public candidate fields for at most 5001 rows and
scores at most 5000 roles in memory. Larger workspaces return explicit 503
instead of silently incomplete results. At that point add indexed normalized
fields/candidate retrieval before raising the cap. This bounded linear scan suits
the current single-user SQLite workspace. Existing source URLs over 4096 characters
are projected as empty strings with `stored_url_exceeds_limit` in the reasons,
bounding response memory even for legacy oversized values. They cannot supply URL
matching evidence, but company/title search still works. Search makes no changes
to roles, activities, contacts, resumes, or extraction tasks; only abuse-budget bookkeeping
writes to the separate throttle store.

## Normalization, scoring, and tradeoffs

Text uses Unicode NFKC, case folding, punctuation/spacing tokenization, and token
set Jaccard similarity (intersection divided by union). C++ and C# remain distinct.
Token order and repetition do not matter. No stemming, brand aliases, translation,
or corporate-suffix removal is attempted. For example, `Acme Corporation` overlaps
`Acme` at 0.5 and is a possible match. Names like `Acme Labs Corporation` versus
`Acme` can fall below the company-only retrieval threshold; normalized aliases are
a future improvement. Accents remain significant.

- A normalized posting URL match scores 100 and is a duplicate even when supplied
  company/title differ.
- Matching host/path with differing nontracking query values scores at least 65,
  remains a possible URL match, and returns `same_base_url_query_ambiguous`.
  Independent company/title evidence can still classify the role as a duplicate.
- Text candidates qualify with company similarity >= 0.5 OR title >= 0.4.
  Their score is rounded `45 * company_similarity + 40 * title_similarity`.
- Exact company and exact specific title score at least 90 and are duplicates
  across different source URLs, including conflicting query IDs; in that case
  `same_base_url_query_ambiguous` still exposes the conflicting URL evidence.
- Generic titles such as `Software Engineer` stay possible matches. A title is
  specific when it contains a token outside the documented generic vocabulary in
  `jobtracker/search.py` and recognized seniority/level markers. This heuristic
  deliberately requires exact text for non-URL duplicates; fuzzy matches remain
  possible matches. A shared generic title or company alone is never sufficient.
- For company >= 0.8 and title >= 0.4, differing seniority/level token sets add
  `seniority_differs`. Recognized levels include junior/jr, senior/sr, staff,
  principal, lead, and levels I–IV/1–4. Missing versus present seniority differs.
  Exact titles with only generic/seniority words add `generic_title_requires_review`.

Scores are deterministic ranking weights, not calibrated probabilities. Exact
specific titles can still represent multiple headcount openings at one company.
Fuzzy company/title thresholds favor recall and will return false positives. Review
the evidence and source; do not suppress an upload automatically based on a score.

### Meaning of “same URL”

Posting identity retains meaningful query parameters.
Normalize host + case-sensitive path + meaningful query, ignoring scheme, fragment,
and trailing slashes. Hostname casing, leading `www.`, and default ports normalize;
nondefault ports remain distinct. A shared domain alone never makes unrelated paths
duplicates. HTTP and HTTPS variants compare equal; this can conflate sites that
intentionally serve different scheme/www content.

Only clearly known tracking keys (`utm_*`, `gclid`, `fbclid`, `msclkid`) are ignored
case-insensitively. All other query keys/values remain potential posting identity,
including `gh_jid`, `jobId`, `requisitionId`, `jid`, `id`, `ref`, and `source`. Keys
and values retain case. Parameters sort by key/value, with duplicate pairs and blank
values retained. Query decoding follows standard form encoding (`+` means a space).
Equal retained queries at the same host/path give a duplicate; different or missing
queries at that base give an explained possible URL match. Thus `?jobId=123` versus
`?jobId=456` is never automatically a URL duplicate. Exact company/specific-title
evidence may independently make it a duplicate, with both reasons exposed.

Unknown query keys can be locale/filter parameters, so conservative handling may
create extra review. Fragment-based SPA job IDs can collide because fragments are
ignored. The API does not follow redirects, fetch URLs, or resolve short links.
Path casing, percent encodings, and aliases remain distinct; these may cause false
negatives. Query parameter ordering that is meaningful to a provider is not
represented; provider-specific identity rules are future work.

## Validation and future ingestion integration

Disposable database tests cover URL/scheme/www/tracking variants, differing paths,
query IDs, partial signals, generic titles/seniority, malformed input, auth scope,
privacy, pagination, rate/capacity limits, prefix mounting, and read-only behavior.
Validation: 147 Python tests (97 existing + 50 search cases), the frontend build,
and all 16 existing Chromium tests passed. Test databases/uploads are disposable;
no hosted data was used.

A later ingestion client can call this endpoint before submission, present ranked
evidence for review, then explicitly decide whether to upload. Keep credentials
separate. Search and upload are not atomic; concurrent uploads may still race.
Enforced deduplication would require a separately reviewed write-side design.
