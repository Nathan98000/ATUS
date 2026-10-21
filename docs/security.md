# Security

Threat model for the Phase 5 production design ([ADR-011](adr/ADR-011-hosting.md)): assets, trust boundaries, the controls as implemented, abuse cases, production exposure decisions and accepted risks.

**Status (2026-10-20): nothing is deployed.** Every control below is implemented in this repository and covered by the tests named with it. The CI workflows that run those tests have not yet run on GitHub ([roadmap](roadmap.md#remaining-for-the-definition-of-done), item 1), so every test result so far is local. None of the controls has been observed on the real providers yet. Owner settings and provider behaviours that the repository cannot test are marked as such (C20, R8). Two claims that had no test before review round 2, C17's "no cookies or Web Storage" and abuse case 4's "no raw HTML", now have one.

Related documents:

- [`docs/api.md`](api.md): error contract and exposure;
- [`docs/adr/ADR-012-load-and-quota-protection.md`](adr/ADR-012-load-and-quota-protection.md): limits and their calibration;
- [`docs/adr/ADR-014-observability.md`](adr/ADR-014-observability.md): log fields and retention;
- [`docs/adr/ADR-015-edge-cache-snapshot-and-governor.md`](adr/ADR-015-edge-cache-snapshot-and-governor.md): edge tiers;
- [`docs/deployment.md`](deployment.md): owner setup, configuration and secret rotation.

## Assets

| Asset | What could go wrong | Where it lives |
|---|---|---|
| Integrity of served statistics | a wrong estimate or standard error is served as a result | the serving DB on Neon; release dumps on GitHub Releases; the static snapshot (`/snap/`); Workers KV; the API's in-process cache |
| Availability and the free quotas | a quota runs out and that tier pauses until it resets. Pausing, never billing, is the only acceptable failure mode (ADR-011, constraint 1) | Cloudflare Worker requests, KV and Durable Object operations (daily); Render instance hours and outbound bandwidth, Neon CU-hours and egress (monthly) |
| Secrets and tokens | the edge is bypassed, foreign code is deployed, or the database is changed | see abuse case 10 |
| Owner accounts | all of the above at once | Cloudflare, Render, Neon and GitHub logins; the owner's machine and `~/.config/atus/` |
| Visitors' privacy | a client IP, or what someone analysed, becomes linkable to a person | requests in transit, logs, the governor's storage |

## Trust boundaries

```text
browser ──TLS──▶ Cloudflare edge ─┬─ static assets: SPA, /snap/   (no Worker code runs)
                                  └─ Worker (/api, /api/*) ── KV (results), Durable Object (governor)
                                        │  X-ATUS-Edge-Auth, X-ATUS-Client-IP
                                        ▼  TLS
              Render's Cloudflare zone ─▶ Render LB ──plain HTTP──▶ API container (one uvicorn process)
                                                                       │  TLS (verify-full), role atus_api
                                                                       ▼
                                                                     Neon (serving DB)

GitHub Actions: image → GHCR, Render deploy hook, wrangler deploy (environment `production`, owner approval)
owner machine:  ~/.config/atus/edge.env and neon-*.env (one per Neon project), the neondb_owner DSN, every provider login
```

| Boundary | What the receiving side trusts | Enforced by |
|---|---|---|
| browser → Worker | nothing the client sends; only Cloudflare's `CF-Connecting-IP` | C1, C3, C4 |
| Worker → API | `X-ATUS-Client-IP` and `X-Request-ID`, and only together with a valid `X-ATUS-Edge-Auth`. All other traffic to the onrender.com hostname is treated as direct-origin traffic and gets 403, except `/health` and, with a valid `ATUS_OPS_TOKEN` bearer, `/ready` and `/metrics` | C2, C3, C14 |
| API → Neon | the API can read the served tables and nothing else | C8, C9; TLS settings from the connection string (R11) |
| GitHub → production | only `.github/workflows/deploy.yml` deploys, on a `v*` tag or a manual run, after the owner approves the `production` environment | C19, C20 |
| owner machine → everything | fully trusted: it holds the owner DSN and every generated secret | C18 keeps those files private; protecting the machine and accounts is the owner's job (R12) |

## Controls

Defaults are the values in the code today. The calibration of timeouts and concurrency belongs to ADR-012: the 2026-10-21 emulation runs kept every default ([ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13); measurements in [`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md)).

**C1. Edge route allowlist.** The Worker serves a fixed set of `/api` routes plus `/api/v1/activities/<code>`, where the code is 1–32 characters from `[A-Za-z0-9_-]`. Anything else under `/api` gets a 404 envelope, and a wrong method gets a 405, with no origin call. Paths are matched exactly, with no case folding and no trailing-slash folding. `/ready`, `/metrics`, `/docs`, `/redoc` and `/openapi.json` are never routed.
Code: `edge/src/routes.ts`. Tests: `edge/test/worker/routing.test.ts`.

**C2. Edge authentication at the API.**
- Every path except `/health` needs one of at most two accepted `X-ATUS-Edge-Auth` values (two, so the secret can be rotated), with one exception: `/ready` and `/metrics` also accept a valid `Authorization: Bearer <ATUS_OPS_TOKEN>` without the edge secret (C14). That makes the ops token a second credential at the origin, and a leaked one can wake Neon through `/ready` ([Leaked tokens](#10-leaked-tokens)). The presented value is compared in constant time against every accepted value.
- A failed check gets 403 `forbidden_origin` before rate limiting, routing or any database work.
- In production the API refuses to start without a secret of at least 32 characters.

Code: `src/atus_pipeline/api/middleware.py` (`edge_secret_matches`, `GuardMiddleware`), `src/atus_pipeline/config.py` (`_edge_secrets`, `check_api_settings`).
Tests:
- `tests/unit/api/test_guard.py::TestEdgeAuth`;
- `tests/integration/api/test_protection_db.py::TestEdgeAuthCostsNothing`;
- `tests/unit/api/test_config.py::TestProductionRefusal`;
- `tests/integration/api_prod/test_server.py::TestProductionServer`;
- `tests/deploy/test_container_image.py::test_production_mode_refuses_to_start_without_its_settings`.

**C3. Client-IP trust chain.**
- The Worker builds a fresh upstream header set:
  - from the client: `Accept`, `If-None-Match`, plus `Content-Type` when there is a body;
  - its own `Accept-Encoding: gzip`, whatever the client accepts (an uncompressed answer would cost several times the Render outbound);
  - its own `X-Request-ID`;
  - `X-ATUS-Client-IP`, copied from `CF-Connecting-IP` when that value looks like an IP address;
  - `X-ATUS-Edge-Auth`;
  - `User-Agent`.

  Client-sent `X-ATUS-*`, `X-Forwarded-*`, `Forwarded`, `X-Real-IP`, `Cookie` and `Authorization` headers never pass.
- The API takes the client IP from `X-ATUS-Client-IP` only when edge auth is valid; otherwise it uses the socket peer. It never reads `X-Forwarded-For`, `Forwarded`, `CF-Connecting-IP` or `True-Client-IP`.
- uvicorn's proxy-header handling is off (`forwarded_allow_ips` is empty).

Code: `edge/src/proxy.ts` (`buildUpstreamHeaders`), `src/atus_pipeline/api/protection.py` (`client_identity`), `src/atus_pipeline/api/serve.py`.
Tests:
- `edge/test/worker/headers.test.ts`;
- `edge/test/worker/integration.test.ts`;
- `tests/unit/api/test_guard.py::TestRequestRateLimit` (forged forwarding headers, a forged client IP with a wrong secret);
- `tests/unit/api/test_protection.py::TestClientIdentity`;
- `tests/integration/api_prod/test_server.py::TestProductionServer::test_forwarded_headers_never_change_the_client`.

**C4. Request size and shape.**
- **Edge.** An analysis POST must be `application/json` (else 415) and at most 64 KiB (else 413). The Worker refuses a declared `Content-Length` over the cap before it reads the body, and cuts a streamed body off at the cap. Malformed framing (a duplicate `Content-Length`, an unknown method) is answered by the runtime before the Worker runs, without the Worker's envelope or headers (R8).
- **API, before routing:**
  - the same body cap (`ATUS_API_MAX_BODY_BYTES`);
  - JSON-only bodies;
  - control characters in the query string get a 422.
- **API schema bounds:** at most 50 years, at most 100 codes per include or exclude list, length-limited labels and presets. A 422 lists at most 20 errors, with truncated strings.
- **uvicorn:** a request head of at most 16 KiB; idle keep-alive connections close after 5 s; `limit_concurrency` sheds excess connections with a 503 before the app runs.

Code: `edge/src/index.ts` (`readJsonBody`), `src/atus_pipeline/api/middleware.py`, `src/atus_pipeline/api/schemas/analysis.py`, `src/atus_pipeline/api/errors.py`, `src/atus_pipeline/api/serve.py`.
Tests:
- `edge/test/worker/limits.test.ts`;
- `tests/unit/api/test_guard.py` (`TestBodySize`, `TestMediaType`, `TestValidationOutput`, `TestRawHeaders`); the schema bounds: `TestValidationOutput::test_schema_bounds_are_422_before_any_work` (51 years, 101 include or exclude codes, a 121-character label, an 81-character `label_a` or `label_b`) and `::test_the_bounds_themselves_pass_the_schema`;
- `tests/integration/api/test_protection_db.py::TestQueryParameters`;
- `tests/integration/api_prod/test_server.py` (`test_body_caps_over_real_sockets`, `TestConnectionBound`).

**C5. Per-client token buckets in the API.**
- Two buckets: one for every request (default 120 per 60 s) and one for every analysis that will compute (default 6 per 60 s).
- Keyed by IPv4 address or IPv6 /64, in an LRU of at most 10,000 keys.
- An empty bucket gets 429 `rate_limited` with `Retry-After`.
- The buckets are per process (R9).

Code: `src/atus_pipeline/api/protection.py` (`TokenBucketLimiter`), `src/atus_pipeline/api/middleware.py`, `src/atus_pipeline/api/analysis_runner.py`.
Tests: `tests/unit/api/test_protection.py::TestTokenBucket`, `tests/unit/api/test_guard.py::TestRequestRateLimit`, `tests/unit/api/test_admission.py::TestAnalysisRateLimit`.

**C6. Quota governor** (one SQLite Durable Object, `global`). It admits uncached analyses, and the metadata GETs the static snapshot cannot answer. Static files, KV hits, snapshot metadata, health and wake never call it.
- **Per client network** (IPv4 /24 or IPv6 /48): 60 analyses and 300 admissions per UTC day, refunded admissions included, and a fifth of the day's Neon compute ceiling charged to its analyses. Over any of them, 429.
- **Shared budgets** (503):
  - 600 analyses per UTC day globally, of which at most 300 heavy;
  - a daily Neon CU-hour ceiling: 10 CU-h with fresh ground truth from Neon, otherwise (100 × 0.9) / 31;
  - without ground truth, a daily Neon egress estimate of at most 4.5 GB / 31.
- **With the optional Neon key:** month-to-date CU-hour pacing and a stop at 4.5 GB of egress.
- **Metadata lookups** (a query string, a code the snapshot does not hold): 200 per client network (429) and 2,000 in all (503) per UTC day. Each also passes the compute and egress budgets above, and one that reports DB time (a freshly started API process's snapshot read) is charged like an analysis.
- **Cost of a denial:** it writes no rows.
- **Fails closed.** Each of these gets 503 `capacity_unavailable` with `Retry-After`, while static files, KV hits and snapshot metadata keep working:
  - a governor error;
  - no answer within 5 s;
  - a malformed answer;
  - a missing `IP_HMAC_KEY`.

Code: `edge/src/governor.ts`, `edge/src/admission.ts`, `edge/src/budget.ts`, `edge/src/constants.ts`, `edge/src/snapshot.ts`.
Tests: `edge/test/worker/governor.test.ts` (including "each network's share of the day's compute" and "Neon egress without ground truth"), `edge/test/worker/governor-flow.test.ts` ("failing closed"; "charges the network of CF-Connecting-IP whatever forwarding headers the client adds"; "lets a run of failed database connections lock out neither a network nor the day"), `edge/test/worker/metadata.test.ts`, `edge/test/worker/budget.test.ts`, `edge/test/worker/cache.test.ts` ("serves a hit while the origin is unconfigured or the governor is down").

**C7. Admission in the API.**
- **Dry run.** Analytically invalid requests get a 422 before any token, lane or statement is spent.
- **Single-flight.** Identical in-flight analyses join one computation.
- **Lanes.** At most 2 uncached analyses compute at once, at most 1 of them heavy. An analysis is heavy when `len(years)`, doubled for a compare, is 8 or more; the edge uses the same rule. A request waits at most 5 s for a lane, then gets 503 `overloaded`.
- **Daily cap.** At most 600 computing analyses per UTC day per process, then 503 `daily_capacity_reached`.
- **Bypass.** Cache hits and joins take no lane and no token.

Code: `src/atus_pipeline/api/analysis_runner.py`, `src/atus_pipeline/api/protection.py` (`AdmissionLanes`, `DailyCap`, `is_heavy`).
Tests:
- `tests/unit/api/test_admission.py` (`TestLanes`, `TestSingleFlight`, `TestDailyCap`);
- `tests/unit/api/test_protection.py::TestAdmissionLanes`;
- `tests/unit/api/test_snapshot.py::TestDryRun`;
- `tests/integration/api/test_protection_db.py::TestSingleFlightOverTheEngine`.

**C8. Bounded database work.**
- **Per-transaction limits.** The first statement of every request's transaction sets these, transaction-locally with `set_config(..., true)` (the data snapshot's REPEATABLE READ read sends them as `SET LOCAL` statements, followed by a share lock on `atus.ingestion_runs`, in the same single query):
  - `statement_timeout`: 85 s for analyses, 10 s for metadata;
  - `lock_timeout`: 5 s;
  - `idle_in_transaction_session_timeout`: 30 s;
  - `client_connection_check_interval`: 5 s;
  - `jit`: off.
- **Deadline.** An analysis may run for `statement_timeout` + 5 s in total; then its running query is cancelled.
- **Read-only transactions.** Every transaction is `READ ONLY`.
- **Pool.** `min_size` 0 and `max_size` 4. When every connection is busy, a request waits 5 s, then gets 503 `overloaded`.
- **Timeouts.** A query timeout becomes 503 `analysis_timeout` and is never cached.

Code: `src/atus_pipeline/api/database.py`, `src/atus_pipeline/config.py`.
Tests: `tests/integration/api/test_protection_db.py` (`TestTransactionLimits`, `TestTimeouts`, `TestPoolExhaustion`), `tests/unit/api/test_admission.py::TestDatabaseErrorMapping`.

**C9. Read-only `atus_api` role.**
- **Attributes.** LOGIN only: no CREATEDB, no CREATEROLE, no role memberships. `CONNECTION LIMIT 10`.
- **Privileges.** SELECT on exactly the ten served tables. CREATE and TEMPORARY on the database are revoked from PUBLIC.
- **Role defaults.** `statement_timeout` 85 s, `lock_timeout` 5 s, `idle_in_transaction_session_timeout` 30 s, `jit` off, `default_transaction_read_only` on (see R4).
- **Self-checks.** The script verifies its own result and aborts on any deviation. It refuses a database without the serving marker.
- **Password.** The restore tool generates a 192-bit password and sends it to the server as a SCRAM verifier only.

Code: `scripts/ops/roles.sql`, `src/atus_pipeline/serving/restore.py`, `src/atus_pipeline/serving/secrets_file.py`.
Tests:
- `tests/deploy/test_stack_roles.py`;
- `tests/integration/test_serving_restore.py`: `test_the_api_role_cannot_write_or_create`, `test_the_api_role_cannot_pass_its_privileges_on`, `test_the_api_role_defaults_bound_every_session`;
- `tests/unit/test_serving_secrets_file.py`.

**C10. KV store gate.**
- **Key.** `r1/<CACHE_SCOPE>/<sha256(path + "?" + query + "\n" + body)>`, computed over the exact request bytes. The edge does no JSON canonicalization.
- **Scope.** `CACHE_SCOPE` hashes `EPOCH`, the API, analytics and data versions, and the response format.
- **What gets stored.** Only the answer to an analysis POST that meets all of these:
  - a 200 JSON response;
  - origin `X-Cache` is `miss`: computed for this request, on an admission the global count keeps;
  - the version headers equal the Worker's vars;
  - the body is complete and at most 16 KiB.

  Entries expire after 30 days. Errors, API cache hits, joins and `bypass` answers are never stored, so KV writes stay within the 600 analyses a day.
- **On read.** An entry whose metadata does not match the deployment counts as a miss.
- **Failures.** A failed KV read is a miss and a failed write is ignored. With empty release vars the cache is off.

Code: `edge/src/cache.ts`. Tests: `edge/test/worker/cache.test.ts`.

**C11. Snapshot provenance.**
- **Build.** CI restores the sha256-verified release dump into `postgres:16` with `scripts/ops/roles.sql`, then runs the pushed image digest against it.
- **Response checks.** The build fails unless every response is a 200 JSON carrying its version headers.
- **File checks.** `scripts/build_snapshot.py --verify` checks every file's SHA-256 against the manifest.
- **Deploy.** The snapshot is published only by the approved deploy job.
- **After deploy.** The smoke test recomputes two snapshot entries live and diffs them under the §9 equivalence criterion.

Code: `.github/workflows/deploy.yml` (job `snapshot`), `scripts/build_snapshot.py`, `scripts/ops/release_manifest.py`, `scripts/ops/deploy_smoke.py`.
Tests:
- `tests/deploy/test_workflows.py::test_the_snapshot_is_built_from_the_pushed_digest_and_the_committed_release`;
- `tests/unit/test_build_snapshot.py` (version-header refusals, `test_detects_a_changed_byte`);
- `tests/unit/ops/test_deploy_smoke.py`.

**C12. Security headers on both origins.**
- **Site** (`frontend/public/_headers`):
  - CSP: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`;
  - HSTS (`max-age=31536000`), `nosniff`, `Referrer-Policy: no-referrer`;
  - a deny-all `Permissions-Policy`;
  - `Cross-Origin-Opener-Policy: same-origin`.

  The build adds the same CSP, minus `frame-ancestors`, as a `<meta>` backstop.
- **`/api` responses from the Worker:**
  - CSP `default-src 'none'; frame-ancestors 'none'`;
  - HSTS, `nosniff`, `no-referrer`;
  - `Cross-Origin-Resource-Policy: same-origin`;
  - `Permissions-Policy`;
  - `Cache-Control: no-store`.

  The Worker drops the origin's `server`, `x-render-*`, `rndr-id` and `set-cookie` headers.
- **The API itself** sets the same set. HSTS is sent in production only, `no-store` on errors, and uvicorn sends no `server` header.

Code: `frontend/public/_headers`, `frontend/vite.config.ts`, `frontend/src/deploy/staticHeaders.ts`, `edge/src/http.ts`, `edge/src/proxy.ts`, `src/atus_pipeline/api/middleware.py` (`security_headers`).
Tests:
- `frontend/src/checkBundle.test.ts`;
- `frontend/src/deploy/staticHeaders.test.ts`;
- `frontend/e2e/service-states.spec.ts` (headers under `vite preview`), and every E2E test: each fails on a CSP violation or Permissions-Policy error in any page of its browser context (`frontend/e2e/fixtures.ts`, review round 2; before, only the core journeys were checked);
- `edge/test/worker/headers.test.ts`;
- `tests/unit/api/test_guard.py::TestHeaders`;
- the `static_headers` and `api_headers` checks in `deploy/stack/edge_probe.py`. `static_headers` compares every header of the `/*` rule of the published `_headers` with its exact value on `/`, `/explore` and a hashed bundle; `tests/deploy/test_edge_probe.py` shows each check of the probe failing on its violation (a missing or changed `Referrer-Policy` or `Permissions-Policy` included) against a stub edge.

**C13. CORS off.**
- CORS is off unless `ATUS_API_CORS_ORIGINS` lists exact origins. `*` and origins with paths are refused at startup, and credentials are never allowed.
- The production template leaves the variable empty: the site and the API share one origin behind the Worker.
- The Worker relays no `Access-Control-*` header.

Code: `src/atus_pipeline/api/app.py`, `src/atus_pipeline/config.py` (`_cors_origins`), `edge/src/proxy.ts`.
Tests: `tests/unit/api/test_review_hardening.py::TestCORS`, `tests/unit/api/test_config.py::TestValidation`.

**C14. Operator surfaces.**
- **Docs pages.** `ATUS_API_DOCS` defaults to off in production, which removes `/docs`, `/docs/oauth2-redirect`, `/redoc` and `/openapi.json`.
- **`/ready` and `/metrics`.** Both need `Authorization: Bearer <ATUS_OPS_TOKEN>`, compared in constant time. Without it, every method gets the same answer as an unknown path.
- **`/api/_ops/budget`.** Needs the Worker's `OPS_TOKEN`; the Worker compares SHA-256 digests of both values with `timingSafeEqual`. Without it, the route is the 404 envelope.

Code: `src/atus_pipeline/api/app.py`, `src/atus_pipeline/api/middleware.py` (`ops_authorized`, `OPS_PATHS`), `src/atus_pipeline/api/routes/health.py`, `edge/src/ops.ts`.
Tests: `tests/unit/api/test_exposure.py::TestDocumentationSwitch`, `tests/unit/api/test_observability.py::TestOperatorEndpoints`, `edge/test/worker/ops.test.ts`.

**C15. No internals in errors.**
- **API.**
  - Every error uses the one JSON envelope.
  - An unexpected exception becomes a generic 500 `internal_error` with the request ID; the details are logged once, server-side.
  - Database errors map to codes without driver text.
  - Hints for an empty database go to the log only.
- **Worker.**
  - Its own exceptions become the same generic 500.
  - Origin responses that are not API envelopes (platform pages, redirects, uvicorn's plain 503) become envelopes, without the origin's body or headers.
  - It logs only an exception's name, because runtime messages can contain the origin URL.

Code: `src/atus_pipeline/api/errors.py`, `edge/src/index.ts`, `edge/src/proxy.ts`, `edge/src/upstream.ts`.
Tests:
- `tests/unit/api/test_exposure.py::TestNoInternalsInErrors`;
- `tests/unit/api/test_review_hardening.py::TestErrorEnvelopeCoverage`;
- `tests/unit/api/test_admission.py::TestDataNotLoaded`;
- `edge/test/worker/routing.test.ts` ("unexpected failures");
- `edge/test/worker/relay.test.ts` ("converted responses never leak the origin body or headers").

**C16. Header and log-line hygiene.**
- **Request IDs.** The API echoes an incoming `X-Request-ID` only from an authenticated edge, and only if it fully matches `[A-Za-z0-9._-]{8,64}`.
- **Logged paths.** Access lines log the path still percent-encoded and cut to 200 characters, so a decoded newline cannot forge a log line.
- **No `Host`-based redirects.** `redirect_slashes=False`, so the API never builds a `Location` header from `Host`.
- **Response headers.** The Worker relays only an allowlist of origin headers.
- **Redirects.** The Worker never follows a redirect (`redirect: 'manual'`), so the edge secret is never resent to another host.
- **Origin URL.** `RENDER_ORIGIN` must be `https`, without credentials; `http` is accepted only for loopback.

Code: `src/atus_pipeline/api/middleware.py`, `src/atus_pipeline/api/app.py`, `edge/src/proxy.ts`, `edge/src/upstream.ts` (`originUrl`), `edge/src/index.ts`.
Tests:
- `tests/unit/api/test_guard.py` (`TestRawHeaders::test_request_id_with_a_trailing_newline_is_replaced`, `TestHttpEdgeCases`);
- `tests/unit/api/test_edge_contract.py::TestRequestIds`;
- `edge/test/worker/headers.test.ts`;
- `edge/test/worker/routing.test.ts` ("origin configuration").

**C17. Visitor privacy.**
- **API logs.** Access lines keep only the client's IPv4 /24 or IPv6 /48. They never contain bodies, specs or query strings, and uvicorn's own access log is off.
- **Worker logs.** No IP, no prefix hash, no body. Cloudflare's per-request invocation logs are off (`invocation_logs: false` in `edge/wrangler.jsonc`), because they may record request headers, `CF-Connecting-IP` included (ADR-014).
- **Governor storage.**
  - It stores an HMAC-SHA256 of the /24 or /48, truncated to 128 bits. The key is derived each UTC day from `IP_HMAC_KEY` and the date.
  - Prefix rows are deleted at the first event after 00:00 UTC, and by a midnight alarm when there is no traffic.
  - Deleted rows can still be brought back for 30 days. Cloudflare documents point-in-time recovery that restores a SQLite-backed Durable Object "to any point in time in the past 30 days" ([SQLite storage API](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/), read 2026-10-21; no plan restriction is stated). Whoever can deploy code to the Worker (the Cloudflare account, or a leaked `CLOUDFLARE_API_TOKEN`, either of which also yields `IP_HMAC_KEY`) could restore up to 30 days of per-network daily counts and reverse them by enumerating prefixes. `IP_HMAC_KEY` changes only when rotated, so whoever holds it can derive any day's key. That attacker controls the Worker and could log raw addresses from then on anyway, so what this adds is the past 30 days of coarse per-network counts (review round 2).
  - The budget report contains no prefix hashes.
- **Raw client IPs** exist only in memory (the Worker's request, the API's bucket keys) and in the `X-ATUS-Client-IP` header.
- **The SPA:**
  - its CSP allows only its own origin, so it loads nothing from third parties;
  - its shipped sources (`frontend/src` without its tests, and `index.html`) set no cookies and use no Web Storage or IndexedDB (`tests/deploy/test_docs_accuracy_r2.py::test_the_spa_sets_no_cookies_uses_no_web_storage_and_writes_no_raw_html`; review round 2 added the test).
- **Retention** is the providers' log retention (ADR-014).

Code: `src/atus_pipeline/api/observability.py` (`ip_prefix`), `src/atus_pipeline/api/middleware.py`, `edge/src/admission.ts` (`prefixHmac`), `edge/src/governor.ts`, `edge/src/index.ts`, `edge/wrangler.jsonc` (`observability`; held by `edge/test/node/config.test.ts`).
Tests:
- `tests/unit/api/test_observability.py` (`TestIpTruncation`, `TestAccessLog::test_no_full_address_body_or_spec_is_logged`);
- `tests/unit/api/test_edge_contract.py::TestMissingClientIp`;
- `edge/test/worker/admission.test.ts` (`prefixHmac`);
- `edge/test/worker/governor-flow.test.ts` ("logs the governor and cache results without the client address or prefix hash");
- `edge/test/worker/governor.test.ts` ("purge and alarms");
- `edge/test/worker/ops.test.ts`;
- `tests/deploy/test_docs_accuracy_r2.py::test_the_spa_sets_no_cookies_uses_no_web_storage_and_writes_no_raw_html` (the SPA's sources).

**C18. Secret handling.**
- **Generation.** `scripts/ops/gen_edge_secrets.sh` generates 256-bit secrets; the database password is 192 bits.
- **Storage.** Secrets are written to `~/.config/atus/*.env` with mode 0600, in a 0700 directory. They are never printed and never passed in a process's arguments.
- **Database password.** It reaches the server only as a SCRAM verifier.
- **Logs and errors:**
  - configured secrets are redacted from log records and tracebacks;
  - configuration errors never echo a secret;
  - restore output and reports are redacted.
- **Repository:**
  - `.gitignore` ignores every `.env*` and `*.env` file except the templates;
  - `edge/wrangler.jsonc` holds no secret values and requires the four Worker secrets.
- **Workflows:**
  - they never interpolate a secret into a script, never use `set -x` and never print a secret variable;
  - deploy secrets exist only in the `production` environment;
  - no workflow holds database credentials.

Code: `scripts/ops/gen_edge_secrets.sh`, `src/atus_pipeline/serving/secrets_file.py`, `src/atus_pipeline/logging_setup.py`, `src/atus_pipeline/config.py`, `src/atus_pipeline/serving/restore.py`, `.gitignore`, `edge/wrangler.jsonc`.
Tests:
- `edge/test/node/gen-edge-secrets.test.ts`;
- `tests/unit/test_serving_secrets_file.py`;
- `tests/unit/api/test_serve_and_logging.py::TestLogging`;
- `tests/unit/api/test_observability.py::TestJsonFormat`;
- `tests/unit/api/test_config.py::TestValidation` (`test_malformed_database_url_never_echoes_the_password`, `test_secrets_are_not_echoed_or_repr`);
- `tests/integration/test_serving_restore.py::test_no_secret_reaches_the_output_or_the_report`;
- `tests/deploy/test_packaging_files.py` (`test_secret_files_are_ignored`, `test_production_example_holds_no_secret_values`);
- `edge/test/node/config.test.ts`;
- `tests/deploy/test_workflows.py` (`test_secrets_never_reach_a_script_directly_or_get_echoed`, `test_only_the_production_environment_job_holds_deploy_secrets`).

**C19. Supply chain.**
- **Workflows:**
  - every action is pinned to a full commit SHA;
  - every workflow and every job declares its `permissions`;
  - no workflow uses `pull_request_target` or `workflow_run`;
  - checkouts do not persist credentials;
  - `packages: write` is granted only to the deploy `push` job, the deploy `promote` job (which only adds the tags `live` and `deployed-<commit>` to the digest that passed the smoke) and the GHCR cleanup.
- **Inputs:**
  - base images are pinned by digest, and so is the `postgres:16` client the owner's restore runs through docker with the owner's password (`serving/pgtools.py`; `ATUS_PG_CLIENT_IMAGE` overrides it);
  - the owner's `wrangler login` (an OAuth token with broad account scopes) and every other owner `wrangler` command run the version locked in `edge/package-lock.json`, after `npm ci` in `edge/`, with `npx --no-install`, which refuses to download another version;
  - Python installs are hash-locked and binary wheels only (`--require-hashes --only-binary=:all:`, in the image, CI and the deploy), so no install builds an sdist;
  - npm installs come from lockfiles (`npm ci`);
  - the image runs as UID 10001, not root.
- **Scans in CI:**
  - Trivy, from a pinned release binary checked against its SHA-256, which fails on HIGH or CRITICAL vulnerabilities that have a fix;
  - `pip-audit --strict` on `requirements/api.lock.txt`;
  - `npm audit --omit=dev` (the frontend's at `--audit-level=high`).
- **Updates.** Dependabot proposes weekly updates for pip, npm (frontend and edge), GitHub Actions, Docker and Compose, each after a 7-day cooldown. For the base images (`postgres:16`, `python:3.12-slim`, `node:22-slim`) it proposes only digest refreshes: their versions are platform contracts (Neon's Postgres major, the locks' Python, the edge's Node), changed by hand when the platform changes (`ignore` rules in `.github/dependabot.yml`). The `postgres:16` digest also has copies no ecosystem updates (the workflows' `services:` images, `deploy/ci/check_image.py`, `tests/deploy/test_container_image.py`, `serving/pgtools.py`): `tests/deploy/test_dependabot.py::test_every_postgres_16_pin_names_the_same_digest` fails a pull request that bumps only some of them, so CI, the deploy snapshot and the stack keep one PostgreSQL build.
- **Deploy.** It pushes exactly the tested image ID, and Render deploys it by digest.

Code: `.github/workflows/`, `.github/dependabot.yml`, `Dockerfile`, `deploy/*/Dockerfile`, `requirements/*.lock.txt`.
Tests:
- `tests/deploy/test_workflows.py`:
  - `test_every_action_is_pinned_by_full_sha_with_a_version_comment`;
  - `test_permissions_and_timeouts_are_explicit`;
  - `test_no_risky_triggers`;
  - `test_checkouts_do_not_persist_credentials`;
  - `test_no_other_workflow_can_deploy_or_push`;
  - `test_the_pushed_image_is_the_tested_one`;
  - `test_trivy_is_a_pinned_binary_never_the_hijacked_actions`;
  - `test_ci_audits_and_lints`;
- `tests/deploy/test_dependabot.py`;
- `tests/deploy/test_packaging_files.py`;
- `tests/deploy/test_container_image.py::test_runs_as_an_unprivileged_user`.

**C20. Owner settings.** No repository test can check these. The owner confirms each one, following [`docs/deployment.md`](deployment.md), "Owner account setup (§19)".
- **GitHub:**
  - the `production` environment, with the owner as required reviewer and only `main` and `v*` allowed to deploy;
  - read-only default workflow permissions;
  - release immutability;
  - CodeQL default setup, Dependabot alerts and Dependabot security updates;
  - secret scanning and push protection (on by default for public repositories; confirm).
- **Render:** no Git repository connected, and no Render API key.
- **Cloudflare:** Workers Builds disconnected, and a CI token scoped to Workers Editor on the one Worker with a 90-day TTL.

## Abuse cases

### 1. Quota exhaustion

- **Uncached analyses.** These are the only requests that spend Render work on the database and Neon CU-hours. C6 caps them per network and globally and charges them conservatively, compute included: one network is held to a fifth of the day's compute ceiling, so spacing analyses (each then charged its own wake session) no longer lets one network spend the ceiling for everyone (`edge/test/worker/governor.test.ts`, "each network's share of the day's compute"). C5 and C7 are a second, per-process layer, which also covers a misconfigured edge.
- **Metadata cannot keep Neon awake for free.** With `ATUS_EXPECTED_DATA_VERSION` set, the API pins its in-process data snapshot. Metadata, cache hits and every 4xx decided before the statistics query then cost no database statement (`src/atus_pipeline/api/snapshot.py`; `tests/unit/api/test_snapshot.py::TestPinnedSnapshot`, `::TestZeroStatementPaths`). The exception is each process's first data request, which reads that snapshot from Neon, after every Render spin-down. Through the Worker it is an analysis or a governed metadata lookup, and C6 charges either its wake session when the API reports DB time, so a network that sends a lookup after each spin-down is refused after about 3 such wakes a day without the Neon key (`edge/test/worker/metadata.test.ts`, "charge a lookup that woke the database"; `governor.test.ts`, "metadata lookups that reach the database"). Before review round 2 lookups were only counted, and these wakes (up to about 2 CU-h a day at 0.25 CU) were uncharged. What remains is the charge model's error (ADR-015, "Model error"). No monitor touches the database (`tests/deploy/test_workflows.py::test_monitors_never_touch_the_database`).
- **Metadata cannot spend Render's monthly outbound.** The Worker answers the metadata routes from the static snapshot, byte for byte. Only what the snapshot cannot answer (a query string, an unknown code) reaches Render, as a counted lookup: 200 per network and 2,000 in all per day, about 0.6 GB a month at most. The origin is always asked for gzip (`edge/test/worker/metadata.test.ts`, `headers.test.ts`).
- **Neon egress without ground truth.** Each day's estimated egress is held to 4.5 GB / 31, and a 422 that ran the statistics query keeps its heavy count (`edge/test/worker/governor.test.ts`, "Neon egress without ground truth"). The 4.5 GB monthly stop needs the optional Neon key.
- **KV over its daily write quota** fails rather than bills (ADR-011, cited terms). A failed write is ignored and a failed read is a miss (`edge/test/worker/cache.test.ts`).
- **Durable Object rows.** A denial writes no rows, refunded admissions are capped at 300 per network per day, and metadata lookups at 2,000 a day (`edge/test/worker/governor.test.ts`: "writes nothing when it denies", "writes at most 8 rows to admit and 4 to settle", "writes at most 5 rows for any settle, and at most 3 for a refunded one with db;dur=0", "writes at most 7 rows to admit a metadata lookup and 4 to settle it, and none to deny one"). KV stores only computed answers, so its writes stay within the 600 analyses a day (`edge/test/worker/cache.test.ts`).
- **Render:**
  - `/api/health` and `/api/v1/wake` reach only origin `/health`, never Neon.
  - The prewake sends at most one origin call per isolate per 10 minutes once the origin answered warm, and at most one a minute while it is waking; `PREWAKE=off` disables it (`edge/test/worker/wake.test.ts`).
  - A per-isolate circuit breaker skips the origin for 30 s after two connection failures (`edge/test/worker/resilience.test.ts`).
- **Remaining:** R1, R2, R3; and R13, a Render suspension that visitors see as a cold start until the owner sets the pause switch.

### 2. Expensive-query DoS

- **Per request.** C8 bounds the cost: statement timeout, a deadline that cancels the query, and lock and idle timeouts. `client_connection_check_interval` lets the server stop a query whose client connection is gone.
- **In aggregate.** C7's lanes and the 4-connection pool bound the total. The edge and the API classify heavy specs by the same rule, and the edge treats a body it cannot parse as heavy.
- **Valid but costly specs.** The schema bounds them (C4).
- **Invalid specs.** Those the schema or the dry run catches are refused before any statement (`tests/unit/api/test_guard.py::TestValidationCostsNothing`, `tests/unit/api/test_snapshot.py::TestDryRun`). A diary-date window outside the requested years, and `insufficient_data`, are found only by the statistics query, which they run first (bounded by C8).
- **Retries.** A retry after a disconnect or a proxy timeout joins the running computation or hits the cache; it does not compute again (`tests/integration/api_prod/test_server.py::TestClientDisconnect`).
- **Measured scale.**
  - The audit measured the pre-fix code under emulation: worst cases took 47–52 s at p50 at Neon's 0.25 CU floor, and database CPU saturated at 2 concurrent uncached analyses ([`docs/evidence/phase5/01-audit-timed.md`](evidence/phase5/01-audit-timed.md)).
  - The current code, under the same emulation on 2026-10-21 ([`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md)): at 0.25 CU the pooled 22-year and 23-year trend worst cases took 21–22 s at p50, the F66 broad selectors 33–35 s pooled or trended and 69–71 s compared by sex. Through the real lanes (2 in flight, 1 heavy, 5 s wait), `503 overloaded` started at 4 concurrent clients and grew to 246 of 354 requests at 16, answered after 5.0 s at p50; no `analysis_timeout` occurred. The 85 s statement timeout is only 1.2 × the F66 compare's p99 at the floor, so CPU contention there can turn it into `503 analysis_timeout`, never a wrong or cached answer ([ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13)).
- **Remaining:** within its 60 a day, one network can still run 60 worst-case analyses. The global and CU-hour budgets bound the total.

### 3. Scraping

- **Public data.** The data is public statistics, and the API holds no user data.
- **Static files.** Metadata and the prewarmed analyses are static files. Fetching them never invokes the Worker and costs no quota.
- **Uncached analyses.** C6 paces them per network and globally. Repeated specs hit KV or the API's cache and cost no database work.
- **Remaining:** a scraper spread over many networks can spend the global daily budget (R1).

### 4. Malformed share links

- **Decoding.** A share link is `/analysis/<operation>?spec=<base64url(JSON)>`, decoded only in the browser. Nothing is sent, and an "invalid link" page appears, for any of these:
  - invalid base64, UTF-8 or JSON;
  - the keys `__proto__`, `constructor` or `prototype`;
  - a structurally wrong spec, including a field the page reads with the wrong type (a preset, label or measure that is not a string, an include list that is not strings, a population that is not an object of scalar values; review round 2: such values crashed the page into its error boundary or made Analyze throw).

  The builder (`/explore?op=…&spec=…`) opens such a link as the default analysis with a visible note instead. Code: `frontend/src/domain/urlSpec.ts`. Tests: `frontend/src/domain/urlSpec.test.ts`, `frontend/src/pages/ExplorePage.test.tsx`, `frontend/e2e/share.spec.ts`.
- **Plausible but invalid specs.** These are sent, and the API refuses them with a 422. Most cost no database statement (C4, C7); the exceptions are listed under abuse case 2.
- **Labels.** Free-text labels are rendered as React text. The SPA's non-test sources contain no `dangerouslySetInnerHTML`, `innerHTML`, `outerHTML` assignment, `insertAdjacentHTML` or `document.write` (`tests/deploy/test_docs_accuracy_r2.py::test_the_spa_sets_no_cookies_uses_no_web_storage_and_writes_no_raw_html`; a component test may still read `innerHTML`), and the CSP forbids inline script.
- **No stored content.** There is no link storage, so a link cannot plant content on the site ([ADR-016](adr/ADR-016-short-share-links.md)).
- **Content spoofing through labels (accepted, R14).** A link chooses the free-text activity label (up to 120 characters) and group labels (up to 80 each), which become the page heading and the result sentence on the site's own hostname: a sleep estimate can be headed "Working (official BLS figure)". The codes and populations actually computed are listed only in the collapsed "How was this calculated?" panel.
- **No cross-spec answers.** A link cannot select a cached answer for another spec: the KV key covers the exact request bytes (C10).

### 5. Forged forwarded-IP and edge headers

- **Through the Worker.** Client-sent `X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `X-ATUS-Client-IP` and `X-ATUS-Edge-Auth` headers never reach the API (C3).
- **Direct to the API.**
  - A forged `X-ATUS-Client-IP` without a valid secret gets a 403.
  - The API never reads the forwarding headers (C2, C3).
- **Governor keys.** The governor keys on `CF-Connecting-IP` only. A network at its daily limit stays refused whatever `X-Forwarded-For`, `X-Real-IP`, `True-Client-IP`, `X-Client-IP`, `X-ATUS-Client-IP`, `Forwarded` or `CF-Connecting-IPv6` the client adds, and naming that network in them charges nothing to it (`edge/test/worker/governor-flow.test.ts`, "charges the network of CF-Connecting-IP whatever forwarding headers the client adds"; `edge/test/worker/metadata.test.ts`, the same for lookups; on the local stack, the `governor_denial` check in `deploy/stack/edge_probe.py` sends forged forwarding headers naming a new network with each request). Before review round 3 no test pinned this, and keying the governor on `X-Forwarded-For` passed every Worker test. That a client cannot override `CF-Connecting-IP` itself on workers.dev is a Cloudflare behaviour, not yet verified on a deployment (R8).
- **Request IDs.** A client's `X-Request-ID` is replaced (C16).

### 6. Direct-origin access

- **The hostname.** The onrender.com hostname cannot be disabled without a custom domain (ADR-011). It is never published, and the recommended service name has a random suffix.
- **The origin lock.** Without the edge secret, every request gets 403 `forbidden_origin`, with no database work (C2), except `/health`, and `/ready` and `/metrics` with a valid `ATUS_OPS_TOKEN` bearer (C14). The one earlier check is the body cap: a declared body over the cap gets 413 first.
  - Checked locally by the `origin_lock` check in `deploy/stack/edge_probe.py`.
  - After deploy, `scripts/ops/deploy_smoke.py --origin-lock` checks it; that has not run yet.
- **`/health`.** It answers anyone with `{"status": "ok", "build": <git SHA>, "expected_data_version": <data version or null>}` and touches nothing. The SHA is public in the repository anyway, and the data version is in every API response's `X-Data-Version` and in `/api/_edge/health`.
- **Remaining:** R3.

### 7. Cache poisoning (KV, snapshot, API cache)

- **KV.**
  - Only the Worker writes KV, and only origin responses that pass the store gate (C10).
  - A client can fill only the entry for its own exact bytes, and only with the API's answer to those bytes.
  - The edge does not canonicalize JSON. A lossy round trip (`1e400` becomes `null`) could otherwise map a malformed request onto another spec's key.
- **Snapshot.** It is built and verified in CI and deployed only by the approved job (C11).
- **API cache.**
  - The key is the operation, the canonical validated spec, the analytics version and the data version (`src/atus_pipeline/api/cache.py`).
  - Errors and timeouts are never cached.
  - A result computed while the data version changed is served with `X-Cache: bypass` and not stored.

  Tests: `tests/unit/api/test_review_hardening.py::TestCacheDegradation`, `tests/unit/api/test_admission.py::TestDatabaseErrorMapping::test_timeouts_are_never_cached`.
- **Wrong database.** An API attached to another data version answers 503 `data_version_mismatch` on every data route (`tests/unit/api/test_snapshot.py::TestExpectedDataVersion`).
- **Remaining:** whoever holds the Cloudflare account or its CI token can replace the Worker, its vars or the snapshot (case 10).

### 8. Header injection

- **API.** C16, and C4: control characters in query strings get a 422 before any handler runs, and the request head is bounded.
- **Upstream headers.** The Worker builds them only from fixed names and checked values; `CF-Connecting-IP` must match an IP character set.
- **Response headers.** Only allowlisted origin headers reach the client, and `set-cookie` is not one of them.

### 9. Supply-chain compromise

- **Controls:** C19 and C20.
- **The only deploy path.**
  - Only `.github/workflows/deploy.yml` deploys, on a `v*` tag or a manual run, after approval.
  - Render pulls the exact digest that the `push` job pushed.
  - Workers Builds and Render's Git integration are never connected. The `edge/wrangler.jsonc` invariants are tested in `edge/test/node/config.test.ts`.
- **The deploy secrets' job.** The `deploy` job holds `CLOUDFLARE_API_TOKEN`, the Render deploy hook and the origin URL. On a GitHub-hosted runner a process started by one step can outlive it and read later steps, so that job installs only what it runs: the API's runtime lock (binary wheels) for the smoke's §9 comparison, and the edge's lockfile for `wrangler`. The `wrangler.jsonc` tests (vitest) run in `edge-config`, the browser journey (the frontend tree, Playwright) in `browser-smoke`; neither job has an environment or a secret (review round 2).
- **Remaining:** scanners only see known vulnerabilities, and coverage has gaps (R10). The deploy job's `npm ci` in `edge/` still installs the whole locked edge tree with install scripts enabled (`wrangler` needs those of `esbuild` and `workerd`), so a compromised pinned version of a package in that tree could read the deploy secrets. Lockfile integrity hashes and Dependabot's 7-day cooldown bound this to a compromised version that was pinned on purpose.

### 10. Leaked tokens

Rotation procedures: [`docs/deployment.md`](deployment.md), "Rotation". After any leak, rotate the secret and every secret its row says it exposes, then check provider logs for use.

| Secret | Lives in | If leaked |
|---|---|---|
| `EDGE_AUTH_SECRET` (`ATUS_EDGE_SECRETS` on Render) | Worker secret, Render env, `edge.env` | Direct API calls that bypass the edge governor. The caller can also choose `X-ATUS-Client-IP` per request, which defeats the API's per-client buckets. The API's lanes, per-process daily cap and timeouts still apply. Rotation uses two accepted values, so no request fails |
| `ATUS_OPS_TOKEN` | Render env, `edge.env` | `/ready` and `/metrics` directly at the origin. `/ready` reads the database, and can wake Neon, only when the process's snapshot is older than `ATUS_API_SNAPSHOT_TTL_S` (600 s in production), so at most about once per 10 minutes per process. `/metrics` holds counters only |
| `OPS_TOKEN` | Worker secret, `edge.env`, GitHub `EDGE_OPS_TOKEN` | The read-only budget report: counts and limits, no IPs or prefix hashes |
| `IP_HMAC_KEY` | Worker secret, `edge.env` | Today's prefix HMACs can be reversed by enumerating prefixes; IPv4 has 2^24 /24s. Earlier days' keys derive from the same secret, which changes only when rotated. Earlier days' rows are deleted, but the Durable Object's point-in-time recovery can bring back up to 30 days of them: with Worker-deploy access as well (the Cloudflare account or `CLOUDFLARE_API_TOKEN`, which also expose this key), up to 30 days of per-network daily counts can be reversed (C17) |
| `RENDER_ORIGIN` | Worker secret, GitHub secret, `edge.env` | Not a credential. It reveals the direct-origin hostname (R3) |
| `atus_api` password (in `ATUS_DATABASE_URL`) | Render env, the project's `neon-<release>-<build8>.env` | Reads of the served tables, which hold public data, and Neon compute spend. No writes (C9). The role defaults do not bind the holder (R4). At most 10 connections |
| `neondb_owner` DSN | the project's `neon-<release>-<build8>.env`, on the owner machine only | Full control of the serving DB: the statistics could be altered. After resetting the password, `atus verify-serving-db --manifest <release manifest> --content-hash` shows whether any served row changed; recovery is a restore from the sha256-verified release dump ([deployment.md "Rotation"](deployment.md#rotation), item 7) |
| `NEON_API_KEY` (optional) | Worker secret | Editor rights on the project (R6), enough to alter the served statistics. After a leak, revoke it, reset the `neondb_owner` and `atus_api` passwords, and check every served row against the release (`atus verify-serving-db --manifest <release manifest> --content-hash`) or restore the release dump into a new project ([deployment.md "Rotation"](deployment.md#rotation), item 7) |
| `CLOUDFLARE_API_TOKEN` | GitHub environment `production` | Deploys arbitrary Worker code, vars and static assets on the public origin, so it controls everything visitors see, statistics included. Worker code deployed with it can read every Worker secret (`EDGE_AUTH_SECRET`, `OPS_TOKEN`, `IP_HMAC_KEY`, `RENDER_ORIGIN` and, when set, `NEON_API_KEY` and the pause switch's `API_PAUSED_UNTIL` and `API_PAUSED_REASON`) and send it anywhere, then be rolled back, so a leak of the token, or of the Cloudflare account, discloses all of them: rotate each credential, treat `RENDER_ORIGIN` as known (R3), and follow the `NEON_API_KEY` row too. The pause switch holds no secret values, but the token can set or clear it: set it again with `deploy.yml` (`pause-api` or `resume-api`). It may also write KV: bump `EPOCH` ([deployment.md "Rotation"](deployment.md#rotation), item 5) |
| `CLOUDFLARE_ANALYTICS_TOKEN` (optional) | GitHub environment `ops` | Reads this account's analytics through Cloudflare's GraphQL API (Account Analytics: Read): usage and traffic figures, no secrets, no write access. Rotate it before its 90-day expiry like the deploy token ([deployment.md "Rotation"](deployment.md#rotation), item 8) |
| `RENDER_DEPLOY_HOOK_URL` | GitHub environment `production` | Redeploys the API with any image whose URL matches the service's image except for the digest (Render's documented rule, ADR-011), that is, another version of `ghcr.io/nathan98000/atus-api` |

## Production exposure decisions

| Path | Through the Worker | Direct to onrender.com | Decision |
|---|---|---|---|
| `/docs`, `/docs/oauth2-redirect`, `/redoc` | 404 (not allowlisted) | 403 without the edge secret; 404 with it (routes removed) | **Off** (`ATUS_API_DOCS=off`, the production default). The pages load third-party CDN scripts without SRI and serve no visitor (audit F43) |
| `/openapi.json` | 404 | as above | **Off.** The same schema is committed as `frontend/openapi.json` and exported by `scripts/export_openapi.py`; `tests/unit/api/test_exposure.py::TestDocumentationSwitch::test_published_snapshot_is_current` keeps it current. The public site publishes that committed copy as a static asset at `/openapi.json` (the site root, outside `/api`): it holds no secrets, and serving it runs neither the Worker nor the API |
| `/ready` | 404, never routed | 403 without the edge secret; 404 with it; 200 or 503 with `Authorization: Bearer <ATUS_OPS_TOKEN>` | **Operator only.** It reads the database whenever its snapshot is older than `ATUS_API_SNAPSHOT_TTL_S` (600 s in production; within that time it answers from the snapshot) and names data versions, so without a token it must not be a way to wake Neon (audit F16). Its failures are ordinary envelopes; hints go to the log |
| `/metrics` | 404, never routed | same as `/ready` | **Operator only.** Prometheus text with bounded label values (route templates, codes), no client input or addresses |
| `/health` | `/api/health` → origin `/health` | 200 `{status, build, expected_data_version}` | **Public.** Render's health check and the monitors need it. It touches no database and needs no edge auth; it is exempt from rate limiting |
| `/api/_edge/health` | answered by the Worker | — | **Public.** Versions, snapshot ID, cache scope and the owner's pause switch (`api_paused`), none of them secret. No origin call |
| `/api/_ops/budget` | 200 with `Authorization: Bearer <OPS_TOKEN>`, otherwise the 404 envelope | — | **Owner and the daily quota report.** Counts and budgets only |
| `/api/v1/wake` | answered by the Worker | — | **Public.** At most one origin `/health` call per isolate per 10 minutes once the origin is warm, one a minute while it is waking; never touches Neon |

## Accepted risks

**R1. Distributed abuse beyond the per-network caps.** Budgets are per /24 or /48, and a few networks are enough to spend the shared ones: ten at their 60 analyses spend the global 600, five sending heavy analyses spend the heavy 300, and five spacing analyses so that each is charged its own wake session spend the daily compute ceiling (each network is held to a fifth of it; before review round 1 one network alone could). An attacker with that many networks (a botnet, a few IPv6 /48s) passes every per-network limit. The global (600/day), heavy (300/day), CU-hour and egress budgets still hold, but legitimate visitors then get 503 `daily_capacity_reached` for new analyses until 00:00 UTC. Static and cached results keep working. Admissions that end refunded (the API shed them, or answered from its cache) are bounded per network only, at 300 a day and a few KB of Render outbound each: dozens of networks keeping that up for a whole month could spend Render's 5 GB and suspend it until the 1st (`deployment.md`, S6).

**R2. Workers Free's 100,000 requests per day are account-wide.** Every `/api` request counts, including those the Worker refuses. On workers.dev at $0, nothing can rate-limit before the Worker runs (ADR-011, accepted risk 4). Once the requests are spent, `/api` is down until 00:00 UTC, KV-cached results included. The SPA and the static snapshot keep working, because static asset requests never invoke the Worker. Early warning: only with the optional read-only analytics token (owner step 4.3), when the daily quota report finds more than 70% of the pro-rated allowance used; it runs once a day, so a flood that starts after 22:05 UTC is seen only when `/api` stops. Without the token there is none: only the owner's own weekly look at the dashboard ([owner checklist](deployment.md#owner-checklist)), which no notification prompts.

**R3. Render direct-origin bandwidth.** The onrender.com hostname cannot be disabled.
- Every direct request costs Render outbound bandwidth, even when refused, and `/health` answers anyone without a rate limit.
- A sustained flood could exhaust the workspace's monthly outbound allowance. Render would then suspend the service until the next month (ADR-011, accepted risk 8).
- Mitigations: an unpublished name with a random suffix, minimal 403 bodies, zero database work, and Render's own Cloudflare layer.
- No early warning: reading Render's bandwidth needs an account-wide Render API key, which is never created, so no monitor sees the allowance running out. The [owner checklist](deployment.md#owner-checklist) asks for a weekly look at the workspace's usage, but nothing sends it: the schedule check writes it only into its run summary, and a passing run emails no one. The first automated signal is the suspension itself.

**R4. Role defaults are a backstop, not a boundary.**
- `atus_api`'s defaults are session defaults: `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, `jit` and `default_transaction_read_only`. The role can `SET` each of them itself.
- `tests/deploy/test_stack_roles.py::test_api_role_cannot_write` does exactly that: it turns `default_transaction_read_only` off, then shows that privileges still refuse every write.
- The boundary for writes is the privileges (C9). The time bounds that matter are the per-transaction values and the deadline the API sets (C8).
- Whoever holds the `atus_api` password can run reads for as long as they like, limited only by the role's 10 connections and Neon's quotas.

**R5. Renaming of the workers.dev subdomain.** Cloudflare may rename it "for any or no reason", attempting a week's notice (ADR-011). Every share link would break. Only a custom domain removes this risk, and ADR-011 excludes one.

**R6. Neon project-scoped key with editor rights**, if `NEON_API_KEY` is set.
- The governor uses it only for `GET /projects/{id}`, sent in the `Authorization` header (`edge/test/worker/governor.test.ts`). The key itself can still edit the project.
- A compromised Cloudflare account or Worker could therefore alter the database. Recovery is a restore from the release dump.
- Without the key, the governor runs in its conservative mode.

**R7. Plain HTTP inside Render**, between its load balancer and the container (documented by Render). The edge secret and the visitor's IP cross that hop unencrypted.

**R8. Unverified provider behaviours.** These are the security-relevant items from ADR-011 "Unknowns", each to be measured after the first deploy within its quota budget:
- which headers reach the app through the Worker, and whether `X-ATUS-Client-IP` arrives unchanged; that a client cannot override `CF-Connecting-IP` on workers.dev is assumed, not tested;
- whether `_headers` applies to the SPA-fallback `index.html`. The `<meta>` CSP backstop covers every directive except `frame-ancestors`, which a meta element cannot carry, and `scripts/ops/deploy_smoke.py` reports it;
- whether Cloudflare's 125 s proxy read timeout applies to the Worker→Render hop;
- whether Workers invocation logs record client IPs or headers. They stay off (`invocation_logs: false`, `edge/test/node/config.test.ts`) unless a deliberate check of one record, made while only the owner's requests reach the Worker, shows they do not (ADR-014);
- the minimal Cloudflare token for `wrangler deploy`;
- whether the Free plan can create project-scoped Neon keys;
- whether Neon accepts `ALTER ROLE atus_api SET ...`;
- whether `sslrootcert=system` verifies Neon's live certificate;
- the exact response after the 100k/day Worker limit, and Neon's error when its quota is exhausted. Neither is induced. Only a non-JSON 429 gets the SPA's daily-allowance page; an Error 1027 page with another status does not (a 5xx is retried as a cold start, another 4xx is a generic error). Neon's error maps to `database_paused`;
- what Cloudflare itself answers, before the Worker runs, to an unknown HTTP method, duplicate or malformed `Content-Length` headers, and a declared `Content-Length` larger than the body sent. The Worker's code adds the security headers and the JSON envelope to every response it produces, and refuses a declared `Content-Length` over 64 KiB before reading the body. Responses the runtime gives before invoking it carry neither: locally, `wrangler dev` answered an unknown method with 501 and a duplicate `Content-Length` with 500 (`text/plain`, no security headers), and waited for an under-delivered body; the API's uvicorn answers malformed framing with a plain 400 (review round 1, `probe-abuse`). The bodies are short static text with nothing sensitive.

**R9. The API's limits reset on restart.** Its buckets, daily cap and lanes live in one process, and Render may restart a Free service at any time. The governor's durable state is the primary limit.

**R10. Gaps in scanner coverage.**
- Trivy fails only on HIGH or CRITICAL vulnerabilities that have a fix (`--ignore-unfixed`). Entries in `.trivyignore` (each with a reason and an expiry at most 31 days out, `tests/deploy/test_infra_review_r2.py`) are accepted until they expire; the runbook is [operations.md, "The image scan blocks a deploy"](operations.md#the-image-scan-blocks-a-deploy).
- `pip-audit` covers only the API's runtime lock, not the build, dev or CI-tool locks.
- `npm audit --omit=dev` covers runtime dependencies only. The Worker has none, so the build and deploy tools (`wrangler`, the frontend's `vite`) are covered only by Dependabot and the owner's Dependabot alerts.

**R11. TLS to the database depends on the connection string.** In production the API refuses an `ATUS_DATABASE_URL` whose `sslmode` is not `verify-full` (`src/atus_pipeline/config.py`; `tests/unit/api/test_config.py`). `verify-ca` is refused as well (review round 2): it checks the certificate chain but never the host name, so with a public CA bundle such as the image's `SSL_CERT_FILE` any publicly issued certificate, for any domain, would pass (`tests/unit/api/test_config.py::TestDatabaseTls::test_production_refuses_verify_ca`). The only exception is `ATUS_DATABASE_TLS=local-stack-only`, accepted solely for local hosts (compose service names, loopback, Unix sockets) in the local production-mode stack; a single-label host name is resolved by the container's resolver, which is the residual risk.
- For any non-local host, the restore tool writes the API's URL with `sslmode=verify-full&sslrootcert=system`. Code: `src/atus_pipeline/serving/secrets_file.py::api_database_url`. Test: `tests/unit/test_serving_secrets_file.py::test_api_url_for_a_remote_server_verifies_the_certificate`.
- The owner copies that value to Render.

**R12. The owner machine is fully trusted.** It holds the `neondb_owner` DSN, every generated secret and the provider logins. The repository can only keep those files private (mode 0600) and never print them. Protecting the machine and the accounts is the owner's job.

**R13. A Render suspension looks like a cold start until the owner sets the pause switch.** When Render suspends the API until next month (outbound bandwidth or instance hours used up) or at its discretion (R3; ADR-011, accepted risk 1), what the suspended service answers is undocumented ([free](https://render.com/docs/free), accessed 2026-10-21). The Worker therefore maps it like any non-API answer, to 503 `service_waking` or 502 `upstream_unavailable` (`edge/src/proxy.ts`, `judge`), and cannot tell it from a sleeping host by itself.
- **The pause switch** (review round 2). Once the owner knows, from Render's email or dashboard, `deploy.yml` with operation `pause-api` sets two optional Worker secrets, `API_PAUSED_UNTIL` and `API_PAUSED_REASON` (`edge/src/pause.ts`), without rebuilding anything or calling Render. Every request that would reach Render then gets 503 `api_paused` with the date, before the governor, and the SPA shows one page: "The analysis service is paused", why, until when, and what still works, with no automatic retry (`edge/test/worker/pause.test.ts`; `frontend/src/domain/presentError.test.ts`, `frontend/src/pages/serviceStates.test.tsx`, `frontend/e2e/service-codes.spec.ts`, "api_paused"). Static files, the snapshot, the metadata the Worker answers from it and KV-cached results keep working. The switch ends by itself at its date, or with `resume-api` ([operations.md, "Pausing the API"](operations.md#pausing-the-api)).
- **What remains.** Nothing detects a suspension automatically: no monitor reads Render's usage (R3), and the deep check sees only `service_waking` or `upstream_unavailable`. Until the owner sets the switch, visitors get the cold-start flow: each novel analysis shows the waking notice for the 150 s cold-start budget, then "The analysis service is still not answering", which says that the free hosting may be paused, for example until next month, and that starter examples and earlier analyses still open, with "Try again" (`frontend/src/domain/presentError.ts`; `presentError.test.ts`, "a host that stays down past the cold-start budget"). Each such analysis makes about 15–30 Worker requests, KV reads and governor admissions. They are refunded from the global count but count toward the network's 300 daily admissions (`PREFIX_DAILY_ADMISSIONS`), so after roughly 10–20 analyses a visitor's network sees the per-network daily-limit page instead. A switch set with `unknown` stays on after Render is back, until the owner clears it. Runbooks: [operations.md, "A quota is exhausted"](operations.md#a-quota-is-exhausted) and ["Render suspends the service"](operations.md#render-suspends-the-service).

**R14. Link-supplied labels (content spoofing).** A share link is stateless, so whoever writes it chooses the label text the site shows as the heading and in the result sentence: up to 120 characters for the activity and 80 per group (`src/atus_pipeline/api/schemas/analysis.py`).
- The numbers are always those of the link's codes and populations. The "How was this calculated?" panel lists them (`frontend/src/features/results/MethodologyPanel.tsx`, `EstimateView.tsx`, `CompareView.tsx`), but it is collapsed by default, so a reader who does not open it cannot tell a misleading label from what was computed.
- Labels are rendered as text with no markup (abuse case 4), and nothing is stored, so a link can only mislead whoever opens it.
- Showing the codes, or a "label set by this link" hint, next to a custom label would narrow this. It is not implemented.

## Verification status

- **Done locally:**
  - unit and integration tests;
  - container tests (`ATUS_DOCKER_TESTS=1`; in CI, `.github/workflows/image.yml` runs them);
  - Worker tests in workerd;
  - Playwright in Chromium and WebKit (the last recorded run is in stack mode, through the local edge; [frontend.md](frontend.md#testing)), and in Firefox, which does not launch natively on the development machine, through the official Playwright Linux image (vite-preview mode, `scripts/e2e_in_container.sh`);
  - the production compose stack behind `wrangler dev`: `scripts/smoke_stack.sh --edge`, with the checks in `deploy/stack/edge_probe.py`.
- **Not done yet on GitHub:** the CI workflows that run all of the above, including the Firefox projects, have no recorded run.
- **Not done, because nothing is deployed:**
  - `scripts/ops/deploy_smoke.py` on the real site: the site's headers, the SPA fallback's `<meta>` CSP, the Worker's headers on an `/api` error, and the origin lock;
  - the R8 measurements.
