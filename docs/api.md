# API Reference (Phase 3)

A read-only, versioned HTTP service around the Phase 2 analytical engine.
The API validates requests, calls `AnalysisEngine`, serializes its result
objects, maps domain errors to HTTP, and caches deterministic results — it
contains **no statistical logic**. Everything an estimate means (weights,
denominators, variance, 2020 rules) is defined in
[analytics.md](analytics.md) and computed by the engine the CLI also uses.

```text
CLI ────────┐
            ├──→ AnalysisEngine (Phase 2) ──→ PostgreSQL (Phase 1)
HTTP API ───┘
```

## Running locally

```bash
docker compose up -d                # PostgreSQL (see README for load steps)
atus api                            # http://127.0.0.1:8000
atus api --port 8080 --reload       # dev mode
python -m atus_pipeline.api.serve   # the same server without the CLI
```

`atus api` runs one uvicorn process with the production settings described
in [ADR-012](adr/ADR-012-load-and-quota-protection.md#process): it listens on
`$PORT`, keeps proxy headers off, sends no `server` header, bounds
connections and shuts down gracefully within 20 s. It binds to 127.0.0.1 in
development and 0.0.0.0 when `ATUS_ENV=production`.

Interactive OpenAPI docs are at <http://127.0.0.1:8000/docs>, with the spec
at `/openapi.json`; both are off in production (see
[Production exposure](#production-exposure)).

Startup is instant and opens no database connection. The pool connects, and
the in-process data snapshot is read, when the first request needs data.
`/health` answers at once; `/ready` (operators only) tells whether the data
can be served. Readiness assumes the database passed `atus validate-db` at
load time; it does not re-run data-quality checks.

### Configuration

In development, configuration uses the pipeline's `.env` mechanism: a `.env`
file in the working directory or a parent. In production
(`ATUS_ENV=production`) only real environment variables count.

Every value is validated at startup. A malformed value stops the process
with a message naming the variable, and the message never echoes a secret.

| Variable | Default (dev) | Production | Meaning |
| --- | --- | --- | --- |
| `ATUS_ENV` | `development` | `production` | production requires `ATUS_DATABASE_URL` and `ATUS_EDGE_SECRETS` (refuses to start otherwise); JSON logs, docs off, HSTS |
| `ATUS_DATABASE_URL` | compose instance on :5434 | **required** | analytical database; production never falls back to the development URL |
| `ATUS_LOG_LEVEL` | `INFO` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `ATUS_LOG_FORMAT` | `text` | `json` | `text` or `json` (one object per line; [ADR-014](adr/ADR-014-observability.md)) |
| `ATUS_API_DOCS` | `on` | `off` | `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json` |
| `ATUS_EDGE_SECRETS` | *(empty: no edge auth)* | **required** | comma-separated accepted values of `X-ATUS-Edge-Auth` (current,next); each at least 32 characters in production |
| `ATUS_EXPECTED_DATA_VERSION` | *(empty)* | set | the data version this deployment must serve; any other gets 503 `data_version_mismatch` on every data route |
| `ATUS_OPS_TOKEN` | *(empty)* | set | bearer token for `/ready` and `/metrics`; empty, both answer like unknown paths. At least 32 characters in production |
| `ATUS_API_CORS_ORIGINS` | *(empty: no cross-origin access)* | *(empty: same origin)* | comma-separated exact origins, e.g. `http://localhost:5173` (the ATUS Explorer dev server); never `*` |
| `ATUS_API_CACHE_SIZE` | `256` | `256` | analysis-result cache entries (LRU) |
| `ATUS_API_POOL_MAX_SIZE` | `4` | `4` | pool connections |
| `ATUS_API_POOL_MAX_IDLE_S` | `240` | `240` | idle period after which the pool closes one idle connection |
| `ATUS_API_POOL_TIMEOUT_S` | `5` | `5` | wait for a busy pool, then 503 `overloaded` |
| `ATUS_API_CONNECT_TIMEOUT_S` | `10` | `10` | libpq `connect_timeout` |
| `ATUS_API_STATEMENT_TIMEOUT_MS` | `85000` | `85000` | per-transaction `statement_timeout` for analyses |
| `ATUS_API_META_STATEMENT_TIMEOUT_MS` | `10000` | `10000` | per-transaction `statement_timeout` for metadata and lexicon reads |
| `ATUS_API_JIT` | `off` | `off` | per-transaction `jit` |
| `ATUS_API_MAX_PARALLEL_WORKERS` | *(unset: server default)* | *(unset: server default)* | per-transaction `max_parallel_workers_per_gather`; left unset by the §13 calibration ([ADR-012](adr/ADR-012-load-and-quota-protection.md#calibration-13)) |
| `ATUS_API_PREPARE_THRESHOLD` | `5` | `5` | psycopg auto-prepare threshold; `none` behind a transaction-mode pooler |
| `ATUS_API_MAX_INFLIGHT` | `2` | `2` | uncached analyses computing at once |
| `ATUS_API_MAX_INFLIGHT_HEAVY` | `1` | `1` | how many of those may be heavy |
| `ATUS_API_ADMISSION_WAIT_S` | `5` | `5` | wait for a lane, then 503 `overloaded` |
| `ATUS_API_RATE_ANALYSIS` | `6/60` | `6/60` | per-client bucket for analyses that compute, as `requests/seconds` (burst = requests) |
| `ATUS_API_RATE_REQUESTS` | `120/60` | `120/60` | per-client bucket for every request except `/health` |
| `ATUS_API_DAILY_ANALYSIS_CAP` | `600` | `600` | uncached analyses per UTC day in this process |
| `ATUS_API_MAX_BODY_BYTES` | `65536` | `65536` | request body cap |
| `ATUS_API_THREADS` | `16` | `16` | worker threads for blocking database work |
| `ATUS_API_SNAPSHOT_TTL_S` | `2` | `600` | how old the in-process data snapshot (data version, years, lexicon) may get before a request reads it again; `0` reads it on every data request |
| `ATUS_BUILD_SHA` | `dev` | image build arg | build identifier, reported by `/health` and `/metrics` |
| `PORT` | `8000` | set by the platform | listening port |

## Endpoints

| Method & path | Purpose |
| --- | --- |
| `POST /api/v1/analysis/estimate` | one (possibly pooled multi-year) weighted estimate |
| `POST /api/v1/analysis/trend` | per-year series with explicit unavailable points |
| `POST /api/v1/analysis/compare` | two populations + covariance-correct difference |
| `GET /api/v1/activities` | lexicon listing (`?level=`, `?parent=`, `?search=`) |
| `GET /api/v1/activities/presets` | named selections incl. BLS table categories |
| `GET /api/v1/activities/{code}` | one code with ancestry, children, leaf count |
| `GET /api/v1/population/metadata` | supported population filters + valid values |
| `GET /api/v1/meta` | API/analytics/data versions + capability discovery |
| `GET /health` | liveness and build (unversioned, public, no database) |
| `GET /ready` | readiness, operators only (`Authorization: Bearer <ATUS_OPS_TOKEN>`) |
| `GET /metrics` | Prometheus metrics, operators only (same bearer) |

Every `GET` route also answers `HEAD`. Paths have exactly one spelling:
a trailing slash is a 404, never a redirect. `/ready` and `/metrics` are not
in the OpenAPI schema; see [Observability](#observability).

The API is strictly read-only: no CRUD, no persistent analysis storage, no
user authentication (public analytical access), no raw-microdata download.
It exposes domain concepts, never database tables. In production it is
reachable only through the edge Worker, which proves itself with a shared
secret (see [Protection and limits](#protection-and-limits)).

## Analysis requests

The request body **is** the canonical Phase 2 analysis specification — the
`spec` field of every response can be resubmitted verbatim. Example files
live in [examples/api/](examples/api/) and are also embedded in the OpenAPI
schema.

```bash
curl -X POST http://localhost:8000/api/v1/analysis/estimate \
  -H "Content-Type: application/json" \
  -d @docs/examples/api/sleep_2025_estimate.json
```

```json
{
  "measure": "average_minutes_per_day",
  "activity": {"preset": "sleep"},
  "years": [2025],
  "population": {"age_min": 25, "age_max": 54},
  "weights": "multiyear",
  "variance": "replicate",
  "confidence_level": 0.95
}
```

Field notes (full semantics in [analytics.md](analytics.md)):

- `activity` — `{"preset": name}` (see `/activities/presets`) **or**
  `{"include": [...], "exclude": [...], "label": ...}` with 2/4/6-digit
  lexicon codes; a tier code means itself plus all descendants.
- `years` — order-insensitive (canonicalized ascending); duplicates rejected.
- `population` — any subset of the dimensions in `/population/metadata`;
  filtering on a dimension excludes respondents missing that dimension.
- `weights` — `multiyear` (TUFNWGTP; **refuses 2020**) or `pandemic`
  (TU20FWGT; 2019/2020 only, collection-window semantics).
- `variance: "none"` skips replicate SEs (faster; `standard_error` is null).

Limits: ≤ 50 years per request; ≤ 100 `include` and ≤ 100 `exclude` codes
per activity selection. Bodies with unknown fields are rejected (422) to
catch typos.

## Responses

Responses are the engine result objects' `to_dict()` — identical structure to
the CLI's `--json` output. Key semantics:

- `estimate.value` + `estimate.unit` (`minutes_per_day`,
  `proportion_of_population` 0–1, `minutes_per_day_of_participants`,
  `persons_per_day`) with full float precision — formatting is the client's job.
- `n_respondents` / `n_participants` are **unweighted sample sizes**;
  `weighted_population_per_day` is the persons represented on an average day.
  These are never collapsed into an ambiguous "count".
- `warnings` are methodological notes clients should display (2020 window
  semantics, CI truncation, harmonized coding) — cached responses carry
  exactly the same warnings.
- Trend points for years that cannot be estimated have `estimate: null` plus
  `unavailable_reason` — never zero.
- `spec` is the canonical analysis specification; with the same
  `analytics_version` and data release (see `/meta`), resubmitting it
  reproduces the result.

Response headers:

- `X-Request-ID`: log correlation. It is the edge's id when the request came
  through the edge, and is generated otherwise.
- `X-Analysis-Key`: the deterministic identifier of the analysis (SHA-256 of
  operation + canonical spec + analytics version + data version). It is a
  reproducibility/cache key, not a stored record ID.
- `X-Cache`: one of
  - `hit`: served from the cache;
  - `miss`: computed and cached;
  - `join`: shared an identical computation that was already running;
  - `bypass`: computed during a data reload and not cached.
- `X-Data-Version`, `X-Analytics-Version` and `X-Response-Format` (`1`) on
  analyses: what the response bytes depend on. Edge caches key on them.
  `X-Data-Version` is also on `/meta`, `/activities` and
  `/activities/{code}`, and `X-Analytics-Version` on `/meta`.
- `Server-Timing: db;dur=<ms>, app;dur=<ms>` on every response: time spent
  in database transactions (including waiting for a connection) and the rest,
  up to the response start. `db;dur=0` exactly means the request opened no
  database transaction (cache hits, single-flight joins, every 4xx, shed
  requests, metadata from the snapshot); any database work reports at least
  `0.1`. The edge governor charges compute only when it is not 0.
- `Retry-After` (seconds) on every 429 and every 503 the API answers. The
  one exception is uvicorn's own connection-limit 503 (beyond
  `limit_concurrency`, before any app code runs): plain text
  `Service Unavailable`, no envelope, no `Retry-After` (ADR-012). The edge
  Worker turns exactly that answer into a 503 `overloaded` envelope with
  `Retry-After: 10`.
- No `/api` path is ever answered with a redirect (`redirect_slashes` is off;
  a trailing slash, a double slash or a case variant is a 404 envelope), and
  every error, including the framework's 404/405, is the JSON envelope with
  `Content-Type: application/json`.
- Security headers on every response:
  - `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`;
  - `X-Content-Type-Options: nosniff`;
  - `Referrer-Policy: no-referrer`;
  - `Cross-Origin-Resource-Policy: same-origin`;
  - `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`
    (the value the edge Worker also sets);
  - `Strict-Transport-Security`, in production only.
- Errors carry `Cache-Control: no-store`, and so do `/health`, `/ready` and
  `/metrics`.
- There is no `server` header.

Responses of 1 KB or more are gzipped for clients that send
`Accept-Encoding: gzip`.

## Errors

One envelope everywhere:

```json
{"error": {"code": "unsupported_analysis", "message": "…", "details": null}}
```

| Status | `code` | When |
| --- | --- | --- |
| 400 | `malformed_json` | body is not valid JSON, or the parser gave up on it (e.g. nested too deeply) |
| 403 | `forbidden_origin` | edge authentication is on and the request has no valid `X-ATUS-Edge-Auth` (every path except `/health`; `/ready` and `/metrics` also pass with a valid ops bearer) |
| 404 | `not_found` | no such route, including a trailing-slash spelling; also `/ready` and `/metrics` without a valid ops bearer, for every method |
| 404 | `activity_not_found` | `GET /activities/{code}` for a nonexistent code |
| 405 | `method_not_allowed` | wrong method; the `Allow` header lists the right one |
| other 4xx | `http_<status>` | any other framework-generated status (none is expected from the documented routes) |
| 413 | `payload_too_large` | body over `ATUS_API_MAX_BODY_BYTES`, declared or streamed |
| 415 | `unsupported_media_type` | a POST body that is not `application/json` |
| 422 | `validation_error` | schema violation: wrong types, enums or unknown fields, or control characters in query parameters. `details.errors` locates each problem: at most 20, with `details.truncated` and `details.error_count` when there were more |
| 422 | `invalid_spec` | structurally valid but domain-invalid (age_min > age_max, duplicate years, unknown preset) |
| 422 | `unknown_activity` | activity code not in the harmonized lexicon |
| 422 | `unsupported_analysis` | methodologically unsupported (e.g. 2020 under `multiyear`); the message explains the alternative |
| 422 | `insufficient_data` | empty population / subgroup too small for replicate variance |
| 429 | `rate_limited` | this client's request or analysis bucket is empty (`details.scope: "client"`) |
| 503 | `overloaded` | no analysis lane within the admission wait, every pool connection busy past `ATUS_API_POOL_TIMEOUT_S` (pool timeout), the database refused for capacity (locks, deadlock, out of resources), or a metadata query was cancelled |
| 503 | `analysis_timeout` | the analysis hit the statement timeout or the request deadline; never cached |
| 503 | `daily_capacity_reached` | the day's uncached-analysis cap is used; `Retry-After` is the time until 00:00 UTC (`details.scope: "daily"`) |
| 503 | `database_paused` | the database provider refuses connections because a usage quota is exhausted (`details.scope: "monthly"`) |
| 503 | `database_unavailable` | no connection could be opened (refused, unreachable, timed out connecting), or the server dropped it (restart, admin shutdown) |
| 503 | `data_not_loaded` | the database has no loaded data (analysis endpoints and `/ready`) |
| 503 | `data_version_mismatch` | the database serves another data version than `ATUS_EXPECTED_DATA_VERSION`: every data route (analyses, `/meta`, `/activities`, `/activities/{code}`, `/ready`), until the deployment and the database agree (`details.scope: "global"`) |
| 503 | `schema_not_initialized` | schema missing (any database-backed endpoint; also `/ready`) |
| 500 | `internal_error` | unexpected failure; details are logged server-side, once, and never returned |

Every 429 and every 503 in this table carries `Retry-After` and
`details.retry_after_seconds`, and `details.scope` where a limit applies
(`client`, `global`, `daily`, `monthly`). All these codes are additive
within v1, and the envelope is unchanged. Messages are generic: operator
hints (run `atus migrate`, `atus load`) and the versions involved in a
mismatch go to the log only.

The engine remains the authoritative validator: API-level validation exists
for fast, well-located client errors, and anything it cannot check cheaply
(lexicon membership, weight-scheme rules, population feasibility) flows
through to the engine and comes back as a structured 422.

## Caching

Server-side: complete successful analysis payloads are cached in an
in-process, thread-safe LRU keyed by

```text
sha256(operation + canonical spec + analytics_version + data_version)
```

where `data_version` identifies the served data (see
`atus_pipeline.database.versions`: the latest succeeded ingestion run's
explicit `data_version`, or `<release>:run<id>:<start timestamp>` for runs
written before that column existed). Reloading data or changing the
statistical implementation therefore changes the key; no TTLs are needed for
correctness, and the LRU bounds memory. Equivalent requests share keys (years and code lists are
canonicalized), errors are never cached, a cache hit returns the
byte-identical payload including warnings, and a cache failure degrades to
recomputation — never to an error. The backend is behind a small protocol so
a multi-process deployment can swap in a shared store (e.g. Redis) later;
in-process is documented as the single-process development default.

The data version used in the key comes from the in-process data snapshot
(below), so a cache hit costs no database statement.

Concurrent-reload guard: if `atus load` finishes while a request is
computing, the data version is re-read in the computation's own transaction
before storing. If it differs from the snapshot's, the result is returned
uncached (`X-Cache: bypass`) instead of being stored under the pre-reload
key, and the snapshot is read again on the next request. With
`ATUS_EXPECTED_DATA_VERSION` set, such a result is refused with 503
`data_version_mismatch` instead. Residual caveat: a computation that started
from a snapshot taken before the reload can use that snapshot's years and
lexicon, and a request whose statements straddle the reload commit can
*observe* mixed data once (standard READ COMMITTED behavior); such results
are never cached, so they cannot persist. In production the data never
changes in place (ADR-013).

Single-flight: concurrent requests for the same key share one computation.
Every request after the first gets `X-Cache: join`.

The computation is detached from the request that started it. If that
client disconnects, the admitted analysis still finishes and is cached, so a
retry either finds it (`hit`) or joins it.

HTTP: metadata endpoints (`/meta`, `/activities*`, `/population/metadata`)
send `Cache-Control: public, max-age=300`. Analysis responses are POST
(bodies too complex for GET) and rely on the server-side cache; ETags are
deliberately not implemented since conditional requests don't apply to POST
in practice.

Measured effect (Phase 3: in-process client, full 2003–25 database on a laptop, Sept 2026). These numbers are historical: the Phase 5 audit could not reproduce them on today's code and data (F69), and they say nothing about the free-tier hosts, where the same requests take far longer (see [`operations.md`](operations.md)). They are kept only to show the hit-versus-miss ratio:

| Request | uncached | cached |
| --- | --- | --- |
| estimate, 1 year, `variance: none` | 0.31 s | 0.006 s |
| estimate, 1 year, replicate SE | 0.97 s | 0.005 s |
| estimate, pooled 22 years, replicate SE | 11.0 s | 0.004 s |
| trend 2003–2025 with per-year SEs | 11.7 s | 0.003 s |
| compare (two groups, SEs) | 1.7 s | 0.005 s |

Expensive multi-year variance requests stay **synchronous** (no job queues,
polling, or websockets): the worst case is one long request per
(spec × version), then cached. On the free-tier database that can mean tens
of seconds: on the emulation of Neon's 0.25 CU floor, pooled and trended
worst cases took 21–22 s at p50, and the F66 broad selector's compare by sex
up to 71 s ([load tests](../loadtests/results/2026-10-21-emulation.md),
2026-10-21; about 3 s at 2 CU). The statement timeout and the request
deadline bound them (ADR-012).

### In-process data snapshot

The API keeps the data version, the latest ingestion run, the loaded years,
the respondent count and the activity lexicon in memory, read in one
consistent (`REPEATABLE READ`) transaction when a request needs them and they
are older than `ATUS_API_SNAPSHOT_TTL_S`. With `ATUS_EXPECTED_DATA_VERSION`
set (production), a snapshot of exactly that version is **pinned**: it is
never read again for its age, so metadata requests cost no statement after
the first read of each process (only an analysis that sees the version
change forces a new read). If a read fails while an earlier snapshot exists,
that snapshot keeps answering (**stale if error**), and the database is tried
again after at most 30 s; uncached analyses still need the database and fail
with the usual 503. `/ready` never uses a pinned or stale snapshot. While the
snapshot is usable:

- `/meta`, `/activities`, `/activities/{code}` and `/population/metadata`
  answer from memory;
- analysis cache hits and every 4xx run no statement;
- uncached analyses reuse its activity resolver and years, so they run only
  their statistics queries and the version recheck.

The lexicon search returns exactly what the SQL `ILIKE` returned, for ASCII
and non-ASCII terms alike (proved by an integration test over thousands of
terms): the term is lowered with the database's own `lower()` of every cased
character, read with the snapshot. Only a database that lowers by context
(an ICU collation) sends search terms to that SQL.
Details: [ADR-012](adr/ADR-012-load-and-quota-protection.md#in-process-data-snapshot-answers-that-cost-nothing-f14).

## Versioning

Three independent versions, all visible in `GET /api/v1/meta` (and its
`X-Data-Version` / `X-Analytics-Version` headers):

| Version | Meaning | Changes when |
| --- | --- | --- |
| `api_version` (`v1`, path prefix) | HTTP contract | request/response semantics change incompatibly |
| `analytics_version` (`0.2`) | statistical implementation | computed numbers could differ |
| data version (`X-Data-Version` on analyses and metadata) | loaded BLS data | `atus load` or a serving-DB build produces new data |

## Protection and limits

The full design, with the values and their sources, is in
[ADR-012](adr/ADR-012-load-and-quota-protection.md). In short, in the order
a request meets them:

1. **Body size.** Declared and streamed bodies over 64 KiB get 413.
2. **Edge authentication.** With `ATUS_EDGE_SECRETS` set (always, in
   production), every path except `/health` needs `X-ATUS-Edge-Auth` equal
   to one of the configured values.
   - The comparison is constant-time.
   - Two values are accepted, which allows rotation.
   - Anything else gets 403 `forbidden_origin`, with no database work.
3. **Client identity.** This is `X-ATUS-Client-IP` when the request is
   edge-authenticated, and the socket peer otherwise. An edge-authenticated
   request without a usable `X-ATUS-Client-IP` is charged to one shared
   bucket (`edge:unknown`), never to the peer, which behind the edge is the
   platform's proxy. `X-Forwarded-For`, `Forwarded`, `CF-Connecting-IP` and
   `True-Client-IP` are ignored.
4. **Per-client buckets.** 120 requests per minute, and 6 *computed*
   analyses per minute. Cache hits and joins are free. Over a limit: 429
   with `Retry-After`.
5. **Media type and shape.** POST bodies must be JSON (415). Control
   characters in query parameters are rejected (422).
6. **Data snapshot.** Data routes answer from the in-process snapshot; with
   `ATUS_EXPECTED_DATA_VERSION` set, another data version gets 503
   `data_version_mismatch`.
7. **Dry run.** An analysis the engine would refuse before its first query
   (unknown activity code, year not loaded, 2020 under the multi-year
   weights) gets its 422 without spending a token, a lane or a statement.
8. **Daily cap.** 600 uncached analyses per UTC day per process, then 503.
9. **Lanes.** At most 2 analyses compute at once, and at most 1 of them
   heavy. An analysis is heavy at 8 or more year-queries (`len(years)`,
   doubled for compare). Others wait up to 5 s, then get 503 `overloaded`.
10. **Bounded queries.** Each transaction is read-only and runs under
   `SET LOCAL`-style limits: `statement_timeout` 85 s for analyses and 10 s for metadata,
   `lock_timeout` 5 s, `jit` off. An analysis is also cancelled after a 90 s
   deadline, giving 503 `analysis_timeout`.

**Development convenience.** Without edge secrets (never the case in
production), loopback peers and in-process test clients are not per-client
rate limited. Local tools, the E2E backend and benchmark replays therefore
work unchanged. The daily cap and the lanes always apply.

ADR-012 lists what this does not cover: distributed abuse, direct-origin
bandwidth, and idle connections held open in front of uvicorn. The edge
Worker and its quota governor are the primary, durable layer
([ADR-015](adr/ADR-015-edge-cache-snapshot-and-governor.md)).

### Statements per request

Counted by the API for every request (the access line's `db_statements`, the
`atus_db_statements_per_request` histogram) and checked against PostgreSQL's
own `log_statement=all` log in `tests/integration/api/test_statement_log.py`.
The API's count includes the pool's connection check (an empty query) and the
per-transaction limits; PostgreSQL's log adds the `BEGIN` and `COMMIT`
psycopg sends around each transaction.

| Request (snapshot fresh unless noted) | API count | PostgreSQL log |
| --- | --- | --- |
| first data request after the TTL (never again while pinned): reads the snapshot (check, limits, version, run, years, respondents, lexicon, lowering table, the resolver's 3 lexicon reads) | +11 | +13 |
| `/meta`, `/activities` (any search term), `/activities/{code}` (also 404), `/activities/presets`, `/population/metadata` | 0 | 0 |
| analysis cache hit, or single-flight join | 0 | 0 |
| any 400, 403, 404, 405, 413, 415, 422 or 429 | 0 | 0 |
| uncached estimate or trend: check, limits, statistics query, version recheck | 4 | 6 |
| uncached compare: one statistics query per group | 5 | 7 |
| `/health`, `/metrics`; `/ready` with a fresh snapshot | 0 | 0 |

Before Phase 5 a cache hit ran 3 statements, a 422 3 to 7, `/meta` 5 and an
uncached estimate 9 (audit F14).

## Security posture

Every endpoint is read-only and uses parameterized queries only. Client input
is never interpolated into SQL:

- activity codes travel as bound parameters;
- search terms are LIKE-escaped;
- identifiers come from fixed internal vocabularies;
- the per-transaction limit names are constants, and their values are bound
  parameters.

CORS is off by default. It is enabled per exact origin via
`ATUS_API_CORS_ORIGINS`, with no credentials, and the contract headers are
exposed.

Errors never expose stack traces, SQL, paths or configuration. 422s are
capped and never echo request values.

Configured secrets (database password, edge secrets, ops token) are scrubbed
from log records. A malformed `ATUS_DATABASE_URL` is rejected at startup
without being echoed.

There is no user authentication: the data is public statistics and there is
no user state.

Every database transaction is `READ ONLY` (the API never writes; I05), so
even a role that could write cannot be made to by the API. `/ready` failures
are ordinary envelopes; operator hints go to the log.

## Production exposure

| Path | Production | Why |
| --- | --- | --- |
| `/health` | public, no edge secret needed | the platform's health check and the deep monitor; touches nothing, reports only `status` and `build` |
| `/docs`, `/docs/oauth2-redirect`, `/redoc` | off (`ATUS_API_DOCS=off`): 404 | they load third-party scripts from a CDN without SRI (F43) and serve no user |
| `/openapi.json` | off: 404 | the same schema is published as a static file, `frontend/openapi.json`, regenerated by `scripts/export_openapi.py` (which needs no docs route) and checked by a test |
| `/ready` | bearer `ATUS_OPS_TOKEN`, else 404 like an unknown path | it can touch the database and names data versions; the Worker never routes it (F16) |
| `/metrics` | bearer `ATUS_OPS_TOKEN`, else 404 like an unknown path | operational detail; the Worker never routes it |

With edge authentication on, a request to `/ready` or `/metrics` without a
valid bearer is refused like any other unknown path (403
`forbidden_origin` without the edge secret, 404 with it); a valid bearer is
enough on its own, so an operator can reach the origin directly.

## Observability

Full design: [ADR-014](adr/ADR-014-observability.md).

- **Logs.** One access line per request (logger `atus.api.access`): request
  id, method, route template, path, status, latency, database time and
  statements, cache outcome, analysis key, error code, the client's IPv4 /24
  or IPv6 /48, edge authentication, disconnect. JSON in production
  (`ATUS_LOG_FORMAT=json`), text in development. Never bodies, specs, query
  strings or full client addresses. uvicorn's access log is off. Every
  5 minutes with traffic, a `metrics` event logs the headline counters.
- **Metrics.** `GET /metrics` with the ops bearer: Prometheus text format
  with request counts and latency histograms per route, error codes, cache
  hits/misses/bypasses/joins, in-flight analyses, lane waits and sheds,
  rate-limit and daily-cap rejections, query timeouts, statements per
  request, pool statistics and the build.
- **`Server-Timing`** on every response (db and app time); the edge Worker
  logs it and its governor uses it.
- **Health.** `/health` for the platform and monitors; `/ready` for
  operators. No monitor touches the database.

## Testing

```bash
pytest tests/unit/api                      # contract, errors, cache, protection (no DB)
ATUS_TEST_DATABASE_URL=postgresql://atus:atus@localhost:5434/atus_test \
  pytest tests/integration/api tests/integration/api_prod
                                           # full stack, pool, timeouts, real uvicorn server
ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_api_benchmarks.py
                                           # BLS benchmarks replayed through HTTP
python scripts/benchmark_api.py            # performance baseline
```

Protection tests, without a database (`tests/unit/api/`):

- `test_guard.py`: 413, 415, 403, 429 and 422, headers, HEAD;
- `test_admission.py`: analysis bucket, daily cap, lanes, single-flight, error mapping;
- `test_snapshot.py`: the data snapshot, zero-statement paths, the dry run, the expected data version;
- `test_observability.py`: access log, IP truncation, JSON logs, `Server-Timing`, `/health`, `/ready`, `/metrics`;
- `test_exposure.py`: the docs switch, the OpenAPI snapshot, no internals in error bodies;
- `test_protection.py`, `test_database_unit.py`;
- `test_config.py`: includes production refusal;
- `test_serve_and_logging.py`.

Against PostgreSQL:

- `integration/api/test_protection_db.py`: transaction-local limits, timeouts, pool exhaustion, terminated backends, zero-statement 403s, an empty database;
- `integration/api/test_snapshot_db.py`: Python lexicon search and detail identical to the SQL, statistics unchanged with the snapshot's resolver and years, read-only transactions, statements per request;
- `integration/api/test_statement_log.py`: statements per request in PostgreSQL's own log (starts a throwaway `postgres:16` container, `atus-wsa-stmt` on port 5461; skipped without Docker);
- `integration/api_prod/test_server.py`: the production server over real sockets, including a client that disconnects, and `atus api` logging JSON in production mode.
