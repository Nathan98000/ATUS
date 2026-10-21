# API Reference

A read-only, versioned HTTP service around the Phase 2 analytical engine
(built in Phase 3, hardened in Phase 5). This page describes the API process
itself (`src/atus_pipeline/api/`). The public deployment puts the edge Worker
(`edge/src/`) in front of it, which changes some paths, headers and error
codes: see [Through the edge Worker](#through-the-edge-worker). Nothing is
deployed yet; everything below is implemented and tested locally.

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
in [ADR-012](adr/ADR-012-load-and-quota-protection.md#process)
(`src/atus_pipeline/api/serve.py`): it listens on `$PORT`, keeps proxy
headers off, sends no `server` header, closes idle keep-alive connections
after 5 s, sheds connections beyond `limit_concurrency` (`max(32, 4 ×
ATUS_API_THREADS)`, so 64 by default) and shuts down gracefully within 20 s.
It binds to 127.0.0.1 in development and 0.0.0.0 when `ATUS_ENV=production`.
`--reload` is refused in production.

Interactive OpenAPI docs are at <http://127.0.0.1:8000/docs>, with the spec
at `/openapi.json`; both are off in production, where the public site serves
the committed schema as a static file instead (see
[Production exposure](#production-exposure)).

Startup is instant and opens no database connection. The pool connects, and
the in-process data snapshot is read, when the first request needs data.
`/health` answers at once; `/ready` (operators only) tells whether the data
can be served. Readiness assumes the data was validated before it went live
(`atus validate-db` on the full database; by default `atus
build-serving-db` refuses a source without recorded validation results,
ADR-013); it does not re-run data-quality checks.

### Configuration

In development, configuration uses the pipeline's `.env` mechanism: a `.env`
file in the working directory or a parent. In production
(`ATUS_ENV=production`) only real environment variables count.

Every value is validated at startup. A malformed value stops the process
with a message naming the variable, and the message never echoes a secret.

| Variable | Default (dev) | Production | Meaning |
| --- | --- | --- | --- |
| `ATUS_ENV` | `development` | `production` | production requires `ATUS_DATABASE_URL` and `ATUS_EDGE_SECRETS` (refuses to start otherwise) and refuses an `ATUS_DATABASE_URL` whose `sslmode` does not verify the server; JSON logs, docs off, HSTS |
| `ATUS_DATABASE_URL` | compose instance on :5434 | **required** | analytical database; production never falls back to the development URL and refuses it if set explicitly. In production the URL itself must set `sslmode=verify-full` (with `sslrootcert=system` for a public CA) or `verify-ca`, or the API refuses to start ([deployment.md, "TLS to the database"](deployment.md#tls-to-the-database)) |
| `ATUS_DATABASE_TLS` | `verify` | *(unset: `verify`)*; never set on Render | `local-stack-only` lifts the `sslmode` rule for the local production-mode stack, whose Postgres has no TLS. It is itself refused unless every host in `ATUS_DATABASE_URL` is a compose service name without dots, `localhost` or a loopback address, `host.docker.internal`, or a Unix socket ([deployment.md](deployment.md#tls-to-the-database)) |
| `ATUS_LOG_LEVEL` | `INFO` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `ATUS_LOG_FORMAT` | `text` | `json` | `text` or `json` (one object per line; [ADR-014](adr/ADR-014-observability.md)) |
| `ATUS_API_DOCS` | `on` | `off` | `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json` |
| `ATUS_EDGE_SECRETS` | *(empty: no edge auth)* | **required** | comma-separated accepted values of `X-ATUS-Edge-Auth` (current,next; at most 2), visible ASCII without commas; each at least 32 characters in production |
| `ATUS_EXPECTED_DATA_VERSION` | *(empty)* | set | the data version this deployment must serve (at most 200 characters); any other gets 503 `data_version_mismatch` on every data route. Unset in production, the API logs a warning at startup |
| `ATUS_OPS_TOKEN` | *(empty)* | set | bearer token for `/ready` and `/metrics`; empty, both answer like unknown paths. At least 32 characters in production |
| `ATUS_API_CORS_ORIGINS` | *(empty: no cross-origin access)* | *(empty: same origin)* | comma-separated exact origins, e.g. `http://localhost:5173` (the ATUS Explorer dev server); never `*` |
| `ATUS_API_CACHE_SIZE` | `256` | `256` | analysis-result cache entries (LRU) |
| `ATUS_API_POOL_MAX_SIZE` | `4` | `4` | pool connections; must exceed `ATUS_API_MAX_INFLIGHT` (startup refuses otherwise), so metadata reads and cache lookups always find a connection |
| `ATUS_API_POOL_MAX_IDLE_S` | `240` | `240` | psycopg_pool closes at most one idle connection per period of this length (the pool keeps no minimum), so idle connections can outlive Neon's 300 s suspend; the first checkout after a suspend finds them dead, closes them all and opens at most two |
| `ATUS_API_POOL_TIMEOUT_S` | `5` | `5` | wait for a connection while every pool connection is busy, then 503 `overloaded`; while a new connection is being opened the wait is this plus `ATUS_API_CONNECT_TIMEOUT_S` |
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
| `ATUS_BUILD_SHA` | `dev` | image build arg | build identifier (1-64 characters of `[A-Za-z0-9._-]`), reported by `/health` and `/metrics` |
| `PORT` | `8000` | set by the platform (the image defaults to `10000`) | listening port |

`WEB_CONCURRENCY` is not read: `atus api` always runs one process, because
the protection state, the result cache and the single-flight map are per
process.

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

Every `GET` route of the API process also answers `HEAD` (the edge Worker
does not; see below). Paths have exactly one spelling: a trailing slash is a
404, never a redirect. `/ready` and `/metrics` are not in the OpenAPI schema;
see [Observability](#observability). `GET /activities` takes `level` (1-3),
`parent` (a 2- or 4-digit code) and `search` (2-80 characters).

The API is strictly read-only: no CRUD, no persistent analysis storage, no
user authentication (public analytical access), no raw-microdata download.
It exposes domain concepts, never database tables. In the production
configuration it is reachable only through the edge Worker, which proves
itself with a shared secret (see [Protection and limits](#protection-and-limits)),
except `/health` and, with the ops bearer, `/ready` and `/metrics`.

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
- Defaults (`src/atus_pipeline/api/schemas/analysis.py`): `measure`
  `average_minutes_per_day`, `weights` `multiyear`, `variance` `replicate`,
  `confidence_level` 0.95 (must lie strictly between 0.5 and 1), `population`
  empty. `activity` and `years` are required; compare also requires `group_a`
  and `group_b` (`label_a`/`label_b` default to `group_a`/`group_b`).

Limits: 1-50 years per request, each 2003-2100 (the engine then refuses
years that are not loaded); ≤ 100 `include` and ≤ 100 `exclude` codes per
activity selection; `preset` ≤ 64, `label` ≤ 120 and `label_a`/`label_b` ≤ 80
characters; `age_min`/`age_max` 0-130; `region` 1-4; `state_fips` two
digits. Bodies with unknown fields are rejected (422) to catch typos.

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

- `X-Request-ID` on every response: log correlation. An incoming
  `X-Request-ID` is kept only on an edge-authenticated request and only if it
  matches `^[A-Za-z0-9._-]{8,64}$` (the Worker sends 32 hex characters);
  otherwise the API generates 16 hex characters.
- `X-Analysis-Key` (successful analyses): the deterministic identifier of
  the analysis (SHA-256 of operation + canonical spec + analytics version +
  data version). It is a reproducibility/cache key, not a stored record ID.
- `X-Cache` (successful analyses): one of
  - `hit`: served from the cache;
  - `miss`: computed and cached;
  - `join`: shared an identical computation that was already running;
  - `bypass`: computed after a data reload that the snapshot did not know
    yet (on the new data, labelled with its version) and not cached; a
    request that joined such a computation gets `bypass` too;
  - `edge`: answered by the edge Worker's result cache (Worker only).
- `X-Data-Version`, `X-Analytics-Version` and `X-Response-Format` (`1`) on
  successful analyses: what the response bytes depend on. Edge caches key on
  them. `X-Data-Version` is also on `/meta`, `/activities` and
  `/activities/{code}`, and `X-Analytics-Version` on `/meta`. Error responses
  carry none of the analysis headers.
- `Server-Timing: db;dur=<ms>, app;dur=<ms>` on every response: time spent
  in database transactions (including waiting for a connection) and the rest,
  up to the response start. `db;dur=0` exactly means the request opened no
  database transaction (cache hits, single-flight joins, shed requests,
  metadata from the snapshot, and every 4xx except a 422 that the statistics
  query itself produces: `insufficient_data`, or a diary-date window outside
  the requested years); any database work reports at least `0.1`. The edge
  governor charges compute only when it is not 0.
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
  - `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`
    (not on the development-only `/docs` pages, which load their own scripts);
  - `X-Content-Type-Options: nosniff`;
  - `Referrer-Policy: no-referrer`;
  - `Cross-Origin-Resource-Policy: same-origin`;
  - `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`
    (the value the edge Worker also sets);
  - `Strict-Transport-Security: max-age=31536000`, in production only.
- Every response with status 400 or above carries `Cache-Control: no-store`,
  and so do `/health`, `/ready` and `/metrics`.
- There is no `server` header.

Responses of 1 KB or more are gzipped for clients that send
`Accept-Encoding: gzip`.

## Errors

One envelope everywhere:

```json
{"error": {"code": "unsupported_analysis", "message": "…", "details": null}}
```

| Status | `code` | When | `Retry-After` (s), `details.scope` |
| --- | --- | --- | --- |
| 400 | `malformed_json` | body is not valid JSON, or the parser gave up on it (e.g. nested too deeply) | |
| 403 | `forbidden_origin` | edge authentication is on and the request has no valid `X-ATUS-Edge-Auth` (every path except `/health`; `/ready` and `/metrics` also pass with a valid ops bearer) | |
| 404 | `not_found` | no such route, including a trailing-slash spelling; also `/ready` and `/metrics` without a valid ops bearer, for every method | |
| 404 | `activity_not_found` | `GET /activities/{code}` for a nonexistent or malformed code; `details.code` repeats it. The message names the loaded lexicon ("... the 2003-25 activity lexicon.") for a well-formed code, and is release-neutral ("... the activity lexicon.") for a code that is not 2, 4 or 6 digits, which never needs the data | |
| 405 | `method_not_allowed` | wrong method; the `Allow` header lists the right one | |
| other 4xx | `http_<status>` | any other framework-generated status (none is expected from the documented routes) | |
| 413 | `payload_too_large` | body over `ATUS_API_MAX_BODY_BYTES`, declared or streamed (the connection is then closed) | |
| 415 | `unsupported_media_type` | a POST (or a body-carrying PUT/PATCH/DELETE) whose `Content-Type` is not `application/json` | |
| 422 | `validation_error` | schema violation: wrong types, enums or unknown fields, or control characters in query parameters. `details.errors` locates each problem as `{location, message, type}` (not FastAPI's `{loc, msg}`; the OpenAPI schema documents this envelope for every 422): at most 20, with `details.truncated` and `details.error_count` when there were more | |
| 422 | `invalid_spec` | structurally valid but domain-invalid (age_min > age_max, duplicate years, unknown preset) | |
| 422 | `unknown_activity` | activity code not in the harmonized lexicon | |
| 422 | `unsupported_analysis` | methodologically unsupported (e.g. 2020 under `multiyear`); the message explains the alternative | |
| 422 | `insufficient_data` | empty population / subgroup too small for replicate variance | |
| 422 | `invalid_analysis` | fallback for an engine domain error without its own code; the current engine raises none | |
| 429 | `rate_limited` | this client's request or analysis bucket is empty | until the bucket holds a token; `client` |
| 503 | `overloaded` | no analysis lane within the admission wait; every pool connection busy past `ATUS_API_POOL_TIMEOUT_S`; the database refused a connection or a query for capacity (lock timeout, deadlock, SQLSTATE class 53 such as too many connections or a full `/dev/shm`); a metadata query was cancelled (also the data snapshot read an analysis request triggered); or the server shut down while the analysis waited | 10; `global` |
| 503 | `analysis_timeout` | the analysis hit the statement timeout or the request deadline; never cached | 60; `global` |
| 503 | `daily_capacity_reached` | the day's uncached-analysis cap is used | until 00:00 UTC; `daily` |
| 503 | `database_paused` | the database provider refuses connections because a usage quota is exhausted or the endpoint is disabled (matched on Neon's refusal texts, `NEON_PAUSED_MARKERS` in `src/atus_pipeline/api/errors.py`) | 3600; `monthly` |
| 503 | `database_unavailable` | no connection could be opened (refused, unreachable, or not open within `ATUS_API_POOL_TIMEOUT_S` + `ATUS_API_CONNECT_TIMEOUT_S` while the pool was not saturated), or the server dropped it (restart, admin shutdown) | 5 |
| 503 | `data_not_loaded` | the database has no loaded data (analysis endpoints and `/ready`) | 300 |
| 503 | `data_version_mismatch` | the database serves another data version than `ATUS_EXPECTED_DATA_VERSION`: every data route (analyses, `/meta`, `/activities`, `/activities/{code}`, `/ready`), until the deployment and the database agree | 60; `global` |
| 503 | `schema_not_initialized` | schema missing (any database-backed endpoint; also `/ready`) | 300 |
| 500 | `internal_error` | unexpected failure; details are logged server-side, once, and never returned | |

Every 429 and every 503 in this table carries `Retry-After` and the same
value in `details.retry_after_seconds` (whole seconds, at least 1), plus
`details.scope` where a limit applies. The one 503 without them is uvicorn's
connection-limit answer described above. All these codes are additive
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

where `data_version` identifies the served data
(`src/atus_pipeline/database/versions.py`): the latest succeeded ingestion
run's explicit `data_version` (migration 0003), in one of these forms:

| Written by | Form |
| --- | --- |
| `atus load` | `<release>:load-<run id>-<UTC YYYYMMDDTHHMMSSZ>-<6 hex>` |
| `atus build-serving-db` | `<release>:serving-<UTC YYYYMMDDTHHMMSSZ>-<8 hex>:<first 12 hex of the content hash>` |
| runs from before migration 0003 (fallback) | `<release>:run<id>:<started_at in epoch microseconds>` |
| no succeeded run | `unloaded:run0` |

Reloading data or changing the statistical implementation therefore changes
the key; no TTLs are needed for correctness, and the LRU bounds memory.
Equivalent requests share keys (years and code lists are canonicalized),
errors are never cached, a cache hit returns the byte-identical payload
including warnings, and a cache failure degrades to recomputation — never to
an error. The cache is per process and lost on every restart; production
runs one process (ADR-011), and the edge adds its own result cache and the
static snapshot in front of it
([ADR-015](adr/ADR-015-edge-cache-snapshot-and-governor.md)). The backend is
behind a small protocol, so a multi-process deployment could swap in a shared
store later.

The data version used in the key comes from the in-process data snapshot
(below), so a cache hit costs no database statement.

Concurrent-reload guard: the computation's own transaction reads the data
version first. That read holds a share lock on `ingestion_runs` until the
transaction ends, and the `atus load` switch must lock that table before it
swaps any table in, so every statement of the computation reads the version
it read first. If that version is the snapshot's, the result is cached under
the snapshot's key (`X-Cache: miss`). If the data changed after the snapshot
was read (a local, in-place `atus load`), the snapshot is invalidated and the
engine reads its own lexicon and years in that transaction instead of the
snapshot's: the result is computed entirely on the new data, labelled with
the version it read (`X-Data-Version`, and `X-Analysis-Key` for that
version), and returned once, uncached (`X-Cache: bypass`; requests that
joined it also get `bypass`). The next request reads the snapshot again.
With `ATUS_EXPECTED_DATA_VERSION` set, such a request is refused with 503
`data_version_mismatch` before anything is computed. In production the data
never changes in place (ADR-013).

Single-flight: concurrent requests for the same key share one computation.
Every request after the first gets `X-Cache: join`.

The computation is detached from the request that started it. If that
client disconnects, the admitted analysis still finishes and is cached, so a
retry either finds it (`hit`) or joins it.

HTTP: metadata endpoints (`/meta`, `/activities*`, `/population/metadata`)
send `Cache-Control: public, max-age=300` from the API process (the edge
Worker replaces it with `no-store`; see below). Analysis responses are POST
(bodies too complex for GET) and rely on the server-side cache; ETags are
deliberately not implemented since conditional requests don't apply to POST
in practice.

Measured effect (Phase 3: in-process client, full 2003–25 database on a
laptop, Sept 2026). These numbers are historical. The Phase 5 audit could not
reproduce them (F69): on the same laptop it measured 13.7-15.6 s p50 for the
worst uncached cases of the pre-Phase-5 code
([`evidence/phase5/01-audit-timed.md`](evidence/phase5/01-audit-timed.md)).
They also say nothing about the free-tier hosts. For those, the 2026-10-21
emulation runs
([`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md))
measured an uncached single-year estimate with replicate SEs at 0.99 s p50
at Neon's 0.25 CU floor and 79 ms at 2 CU, and a cache hit at 1.6 ms p50
with one client at either profile. The Phase 3 numbers are kept only to show
the hit-versus-miss ratio:

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
with the usual 503. `/ready` never uses a pinned or stale snapshot, but it does
answer from a snapshot younger than `ATUS_API_SNAPSHOT_TTL_S` (600 s in
production) without contacting the database: its `"database": "ok"` then means
the database answered within that time, and `snapshot_age_s` says how long ago.
While the snapshot is usable:

- `/meta`, `/activities`, `/activities/{code}` and `/population/metadata`
  answer from memory;
- analysis cache hits and every 4xx decided before the statistics query run
  no statement;
- uncached analyses reuse its activity resolver and years, so they run only
  the data-version read and their statistics queries.

The lexicon search returns exactly what the SQL `ILIKE` returned, for ASCII
and non-ASCII terms alike (proved by an integration test over thousands of
terms): the term is lowered with the database's own `lower()` of every cased
character, read with the snapshot. Only a database that lowers by context
(an ICU collation) sends search terms to that SQL.
Details: [ADR-012](adr/ADR-012-load-and-quota-protection.md#in-process-data-snapshot-answers-that-cost-nothing-f14).

## Versioning

`GET /api/v1/meta` reports `api_version` and `analytics_version` in its body
(and `X-Analytics-Version`); the data version is its `X-Data-Version` header,
while the body names the release and ingestion run. A fourth version, the
response format, is sent only as a header on analyses:

| Version | Meaning | Changes when |
| --- | --- | --- |
| `api_version` (`v1`, path prefix) | HTTP contract | request/response semantics change incompatibly |
| `analytics_version` (`0.2`) | statistical implementation | computed numbers could differ. Engine changes that keep every number equal under the §9 equivalence criterion (Phase 5: the year pushdown and its materialized CTE) keep the version; see the version history in [analytics.md](analytics.md) |
| data version (`X-Data-Version` on analyses and metadata) | loaded BLS data | `atus load` or a serving-DB build produces new data (forms under [Caching](#caching)) |
| `X-Response-Format` (`1`) | the serialized bytes of an analysis response | any change to analysis response bytes (fields, their order or formatting, warning or label text) without an `analytics_version` change, as in ADR-015: edge KV entries survive code-only deploys. `RESPONSE_FORMAT` in `src/atus_pipeline/api/__init__.py`, with the Worker's var; `tests/unit/api/test_response_format.py` pins the response schemas and bytes to it, so a change without a bump fails CI |

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
   `True-Client-IP` are ignored. IPv4 addresses are keyed whole, IPv6
   addresses by their /64.
4. **Per-client buckets** (token buckets, at most 10,000 clients remembered).
   120 requests per minute on every path except `/health`, checked before
   routing. Separately, 6 *computed* analyses per minute, charged only after
   the cache, single-flight and dry-run steps below; cache hits, joins and
   the dry run's 422s are free (an `insufficient_data` found by the query
   itself is charged), and a token is refunded when the lanes or the daily
   cap then refuse the analysis. Over a limit: 429 with `Retry-After`.
5. **Media type and shape.** POST bodies must be JSON (415). Control
   characters in query parameters are rejected (422).
6. **Data snapshot.** Data routes answer from the in-process snapshot; with
   `ATUS_EXPECTED_DATA_VERSION` set, another data version gets 503
   `data_version_mismatch`.
7. **Dry run.** An analysis the engine would refuse before its first query
   (unknown activity code, year not loaded, 2020 under the multi-year
   weights) gets its 422 without spending a token, a lane or a statement.
8. **Daily cap.** 600 uncached analyses per UTC day per process, then 503
   `daily_capacity_reached`. The count is in memory and restarts with the
   process; the edge governor holds the durable daily budget.
9. **Lanes.** At most 2 analyses compute at once, and at most 1 of them
   heavy. An analysis is heavy at 8 or more year-queries (`len(years)`,
   doubled for compare). Others wait up to 5 s, then get 503 `overloaded`.
10. **Bounded queries.** Each transaction is `READ ONLY` and starts with
    one statement that sets transaction-local limits (`set_config(..., true)`,
    the function form of `SET LOCAL`; the data snapshot's REPEATABLE READ
    read sends them as `SET LOCAL` statements plus a share lock on
    `atus.ingestion_runs`, in one query, so its snapshot is taken only after
    the lock): `statement_timeout` 85 s for analyses
    and 10 s for metadata, `lock_timeout` 5 s,
    `idle_in_transaction_session_timeout` 30 s,
    `client_connection_check_interval` 5 s, `jit` off, and
    `max_parallel_workers_per_gather` when `ATUS_API_MAX_PARALLEL_WORKERS` is
    set. An analysis is also cancelled after a 90 s deadline (statement
    timeout + 5 s), giving 503 `analysis_timeout`: its running query is
    cancelled and it sends no further statement, so the deadline holds even
    when it passes between two statements.

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
| any 400, 403, 404, 405, 413, 415 or 429, and every 422 decided before the statistics query | 0 | 0 |
| uncached estimate or trend: check, limits, data-version read, statistics query | 4 | 6 |
| uncached compare: one statistics query per group | 5 | 7 |
| `/health`, `/metrics`; `/ready` with a fresh snapshot | 0 | 0 |

Before Phase 5 (PostgreSQL log counts) a cache hit ran 3 statements, a 422
decided before the statistics query 3 to 7, `/meta` 5 and an uncached
estimate 9 (audit F14).

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
| `/openapi.json` | off: 404 | the same schema is committed in the repository as `frontend/openapi.json`, regenerated by `scripts/export_openapi.py` (which needs no docs route) and checked by a test. The public site publishes that committed copy as a static asset at `/openapi.json` (the site root, outside `/api`, so neither the Worker nor the API runs for it) |
| `/ready` | bearer `ATUS_OPS_TOKEN`, else 404 like an unknown path | it can touch the database and names data versions; the Worker never routes it (F16) |
| `/metrics` | bearer `ATUS_OPS_TOKEN`, else 404 like an unknown path | operational detail; the Worker never routes it |

With edge authentication on, a request to `/ready` or `/metrics` without a
valid bearer is refused like any other unknown path (403
`forbidden_origin` without the edge secret, 404 with it); a valid bearer is
enough on its own, so an operator can reach the origin directly.

## Through the edge Worker

In the planned public deployment ([ADR-011](adr/ADR-011-hosting.md), not
deployed yet), browsers reach the API only through the Cloudflare Worker in
`edge/src/`, on the same origin as the SPA. Static files (the SPA and
`/snap/`) never reach the Worker. For `/api` and `/api/*` it differs from
the API process as follows; [ADR-015](adr/ADR-015-edge-cache-snapshot-and-governor.md)
has the design.

**Routes** (`edge/src/routes.ts`), matched exactly as sent:

| Public path | Method | Answered by |
| --- | --- | --- |
| `/api/v1/meta`, `/api/v1/activities`, `/api/v1/activities/presets`, `/api/v1/population/metadata` | GET | the Worker, from the deployed static snapshot (the same bytes the API gives), when there is no query string; otherwise the API (query string forwarded) once the quota governor admits it as a metadata lookup |
| `/api/v1/activities/<code>` (1-32 characters of `[A-Za-z0-9_-]`) | GET | the same: the snapshot holds every lexicon code (2, 4 or 6 digits); any other code goes to the API, which answers `activity_not_found` |
| `/api/v1/analysis/estimate`, `/trend`, `/compare` | POST | the Worker's KV result cache, else the API once the quota governor admits the request |
| `/api/health` | GET | the API's `/health` |
| `/api/v1/wake` | GET | the Worker: starts waking the API through its `/health` (never the database) and answers `{"origin": "warm" \| "waking" \| "unknown", "wake": ...}` |
| `/api/_edge/health` | GET | the Worker, with its versions; no origin call |
| `/api/_ops/budget` | GET | the Worker, with the governor's state, for `Authorization: Bearer <OPS_TOKEN>` only; otherwise 404 |

Any other path under `/api` gets 404 `not_found` without an origin call, so
the API's `/ready`, `/metrics`, `/docs` and `/openapi.json` are unreachable
through the Worker (the site's static `/openapi.json` is a file, not the API
route). A listed path with another method, `HEAD` included, gets 405
`method_not_allowed` (`/api/_ops/budget` answers 404 instead).

**Headers** (`edge/src/proxy.ts`, `edge/src/http.ts`):

- The request to the API carries a fresh header set: `Accept`,
  `If-None-Match` and the body's `Content-Type` from the client;
  `Accept-Encoding: gzip` whatever the client accepts (the runtime decodes
  the body, which the Worker relays without `Content-Encoding`); a new
  `X-Request-ID` (32 hex characters), `X-ATUS-Client-IP` (from Cloudflare's
  `CF-Connecting-IP`), `X-ATUS-Edge-Auth` and
  `User-Agent: atus-edge/<version>`. Nothing else the client sent is
  forwarded.
- The response keeps the API's status and body and only these headers:
  `Content-Type`, `ETag`, `Last-Modified`, `Retry-After`, `Allow`,
  `Server-Timing`, `X-Request-ID`, `X-Data-Version`, `X-Analytics-Version`,
  `X-Analysis-Key`, `X-Cache`, `X-Response-Format`. The Worker then sets the
  same security headers as the API on every `/api` response it produces
  (answers the runtime gives before the Worker runs, such as to an unknown
  HTTP method or a malformed `Content-Length`, are Cloudflare's), plus
  `Strict-Transport-Security: max-age=31536000` and `Cache-Control: no-store`
  (so the metadata's `public, max-age=300` does not reach browsers). CORS
  headers are dropped: the site is same-origin.
- `X-Cache: edge` marks an analysis answered from the KV result cache. The
  Worker stores only complete 200 JSON analysis responses of at most 16 KiB
  whose `X-Cache` was `miss` and whose version headers equal its
  `DATA_VERSION`, `ANALYTICS_VERSION` and `RESPONSE_FORMAT`.
- A metadata answer from the snapshot has `Content-Type: application/json`,
  `X-Data-Version` (except presets and population metadata, as from the API),
  `X-Analytics-Version` on `/meta`, `X-Request-ID` and the security headers,
  and no `Server-Timing`.

**Codes the Worker adds** (same envelope; `edge/src/index.ts`,
`edge/src/proxy.ts`, `edge/src/ops.ts`). The API's own error envelopes pass
through unchanged.

| Status | `code` | When | `Retry-After` (s), details |
| --- | --- | --- | --- |
| 404 | `not_found` | path not in the allowlist | |
| 405 | `method_not_allowed` | another method on a listed path | `Allow` header, `details.allowed` |
| 413 | `payload_too_large` | analysis body over 64 KiB (declared or streamed), checked before the cache and the API | `details.max_bytes` |
| 415 | `unsupported_media_type` | analysis body not `application/json` | |
| 429 | `rate_limited` | the governor's per-network daily budget is spent: 60 analyses, 300 admissions, or a fifth of the day's estimated database compute per client IP prefix per UTC day (`budget: "prefix"`); or 200 metadata lookups (`budget: "metadata"`) | until 00:00 UTC; `scope: "client"`, `budget`, `resumes_at` |
| 503 | `daily_capacity_reached` | a shared daily budget is spent: 600 analyses (`budget: "global"`, `scope: "global"`), 300 heavy analyses (`heavy`), the day's estimated database compute (`cu_day`) or, without the Neon usage poll, the day's estimated database egress (`egress_day`; these three `scope: "daily"`), or 2,000 metadata lookups (`metadata`, `scope: "global"`) | until 00:00 UTC; `scope`, `budget`, `resumes_at` |
| 503 | `capacity_paused` | with the optional Neon usage poll: the month's egress or compute allowance is (nearly) used | until it resumes; `scope: "monthly"`, `budget`, `resumes_at` |
| 503 | `capacity_unavailable` | the governor errored, did not answer within 5 s, or the Worker lacks `IP_HMAC_KEY`: uncached analyses and metadata lookups fail closed | 60 |
| 503 | `service_waking` | the API host is spinning up (Render's hibernate answer or an HTML loading page) | 5 |
| 503 | `overloaded` | uvicorn's own plain-text connection-limit 503 | 10; `scope: "global"` |
| 502 | `upstream_unavailable` | the answer is not the API's: `details.reason` is `connect_failed`, `platform_error`, `origin_timeout` (Cloudflare 524), `broken_response` or `not_configured` | 10 |
| 503 | `upstream_unavailable` | `details.reason: "circuit_open"`: after repeated connection failures the Worker skips the API for a while | the remaining breaker time |
| 500 | `internal_error` | unexpected Worker failure | |

After `origin_timeout` or `broken_response` the API may still have computed
the analysis; after the other reasons it never received it. The SPA's retry
policy depends on that difference ([frontend.md](frontend.md)).

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

Without a database (`tests/unit/api/`):

- `test_contract.py`: serialization, OpenAPI invariants, the examples;
- `test_error_mapping.py`: domain errors and bad input to the error contract;
- `test_cache.py`: the result cache and its deterministic keys;
- `test_edge_contract.py`: the API's half of the edge contract (request ids, the shared `edge:unknown` bucket, JSON envelopes, no redirects, `db;dur=0` exactly when the database did no work);
- `test_review_hardening.py`: the Phase 3 adversarial-review regressions (key canonicalization, cache bypass, CORS, framework errors, 500 headers, readiness failures);
- `test_guard.py`: 413, 415, 403, 429 and 422, headers, HEAD;
- `test_admission.py`: analysis bucket, daily cap, lanes, single-flight, error mapping;
- `test_snapshot.py`: the data snapshot, zero-statement paths, the dry run, the expected data version;
- `test_observability.py`: access log, IP truncation, JSON logs, `Server-Timing`, `/health`, `/ready`, `/metrics`;
- `test_exposure.py`: the docs switch, the OpenAPI snapshot, no internals in error bodies;
- `test_protection.py`, `test_database_unit.py`;
- `test_config.py`: includes production refusal;
- `test_serve_and_logging.py`.

Against PostgreSQL:

- `integration/api/test_api_endpoints.py`: HTTP to the engine to PostgreSQL on the hand-computed fixture dataset;
- `integration/api/test_protection_db.py`: transaction-local limits, timeouts, pool exhaustion, terminated backends, zero-statement 403s, an empty database;
- `integration/api/test_snapshot_db.py`: Python lexicon search and detail identical to the SQL, statistics unchanged with the snapshot's resolver and years, read-only transactions, statements per request;
- `integration/api/test_statement_log.py`: statements per request in PostgreSQL's own log (starts a throwaway `postgres:16` container, `atus-wsa-stmt` on port 5461; skipped without Docker);
- `integration/api_prod/test_server.py`: the production server over real sockets, including a client that disconnects, and `atus api` logging JSON in production mode.
