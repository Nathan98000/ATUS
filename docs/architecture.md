# Architecture

This document describes the whole system: the data pipeline, the analytical
engine, the HTTP API, the serving database, the frontend, the edge Worker and
the hosting design. It explains how the parts fit together and points to the
decision records (ADRs) and reference documents for detail. Where this
document and the code disagree, the code is right.

**Deployment status.** Phase 5 is implemented and runs locally, including a
production-mode stack under free-tier emulation. Nothing is deployed: the
hosting described under [Hosting](#hosting-designed-not-deployed) is the
decided design ([ADR-011](adr/ADR-011-hosting.md)), not a running service.

## System overview

The system has an offline side that runs only on the owner's machine and an
online side meant for free hosting tiers.

```text
OFFLINE (owner's machine)

  bls.gov ──atus download / extract / validate-source──▶ data/raw, data/staging/<release>/
          ──atus migrate, atus load──────────────────────▶ full database (PostgreSQL 16, ~2 GB)
          ──atus validate-db, atus validate-analytics
          ──atus build-serving-db────────────────────────▶ serving database (~0.6 GB)
                                                            + release artifact (.dump, .sha256, manifest)
          ──restore script (owner)──────────────────────────────────────────────┐
                                                                                ▼
ONLINE (free tiers; designed, not deployed)

  Browser ──HTTPS──▶ Cloudflare workers.dev origin
                       ├─ static assets: SPA, /snap/ static snapshot, _headers
                       └─ Worker on /api, /api/*: allowlist, KV result cache,
                          quota governor (Durable Object), security headers
                              │ HTTPS, edge secret, client IP
                              ▼
                     Render Free: API container (FastAPI, one uvicorn process)
                              │ TLS, read-only role atus_api
                              ▼
                     Neon Free: serving database (Postgres 16)
```

| Layer | Code | Reference |
|---|---|---|
| Data pipeline | `src/atus_pipeline/` (`sources.py`, `acquisition/`, `staging/`, `validation/`, `transformation/`, `loading/`, `database/`), `migrations/` | [database.md](database.md), [data-lineage.md](data-lineage.md), [source-data.md](source-data.md) |
| Analytical engine | `src/atus_pipeline/analytics/` | [analytics.md](analytics.md), ADR-001 to ADR-006 |
| HTTP API | `src/atus_pipeline/api/` | [api.md](api.md), ADR-012, ADR-014 |
| Serving database | `src/atus_pipeline/serving/`, `scripts/ops/` | ADR-013 |
| Frontend | `frontend/` | [frontend.md](frontend.md), ADR-007 to ADR-010, ADR-016 |
| Edge Worker | `edge/` | [`edge/README.md`](../edge/README.md), ADR-011, ADR-015 |
| Packaging, CI/CD, monitors | `Dockerfile`, `docker-compose*.yml`, `deploy/`, `.github/workflows/` | [deployment.md](deployment.md), ADR-011, ADR-014 |
| Operations | `scripts/ops/` | [operations.md](operations.md) |

The engine is the only place where statistics are computed. The CLI and the
API call the same engine; the frontend, the edge cache and the static
snapshot only present or copy its outputs.

## Data pipeline

```text
   bls.gov ───────────────► data/raw/*.zip ───────► data/staging/<release>/*.dat ──┐
            atus download            atus extract                                  │
            (curl_cffi,              (streaming unzip,                             │
             manifest.json)           row counts)                                  │
                                                                                   │
                              atus validate-source  ◄──────────────────────────────┤
                              (headers, official row counts, survey years)         │
                                                                                   │
   PostgreSQL schema "atus" ◄──────────────────────────────────────────────────────┘
            atus migrate          plain-SQL migrations, checksummed runner
            atus load [--validate] build in schema atus_next, then switch (below)
            atus validate-db      scalar / violation / lexicon / cross-file checks
```

Every stage is a separate CLI command (`src/atus_pipeline/cli.py`) with an
inspectable artifact between stages: files on disk or rows in tables. Each
command can be re-run.

### Source registry (`sources.py`)

One `Release` entry per BLS multi-year release: its code (for example `0325`
= 2003–2025), first and last survey year, and its source files, each with its
exact expected header and the official record count BLS publishes.
`multiyear_release()` derives archive names, member names and URLs from the
code. Everything release-specific downstream derives from the entry: the
staging directory, the expected survey years in source and database
validation, the committed lexicon file
`data/reference/activity_lexicon_<release>.csv`, and the lexicon label
(`2003-25`) in analysis warnings and errors.

A new multi-year release is therefore a registry entry with its record
counts, plus the new lexicon CSV. `tests/integration/test_release_checklist.py`
runs a synthetic `0326` (2003–2026) through source validation, the load, the
engine, `validate-db` and the API with no other code change. The procedure,
including how benchmarks are handled, is ADR-013's "New ATUS year procedure".
The OpenAPI examples (for example in `src/atus_pipeline/api/schemas/analysis.py`)
are recorded `0325` responses and keep naming that release.

### Acquisition and staging

- `acquisition/download.py` downloads each published file once, sequentially
  with a delay, and records URL, size, SHA-256 and timestamps in
  `data/manifest.json`. BLS's CDN rejects non-browser TLS clients, so the
  downloader uses `curl_cffi` browser impersonation; the manual fallback is
  in [source-data.md](source-data.md). Nothing ever modifies `data/raw/`.
- `staging/extract.py` streams the data member of each archive into
  `data/staging/<release>/` and records staged row counts.

### Source validation (`validation/source_checks.py`)

Fails before the database is involved if a staged file's header differs from
the registered layout, its row count differs from the official count, or a
survey year is missing. Such a failure means a corrupted download or a BLS
layout change, which needs a person, not a retry.

### Transformation (`transformation/`)

Pure functions, no I/O. `common.py` holds the field rules (ATUS sentinels,
implied decimals, diary times, zero-padded activity codes); `tables.py` maps
each source row to a canonical row, one explicit line per column, so
[data-lineage.md](data-lineage.md) can be checked against the code. Unknown or
malformed values raise `TransformError`; nothing is guessed.

### Loading: build, then switch (`loading/loader.py`, `loading/switch.py`)

`atus load` never truncates live tables (ADR-013, Decision 1). The audit found
that the earlier in-place TRUNCATE + COPY blocked every database-backed API
request for the whole load, could deadlock with concurrent analyses, and committed the run's
status separately from the data (audit F08, F19, F20 in
[operations.md](operations.md)). The loader now works in four steps:

1. **Record the attempt.** Under a session advisory lock (one loader per
   database), runs left `running` by a killed loader are marked `failed`, the
   live schema must match the migration files exactly, and a new
   `ingestion_runs` row is inserted with status `running`.
2. **Build.** In one transaction, every migration is replayed into schema
   `atus_next`, so the shadow tables are structurally identical to the live
   ones, and the lexicon and staged files are COPYed into them. The shadow
   tables are then `ANALYZE`d.
3. **Validate** (with `--validate`). The `validate-db` checks run against
   `atus_next`; a failed error-severity check refuses the switch.
4. **Switch.** One short transaction: lock `ingestion_runs` and the ten
   canonical tables in the order readers touch them, each wait bounded by
   `lock_timeout` (50 ms by default); drop the old tables; move the shadow
   tables into `atus`; restore their owner and grants; record provenance and
   validation results; mark the run `succeeded` and write its `data_version`;
   commit.

Consequences:

- **Readers are not blocked and never see a mix.** A request that touched a
  table before the switch holds the switch back until it ends; a statement
  that starts during the switch waits, then reads the new tables. Each API
  request is one transaction, so its data and its `data_version` come from
  the same commit.
- **The switch gives way, not the reader.** Its lock waits are kept below
  PostgreSQL's deadlock detection, so in a lock-order cycle the switch rolls
  back and retries until `--switch-timeout` (120 s by default); then the load
  fails with the live data unchanged.
- **REPEATABLE READ** sessions older than the new data are waited out before
  each attempt (audit F21).
- **A failed load changes nothing live.** COPY errors, failed validation and
  failures inside the switch transaction leave the old data and version in
  place and mark the run `failed`.
- **Costs.** The database holds old and new data until the switch: about
  twice the canonical data's disk space during a load. A long-open
  transaction (for example an idle `psql` session) delays the switch. A view
  that depends on a canonical table makes the switch fail.
- **Metadata tables** (`ingestion_runs`, `source_files`,
  `validation_results`) are never swapped; run ids keep increasing.
- **Memory stays flat.** Rows stream from the staged CSVs through the
  transformers into COPY; only the respondent case ids (~259k integers) are
  held, to filter the ATUS-CPS file.

The tests are `tests/integration/test_reload_switch.py` and
`tests/integration/test_reload_under_api_load.py` (continuous reads through
the real API during a rebuild and switch: no failed request, and the version
changes).

### Data version (migration 0003)

`migrations/0003_data_version.sql` adds `ingestion_runs.data_version`
(`UNIQUE`, allowed only on `succeeded` runs). It is written in the same
transaction that makes the data live:

| Writer | Format |
|---|---|
| `atus load` | `<release>:load-<run_id>-<UTC YYYYMMDDTHHMMSSZ>-<6 hex>` |
| `atus build-serving-db` | `<release>:serving-<build_id>:<content_hash[:12]>` |
| rows written before 0003 (reader fallback) | `<release>:run<id>:<started_at epoch microseconds>` |

`atus_pipeline.database.versions.read_data_version` reads it. Before Phase 5
every freshly built database reported `0325:run1` (audit F06); the random and
time parts now make the version differ for every load and every build,
including two builds of the same data. Every cache in the system is keyed by
it (see [Versions and caches](#versions-and-caches)).

### Database validation (`validation/db_checks.py`)

Three kinds of check, run against the live schema or, for
`atus load --validate`, the shadow:

- **scalar**: measured values against authoritative expectations (official
  record counts, BLS-documented interview counts, lexicon sizes);
- **violations**: invariants that must hold row by row (every diary sums to
  1,440 minutes, cumulative durations are running totals, no diary dates in
  the 2020 collection gap, weight rules, roster/CPS/who structure);
- **cross-file**: episode durations re-aggregated and compared with the
  independently produced BLS Activity Summary file for a sample of cases.

Results are recorded in `atus.validation_results`. That table is also written
by `atus load --validate` (with the switch) and by `atus validate-analytics`
(benchmark results; needs the owner role). The serving database builder
requires passing `validate-db` and `validate-analytics` results recorded for
the source's loaded data.

### Migrations (`database/migrate.py`, `migrations/`)

Plain `.sql` files (`0001_core_schema.sql`, `0002_indexes.sql`,
`0003_data_version.sql`) applied in filename order, each in its own
transaction, tracked with checksums in `public.schema_migrations`; editing an
applied migration fails. The directory is `$ATUS_MIGRATIONS_DIR` or the
checkout's `migrations/`. `atus migrate`, `atus load` and `atus validate-db`
refuse to run against a serving database (`serving/guard.py`).

## Analytical engine

`atus_pipeline.analytics` turns an `AnalysisSpec` (data) into result objects:

```text
AnalysisSpec → ActivityResolver → population filter SQL
  → sufficient-statistics query (PostgreSQL) → estimators → replicate variance
  → result objects (+ metadata, warnings)
```

- PostgreSQL does selection, joins and all weighted sums, including the 160
  replicate sums; Python turns the sufficient statistics into estimates,
  variances and results (ADR-006). Full-sample sums are exact `numeric`,
  replicate sums `float8`.
- Estimators follow the ATUS User's Guide ch. 7.4 (ADR-002), variance the
  replicate-weight formula of ch. 7.5 (ADR-003), and 2020 has explicit rules
  (ADR-004). Activities are aggregated from episodes (ADR-005).
- `AnalysisEngine.estimate`, `trend` and `compare` are the only entry points
  (ADR-001). `atus validate-analytics` reproduces 24 published BLS numbers.
- **Phase 5 change (no number changed).** The per-respondent activity CTE in
  `analytics/queries.py` now aggregates only the requested years'
  respondents and is `MATERIALIZED` (audit F39; ADR-013, Decision 9; the
  materialization fixed a plan regression found by the equivalence run).
  `ANALYTICS_VERSION` stays `0.2`.
- The engine accepts an optional activity resolver and year list, which the
  API supplies from its in-process data snapshot (ADR-012).

The statistical contract is [analytics.md](analytics.md).

## HTTP API

`atus_pipeline.api` is a FastAPI application (contract `v1`) started by
`atus api` (`api/serve.py`): one uvicorn process, listening on `$PORT`, proxy
headers off, no `server` header. It contains no statistics: request bodies
are engine specs and responses are the engine results' `to_dict()`.

Endpoints: `POST /api/v1/analysis/{estimate,trend,compare}`;
`GET /api/v1/activities` (list and search), `/activities/presets`,
`/activities/{code}`, `/population/metadata`, `/meta`; `/health` (no
database); `/ready` and `/metrics` (operator bearer token only).

Phase 5 added protection in front of the engine (ADR-012):

- **Order of checks.** Body cap (64 KiB), edge authentication
  (`X-ATUS-Edge-Auth`, constant-time; `/health` exempt), per-client token
  buckets, content type, validation, then the data snapshot, cache lookup and
  single-flight join, a dry run of the engine for domain errors, the daily
  cap and per-client analysis bucket, admission lanes, and finally the query.
  Rejections before the query cost no SQL statement while the snapshot is
  fresh (`api/middleware.py`, `api/snapshot.py`, `api/analysis_runner.py`,
  `api/database.py`).
- **In-process data snapshot.** Data version, latest run, years, respondent
  count and lexicon are read in one read-only transaction and reused, so
  metadata routes, cache hits and 4xx answers decided before the statistics
  query run no SQL. With
  `ATUS_EXPECTED_DATA_VERSION` set (production) the snapshot is pinned, and a
  database serving another version gets 503 `data_version_mismatch`.
- **Admission lanes.** At most 2 uncached analyses compute at once, at most 1
  of them heavy; a lane is taken before a pool connection. Concurrent
  identical requests share one computation.
- **Bounded work.** Each request is one `BEGIN READ ONLY` transaction whose
  limits are set per transaction (`statement_timeout`, `lock_timeout`,
  `idle_in_transaction_session_timeout`, `jit = off`), plus a request
  deadline that cancels the query. Timeouts and overload map to specific 503
  codes with `Retry-After`; errors are never cached.
- **Pool** (`psycopg_pool`): `min_size=0` (an API start never wakes the
  database), `max_size` 4, `max_idle` 240 s, and a connection check on every
  checkout so that connections closed by a suspended database are replaced
  (audit F05).
- **Observability** (ADR-014): one JSON access line per request with
  truncated client addresses (/24, /48) and per-request database time and
  statement counts; Prometheus text at `/metrics`; a periodic metrics log
  event; `Server-Timing`.

The values above were calibrated on the free-tier emulation on 2026-10-21
and kept at their code defaults
([ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13));
the post-deploy replay on Neon must still confirm them. The HTTP reference is
[api.md](api.md).

## Serving database

The full database (1,980 MiB measured) fits no no-card free Postgres tier.
`atus build-serving-db` (`serving/builder.py`) derives a reduced database
that keeps exactly what the engine, the API and `validate-analytics` read
(the serving contract, `serving/contract.py`), with the same table and
column names, types, NULL rules and CHECK constraints, so the engine runs on
it unchanged (ADR-013, Decisions 5–7):

- `activities` pre-aggregated to one row per (respondent, activity code),
  stored in activity-code order behind its index;
- replicate weights as `float8` (the engine casts to `float8` anyway), with
  all-NULL and all-zero rows omitted (they are excluded by the weight filters);
- roster and CPS tables reduced to line 1 and the contract columns;
- `activity_companions` dropped; `validation_results` kept empty.

The build reads the source in one read-only transaction, writes the target in
one transaction that commits only if every invariant holds, and computes a
content hash over every served row, which is the same for two builds of the
same data. It marks the database as serving (schema comment and data
version). `--artifact-dir` writes the release artifact: a
`pg_dump --format=custom --no-owner --no-acl`, its SHA-256, and a manifest
with no connection string. The artifact is also the backup.

Restoring (`scripts/ops/restore_serving_db.sh`, `atus restore-serving-db`)
verifies the checksum, refuses a non-empty target, runs `pg_restore`, applies
`scripts/ops/roles.sql` (role `atus_api`: SELECT on the ten served tables
only, read-only by default, the role defaults of ADR-011), runs
`VACUUM (ANALYZE)`, and checks row counts, version, plans and the metered
size. `atus compare-serving-db` diffs the engine on the full and the serving
database under the equivalence criterion (ADR-013, Decision 10).

## Frontend

ATUS Explorer (`frontend/`) is a client-side React 19 + TypeScript SPA built
with Vite (ADR-007): an analysis builder, result views with uncertainty and
methodology, hand-rolled SVG charts with explicit 2020 gaps (ADR-010), and
shareable URLs that encode the canonical spec (ADR-009). Short links are
deferred (ADR-016).

- **One origin.** The app calls `/api/v1` on its own origin. In production
  the edge Worker forwards `/api`; under `npm run dev` and `npm run preview`
  Vite proxies it to the local API, so no CORS setup is needed.
- **Static snapshot first.** Each deploy publishes, next to the SPA, the
  exact API responses for all metadata and about 130 prewarmed analyses under
  `/snap/<snapshot_id>/` (ADR-015). `api/snapshot.ts` serves listed requests
  from there, keyed by the same request hash the edge cache uses; everything
  else goes to the API.
- **Server state** via TanStack Query, with analysis query keys tagged by the
  data version (ADR-008).
- **Cold start and service states** (`api/resilience.ts`,
  `domain/presentError.ts`): retries within a 150 s budget that honours
  `Retry-After`, a "waking up" notice in a polite live region (after 3 s of
  pending metadata, when an attempt fails as waking, or within 90 s of a
  prewake that answered "waking"), a
  check that the API's `api_version` is compatible (`api/compat.ts`),
  full-page states for paused quotas and an incompatible API, and a prewake
  request on user intent (`api/prewake.ts`).
- **Exports**: CSV and JSON of the response on screen, with no recomputed
  numbers (`domain/exports.ts`).
- **Headers**: `public/_headers` carries the CSP (`connect-src 'self'`),
  HSTS and cache rules; the build also embeds the CSP as a `<meta>`
  backstop.

Details: [frontend.md](frontend.md).

## Edge

The edge (`edge/`) is one Cloudflare Worker on the same `workers.dev` origin
as the SPA ([ADR-011](adr/ADR-011-hosting.md), [ADR-015](adr/ADR-015-edge-cache-snapshot-and-governor.md)).
Static paths (the SPA routes, `/assets/*`, `/snap/*`) are served as static
assets and never run Worker code. Only `/api` and `/api/*` do
(`run_worker_first` in `edge/wrangler.jsonc`):

- **Allowlist** (`src/routes.ts`): the public v1 routes, `/api/health`, and
  the Worker's own `/api/v1/wake`, `/api/_edge/health` and
  `/api/_ops/budget` (bearer token). Everything else under `/api` is a 404
  envelope; `/ready`, `/docs` and `/openapi.json` are never routed.
- **Limits and headers** (`src/proxy.ts`, `src/http.ts`): 413 above 64 KiB,
  415 for non-JSON POSTs; a fresh upstream header set with
  `X-ATUS-Edge-Auth`, `X-ATUS-Client-IP` (from `CF-Connecting-IP`) and
  `X-Request-ID`, and always `Accept-Encoding: gzip`; the Worker's own
  security headers on every `/api` response it produces.
- **Metadata** (`src/snapshot.ts`): the metadata GETs are answered from the
  deployed static snapshot, byte for byte, with no origin call; only those
  it cannot answer (a query string, an unknown code) reach the API, as
  governed lookups.
- **Result cache** (`src/cache.ts`): Workers KV keyed by the SHA-256 of the
  exact request bytes, scoped by the release (`CACHE_SCOPE`); stores only
  complete 200 responses the API computed (`X-Cache: miss`) whose
  `X-Data-Version` equals the Worker's `DATA_VERSION`.
- **Quota governor** (`src/governor.ts`, `src/admission.ts`, `src/budget.ts`):
  one SQLite-backed Durable Object that admits uncached analyses against
  per-network, global and heavy daily caps, a per-network share of the
  daily Neon compute, Neon compute-hour and egress budgets, and counts
  metadata lookups and charges those that wake Neon the same way; it fails
  closed for those two only.
- **Upstream** (`src/upstream.ts`): no timeout of its own, one retry only when
  the connection fails before response headers; non-API platform responses
  (Render's waking page, Cloudflare 52x) become JSON envelopes such as
  `service_waking` with `Retry-After`.

| Cache tier | Holds | Survives | Keyed by |
|---|---|---|---|
| L0 static snapshot (`/snap/`) | metadata, ~130 prewarmed analyses | everything, including Worker quota exhaustion and API or database pauses | snapshot id (API build + data version) |
| L1 Workers KV | earlier analysis responses, byte for byte | API spin-down and monthly pauses | `CACHE_SCOPE` (a hash of an epoch, the API, analytics and data versions and the response format) + request hash |
| L2 API in-process LRU (`api/cache.py`) | recent results | nothing (lost on restart) | operation, canonical spec, analytics version, data version |

## Hosting (designed, not deployed)

ADR-011 chose this design through a judge panel of five proposals; the
runner-up (Vercel Hobby as the API host) is the documented fallback. It is a
manual procedure, and none of its preconditions is implemented (ADR-011,
amendments): the Worker reads its origin only from the secret
`RENDER_ORIGIN`.

| Role | Provider (free plan) | Notes |
|---|---|---|
| Public origin: SPA, snapshot, `/api` proxy, KV, governor | Cloudflare Workers Free | `https://<worker>.<account-subdomain>.workers.dev`, the only hostname in share links |
| API | Render Free web service (0.1 CPU, 512 MB) | image from GHCR by digest; requests to the onrender.com URL without the edge secret get 403 (`/health` excepted) |
| Database | Neon Free, Postgres 16 | direct endpoint, role `atus_api`, one project per data release |
| Image registry, CI/CD, monitors, release artifact | GitHub (GHCR, Actions, Releases) | public repository |

- **Trust boundaries:** browser → Cloudflare edge → Worker → Render load
  balancer → API → Neon. The API trusts `X-ATUS-Client-IP` only together
  with a valid edge secret and ignores `X-Forwarded-For`.
- **Sleep.** Render spins down after 15 idle minutes and Neon suspends after
  5; no keep-alive pings either. The SPA shell and the static snapshot stay
  available while they sleep, and the frontend explains the wait.
- **Deploy path.** `.github/workflows/deploy.yml` is the only path: on a
  version tag or manual run, behind the `production` environment's required
  reviewer, it deploys the exact image its build job built, tested and
  scanned, with the static snapshot built for the release before the
  approval; it runs `wrangler deploy`, a post-deploy smoke of at most 30
  requests (`scripts/ops/deploy_smoke.py`) and a one-journey browser smoke.
  Rollback per component is in [deployment.md](deployment.md#rollback).
- **Release switch.** A new data release is restored into a new Neon
  project; the owner then changes the API's `ATUS_DATABASE_URL` and
  `ATUS_EXPECTED_DATA_VERSION` together, and the deploy workflow publishes the
  matching Worker variables and snapshot (ADR-013, "Release procedure").
- **Monitors** (ADR-014): scheduled GitHub workflows check the edge every 30
  minutes, the API's `/health` every 6 hours (never the database), and the
  governor's budgets daily; a failed run emails the owner.

The free-tier budget, failure modes, owner account steps and configuration
reference are in [deployment.md](deployment.md).

## Local production-mode stack

`docker-compose.prod.yml` runs the production shape on one machine, bound to
`127.0.0.1` and calling no provider: `db` (Postgres 16 with Neon's compute
settings), `seed` or `restore` (a serving database from the fixture or from a
release dump), `roles` (`scripts/ops/roles.sql`), `api` (the production image
with production settings, not published on the host), and, under profiles,
`edge` (the Worker under `wrangler dev` in local mode), `snapshot-api` and
`smoke`. `docker-compose.emulation.yml` adds the free-tier CPU and memory
limits (API 0.1 CPU / 512 MB, database 0.25 vCPU / 1 GB) and a PgBouncer
profile. `scripts/smoke_stack.sh` builds, seeds, probes and tears the stack
down; `--edge --e2e` adds the public origin and the Playwright suite. Usage is
in [deployment.md](deployment.md#0-rehearse-the-whole-stack-locally).
`docker-compose.yml` is separate: the development PostgreSQL that holds the
full database.

## Versions and caches

| Version | Value | Source | Used by |
|---|---|---|---|
| `api_version` | `v1` | `src/atus_pipeline/api/__init__.py` | URL prefix, `/meta`, the frontend's compatibility check |
| `analytics_version` | `0.2` | `src/atus_pipeline/analytics/__init__.py` | every result, cache keys |
| `data_version` | see [Data version](#data-version-migration-0003) | `atus.ingestion_runs` | API cache key, `X-Data-Version`, KV `CACHE_SCOPE` and store gate, snapshot id, frontend query keys |

Because every cache is keyed by a `data_version` that never repeats, a cached
result can never be served for a different database. A statistical change in
the engine requires bumping `ANALYTICS_VERSION` and recording it in
[analytics.md](analytics.md).

## Performance

Performance under free-tier emulation is measured by the load tests
([`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md),
local runs on 2026-10-21; the API at 0.1 CPU / 512 MB, the database at
Neon's 0.25 CU floor and 2 CU ceiling):

| Request | 0.25 CU, p50 | 2 CU, p50 |
| --- | --- | --- |
| metadata route or cached analysis, one client (no SQL; the 0.1-CPU API serves 33–35 metadata requests/s and 67–71 cache hits/s) | 1.6–1.9 ms | 1.6–1.9 ms |
| uncached single-year estimate with replicate SEs | 0.99 s | 79 ms |
| worst cases: pooled 22 years, 23-year trends (by kind) | 21.4–21.8 s | 2.03–2.12 s |
| F66 broad selector pooled or trended / compared by sex | 33.5–35.0 s / 69.2 s | 2.67–2.68 s / 2.99 s |
| container cold start to the first `/health` (0.1 CPU; Render's spin-up excluded) | 5.8 s (13.1 s before 5844ab7) | 12.9 s before 5844ab7; not re-measured |

The cold-start row's current figure comes from a later A/B on the same stack
([`../loadtests/results/2026-10-21-cold-start-ab.md`](../loadtests/results/2026-10-21-cold-start-ab.md),
n=8 per image, at 0.25 CU only). It compares the image at e5ca1fd, which
precompiles the standard library (commit 5844ab7), with one built before that
commit, which took 12.7 s at p50. The figures in parentheses are the emulation
runs' own, from an earlier image.

The analyses in these rows ran one at a time, and PostgreSQL's statement
log puts 96.6–99.9% of their summed latency in the database (the metadata
and cached rows run no SQL). At 0.25 CU
throughput peaks at one analysis in flight (1.14 per second), and the
admission lanes shed with `503 overloaded` from 4 concurrent clients; at 2 CU
it plateaus at about 24 per second from 2 clients, with no shed. The API's
peak RSS stayed at or below 76.1 MiB of 512 MB. These runs kept every
protection default, including the 85 s statement timeout, which is only
1.2 × the F66 compare's p99 at the floor
([ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13)).
The Phase 5 audit's timed measurements of the pre-Phase-5 code (latency at
0.25 CU and 2 CU, saturation, memory, cold start) are in
[`evidence/phase5/01-audit-timed.md`](evidence/phase5/01-audit-timed.md) and
summarized in [operations.md](operations.md).

## Design decisions and tradeoffs

**Streaming stdlib CSV instead of pandas.** Transformations are row-wise and
typed; streaming keeps peak memory flat regardless of file size and keeps the
transformers pure and unit-testable.

**Full rebuild per release, built beside the live data.** BLS re-releases the
entire multi-year dataset each year (weights and harmonized codes can change
for past years), so incremental loading would be wrong. Building in a shadow
schema and switching in one short transaction keeps "always the complete old
or the complete new data" without blocking readers (ADR-013).

**Canonical, not exhaustive.** The database holds the columns analyses
foreseeably need, exactly typed and documented, not every BLS variable as
text. The full variable set stays in the raw and staged files; adding a
column is a migration plus one transformer line.

**Constraints in the schema, expectations in validation.** Rules BLS
documents unconditionally are CHECK and foreign-key constraints. Expectations
that are empirical or could change (row counts, topcodes, year coverage) are
validation checks that report rather than block.

**A reduced serving database instead of a bigger host.** The production
database is derived from the full one and proven equivalent locally (ADR-013,
Decision 10, and the evidence in
[`evidence/phase5/equivalence/`](evidence/phase5/equivalence/)), so a free
tier can serve the same numbers without a paid plan.

**Protection at two layers.** The edge governor is durable and sees all
Worker traffic; the API's own buckets, daily cap and lanes protect the
process even when the Worker is bypassed (ADR-012, "Relation to the edge
governor").

## Decision records

| ADR | Decision |
|---|---|
| [001](adr/ADR-001-analytical-layer-architecture.md) | Analytical layer architecture |
| [002](adr/ADR-002-weighted-estimator-design.md) | Weighted estimators over sufficient statistics |
| [003](adr/ADR-003-replicate-variance-method.md) | Replicate-weight variance |
| [004](adr/ADR-004-2020-handling.md) | 2020 statistical handling |
| [005](adr/ADR-005-summary-vs-episodes.md) | Episode aggregation instead of the Activity Summary file |
| [006](adr/ADR-006-sql-vs-python.md) | SQL for weighted sums, Python for estimators |
| [007](adr/ADR-007-frontend-stack.md) | Vite + React + TypeScript, client-side only |
| [008](adr/ADR-008-server-state.md) | TanStack Query, version-tagged analysis keys |
| [009](adr/ADR-009-shareable-urls.md) | Share links encode the canonical spec |
| [010](adr/ADR-010-visualization.md) | Hand-rolled SVG charts |
| [011](adr/ADR-011-hosting.md) | $0 hosting: Cloudflare Workers, Render Free, Neon Free |
| [012](adr/ADR-012-load-and-quota-protection.md) | Load and quota protection in the API |
| [013](adr/ADR-013-data-releases-and-serving-db.md) | Data releases, the local reload and the serving database |
| [014](adr/ADR-014-observability.md) | Observability on free tiers |
| [015](adr/ADR-015-edge-cache-snapshot-and-governor.md) | Edge result cache, static snapshot and quota governor |
| [016](adr/ADR-016-short-share-links.md) | Short share links deferred |
