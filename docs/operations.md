# Operations

This document covers how the ATUS platform is run on the $0 stack (Cloudflare Workers Free, Render Free, Neon Free, GitHub; [ADR-011](adr/ADR-011-hosting.md)). It has four parts:

- [Phase 5 audit](#phase-5-audit-2026-10-20): what was found before any Phase 5 change, at commit 387fc2d. Kept as written.
- [Audit resolution](#audit-resolution): what Phase 5 did with each finding.
- [Equivalence criterion (§9)](#equivalence-criterion-9): when the serving database and the full database count as giving the same answers, where that is checked, and the results.
- [Runbooks](#runbooks): deploy, rollback, data releases, restores, incidents, secrets, quotas and provider changes.

**Status: not deployed.** Everything below has run locally: unit and integration tests, the production-mode compose stack, its free-tier emulation profile and local deploy rehearsals. Nothing has run against a provider, and no runbook step that needs an account has been executed. Performance numbers for the fixed code come from the local emulation runs of 2026-10-21 ([`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md)) and the calibration taken from them ([ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13)). The audit's timed numbers measure the code as it was before the fixes.

## Phase 5 audit (2026-10-20)

### Baseline

The audit ran against commit 387fc2d (branch `phase5`) and the full local database (release `0325`, one ingestion run). The full output is in [`evidence/phase5/00-baseline.md`](evidence/phase5/00-baseline.md).

| Suite | Result |
|---|---|
| Backend lint (`ruff`) | clean |
| Backend tests | 244 passed = 194 unit (0.50 s) + 50 integration (11.11 s) |
| `atus validate-analytics` (full DB) | 24/24 BLS benchmarks pass |
| `ATUS_DATA_QUALITY=1 pytest tests/data_quality` (full DB) | 14/14 pass |
| Frontend typecheck, lint, format | pass (3 `only-export-components` lint warnings) |
| Vitest | 119/119 |
| Playwright E2E (`CI=1`, against `scripts/e2e_backend.py`) | 20/20 |
| Production bundle (vite) | `index-*.js` 365.00 kB (111.85 kB gzip), CSS 14.97 kB (3.82 kB gzip), `index.html` 0.64 kB |

### Method

The audit ran in four parts:

- **Parallel readers.** Four agents covered the API, the engine and loader, the frontend, and CI/docs/packaging. They read the code and the installed library sources, and ran small confirming experiments on scratch databases and local builds.
- **Live experiments.** Six agents ran experiments on scratch databases named `atus_audit_*` and on throwaway containers:
  - reload under load;
  - client disconnects and timeouts;
  - server-side statement counts;
  - exposure and error paths;
  - frontend cold start;
  - a prototype serving database (described in [`evidence/phase5/02-serving-proto.md`](evidence/phase5/02-serving-proto.md)).
- **One serialized timed agent.** This agent was the only ATUS audit workload running while it measured free-tier fit and saturation. Its report is in [`evidence/phase5/01-audit-timed.md`](evidence/phase5/01-audit-timed.md).
- **Verification.** Every finding went to two independent refuters. A tie-break agent ran wherever the refuters disagreed on substance. Refuted findings are listed under "Refuted or corrected claims". Where a refuter corrected a confirmed finding, this document uses the corrected wording.

**Evidence rules**

- Raw text outputs (logs, scripts, query results; files under 300 KB) are committed under [`docs/evidence/phase5/audit/`](evidence/phase5/audit/) as `$AUDIT`. Each reference below has the form `$AUDIT/<agent>/<file>`. Large payloads, screenshots, local virtualenvs and copies of third-party documentation pages were not committed.
- Code references are repo-relative `path:line` at 387fc2d.
- **Timings marked *illustrative*** come from experiments that ran while other audit agents shared the machine and the Postgres server. Only the timed agent's numbers were measured in isolation, though other projects' idle containers and host load (load average 3.0–4.7) remained.
- Counts, sizes, hashes and equality results do not depend on load.

**Hardware.** Apple M4 with 10 cores and 24 GB of memory. Docker Desktop ran a VM with 10 vCPU and 7.75 GiB.

**Severity rule.** A finding's severity is the median of three ratings: the finder's and the two refuters'. A tie-break verdict overrides the median. When findings are merged into one row of the table, the row takes the highest severity among them.

**Side effects on the full database.** All access was read-only, with one exception: `atus validate-analytics`, which by design INSERTs 24 rows into `atus.validation_results` on every run (`src/atus_pipeline/analytics/benchmarks.py:304-318`). It ran twice: once for the baseline and once in the serving-prototype comparison. The table held 196 rows when it was measured between those two runs and 220 rows at the end of the audit, both counts taken with read-only queries. Nothing else was written.

**State left running.** The prototype serving database remains in container `atus-emu-pg` (port 5441) for the implementation phase. All other audit containers and scratch databases were removed. Besides this file and `docs/evidence/phase5/01-*.md` and `02-*.md`, no repository file was changed.

### 7.1 Concurrent requests during `atus load`

**Answer.** On the database the API reads, a reload makes every database-backed request block or fail with 503 for the whole load, and the reload itself can fail. Of the API routes, only `/health` and a few routes that never touch the database stay fast.

**Mechanism (code)**

- `load_all` runs one `TRUNCATE` over all 10 canonical tables, followed by every `COPY`, in a single transaction (`src/atus_pipeline/loading/loader.py:36-47,203-212`). That transaction holds ACCESS EXCLUSIVE on all of them until it commits.
- The run row is inserted, and later marked `succeeded`, in separate transactions (`loader.py:193-198,223-228`).
- Nothing sets `lock_timeout` or `statement_timeout`; the pool is created with no options (`src/atus_pipeline/api/app.py:62-68`).
- The one load in the full database took 2:29.58 (`$AUDIT/read-engine-loader/ingestion_runs_full_db.txt`). That is an upper bound on how long the locks are held.

**Measured** on the fixture database with the real loader and an injected hold inside its data transaction (`$AUDIT/exp-reload/`; illustrative):

- **Exp A (12 clients, 60 s hold).**
  - Within 0.12 s, all 10 pool connections were held by requests blocked on locks (`/ready`, `/meta`, `/activities`, uncached analyses). Those requests hung for 60.0–60.13 s.
  - Every later database-backed request got 503 `database_unavailable` after 10.00–10.01 s, for 60 such responses in total.
  - 50 of 60 probes for an already-cached spec also got 503. A cache hit reads only the unlocked `ingestion_runs` table, but it still needs a pool connection (`src/atus_pipeline/api/dependencies.py:63-64`, `src/atus_pipeline/api/routes/analysis.py:117-124`).
  - `/health` returned 200 every time, max 22 ms (`expA/summary.txt`).
- **Exp C (60 clients, 45 s hold).** All 40 anyio threads were in use, with up to 93 tasks waiting for a thread. Sync endpoints that never touch the database stalled for up to 24.9 s. 503s took 10.0–23.9 s, because the 10 s pool timer starts only once a thread is free (`expC/summary.txt`).
- **Deadlock.** TRUNCATE locks tables in list order. Engine requests lock `respondents` first (`src/atus_pipeline/analytics/engine.py:310-312`) and `activities` later (`src/atus_pipeline/analytics/queries.py:123-128`).
  - With a reader in flight when the TRUNCATE started, plus concurrent uncached analyses, PostgreSQL aborted the real loader in 2 of 2 runs (`expB/`, `expB2/`) and in 3 of 3 runs of a separate experiment (`$AUDIT/read-engine-loader/exp_load_deadlock2.out`).
  - Without uncached analyses there was no deadlock (`expB3/`).
  - The failure is safe: the transaction rolls back and the run is marked `failed`. In practice the window is milliseconds wide; the experiments widened it.
- **After the commit.** Blocked analyses finished 3–47 ms later with the new values and `X-Cache: bypass`.
  - Exceptions: `/meta` requests that had been blocked during the load returned the old `ingestion_run_id` together with the new data, with `max-age=300` (`$AUDIT/exp-reload/expA/summary.txt`).
  - Because the version bump is a separate transaction, results can be cached under the pre-reload key. This was seen once (5 of 10 woken requests, `expE/api_log_excerpt.txt`) and never in reruns that checked the stored keys.
  - An emulated failure between the data commit and the status update left stale cache hits and fresh misses served under one `data_version`, with the run stuck at `running` and `atus load` exiting 1 (`expD/summary.txt`).
- **REPEATABLE READ caveat.** A REPEATABLE READ request whose snapshot predates the commit saw the truncated tables as empty and got "The database contains no respondents" (`$AUDIT/read-engine-loader/exp_load_locks.out`). Requests run at READ COMMITTED today, so they are not affected.

Findings: F08, F19, F20, F21, F27.

### 7.2 What bounds one request's cost; disconnected clients

**Answer.** No time limit applies anywhere; the only bounds are the input limits multiplied by the data size. A disconnected client's analysis runs to completion and its result is cached.

- **No timeouts are set.**
  - The server reports `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`, `idle_session_timeout` and `client_connection_check_interval` all at 0, with source `default` (`$AUDIT/exp-disconnect-timeout/db_settings.txt`).
  - The pool passes no options (`app.py:62-68`), and `atus api` passes only host, port and reload to uvicorn (`src/atus_pipeline/cli.py:186-192`).
  - The only timeout is the 10 s wait for a pool connection (`dependencies.py:24,38`).
  - There is no rate limit and no authentication.
- **Input limits.**
  - Up to 50 years pass the schema (`src/atus_pipeline/api/schemas/analysis.py:130-133`), but the engine rejects years that are not loaded (`engine.py:207-213`), so at most 23 years run.
  - `include` and `exclude` accept up to 100 codes each (`schemas/analysis.py:58-63`).
  - A compare runs two statistics queries (`engine.py:156-157`). With replicate variance, each query has 480 replicate aggregates (`queries.py:77-85`).
  - On the laptop with the full DB (illustrative): a 23-year replicate trend took 11.4–11.6 s, the same trend over all tier-1 codes 15.0 s, and a compare over all codes for 22 years 31.3 s (`$AUDIT/exp-disconnect-timeout/expA_api_log_slice.txt`, `expD1_api_log_slice.txt`, `expC_api_log_slice.txt`).
- **The cache does not bound cost.** The key covers the spec as written: label, `confidence_level`, duplicate codes, prefix versus leaf spelling (`src/atus_pipeline/api/cache.py:26-46`, `schemas/analysis.py:75-83`). Trivial variations miss the cache (`$AUDIT/exp-exposure-errors/raw/cache_bust.txt`). Identical requests already in flight are not merged.
- **Disconnects do not cancel work.**
  - Sync handlers run in anyio's threadpool and cannot be cancelled, and nothing checks for a disconnect.
  - In one run the client gave up at 1.005 s, but the query (a leader plus 2 parallel workers) ran for about 11.5 s, was logged as `200 ... cache=miss`, and the identical follow-up request was a cache hit (`$AUDIT/exp-disconnect-timeout/expA_*`).
  - Five abandoned identical requests ran as 5 concurrent computations. 3 of them failed with `DiskFull` on Docker's default 64 MiB `/dev/shm` and were returned as 503 (`expB_*`, `expB2_*`).
  - On SIGTERM, uvicorn waited for the abandoned analysis to finish. After SIGKILL, the backend kept running for about 8 s (`expD1_*`, `expD3_*`).
- **Request size is unbounded.**
  - No body limit exists: one 100 MB POST raised server RSS from 54 to about 500 MB before a 422 (`$AUDIT/read-api/logs/bodysize_100MB.txt`). Three concurrent 50 MB bodies peaked at 587 MB (`$AUDIT/exp-exposure-errors/raw/body_mem_test.txt`; macOS RSS, so an OOM on a 512 MB instance is inferred, not observed).
  - 422 responses list every validation error. A 930,180-byte body produced a 9,730,211-byte 422 (`$AUDIT/exp-exposure-errors/raw/post_extrakeys100k_short.txt`; `src/atus_pipeline/api/errors.py:104-124`).
- **A server-side timeout works but is misreported.** A `statement_timeout` set through the connection URL does cancel the query. The API then returns 503 `database_unavailable` "Try again shortly" (`$AUDIT/exp-disconnect-timeout/expD2_driver.txt`), because every `OperationalError` maps to that response (`errors.py:142-150`).

Findings: F01, F03, F04, F15, F33, F34, F52, F53.

### 7.3 Pool and threadpool under load

**Answer.** On free-tier compute, database CPU saturates first. The 10-connection pool then turns the overload into 503s after 10 s, labelled "database unavailable". With about 50 or more concurrent database requests, a threadpool convoy also stalls the whole API.

- **Configuration.** The pool has min 1 and max 10 connections (`app.py:62-68`), and a request waits up to 10 s to acquire one (`dependencies.py:24,38-43`). anyio's default limiter allows 40 threads. Each sync dependency and the endpoint itself is a separate trip through the threadpool, so a request waiting for a connection holds a thread, while requests that already hold a connection need more threads to finish.
- **Measured with instant queries** (fixture DB, real uvicorn; illustrative; `$AUDIT/read-api/logs/saturation_N*.txt`):
  - 41 concurrent requests: all 200 by 0.16 s.
  - 50 concurrent: 40 × 503 at 10.04–10.07 s and 10 × 200.
  - 60 concurrent: 40 × 503 plus 20 × 200, all at about 10 s.
  - Sync endpoints that never touch the database, such as `/population/metadata`, took about 9 s. The async `/health` took 2–24 ms.
- **Measured at the 0.25 CU emulation** (timed agent):
  - Distinct uncached single-year estimates share 0.25 vCPU. Latency was 2.3–6.1 s at c=1, 4.4–5.4 s at c=2, 7.9–13.2 s at c=4, and 18.5–39.0 s at c=8.
  - At c=16, 10 requests returned 200 after 16.6–45.2 s and 6 returned 503 at 10.04–10.13 s.
  - The API's CPU was throttled in at most 6 of 397 periods, and its memory stayed at or below 49 MB. The 40-thread pool was never the limit at c ≤ 16.
  - At 2 CU, c=16 all returned 200 within 7.94 s.
- **Database unreachable** (illustrative): 45 concurrent `/ready` all returned 503 after 10.0–19.97 s, and a concurrent `/population/metadata` took 8.97 s (`$AUDIT/exp-exposure-errors/raw/bogus_threadpool_exhaustion.txt`).

Findings: F02, F17, F35, F36.

### 7.4 Can `data_version` repeat for different data?

**Answer.** Yes. `read_data_version` returns `<release>:run<id>` (`src/atus_pipeline/api/dependencies.py:50-60`), and `id` is `GENERATED ALWAYS AS IDENTITY` (`migrations/0001_core_schema.sql:13`), so every freshly built database starts at `run1`.

- **Measured `0325:run1`** on all of these: the full DB (258,954 respondents), `atus_e2e`, fixture scratch databases (6 respondents), and the prototype serving DB (`$AUDIT/read-api/data_versions_across_dbs.txt`, `$AUDIT/read-engine-loader/exp_versions_writes.out`, `$AUDIT/exp-serving-proto/final_state.txt`).
  - `pg_dump`/`pg_restore` keeps the sequence through `setval`; a new build does not.
- **Where the version is used:**
  - the server cache key and `X-Analysis-Key` (`routes/analysis.py:77-78`, `cache.py:26-46`);
  - the re-read guard (`analysis.py:88`);
  - the frontend's analysis query key, rebuilt from `/meta` (`frontend/src/api/queries.ts:83-85,111`).
- **Browser-side staleness.**
  - `/meta` is sent as `public, max-age=300` (`src/atus_pipeline/api/routes/meta.py:66`), and the frontend's `staleTime` is also 5 min (`queries.ts:25,39`).
  - A reload within 300 s sent 0 `/meta` requests (`$AUDIT/read-frontend/exp_refetch_and_swap.json`).
  - So a tab keeps the old tag for about 5–10 min after a database swap, or indefinitely if the run id repeats.
- **Exposure today is limited:**
  - the server cache is per process and is lost on restart;
  - analysis POSTs carry no `Cache-Control`;
  - the TanStack cache is per tab.

  It becomes a stale-result bug as soon as Phase 5 switches a running API to a rebuilt or serving database.
- **The loader's separate status transaction** is covered under 7.1.

Findings: F06, F20, F27.

### 7.5 Does the API write?

**Answer.** No. A read-only API role works.

- **Where writes exist.** The only write SQL in `src/` is in `database/migrate.py`, `loading/loader.py`, `validation/db_checks.py` and `analytics/benchmarks.py:311-318`, and `api/` imports none of them.
- **Read-only test.** With `default_transaction_read_only=on`, every endpoint returned its normal status (`$AUDIT/read-api/logs/readonly_test.txt`, `$AUDIT/read-engine-loader/exp_versions_writes.out`). That covered cache hits and misses, both weight schemes, both variance modes, roster and CPS filters, and error responses.
- **Statements the API sends:**
  - `SELECT`s only;
  - psycopg's implicit `BEGIN`/`COMMIT`/`ROLLBACK`;
  - auto-prepared statements after 5 executions;
  - `DEALLOCATE ALL` after a `ROLLBACK` on a connection that holds prepared statements (`$AUDIT/exp-statements/statements_table.txt`).
- **`atus validate-analytics` does write.** It INSERTs 24 rows into `atus.validation_results` (`src/atus_pipeline/analytics/benchmarks.py:304-318`).
  - Under a read-only session it exits 1 with a `ReadOnlySqlTransaction` traceback, prints no results (`src/atus_pipeline/analytics_cli.py:356-366`), and discards what it computed.
  - One database error inside `run_benchmarks` makes every later benchmark fail with `InFailedSqlTransaction` (`$AUDIT/read-engine-loader/exp_benchmark_abort3.out`).
  - It must run as the owner role, and the serving DB must keep `validation_results`.

Findings: F22; fact I05.

### 7.6 What `/ready`, `/docs` and `/openapi.json` expose

**Answer.** Only `/health` should be public. `/ready` touches the database and should not be public. The interactive docs pages should be disabled in production.

- **`/ready`** (`src/atus_pipeline/api/routes/health.py:45-55`) is public and uncached.
  - Each call checks out a connection and runs 4 statements: `BEGIN`, `SELECT 1`, `SELECT EXISTS(... respondents)`, `COMMIT`.
  - Failure bodies carry operator hints (`health.py:52-54`).
  - If a platform check or uptime monitor polls it more often than Neon's 5-min idle window, the database never suspends. By the brief's figures (0.25 CU, 100 CU-h/month) that is about 182 CU-h/month, over quota. This is arithmetic, not a measurement.
  - When the database is down, each call holds a thread for 10 s. During a reload it hangs or returns 503.
  - `HEAD` returns 405, as it does on `/health`.
- **`/docs`, `/redoc`, `/docs/oauth2-redirect` and `/openapi.json`** are served by FastAPI defaults (`app.py:77-82`).
  - `/docs` loads `swagger-ui-dist@5` from cdn.jsdelivr.net and a favicon from fastapi.tiangolo.com.
  - `/redoc` loads `redoc@2` from cdn.jsdelivr.net plus Google Fonts.
  - These are floating major versions with no SRI, and they would break under a strict CSP.
  - `/openapi.json` is 32,892 bytes. It contains no secrets and costs 0 database statements.
- **Responses in general.**
  - No response carries security headers: none of 183 recorded responses had CSP, HSTS, `nosniff`, `Referrer-Policy` or `frame-ancestors`.
  - Every response advertises `server: uvicorn`, and nothing is compressed (`$AUDIT/exp-exposure-errors/raw/header_inventory.txt`).
  - Error bodies leak no stack traces, SQL, file paths or connection strings. One caveat: a `%` in the database password makes psycopg_pool log the password at WARNING.

Findings: F16, F42, F43, F46, F47; fact I07.

### 7.7 Frontend behaviour on an API version mismatch

**Answer.** It does not fail clearly.

- `api_version` is only displayed (`frontend/src/components/AppShell.tsx:43`, `frontend/src/pages/AboutPage.tsx:88`), and the `/api/v1` prefix is hard-coded (`frontend/src/api/client.ts:14`).
- If `/meta` reports `v2`, the app accepts it silently and keeps posting to `/api/v1`.
- If the `/meta` shape changes, `AppShell.tsx:41` throws on every route, and the root ErrorBoundary blames "ATUS Explorer itself".
- If `/api/v1` returns 404, users see generic "could not be reached" or "unexpected problem" messages.

Evidence: `$AUDIT/exp-frontend-coldstart/runs/{c_v2_same_shape,c2_v2_changed_shape,c3_v1_removed_404}/`, `$AUDIT/read-frontend/exp_errors.json`. Refuters rated this medium: the realistic trigger is a deploy where frontend and API versions are out of step.

Finding: F25.

### 7.8 Hard-coded release identifiers

**Answer.** `ATUS_RELEASE` reaches the whole pipeline CLI, and the API takes the release from the database. Release-specific literals remain in served text and in tests and scripts.

**Where the release does flow through**

- `ATUS_RELEASE` (`src/atus_pipeline/config.py:18,60`) drives the CLI's download, extract and validate-source steps.
- It sets the lexicon path `activity_lexicon_<release>.csv` (`loader.py:61-62`), the staging directory (`config.py:36-37`), `ingestion_runs.release`, and the `validate-db` expectations.
- The API reads the release from the database, not the environment (`dependencies.py:50-60`, `meta.py:56-76`).

**Hard-coded:**

- **Source registry.** It knows only `0325` (`src/atus_pipeline/sources.py:95-208`). Setting `0326` raises `KeyError`.
- **Text served to users:**
  - the warning "BLS-harmonized 2003-25 multi-year lexicon", added to every analysis response (`src/atus_pipeline/analytics/engine.py:47-50`);
  - error messages (`src/atus_pipeline/analytics/activities.py:56-58`, `src/atus_pipeline/api/errors.py:40`);
  - OpenAPI text (`app.py:38`; `routes/activities.py:54-56`, which also fixes the 18/107/431 code counts).
- **Tests, fixtures and scripts:** the fixture release (`tests/integration/analytics_dataset.py:226`), `scripts/export_openapi.py`, and `range(2003, 2026)` in the benchmark scripts and data-quality tests. A full grep is in `$AUDIT/read-engine-loader/release_grep_all.txt` (215 hits).
- **Docs:**
  - The release procedure in `docs/source-data.md:98-109` lists none of the above.
  - `docs/architecture.md:37-40` claims a new release needs no scattered code changes.
- **Frontend:** no release identifiers outside test fixtures.

Findings: F23, F55.

### 7.9 Free-tier fit

**Answer.** Memory and API CPU fit easily. Database compute does not fit the current request mix at Neon's 0.25 CU floor:

- worst-case analyses take 47–52 s at p50;
- 16 concurrent analyses produce 503s;
- every cache hit and invalid request still reaches the database;
- the pool hands out dead connections after the database closes them.

Details are under "Free-tier fit measurements" below. Brief summary:

- **RSS.** About 70 MB peak (VmHWM 70,364 kB) after 300 distinct analyses, with the 256-entry cache full and overflowing. That is more than 7× under 512 MB for well-formed traffic. Hostile request bodies are a separate problem (F03).
- **Cold start.** At 0.1 CPU the container reaches its first `/health` 200 at p50 7.65 s, plus the platform's own spin-up, which was not measured.
- **Latency.** At 0.25 CU (`jit=off`), the database accounts for at least 98.4% of every uncached request's wall time. The 0.1-CPU API adds 7–17 ms per request, and 96–189 ms for trends.
- **Statements per request.** Counted server-side, including implicit transaction statements (table below). A cache hit costs 3. Malformed JSON and DB-free endpoints cost 0.
- **psycopg_pool background traffic.** None. Idle periods of 12.5 min (API) and 25 min (identical pool) produced 0 statements (`$AUDIT/exp-statements/idle_monitor.out`, `pool_probe.out`).
  - `max_lifetime` (3600 s) is applied only when a connection is returned after a request.
  - `ShrinkPool` closes at most one connection per 600 s. Going from 10 back to 1 connection takes about 100 min, extrapolated from code; only the 10 → 9 step was observed, at t+1200 s.
- **Does `min_size=1` keep a scale-to-zero database awake?** Not tested on Neon. Neon's docs say compute suspends after 5 min with "no active queries" and that inactive connections are closed, so the idle connection should not burn CU-hours. However, `pool.open()` at API start connects immediately, which wakes the database on every API cold start (`app.py:70`).
- **Dead connections.**
  - After `pg_terminate_backend`, `docker restart` or `docker kill`, the next K database requests each got an immediate 503, cache hits and `/ready` included, where K is the number of pooled connections. 10 of 10 were observed with 10 connections (`$AUDIT/exp-statements/stale_terminate*.out`, `outage_*.out`).
  - The pool made no reconnect attempt while idle.
  - `check=ConnectionPool.check_connection` replaced a terminated connection transparently in 11.2 ms, at the cost of one empty statement per checkout (`check_probe.out`).

Findings: F05, F10, F14, F17, F18, F35, F37–F41, F67, F68; facts I08, I09.

These are the audit's numbers for the code at 387fc2d and stay as measured; the fixed code's numbers under the same emulation are in [`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md) and the current calibration in [ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13).

### 7.10 Frontend when `/meta` takes about 60 s

**Answer.** The user sees a long, unexplained spinner, and on the deep-link page it is mislabelled. If the host answers with errors while waking, the user sees an error page after about 3 s that never recovers by itself.

- **With `/meta` held for 60 s** (`$AUDIT/exp-frontend-coldstart/runs/a_meta_delay60/`, `a2_spinup60/`):
  - Home shows "Loading starter examples…".
  - `/explore` shows "Loading the analysis builder…".
  - A shared deep link shows "Calculating estimate…" plus a hint that it can take "around ten seconds". No analysis has been sent at that point: the POST went out at 60.055 s, after `/meta`. The analysis query is disabled until `/meta` supplies the version tag (`frontend/src/pages/AnalysisPage.tsx:194-202`, `frontend/src/api/queries.ts:114`).
  - There is no client timeout (`client.ts:110-116`). Nowhere does the UI say the service is waking up.
- **Retries.** Metadata queries retry at most twice, only on `NetworkError` or HTTP 503 (`queries.ts:28-33`), with backoff gaps measured at 1.006 s and 2.006 s.
  - A wake-up that keeps answering errors produces an error page at about 3.07 s that stays: 503s, HTML 200 loading pages, and 502/429 responses without CORS headers all behaved this way.
  - Recovery needs a "Try again" click (two on Explore) or refocusing the tab.
  - A 503 caused by the 10 s pool timeout fails at about 33 s. If every `/meta` waits 60 s and then returns 503, the error appears at 183.2 s.
  - Analyses are never retried (`queries.ts:119`).
- **Error copy.**
  - A 429 (JSON or Cloudflare 1027) and HTML 502/504 pages get the generic "problem in the service" panel, and `Retry-After` is ignored (`frontend/src/domain/presentError.ts:98-108`).
  - The 503 copy claims "The service is up" (`presentError.ts:86-97`).
  - Home has no retry control (`frontend/src/pages/HomePage.tsx:122-127`).
- **Background refetch.** A failed background refetch of `/meta` unmounts the Explore builder, losing the user's edits, and hides a displayed result (`ExplorePage.tsx:59-61`, `AnalysisPage.tsx:189-193`; `$AUDIT/read-frontend/exp_refetch_and_swap.json`).

Findings: F11, F12, F13, F26, F62–F64; fact I14.

### Confirmed findings

There are 71 findings (F) and 14 recorded facts (I): 1 blocker, 12 high, 28 medium, 30 low and 14 info. Severity follows the rule under Method.

Kinds: defect = wrong behaviour today; risk = harm under plausible conditions; gap = something missing; doc-inaccuracy; fact = a verified property recorded for the design.

Fix references:

- §-numbers refer to the Phase 5 brief.
- ADR-011, ADR-012 and ADR-013 are the hosting, protection and data-release ADRs that Phase 5 will write.
- "Test" means a regression test added with the fix.

| Id | Severity | Kind | Title | Evidence (one line) | Planned Phase 5 fix |
|---|---|---|---|---|---|
| F01 | blocker | defect | Nothing bounds one request's database time: no `statement_timeout`, lock or idle-in-transaction timeout, no request deadline, no cancellation on client disconnect | Server settings all 0 (`exp-disconnect-timeout/db_settings.txt`); `app.py:62-68`, `cli.py:186-192`; client left at 1.005 s, query ran ~11.5 s and its result was cached | `statement_timeout` (role-level, sized from the §13 emulation) plus `idle_in_transaction_session_timeout`; an API deadline that cancels the query on timeout or disconnect; admission cap on uncached analyses (§10); distinct timeout error code; test |
| F02 | high | defect | Threadpool/pool convoy: about 50+ concurrent DB requests hold all 40 anyio threads, nothing progresses until the 10 s PoolTimeout, and DB-free sync endpoints stall | N=41: all 200 in 0.16 s; N=50: 40 × 503 at ~10.05 s (`read-api/logs/saturation_N*.txt`, illustrative); reload Exp C: 40/40 threads busy, up to 93 waiting | Async admission limit ≤ pool size, failing fast with 503/429 + `Retry-After`; make DB-free routes and trivial dependencies async; one threadpool hop per request; configurable sizes; concurrency test |
| F03 | high | defect | No request-body size limit | 100 MB POST: RSS 54 → ~500 MB, then 422 (`read-api/logs/bodysize_100MB.txt`); 3 × 50 MB concurrent peaked at 587 MB (macOS RSS; OOM at 512 MB inferred) | Pure-ASGI cap (e.g. 64 KB) on `Content-Length` and on streamed bytes, enforced before any DB checkout, returning 413; same cap in the Worker; test |
| F04 | high | defect | 422 bodies are uncapped and uncompressed: about 10× egress amplification; `preset` has no `max_length` and is echoed back | 930,180-byte body → 9,730,211-byte 422 (`exp-exposure-errors/raw/post_extrakeys100k_short.txt`); `errors.py:104-124`, `schemas/analysis.py:54-57` | Cap the error list and truncate echoed strings; `max_length` on `preset`; body cap (F03); compression; test that adversarial 422s stay small |
| F05 | high | defect | The pool hands out server-closed connections: one 503 per dead pooled connection after the DB suspends or restarts (cache hits included) | `app.py:62-68` (no `check=`); 10/10 consecutive 503s after `pg_terminate_backend` (`exp-statements/stale_terminate.out`) | `check=ConnectionPool.check_connection` (count the extra statement); `connect_timeout`; `min_size=0` with a short `max_idle`; test that terminates backends and expects 200 |
| F06 | high | defect | `data_version` (`<release>:run<id>`) is not globally unique; every fresh DB, including the serving DB, reports `0325:run1`. Docs claim cache invalidation "by construction" | `dependencies.py:50-60`, `0001_core_schema.sql:13`; full DB, `atus_e2e`, fixture DBs and the prototype all `0325:run1` | Globally unique version (release + build id + content hash) written by the loader and the serving builder and used in the cache key, `X-Analysis-Key`, `/meta` and the frontend tag; ADR-013; test that two builds differ; correct `docs/api.md:159-161`, `docs/frontend.md:89-92`, ADR-008 |
| F07 | high | gap | No rate limiting, per-client concurrency limit, authentication or trusted-proxy configuration; the provider hostname bypasses any protection that lives only in a Worker | `cli.py:178-192`; uvicorn trusts `X-Forwarded-For` only from loopback; disallowed `Origin` and arbitrary `Host` both get 200 (`exp-exposure-errors/raw/simple_get_evil_origin.txt`, `hdr_host_evil.txt`) | Limits enforced in the API process (§10, ADR-012); Worker-only shared-secret header checked in constant time; explicit trusted proxy chain, never `*` |
| F08 | high | defect | In-place `atus load` blocks every canonical-table read for the whole load: the first 10 requests hang, all later ones (cache hits too) get 503 after 10 s; `/ready` hangs or returns 503 | `loader.py:203-212`; full load 2:29.58; Exp A: 10 requests hung 60 s, 60 × 503 at 10.00–10.01 s, 50/60 cached probes 503 (`exp-reload/expA/summary.txt`) | Build a new target and switch to it instead of truncating live tables (§15, ADR-013); API `lock_timeout`; serve cache hits without a connection; continuous-read-during-rebuild test |
| F09 | high | gap | No Dockerfile, `.dockerignore`, production compose, emulation profile or deploy configuration | `git ls-files`: only `docker-compose.yml` (dev Postgres, floating `postgres:16`) | §8 image: multi-stage, slim, non-root, locked deps, `/health` healthcheck; production and emulation compose; `.dockerignore` |
| F10 | high | risk | Worst-case analyses take 47–52 s (p50) at Neon's 0.25 CU floor, and 64–71 s with JIT on | Timed matrix (a): pooled 22 years p50 51.7 s, trends 46.9 / 48.5 s; 2 CU: 2.7–3.4 s | Calibrate timeouts on the `jit=off` emulation; reduce cost (F38, F39) only with §9 proof; limit or shed worst-case requests at the floor with an honest message |
| F11 | high | defect | Frontend shows no cold-start message; the deep link says "Calculating estimate…" while no analysis has been sent | `AnalysisPage.tsx:194-202`, `queries.ts:114`; POST sent at 60.055 s (`exp-frontend-coldstart/runs/a_meta_delay60`) | Shared "waking up" state after ~3 s of pending metadata, separate from "calculating"; MSW and Playwright delayed-route tests |
| F12 | high | defect | Frontend retry budget is ~3 s (3 attempts, 503/NetworkError only), with no automatic recovery afterwards and no fetch timeout | `queries.ts:28-33,119`, `client.ts:110-116`; error at ~3.07 s, still shown at 75 s; pool-timeout 503 → error at ~33 s | Time-budgeted (~90–120 s) metadata retry with capped backoff that honours `Retry-After`; client timeout; one analysis retry on 503 or connection error; Home "Try again"; tests |
| F13 | high | defect | 429s, `Retry-After`, proxy and host HTML errors and quota exhaustion all show as a generic "problem in the service", and the 503 copy says "service is up" | `presentError.ts:86-108`; `client.ts:69-100` never reads `Retry-After` (`read-frontend/exp_errors.json`) | Specific presentations for rate limiting, overload, timeout, paused/quota (full-page state) and non-envelope 502/503/504/524; honour `Retry-After`; public-facing wording; tests |
| F14 | medium | defect | Every analysis request (cache hits and schema-invalid 422s included) checks out a connection and queries `ingestion_runs` before the cache lookup and before body validation | `analysis.py:117-124`; hit = 3 statements, schema-invalid 422 = 3–4 (`exp-statements/statements_table.txt`) | Keep `data_version` in process (startup or TTL); look up the cache before acquiring a connection; validate the body first; test zero statements for hits, 400 and 422 |
| F15 | medium | risk | The cache key depends on how the request is spelled (label, `confidence_level`, duplicate codes, prefix vs leaf), so it is trivially bypassed; identical in-flight requests are not merged | `cache.py:26-46`, `schemas/analysis.py:75-83`; variants miss (`exp-exposure-errors/raw/cache_bust.txt`) | Key on the resolved computation and apply labels after the lookup; merge identical in-flight requests; rate-limit misses (§10) |
| F16 | medium | risk | `/ready` is public and uncached, runs 4 statements per call and returns operator hints; with the DB down it holds a thread for 10 s | `health.py:45-55`; 503 after 10.0 s with the DB unreachable (`exp-exposure-errors/raw/bogus_ready.txt`) | `/health` as the only platform and monitor check; `/ready` internal or token-gated and cached; hints in logs only |
| F17 | medium | risk | Pool sizing is hard-coded (max 10, 10 s acquire); after a burst the pool shrinks by one connection per 600 s, which multiplies stale-connection 503s | `app.py:62-68`, `dependencies.py:24`; 10 → 9 connections at t+1200 s (`exp-statements/pool_probe.out`) | Configure from the environment; `max_size` ≤ provider limit and threadpool; `min_size=0` and a short `max_idle` on scale-to-zero DBs |
| F18 | medium | risk | psycopg auto-prepares statements after 5 executions and sends `DEALLOCATE ALL` after a `ROLLBACK`, which transaction-mode poolers may not support | `execute _pg3_0`, `DEALLOCATE ALL` in the server log (`exp-statements/statements_table.txt`) | `prepare_threshold=None` behind any pooler, or verify against it in the deploy test |
| F19 | medium | defect | `atus load` deadlocks with concurrent analyses (TRUNCATE lock order vs engine read order); the load fails safely | `loader.py:36-47,204-205` vs `engine.py:310-312`, `queries.py:123-128`; loader aborted 3/3 and 2/2 | Removed by build-and-switch (F08); any remaining swap uses `lock_timeout` + retry; test |
| F20 | medium | defect | The data commit and the `succeeded` status are separate transactions: results can be cached under the pre-reload key, and a failure between them leaves new data under the old version (run stuck at `running`) | `loader.py:203-228`; emulated failure served stale hits (75.0) and fresh misses (90.0) under one version (`exp-reload/expD/summary.txt`) | Commit the version marker atomically with the data; fix the docstrings `analysis.py:69-75` and `loader.py:3-7`; failure-injection test |
| F21 | medium | risk | REPEATABLE READ requests would see TRUNCATEd tables as empty | "The database contains no respondents" (`read-engine-loader/exp_load_locks.out`) | Use REPEATABLE READ only together with a non-TRUNCATE switch |
| F22 | medium | defect | `run_benchmarks` never rolls back: one DB error fails every later benchmark and the persist step; read-only, `validate-analytics` exits 1 with a traceback and prints no results | `benchmarks.py:281-318`, `analytics_cli.py:356-366`; `read-engine-loader/exp_benchmark_abort3.out` | Savepoint per benchmark; print before persisting; `--no-persist`; document the owner-role requirement |
| F23 | medium | gap | "2003-25" lexicon text is hard-coded in every analysis response's warnings, in two error messages and in the OpenAPI text | `engine.py:47-50`, `analytics/activities.py:56-58`, `api/errors.py:40`, `app.py:38`, `routes/activities.py:54-56` | Derive the label from the loaded release; release-neutral OpenAPI text; new-release checklist (ADR-013); test |
| F24 | medium | defect | An empty or unset `VITE_API_BASE_URL` silently bakes in `http://localhost:8000`; local production builds pick up `frontend/.env.local` | `client.ts:16-20`; `read-frontend/build_matrix_apibase_snippets.log` | Same-origin relative base as the production default, or fail the build; document it; test `apiBaseUrl`; CI check for `localhost` in the bundle |
| F25 | medium | defect | No `api_version` check: `v2` is accepted silently, and a changed `/meta` shape crashes every route into the root ErrorBoundary | `AppShell.tsx:38-45`, `client.ts:14`; `exp-frontend-coldstart/runs/c*` | Check the version in `useMeta`; non-retryable incompatibility state; defensive AppShell; unit and E2E tests (§14) |
| F26 | medium | defect | A failed background `/meta` refetch unmounts the Explore builder (losing edits) and hides a displayed result | `ExplorePage.tsx:59-61`, `AnalysisPage.tsx:189-193`; `read-frontend/exp_refetch_and_swap.json` | Page-level error only when there is no data; non-blocking banner otherwise; reconsider focus refetch |
| F27 | medium | risk | `/meta` is `public, max-age=300` and analysis results are tagged with its version: a tab keeps the old tag ~5–10 min after a swap, and a `/meta` blocked by a reload returns the old run id with new data | `meta.py:56-66`, `queries.ts:25,83-118`; 0 `/meta` requests on reload; `exp-reload/expA` | `no-cache` + ETag for `/meta`; data version in analysis responses; consistent snapshot; needs F06 |
| F28 | medium | gap | Python dependencies have only lower bounds and no lock file; CI runs and images are not reproducible | `pyproject.toml` dependency lists; venv already at fastapi 0.155, starlette 1.9 | Hash-pinned lock installed with `--require-hashes` in CI and the image; split API runtime dependencies from pipeline-only ones |
| F29 | medium | gap | No `pip-audit`, `npm audit`, image scan, Dependabot or Renovate | `.github` has only `workflows/ci.yml`; `npm audit --omit=dev` 0, full audit 2 high dev-only (`read-ci-docs/npm_audit_*.json`) | CI audit steps (`--omit=dev --audit-level=high`), image scan, `dependabot.yml` (pip, npm, actions, docker) |
| F30 | medium | defect | The documented `npm run generate:api-types` does not reproduce the committed `generated.ts`; running it breaks `tsc` with 16 errors | `frontend/package.json:17` lacks `--default-non-nullable false`; 14 defaulted request properties become required | Add the flag; CI drift job for `openapi.json` and `generated.ts` |
| F31 | medium | risk | `.gitignore` misses `.env.production`, root `.env.local`, `secrets.env` and `frontend/.env.production` | `.gitignore:13`, `frontend/.gitignore:13`; `read-ci-docs/gitignore_env_coverage.txt` | Ignore `.env*` except `*.example`; secret scanning or push protection |
| F32 | medium | risk | Compose Postgres is published on all interfaces with a superuser and a well-known password | `docker-compose.yml:3-11` (`"5434:5432"`) | Bind to `127.0.0.1`; pin the image digest; non-superuser owner and API roles in production compose |
| F33 | medium | defect | Docker's default 64 MiB `/dev/shm` makes concurrent heavy analyses fail with `DiskFull`, reported as a 503 | 3 of 5 concurrent trends failed at 62.4 MiB used (`exp-disconnect-timeout/expB2_pgstat.txt`) | `shm_size` in compose and `--shm-size` in every emulation; cap concurrent analyses; map SQLSTATE class 53 to an overload code |
| F34 | medium | defect | `QueryCanceled`, `DiskFull`, `DeadlockDetected` and `PoolTimeout` all map to 503 `database_unavailable` "Try again shortly", with no `Retry-After` | `errors.py:142-150`; timeout cancel → that 503 (`exp-disconnect-timeout/expD2_driver.txt`) | Specific codes (`analysis_timeout`, overloaded + `Retry-After`); keep `database_unavailable` for lost connections; tests and docs |
| F35 | medium | defect | At 0.25 CU, DB CPU saturates from 1–2 concurrent uncached analyses; with more than 10 in flight, requests fail with 503 after exactly 10 s | Timed saturation (a): c=16 → 10 × 200 in 16.6–45.2 s, 6 × 503 at 10.04–10.13 s; 2 CU: all 200 within 7.94 s | Cap uncached analyses (1–2 at the floor) with a structured 503/429 + `Retry-After`; cache hits outside the cap |
| F36 | medium | risk | A DB that stops responding hangs every request that holds a connection for the whole outage (no `connect_timeout`, keepalives or deadline) | 15 s `docker pause`: holders returned 200 after ~15.03 s, waiters got 503 at 10 s (`exp-statements/outage_pause15.out`) | Pool kwargs: `connect_timeout`, TCP keepalives; per-request deadline (with F01) |
| F37 | medium | risk | JIT is about 55% of the full DB's worst case; Neon computes have no LLVM, but the stock `postgres:16` image (local and emulation) runs `jit=on` | Full DB 11,958 ms with JIT vs 5,289 ms without (`exp-serving-proto/jit_probe_full.txt`); 0.25 CU pooled p50 64.5 s vs 51.7 s | `jit=off` for the API role and in every emulation; record in ADR-011 |
| F38 | medium | risk | Parallel query doubles the 0.25 CU worst case: the activity CTE is recomputed by 3 processes sharing 0.25 vCPU | 40.6 / 39.5 s default vs 19.2 / 19.1 s with `max_parallel_workers_per_gather=0` (`timed/probe_parallel.txt`) | Evaluate a role-level setting at 0.25 and 2 CU; adopt only with §9 proof and an ADR |
| F39 | medium | risk | The activity CTE is not restricted to the requested years, so single-year requests aggregate all 23 years | `queries.py:123-128`; 2.90 / 3.39 s → 0.50 / 0.90 s with the years pushed down; counts and point sums identical (`timed/explain_yearpushdown.txt`) | SQL change only with the §9 randomized equivalence check and the benchmarks |
| F40 | medium | gap | A purely relative ~1e-12 SE tolerance fails on zero-variance specs and is tight on near-universal ones | Zero-variance SE 7.83e-12 vs 7.93e-12 (1.31e-2 relative); 6.24e-13 for sleep participation pooled over 22 years (`exp-serving-proto/extremes.txt`) | Scale-floored criterion in "Equivalence criterion (§9)"; seed the randomized diff with these specs |
| F41 | medium | gap | Not emulated: Neon pageserver reads, wake-from-suspend, autoscaling, Render↔Neon RTT, Render's real CPU share and spin-up, cloud vCPU speed, Neon `work_mem`/parallel settings | `timed/neon_docs_notes.txt` | URL-targeted, quota-budgeted timing replay on the real stack (§16) before finalizing timeouts and caps |
| F42 | low | gap | No security headers, a `server: uvicorn` banner, no compression | None of 183 responses had CSP/HSTS/`nosniff`/`Referrer-Policy`; `/api/v1/activities` is 70,851 B uncompressed | Header middleware or Worker-set headers; `server_header=False`; GZip in the app or verified at the proxy |
| F43 | low | risk | `/docs`, `/redoc`, `/docs/oauth2-redirect` and `/openapi.json` are public by default, and the docs pages load floating-major CDN assets without SRI | `app.py:77-82`; swagger-ui-dist@5 and redoc@2 from cdn.jsdelivr.net | Disable docs pages in production; publish `openapi.json` statically; record the decision in `docs/security.md` |
| F44 | low | risk | A missing `ATUS_DATABASE_URL` silently falls back to the dev URL; `atus api` ignores `$PORT`; `.env` lookup depends on the entrypoint | `config.py:16,50,58`, `cli.py:178-192`; `read-ci-docs/dotenv_cwd_probe.txt` | Production mode that requires the URL and refuses localhost; host and port from the environment |
| F45 | low | defect | CORS sets no `expose_headers`, so cross-origin clients cannot read `X-Analysis-Key`, `X-Request-ID` or `X-Cache` | `app.py:84-91`; analysis-key row missing cross-origin (`read-frontend/exp_crossorigin_headers.json`) | Same-origin proxy as the only production mode; otherwise `expose_headers`; E2E assertion |
| F46 | low | defect | The HTTPException handler drops `exc.headers`: no `Allow` on 405, and a future `Retry-After` would be lost | `errors.py:126-132`; `exp-exposure-errors/raw/put_health.txt` | Pass the headers through; test |
| F47 | low | defect | `HEAD` returns 405 on `/health`, `/ready` and `/api/v1/meta` | `health.py:19-29`; `exp-exposure-errors/raw/head_health.txt` | Accept `GET` and `HEAD` on health routes; test |
| F48 | low | risk | Trailing-slash 307 redirects build `Location` from the Host header | `Host: evil.example` → `Location: http://evil.example/api/v1/meta` | `redirect_slashes=False` for the JSON API and/or TrustedHostMiddleware |
| F49 | low | risk | Slow clients (partial headers or body, idle connections) are never timed out | 3 raw-socket clients still open after 41 s (`exp-exposure-errors/raw/slow_client.txt`) | `limit_concurrency`; verify edge buffering; document |
| F50 | low | defect | A NUL byte in `search` returns a 500, and every unhandled 500 is logged twice with full tracebacks | `routes/activities.py:67,77-84`; `exp-exposure-errors/raw/search_nul.txt` | Reject control characters with 422; log once |
| F51 | low | defect | A migrated but empty DB answers 200 or 422 and leaks the `atus load` hint on analysis endpoints | `engine.py:314-318`, `errors.py:88-94` | 503 `data_not_loaded` with a generic message |
| F52 | low | risk | SIGTERM waits for abandoned analyses; after SIGKILL the backend runs the query to completion | 8.5 s shutdown delay (`expD1_driver.txt`); backend ran ~8 s after the kill (`expD3_pgstat.txt`) | F01; `timeout_graceful_shutdown` below the platform grace period |
| F53 | low | gap | Abandoned requests are logged as 200s; logs have no client IP and no incoming request id | `app.py:110-117`; `exp-disconnect-timeout/expA_api_log_slice.txt` | Log disconnects; abandoned/cancelled metric (§12) |
| F54 | low | gap | Tests never exercise the pool, threadpool, statement budgets, dead connections or limits; E2E is Chromium-only and cross-origin-only, with no slow `/meta`, version-mismatch or 429 case | `tests/unit/api/conftest.py:62-65`, `frontend/playwright.config.ts:21-32,46` | §17 tests as listed in the fixes above; Firefox and WebKit projects; same-origin E2E build |
| F55 | low | gap | The release registry, fixtures, scripts and benchmarks hard-code `0325` and 2003–2025 | `sources.py:95-208`; `tests/integration/analytics_dataset.py:226`; `range(2003, 2026)` in the benchmark scripts | Parameterize; release-checklist test; scripts read the years from `/meta` |
| F56 | low | risk | `atus migrate` works only from a source checkout | `cli.py:165-175` | Migrations as package data, or keep migration tooling out of the API image |
| F57 | low | risk | The serving DB records migrations 0001/0002 as applied, and `migrate`/`load`/`validate-db` have no serving guard | `exp-serving-proto/prototype_schema_check.txt`; `src/atus_pipeline/database/migrate.py:74-104` | Explicit serving marker that the CLI refuses to operate on; test |
| F58 | low | risk | CI uses node20 action majors (checkout@v4, setup-node@v4, setup-python@v5); Node 20 left the hosted runners on 2026-09-23 | `ci.yml:26,27,47,48,83,84,89`; `read-ci-docs/gha_action_runtimes.txt` | v7 majors pinned by SHA; Dependabot for actions |
| F59 | low | risk | CI has no `permissions`, `concurrency`, `timeout-minutes`, Python 3.11 job, Playwright trace upload or OpenAPI/types drift check | `.github/workflows/ci.yml` | Add them (§16) |
| F60 | low | risk | `scripts/e2e_backend.py` drops schema `atus` on any URL it is given and prints the URL with its password | `scripts/e2e_backend.py:64-71,84` | Refuse non-disposable targets; redact the password |
| F61 | low | gap | The About page says nothing about logging, IP handling or retention (§12) | `AboutPage.tsx:15-120` | "Privacy and logs" section once the logging design is final |
| F62 | low | risk | Returning to a tab after 5 min refetches `/meta` (3 uncached SQL statements), which can wake sleeping hosts | `main.tsx:10`, `queries.ts:25,35-42`, `meta.py:56-64` | Longer `staleTime` or no focus refetch for metadata; server-side `/meta` cache |
| F63 | low | defect | Explore's "Try again" refetches only one of two failed queries | `ExplorePage.tsx:59-69`; `exp-frontend-coldstart/explore_retry_check.txt` | Refetch every failed metadata query |
| F64 | low | risk | A transient 503 on an analysis POST is shown immediately (`retry: false`); open tabs after a DB suspend are the exposed case | `queries.ts:119`; tie-break analysis | Server fix (F05); optional single client retry |
| F65 | low | risk | No in-shell fallback if the hashed entry bundle fails to load (blank page) | `frontend/index.html` has an empty `#root`; host defaults revalidate HTML | `/assets/*` immutable; keep HTML revalidation; static fallback or module-load error handler |
| F66 | low | risk | On the serving DB the costliest analyses are broad activity selectors, not pooled sleep | All 18 tier-1 codes pooled 22 years: 2,440 ms vs 935 ms for leisure with `jit=off` (`exp-serving-proto/jit_heavy_serving.txt`, illustrative) | Include broad selectors in §13 load tests and the rate-limit cost model |
| F67 | low | fact | API container cold start at 0.1 CPU is 7–11 s, before the platform's own spin-up | Timed: `/health` p50 7.65 s; `/meta` max 11.04 s | Budget for it in the cold-start UX; no DB work at startup |
| F68 | low | risk | At 0.25 CU (1 GB) the DB cgroup was under memory pressure during the mixed matrix | 7,017 `memory.max` events (timed transcript; no raw file); the worst case's ~387 MiB working set stays resident after a restart (tie-break) | Keep the serving DB small; avoid temp spills (F39); re-measure on the provider |
| F69 | low | doc-inaccuracy | API docs: the error table omits `not_found`, `method_not_allowed` and `http_<status>`; `database_unavailable` is described as "unreachable" although PoolTimeout maps to it; the header summary omits `X-Cache: bypass`; the timing table (11.0 / 11.7 s) is not reproducible today; docstrings `dependencies.py:3-6`, `analysis.py:69-75` and `loader.py:3-7` overstate safety | `docs/api.md:118,131-143,183-190`; `exp-exposure-errors/raw/*`; timed matrix (c) | Correct in the Phase 5 docs pass |
| F70 | low | doc-inaccuracy | Frontend docs: non-2xx non-JSON responses become `ApiError malformed_response`, not `NetworkError`; "invalidates by construction" and the analysis-key display do not hold after a swap or cross-origin; the empty-base-URL fallback is undocumented | `docs/frontend.md:64-70,89-92,117,200-213`; `client.ts:69-100` | Correct |
| F71 | low | doc-inaccuracy | README/architecture/database docs: "Node 20+" (the lockfile needs ≥ 22.22); stale ADR, scripts and docs listings; "~35 checks" (38 recorded); `architecture.md` covers Phase 1 only and makes a wrong new-release claim; `validation_results` described as validate-db-only | `README.md:135,196-211,229`; `docs/architecture.md:37-40,95,145-149`; `docs/database.md:154-155` | Correct; system-level architecture doc with the deployment diagram |
| I01 | info | fact | Pre-aggregating activities to (tucaseid, code, summed minutes) is exact; a `smallint` column keeps `sum()` returning `bigint` | Equal counts and numeric sums over 9 specs / 16 year-rows; replicate sums within 8.08e-15 relative (`read-engine-loader/exp_preagg_equiv.out`) | Builder keeps `smallint` and `ORDER BY activity_code` |
| I02 | info | fact | Dropping the 8,782 all-NULL 2020 replicate rows and the 1,466 all-zero pandemic rows cannot change any result | `exp-serving-proto/verify_claims_full.txt` | Builder asserts these invariants on every build |
| I03 | info | fact | The serving-DB contract (tables, columns and keys the engine, API and `validate-analytics` read) is known; `validate-db` fails there by design | `read-engine-loader` (engine-db-reads answer); `db_checks.py:62-80` | Builder assertion plus a schema-introspection test |
| I04 | info | fact | The prototype serving DB is 598.4 MiB (cluster sum ~0.65 GB) and matches the full DB: 24/24 benchmarks, exact points, SE within 6.24e-13 relative on non-degenerate specs; its physical order survives dump/restore | [`evidence/phase5/02-serving-proto.md`](evidence/phase5/02-serving-proto.md) | Basis for the §9 builder |
| I05 | info | fact | The API never writes, so a read-only API role works | `read-api/logs/readonly_test.txt` | Read-only API role (§9) |
| I06 | info | fact | The production bundle runs under a strict CSP with no third-party requests | 0 violations (`read-frontend/exp_csp_runtime.json`) | Ship the policy via host headers; Playwright CSP test |
| I07 | info | fact | No stack traces, SQL, paths or connection strings leak in responses; a `%` in the DB password is logged by psycopg_pool | `exp-exposure-errors/raw/*`; `refute/no-internal-leaks-observed-1/server_8447.log` | Regression tests; DSN validation or log redaction at startup |
| I08 | info | fact | psycopg_pool sends no idle traffic; Neon's docs base suspension on active queries (not tested on Neon); `pool.open()` connects at startup | `exp-statements/idle_monitor.out` | Lazy pool or `min_size=0`; record in ADR-011 |
| I09 | info | fact | API memory peaks at ~70 MB for well-formed traffic at 0.1 CPU / 512 MB | `timed/rss.log` | Body cap (F03) covers hostile input |
| I10 | info | fact | The connection and its transaction are released only after the response is sent | `fastapi/routing.py:147-155`; COMMIT logged 92 ms after the response (timed pglog) | Account for it in pool sizing |
| I11 | info | fact | psycopg_pool's `requests_waiting` gauge counts timed-out waiters | `psycopg_pool/pool.py:261,298,776-780,859` | Do not alert on it alone |
| I12 | info | fact | The engine returns SE ≈ 8e-12 for estimates whose true variance is 0 | `exp-serving-proto/extremes.txt` | Covered by F40 |
| I13 | info | fact | No git tags; the package version is defined twice | `pyproject.toml:7`, `src/atus_pipeline/__init__.py:4` | Single-source the version; tag releases (§16) |
| I14 | info | fact | Analysis and lexicon requests wait for `/meta`, so cold-start delays add up in sequence | `exp-frontend-coldstart/runs/a_meta_delay60` | Optional parallel prefetch |

Audit finding ids merged into each row (evidence paths are under `$AUDIT`):

- **F01:** api-unbounded-request-cost-no-statement-timeout, api-no-statement-timeout-and-disconnect-runs-on, api-disconnect-does-not-cancel, reload-abandoned-requests-hold-pool, cache-stores-abandoned-results.
- **F02:** api-pool-threadpool-convoy, reload-threadpool-saturation, db-down-threadpool-starvation.
- **F03:** api-unbounded-request-body, api-no-body-size-limit.
- **F04:** api-validation-error-amplification.
- **F05:** api-dead-pooled-connection-503, pool-stale-connections-503.
- **F06:** data-version-not-unique, serving-data-version-collision, api-data-version-collides-across-databases, docs-data-version-by-construction-false.
- **F07:** api-no-proxy-trust-no-rate-limit, api-origin-bypasses-edge-protection, xff-trusted-from-loopback-only.
- **F08:** reload-truncate-blocks-all-db-requests, load-blocks-all-reads, reload-ready-semantics.
- **F09:** no-dockerfile-or-deploy-config.
- **F10:** worst-case-50s-at-neon-floor.
- **F11:** fe-coldstart-no-waking-message, fe-no-coldstart-ux.
- **F12:** fe-retry-budget-3s-no-autorecovery, fe-coldstart-503-becomes-error-page, fe-no-fetch-timeout.
- **F13:** fe-unknown-codes-generic-message, fe-429-retry-after-ignored, fe-nonjson-error-copy-misleading, fe-503-copy-and-no-quota-ux.
- **F14:** api-cache-hit-still-queries-db, cache-hit-wakes-db, validation-errors-wake-db, invalid-body-touches-db.
- **F15:** api-cache-key-presentation-fields, result-cache-trivially-bypassed.
- **F16:** api-ready-public-db-touching, ready-public-db-touching.
- **F17:** api-pool-size-hardcoded, pool-grows-and-shrinks-slowly, pool-retains-grown-connections.
- **F18:** api-psycopg-autoprepare-pooler, deallocate-all-after-rollback, psycopg-autoprepare-and-late-commit.
- **F19:** load-truncate-deadlock, reload-deadlock-aborts-load, api-load-deadlock-lock-order.
- **F20:** reload-version-bump-not-atomic, succeeded-status-separate-tx, doc-claims-refuted-by-reload.
- **F21:** repeatable-read-truncate-empty.
- **F22:** benchmarks-abort-cascade, validate-analytics-readonly-crash.
- **F23:** release-strings-in-served-output, release-hardcoded-harmonization-warning.
- **F24:** fe-api-base-empty-falls-back-to-localhost, frontend-api-base-fallback-and-envlocal.
- **F25:** fe-no-api-version-check (both reports).
- **F26:** fe-background-refetch-error-destroys-state.
- **F27:** fe-stale-results-across-data-swap, api-meta-cache-staleness, reload-meta-mixed-snapshot.
- **F28–F32:** py-deps-unpinned-no-lock, no-supply-chain-scanning, generated-types-not-reproducible, gitignore-env-variants, compose-db-exposed-superuser.
- **F33–F36:** docker-shm-64mb-parallel-hash-diskfull, api-operationalerror-mapping-hides-timeouts, saturation-db-cpu-then-pool-503, no-query-or-socket-timeout-hang.
- **F37:** jit-dominates-worst-case, neon-has-no-jit-emulation-must-disable.
- **F38–F41:** parallel-query-hurts-at-quarter-cpu, activity-cte-not-year-restricted, equivalence-tolerance-needs-scale-floor, not-emulated-gaps.
- **F42:** api-no-security-headers-no-compression, api-no-security-headers.
- **F43:** api-docs-public-third-party, docs-redoc-public-third-party-assets, openapi-docs-exposure-undecided.
- **F44:** api-config-unsafe-defaults, config-silent-db-default.
- **F45:** fe-cors-expose-headers-missing, cors-no-expose-headers, api-cors-no-expose-headers.
- **F46–F53:** http-exception-handler-drops-headers, head-405-on-health, host-header-redirect-reflection, slow-clients-never-timed-out, nul-search-500-double-traceback, unloaded-db-returns-4xx-with-hint, api-graceful-shutdown-and-kill-dont-stop-db-work, api-abandoned-requests-logged-as-200.
- **F54:** api-tests-bypass-pool, fe-e2e-gaps.
- **F55:** release-registry-fixtures-0325, scripts-hardcoded-release.
- **F56–F68:** migrations-not-packaged, serving-schema-migrations-stale, gha-node20-actions, ci-workflow-hygiene, e2e-backend-destructive-and-logs-url, fe-about-no-logging-statement, fe-focus-refetch-wakes-free-tier, fe-explore-retry-needs-two-clicks, fe-analysis-503-not-retried, fe-stale-index-asset-risk, serving-worst-case-is-broad-selector, coldstart-8s-at-tenth-cpu, quarter-cu-memory-pressure.
- **F69:** docs-error-table-incomplete-503-conflation, doc-400-codes-incomplete, api-docs-laptop-timings-stale, dependencies-docstring-reset.
- **F70:** fe-docs-inaccuracies, doc-frontend-nonjson-networkerror.
- **F71:** readme-node-version-wrong, readme-architecture-stale-listings, doc-validation-results-scope.
- **I01–I14:** preagg-equivalent, replicate-row-drops-safe, serving-db-contract, proto-footprint-confirmed + proto-equivalence-ok + physical-order-survives-dump, (7.5 read-only result), fe-csp-compatible, no-internal-leaks-observed, idle-pool-no-keepalive-traffic, rss-fits-512mb, api-yield-dep-exit-after-response, pool-requests-waiting-gauge-stale, nonzero-se-for-zero-variance, no-version-tags, fe-coldstart-waterfall-fact.

### Refuted or corrected claims

**Refuted by verification**

- **"`docs/api.md` overstates data-version uniqueness"** (api-docs-data-version-claim). `docs/api.md:205` says the version changes when `atus load` rebuilds the database. That is true within one database: each load inserts a new identity row (`loader.py:193-198`), and `ingestion_runs` is not truncated. The cross-database collision is real, but it belongs to F06, which also covers the separate "by construction" statements.
- **"Reload is working as designed"** (reload-health-and-bypass-ok) was rejected as a finding. Its measurements were confirmed and are used in 7.1: `/health` max 22 ms in Exp A and 60 ms in Exp C; bypass on 4/4, 4/4 and 40/40 blocked analyses. The refuter's full reasoning is truncated in the audit record. The conclusion does not stand regardless, because of F08, F19 and F20.

**Brief statements corrected by measurement**

- **§1: "worst-case uncached request ~11–12 s on a laptop".**
  - In the serialized run on the full DB (JIT on, host load average 3–4.7), the pooled 22-year request was p50 13.7 s and the 23-year trends p50 14.8–15.6 s, with a max of 24.0 s.
  - With JIT off, the same SQL took about 5.3 s (`exp-serving-proto/jit_probe_full.txt`, illustrative).
- **§2: "plan-order noise about 1e-14 relative; full DB varied by about 7e-16 between identical runs".**
  - Full DB vs serving: up to 1.28e-13 relative SE difference, and 6.24e-13 on near-universal participation.
  - Full DB vs itself: up to 8.57e-14 (`exp-serving-proto/self_noise_full.txt`).
  - A zero-variance spec differs by 1.31e-2 relative, so a relative 1e-12 bound needs a scale floor (F40).
- **§2 sizes.** The brief's "MB" are MiB. The full DB is 1,980.1 MiB (2,076,277,783 B). The serving set is 589.5–591.3 MiB of user objects and 598.4–599.9 MiB as a database, which confirms the §2 estimate within about 3 MiB.
- **§1: "`atus load` is an atomic single-transaction TRUNCATE + COPY rebuild".** The data swap is atomic, but the `succeeded` status is set in a separate, later transaction, so `data_version` is not atomic with the data (F20). The loader docstring's "a failed load changes nothing" is wrong for a failure in that window.
- **§1: "Bundle: 110.8 kB gzip JS".** vite reports 111.85 kB gzip at baseline; `gzip -9` gives 110,574 B. This is a difference in measurement method, not a regression.
- **§1: "CI ... emit Node 20 deprecation warnings".** Node 20 was removed from GitHub-hosted runners on 2026-09-23, per GitHub's changelog. The node20 actions now run under Node 24. Whether CI is still green was not verified, because no run logs were opened.
- **§7.9: whether `min_size=1` keeps a scale-to-zero DB awake.** The pool sends no idle traffic, and Neon's docs say idle connections do not prevent suspension (not tested on Neon). The actual costs are stale-connection 503s (F05) and the connection opened at API start.
- **§7.9: "`max_lifetime` defaults to about an hour".** Confirmed at 3600 s. It is applied only when a connection is returned after a request, so it generates no idle reconnects.
- **§1: `/ready` failure bodies include operator hints.** Correct. The `atus load` hint also reaches analysis endpoints when the DB is empty (F51).

**Finder claims corrected by refuters** (the corrected versions are used above)

- **Generated types:** 14 request properties become required (28 diff lines), not 28.
- **`validate-analytics` under a read-only role** is not silent: it prints a traceback to stderr, and stdout is empty.
- **Pool shrinking** after a burst takes about 100 min from 10 connections, not "up to 10 min".
- **DB share of wall time.** The timed summary says "> 99.5% of every uncached request". Recomputed from the CSVs: ≥ 98.4% at 0.25 CU (≥ 99.4% for the worst cases) and ≥ 95.7% at 2 CU.
- **Cold start cost:** "cold start is import time" was not measured separately.
- **Page-cache residency:** 62.5% residency is not evidence of eviction (tie-break). The memory-pressure counters exist only in the timed agent's transcript.
- **Deadlock window:** in practice it is milliseconds wide; the experiments widened it artificially. The failure is a clean rollback.
- **`data_version` collision:** today there is no path to stale results without swapping the DB under a running process or an open tab.
- **"No leaks in logs":** psycopg_pool logs a database password that contains `%`.
- **Body-size OOM:** an OOM on a 512 MB instance is inferred from macOS RSS. It was not reproduced in a 512 MB Linux container.
- **HEAD 405** also affects `/health`. The cited `DELETE /meta` returned 404, not 405.
- **0.25 CU compare latency** is 5.8–7.6 s.
- **`/dev/shm` exhaustion:** only 5 concurrent requests were tested, so the "3–5 concurrent" threshold is extrapolated below 5.
- **`docs/api.md` timing table:** it was measured in process in September 2026 under different conditions. It is not wrong; it is not reproducible today (F69).

### Free-tier fit measurements

Source: the serialized timed agent. Its report is copied verbatim, with raw tables, in [`evidence/phase5/01-audit-timed.md`](evidence/phase5/01-audit-timed.md). The values below come from that report's tables. Percentiles exclude the warm-up round.

**Setup**

| Profile | Database | API |
|---|---|---|
| (a) Neon Free floor, 0.25 CU | prototype serving DB in `atus-emu-pg`, `--cpus 0.25 --memory 1g`, `jit=off` | `--cpus=0.1 --memory=512m --memory-swap=512m`, one uvicorn process, image from `python:3.12-slim` with a non-editable install of 387fc2d |
| (b) Neon Free ceiling, 2 CU | same DB, `--cpus 2 --memory 8g` (VM has 7.75 GiB), `jit=off` | same |
| (c) laptop, full DB | full DB on 5434, read-only sessions, unconstrained, `jit=on` (stock) | unconstrained |
| (c2) laptop, serving DB | prototype, unconstrained, `jit=off` | unconstrained |

Notes on the setup:

- CPU and RAM per CU follow Neon's statement "1CU = 1vCPU, 4GB RAM". `jit=off` was chosen because Neon's public compute build has no LLVM.
- DB time is the sum of the API backends' logged statement durations between MARK statements. API time is wall time minus DB time.

**Latency, wall seconds p50 / p95 (max)**

| Request (statements) | (a) 0.25 CU | (b) 2 CU | (c) laptop full DB | (c2) laptop serving |
|---|---|---|---|---|
| `/meta` (5) | 0.17 / 0.24 | 0.03 / 0.09 | 0.05 / 0.06 | 0.03 / 0.03 |
| `/activities` (3) | 0.01 / 0.05 | 0.01 / 0.01 | 0.01 / 0.02 | 0.01 / 0.02 |
| cached estimate (3) | 0.01 / 0.01 | 0.01 / 0.07 | 0.01 / 0.01 | 0.01 / 0.01 |
| estimate, 1 year, SE (9) | 6.95 / 7.35 | 0.38 / 0.45 | 1.88 / 2.53 | 0.30 / 0.40 |
| compare by sex, 1 year (10) | 6.38 / 7.54 | 0.44 / 0.48 | 3.25 / 4.64 | 0.46 / 0.67 |
| pooled 22 years, BLS leisure (9) | 51.69 / 52.37 (52.51) | 2.80 / 3.35 | 13.67 / 22.26 (23.80) | 2.66 / 3.70 |
| trend 23 years, tier-1 `12` (9) | 46.87 / 51.28 (51.98) | 2.96 / 3.04 | 15.56 / 22.55 (23.99) | 2.78 / 3.04 |
| trend 23 years, `010101` (8–9) | 48.53 / 50.44 (50.80) | 2.69 / 2.90 | 14.81 / 22.59 (24.12) | 2.53 / 2.67 |
| pandemic 2019–2020 (9) | 2.88 / 3.11 | 0.57 / 0.62 | 3.42 / 3.52 | 0.55 / 0.56 |

Notes on the latency table:

- At (a), latency stepped up between rounds 2 and 3. Read the worst cases as a band of about 33–52 s.
- API time was 7–17 ms for estimates and compares and 96–189 ms for trends at (a), and 11–16 ms (trends ~100 ms) at (b).
- With `jit=on` at (a): pooled p50 64.49 s (max 70.71), trend `12` p50 64.32 s (max 70.80), 1-year estimate 4.16 s.

**Saturation, distinct uncached 1-year estimates released together**

| Concurrency | (a) 0.25 CU | (b) 2 CU |
|---|---|---|
| 1 | 2.29–6.10 s, all 200 | 0.16–0.37 s |
| 2 | 4.41–5.41 s | 0.22–0.38 s |
| 4 | 7.90–13.21 s | 0.43–0.82 s |
| 8 | 18.51–39.02 s, all 200 | 1.52–4.81 s |
| 16 | 10 × 200 in 16.63–45.24 s; **6 × 503 `database_unavailable` at 10.04–10.13 s** | 16 × 200 in 1.29–7.94 s |

The API's CPU was throttled in at most 6 of 397 periods, and its cgroup memory stayed at or below 49.6 MiB.

**Memory and cold start**

| Measure | Value |
|---|---|
| Process VmHWM at the first `/health` / after metadata GETs / after 256 analyses (LRU full) / after 300 | 64,188 / 65,208 / 70,256 / 70,364 kB |
| cgroup `memory.peak` (final) | 55,451,648 B |
| Cache entry size | ~19.7 KiB measured; deep payload 4.5 KB (1-year estimate) to 21.2 KB (23-year trend) |
| `docker run` → first `/health` 200 at 0.1 CPU (n=5) | p50 7.65 s, p95 8.21 s, max 8.29 s (DB at 0.25 CU: p50 8.95 s, max 10.70 s) |
| `docker run` → first `/meta` 200 (n=5) | p50 7.74 s, max 8.40 s (DB at 0.25 CU: p50 9.15 s, max 11.04 s) |

**Probes at (a)** (experiments only; any adoption needs the §9 proof)

| Probe | Result |
|---|---|
| `max_parallel_workers_per_gather = 0`, pooled 22 years | 40.63 / 39.49 s → 19.21 / 19.11 s; integer and numeric point sums identical; max replicate relative difference 1.34e-14 |
| Year restriction inside `activity_minutes`, 1-year estimate | 2.90 / 3.39 s → 0.50 / 0.90 s; year, n and n_participants identical; max relative difference 7.38e-15 over 486 values |
| Cold DB cache (restart + OS page-cache drop), pooled 22 years | first request 32.3 / 48.8 / 47.1 s vs warm repeat 33.2 / 45.0 / 40.9 s; Postgres restart only: 40.4 / 39.9 s vs 38.5 / 38.9 s |

**Statements per request** (server-side `log_statement=all` on the fixture DB, including implicit transaction statements; `$AUDIT/exp-statements/statements_table.txt`; the timed matrices agree)

| Request | Statements |
|---|---|
| `/health`, `/activities/presets`, `/population/metadata`, `/openapi.json`, `/docs`, unknown route 404, 405, malformed JSON 400 | 0, no pool checkout |
| `/activities/abc`, `/activities?level=9` (422) | 0, but a pool checkout |
| `/ready` | 4 |
| `/meta` | 5 |
| `/activities` list or search | 3 |
| `/activities/{code}` (2-, 4-, 6-digit) | 5 / 6 / 7 |
| well-formed unknown activity (404) | 3 |
| estimate or trend, cache miss | 9 |
| compare, cache miss | 10 |
| any analysis cache hit | 3 (`BEGIN`, `data_version`, `COMMIT`) |
| 422 `validation_error` or `invalid_spec` | 3 (4 when `DEALLOCATE ALL` follows the `ROLLBACK`) |
| 422 `unsupported_analysis` / `unknown_activity` / `insufficient_data` | 4 / 7 / 8 |
| idle pool, reconnects, `max_lifetime` replacement | 0 |

**What this means for the free tiers**

- **Memory:** fits the 512 MB instance for well-formed traffic, with more than 7× headroom. Hostile request bodies are not bounded (F03).
- **API CPU** at 0.1 CPU is not the bottleneck: under 0.2 s per request plus a 7–11 s cold start.
- **Database at 0.25 CU:** the current SQL makes worst cases about 50 s, and more than 10 concurrent analyses produce 503s. At 2 CU the same requests take 2.7–3.4 s. How quickly Neon autoscaling reacts was not measured (F41).
- **Database size:** the prototype serving DB (cluster sum 650,458,708 B ≈ 0.65 GB) fits Neon Free's 1 GB and Prisma Free's 2 GB. Growth during a restore was not measured.
- **Statement budget.** This is arithmetic on the brief's unverified quota, assuming every statement counts as an operation. 200,000 operations a month allow about 66,000 cache hits or about 22,000 uncached estimates. Today every cache hit and every invalid request consumes operations (F14).

## Audit resolution

Each finding was checked against the code, the tests and the git history at commit 928f50e, and against the docs at 26e6e67 (no code changed in between). Four rows describe later commits and were re-checked at f208719: F52 (6222a19, `TestShutdownCancel`), F53 (2f46bd2, the disconnect counter), F55 (b2e44a5, the new-year frontend tests) and F69 (f8c57e7, the `cache.py` docstring). The tests named below were not re-run for this document.

Status values:

- **Fixed**: changed in code, configuration or docs. The row names where, and the tests that cover it.
- **Partly fixed**: part is done; the row says what is left and where it is tracked.
- **Accepted**: left as it is on purpose; the row says why and where that is recorded.
- **Open**: not done yet; the row says where it is tracked.
- **Applied** (facts I01–I14): the design relies on the fact.

"Verified locally" applies to every row: no protection has been observed on a provider yet. Test paths are repo-relative; `test_x.py::name` names one test.

### Findings F01–F71

| Id | Status | Resolution | Verified by |
|---|---|---|---|
| F01 | Fixed | Every DB transaction sets `statement_timeout` (85 s analyses, 10 s metadata), `lock_timeout`, `idle_in_transaction_session_timeout` and `client_connection_check_interval` with `set_config(..., true)`. An analysis also has a deadline (timeout + 5 s) that cancels its query. Uncached analyses pass admission lanes first. The `atus_api` role defaults repeat the timeouts (`scripts/ops/roles.sql`). Changed from the audit's plan: a client disconnect does not cancel an admitted computation; it finishes within the deadline and is cached, so a retry joins it or hits the cache (ADR-012 "Single-flight and disconnects"). Code: `src/atus_pipeline/api/database.py`, `src/atus_pipeline/api/analysis_runner.py` (8e84789) | `tests/integration/api/test_protection_db.py` (`test_limits_are_applied_and_transaction_local`, `test_statement_timeout_is_analysis_timeout_and_never_cached`, `test_request_deadline_cancels_a_multi_statement_analysis`, `test_a_deadline_that_passes_between_statements_still_ends_the_analysis`); `tests/integration/api_prod/test_server.py::test_abandoned_analysis_completes_is_cached_and_logged`; `tests/unit/api/test_database_unit.py`. The 85 s value was kept by the emulation calibration: at the 0.25 CU floor, 3 × p99 of the worst cases (70.52 s) is 211.6 s, clamped to 85 s ([ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13)). The Neon replay must confirm it |
| F02 | Fixed | Routes and dependencies are async. Database work takes one worker-thread hop per transaction; `ATUS_API_THREADS` (16) sizes the limiter. The admission lane is taken before a pool connection. The app serves at most 64 requests besides `/health` at once (503 `overloaded` beyond), and uvicorn's `limit_concurrency` (128) sheds excess connections before app code (review round 3: it was 64 and the app had no bound of its own). Code: `api/app.py`, `api/dependencies.py`, `api/serve.py`, `api/middleware.py` | `tests/unit/api/test_serve_and_logging.py::test_lifespan_sizes_the_worker_thread_limiter`; `test_protection_db.py::test_lane_is_taken_before_a_pool_connection`; `test_server.py::test_connections_beyond_the_limit_are_shed_before_the_app`. Under load at 0.1 CPU (2026-10-21 emulation, [record](../loadtests/results/2026-10-21-emulation.md) sections 5.1 and 5.4): the API served 33–35 metadata requests/s and 67–71 cache hits/s at either database profile; at 0.25 CU its own CPU stayed under 6% of a core even with 16 concurrent clients, and `/metrics` counted 0 pool request errors, 0 connection errors or lost connections, and 0 query timeouts in every run |
| F03 | Fixed | A 64 KiB cap on declared and streamed bodies answers 413 before routing (`api/middleware.py`). The Worker applies the same cap (`edge/src/index.ts`) | `tests/unit/api/test_guard.py` (declared, streamed and huge lengths); `test_server.py::test_body_caps_over_real_sockets`; `edge/test/worker/limits.test.ts` |
| F04 | Fixed | A 422 lists at most 20 errors, with bounded strings and no echoed values. `preset` has `max_length` 64. Responses of 1 KB or more are gzipped | `test_guard.py::test_422_lists_at_most_twenty_errors_with_bounded_strings`, `::test_preset_length_is_bounded`, `::test_large_responses_are_gzipped_small_ones_are_not` |
| F05 | Fixed | Every checkout runs `check_connection`. A failed check closes the other idle connections without reopening them (`api/database.py`, `_ApiPool.discard_idle`; review round 1: `drain()` reopened one for each, so a wake rebuilt the whole pool) | `test_protection_db.py::test_requests_succeed_after_every_backend_is_terminated`, `::test_a_failed_check_closes_the_dead_connections_without_reopening_them` |
| F06 | Fixed | `data_version` is now `<release>:load-<run>-<UTC>-<hex>` for a load and `<release>:serving-<build_id>:<hash12>` for a serving build. It is written in the same commit as the data (migration `0003_data_version.sql`, `src/atus_pipeline/database/versions.py`; ea3aefe, 2e59f87, 9769f52). The cache key, `X-Data-Version`, `ATUS_EXPECTED_DATA_VERSION` and the SPA's tag all use it. `docs/api.md` and `docs/frontend.md` were corrected, and [ADR-008](adr/ADR-008-server-state.md) has a dated amendment that supersedes its Phase 4 key text (`analytics_version:release:runN`, invalidation "by construction") | `tests/unit/test_data_versions.py`; `tests/integration/test_reload_switch.py::TestSwitch::test_identical_reloads_still_get_distinct_versions`; `tests/integration/test_serving_build.py::test_two_builds_have_the_same_content_hash_and_different_versions` |
| F07 | Fixed | The API requires the edge secret `X-ATUS-Edge-Auth`. It accepts two values for rotation and compares them in constant time. Per-client token buckets use the Worker's `X-ATUS-Client-IP` only when the request is authenticated. Proxy headers are off. Production refuses to start without secrets. The edge governor is the primary layer (ADR-015). Code: `api/middleware.py`, `api/protection.py`, `api/serve.py`, `edge/src/` | `test_guard.py` (403, both rotation values, forged forwarding headers, 429); `test_server.py::test_forwarded_headers_never_change_the_client`; `test_protection_db.py::test_forbidden_origin_runs_zero_statements`; `edge/test/worker/governor.test.ts`, `governor-flow.test.ts` |
| F08 | Fixed | `atus load` builds in a shadow schema (`atus_next`) and switches the tables in one short transaction with bounded lock waits (ADR-013 Decision 1; 2e59f87). A hosted release switches to a new database instead | `tests/integration/test_reload_under_api_load.py`: continuous reads through the real API during a rebuild and switch, zero failures. Its negative control with the old loader blocks every reader (2321e28) |
| F09 | Fixed | `Dockerfile`: multi-stage, base pinned by digest, hash-locked wheels, UID 10001, `/health` healthcheck. Also an allow-list `.dockerignore`, `docker-compose.prod.yml`, `docker-compose.emulation.yml` and `deploy/` (837d776) | `tests/deploy/test_packaging_files.py`; `tests/deploy/test_compose_config.py`; `deploy/ci/check_image.py` with `tests/deploy/test_check_image.py`; opt-in `tests/deploy/test_container_image.py` |
| F10 | Partly fixed | Bounded, not removed: the 85 s timeout and 90 s deadline, one heavy lane, and the edge's 300 heavy analyses a day. The year pushdown (F39) and `jit=off` (F37) cut the cost. At 0.25 CU under emulation (2026-10-21, [record](../loadtests/results/2026-10-21-emulation.md) section 5.3), pooled 22-year leisure and 23-year trends took 21.35–21.81 s at p50, the F66 broad selector 33.49–34.99 s pooled or trended and 69.21 s compared by sex (max 71.02 s); at 2 CU every worst case took 2.0–3.0 s at p50. They fit under 85 s at the floor, but the F66 compare with only 1.2 × headroom, and two heavy analyses at once took 88–106 s for the F66 requests, which the single heavy lane prevents ([ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13)). The final timeout comes from the week-1 calibration ([deployment.md step 10](deployment.md#10-week-1-calibration), ADR-012 "Calibration") | see F01, F37, F39 |
| F11 | Fixed | A waking notice (polite live region) appears after 3 s of pending metadata. A share link says "Preparing the analysis…" until `/meta` answers. Code: `frontend/src/api/serviceStatus.ts`, `frontend/src/components/ServiceNotice.tsx`, `frontend/src/pages/AnalysisPage.tsx` | `frontend/src/api/serviceStatus.test.ts`; `frontend/src/pages/serviceStates.test.tsx` ("cold start (F11, F12)"); `frontend/e2e/service-states.spec.ts`; `frontend/e2e/service-codes.spec.ts` (the deployed path: snapshot, prewake "waking", `service_waking`; review round 1) |
| F12 | Fixed | Metadata and transport failures are retried for up to 150 s, honouring `Retry-After`. Each metadata attempt has a 60 s client timeout. An analysis is retried once on `overloaded`, `database_unavailable` or an unknown 503. Home has "Try again". Code: `frontend/src/api/resilience.ts` | `frontend/src/api/resilience.test.ts`; `serviceStates.test.tsx` |
| F13 | Fixed | Every error code has its own presentation, including full-page quota states and a `Retry-After` countdown. A non-JSON 429 is shown as the edge's daily limit. The "service is up" copy is gone. Code: `frontend/src/domain/presentError.ts`, `frontend/src/api/client.ts` | `frontend/src/domain/presentError.test.ts`; `frontend/src/api/client.test.ts`; `serviceStates.test.tsx`; `service-states.spec.ts` (quota page, edge 429, countdown); `service-codes.spec.ts` (every other Phase 5 code in a browser, review round 1) |
| F14 | Fixed | An in-process data snapshot holds the data version, years and lexicon. It is read in one REPEATABLE READ transaction and pinned by `ATUS_EXPECTED_DATA_VERSION`. Bodies are validated and the cache is checked before any connection, so cache hits, metadata routes and every 4xx decided before the statistics query run 0 statements; a 422 found by that query (`insufficient_data`, or a diary-date window outside the requested years) runs it first (422cb4a, 18ca16e) | `tests/integration/api/test_statement_log.py::test_documented_counts` (PostgreSQL's own statement log); `tests/integration/api/test_snapshot_db.py`; `tests/unit/api/test_snapshot.py` |
| F15 | Partly fixed | Identical in-flight requests share one computation. Year and code order are canonical. A respelled miss costs an analysis token and a lane. **Deferred:** keying on the resolved computation (label, `confidence_level`, prefix vs leaf), recorded in ADR-012 "What this does not protect against". ADR-015 explains why the edge keys on exact bytes | `tests/unit/api/test_admission.py::test_identical_concurrent_requests_share_one_computation`; `tests/unit/api/test_review_hardening.py` (canonical order) |
| F16 | Fixed | `/health` is the only platform and monitor target. `/ready` and `/metrics` need `Authorization: Bearer <ATUS_OPS_TOKEN>`; without it they answer like an unknown path. Hints go to the log (`api/routes/health.py`) | `tests/unit/api/test_observability.py` (hidden without a token, wrong bearer); `tests/deploy/test_workflows.py::test_monitors_never_touch_the_database` |
| F17 | Fixed | The pool is configured from the environment: `ATUS_API_POOL_MAX_SIZE` (4), `ATUS_API_POOL_MAX_IDLE_S` (240), `ATUS_API_POOL_TIMEOUT_S` (5), with `min_size=0` | `tests/unit/api/test_config.py` |
| F18 | Fixed | `ATUS_API_PREPARE_THRESHOLD` is configurable (`none` behind a pooler). Production uses Neon's direct endpoint, so the default 5 stays. The PgBouncer run is in [deployment.md step 0](deployment.md#0-rehearse-the-whole-stack-locally) | `test_config.py`; `tests/unit/api/test_database_unit.py::test_connection_kwargs`; `scripts/smoke_stack.sh --integration-via-pgbouncer` (the integration tests not marked `direct_connection`) |
| F19 | Fixed | The switch locks tables in reader order and bounds each wait with `lock_timeout`. It retries, so a reader is never the deadlock victim | `test_reload_switch.py::TestReaders::test_a_lock_order_cycle_with_a_reader_never_kills_the_reader` |
| F20 | Fixed | Data, the `succeeded` status and `data_version` commit together. The docstring in `loading/loader.py` was corrected | `test_reload_switch.py::TestSwitch::test_reload_switches_data_and_version_in_one_commit`; `TestFailedLoads::test_failure_inside_the_switch_transaction_changes_nothing` |
| F21 | Fixed | The switch waits until every transaction older than the new data has ended. Review round 1: a REPEATABLE READ read that began after the new data committed could still take its snapshot before the switch and pair the old data version with the new tables; the API's snapshot read now locks `ingestion_runs` (ACCESS SHARE) before its snapshot is taken (`api/database.py`, `SNAPSHOT_LOCK`), so the switch commits first or waits for it | `test_reload_switch.py::TestRepeatableRead` (incl. `test_the_api_snapshot_read_never_pairs_the_old_version_with_the_new_data`) |
| F22 | Fixed | Each benchmark runs in its own transaction (a savepoint). Results are printed before they are recorded. `--no-persist` only prints. Under a read-only role the command reports that nothing was recorded and exits 1 (ADR-013 Decision 3; 4eedea2) | `tests/integration/test_validate_analytics.py` |
| F23 | Fixed | Warnings and errors name the loaded release's lexicon. The OpenAPI description and the activities listing are release-neutral; one example response still shows 0325 output (42f6251). Review round 1: the 404 for a malformed code named the lexicon only once the process held a snapshot, so a cold and a warm process answered differently; it is now always release-neutral (`api/routes/activities.py`, `test_snapshot.py::test_a_malformed_code_gets_the_same_404_from_a_cold_or_warm_process`), and the harness's `lexicon_wording` accepts that form for malformed codes only | `tests/unit/analytics/test_lexicon_text.py`; `tests/unit/api/test_exposure.py::TestReleaseNeutralText`; `tests/integration/test_release_checklist.py` |
| F24 | Fixed | An empty base URL means same-origin. Any other value must be an absolute http(s) URL that the site CSP allows, or the build fails. `check:bundle` rejects a loopback API address. Code: `frontend/src/api/apiBase.ts`, `frontend/scripts/check-bundle.mjs` | `frontend/src/api/apiBase.test.ts`; `frontend/src/checkBundle.test.ts` |
| F25 | Fixed | `/meta` must report `v1` and the expected shape (`frontend/src/api/compat.ts`). Anything else is a non-retryable full-page state | `frontend/src/api/compat.test.ts`; `serviceStates.test.tsx`; `service-states.spec.ts` (incompatible API version) |
| F26 | Fixed | A page shows a page-level error only while nothing is loaded | `serviceStates.test.tsx` ("background failures and retries (F26, F63)") |
| F27 | Fixed | The SPA fetches `/meta` with `cache: "no-cache"` and compares every response's `X-Data-Version` with the loaded one; a difference shows a reload notice. The API's snapshot answers `/meta` from one consistent read. The API still sends `/meta` as `public, max-age=300` with no ETag (`api/routes/meta.py`). The SPA revalidates anyway, and in production it reads metadata from the static snapshot. Review round 1: an uncached analysis computed after an in-place reload that the snapshot did not know yet used the snapshot's lexicon and years and carried the old `X-Data-Version` and `X-Analysis-Key`, so a tab could keep it under the old version without a reload notice. The computation now reads the data version first, computes on that version with its own lexicon and years when it differs from the snapshot's, and labels the `bypass` result (and its joiners) with it (`api/analysis_runner.py`) | `serviceStatus.test.ts`; `frontend/src/api/snapshot.test.ts` ("revalidation across releases": the pointer and `/meta` are fetched with `cache: "no-cache"`, added in review round 1); `service-states.spec.ts` (release switch under an open page); `test_reload_under_api_load.py` (`/meta` never pairs one run with the other's counts; every estimate's key matches its value, whatever its `X-Cache`); `test_snapshot_db.py::TestDataChangedAfterTheSnapshot`; `test_snapshot.py::TestExpectedDataVersion`; `test_admission.py::TestSingleFlight::test_joiners_of_a_bypass_see_the_bypass_and_its_labels` |
| F28 | Fixed | Hash-locked `requirements/*.lock.txt` files (`requirements/compile.sh`) are installed with `--require-hashes` in CI and in the image. The API's runtime lock is separate from the pipeline's (3278134) | `tests/deploy/test_requirements_locks.py`; `test_packaging_files.py::test_api_image_installs_only_hash_locked_wheels` |
| F29 | Fixed | `ci.yml` runs `pip-audit` on the API lock and `npm audit --omit=dev`. `image.yml` runs Trivy as a pinned binary. `.github/dependabot.yml` covers pip, npm, actions, docker and docker-compose. CodeQL and Dependabot alerts are owner settings ([owner step 4.4](deployment.md#4-github-repository-nathan98000atus-stays-public)), not yet applied | `test_workflows.py::test_ci_audits_and_lints`, `::test_trivy_is_a_pinned_binary_never_the_hijacked_actions`; `tests/deploy/test_dependabot.py` |
| F30 | Fixed | `frontend/scripts/api-types.mjs` passes `--default-non-nullable false`. CI checks for drift (`npm run check:api-types`, OpenAPI drift step) | `frontend/src/apiTypes.test.ts`; `test_workflows.py::test_ci_backend_covers_both_pythons_and_the_openapi_drift` |
| F31 | Fixed | `.gitignore` ignores `.env*` and `*.env`, except the templates. Secret scanning and push protection are on by default for public repositories; owner step 4.4 confirms them | `test_packaging_files.py::test_secret_files_are_ignored` |
| F32 | Fixed | The development compose binds to 127.0.0.1 and pins the image by digest. The production compose uses the non-superuser `neondb_owner` and `atus_api` | `test_compose_config.py::test_postgres_is_pinned_and_bound_to_loopback`, `::test_database_bootstraps_only_what_neons_console_creates` |
| F33 | Fixed | Both compose files set `shm_size: 1g` (the emulation inherits it). SQLSTATE class 53 maps to 503 `overloaded`. The lanes cap concurrency | `test_compose_config.py::test_shared_memory_is_raised`; `test_database_unit.py::test_too_many_connections_is_overload`; `test_admission.py::test_mapping` |
| F34 | Fixed | Distinct codes, each with `Retry-After`: `analysis_timeout`, `overloaded`, `database_paused`, `database_unavailable`, `data_not_loaded` (ADR-012 "Error mapping") | `test_admission.py::test_mapping`; `test_database_unit.py`; `test_protection_db.py::test_busy_pool_is_overloaded_with_retry_after` |
| F35 | Fixed | At most 2 uncached analyses run at once, at most 1 of them heavy. A request waits at most 5 s, then gets 503 `overloaded` with `Retry-After`. Cache hits bypass the lanes. The edge governor caps each network and each day | `test_admission.py` (lanes, heavy lane, refunds). Saturation under emulation (2026-10-21, [record](../loadtests/results/2026-10-21-emulation.md) section 5.4), uncached single-year estimates through the real lanes: at 0.25 CU throughput peaks at 1 concurrent client (1.14 successful analyses/s); `503 overloaded` starts at 4 clients (9 of 101 requests) and grows to 246 of 354 at 16, answered after 5.0 s at p50. At 2 CU throughput plateaus at 2 clients (about 24/s) with no 503 up to 16. No `analysis_timeout` |
| F36 | Fixed | libpq `connect_timeout` (10 s), TCP keepalives and the request deadline | `test_database_unit.py::test_connection_kwargs`; the deadline tests in `test_protection_db.py` |
| F37 | Fixed | `jit=off` is set in four places: the `atus_api` role default, the per-request `ATUS_API_JIT`, the production and emulation database (`-c jit=off`), and `compare-serving-db` sessions | `test_compose_config.py::test_database_matches_neon`; `test_database_unit.py::test_optional_parallel_workers_and_jit`; opt-in `tests/deploy/test_stack_roles.py` |
| F38 | Accepted | `ATUS_API_MAX_PARALLEL_WORKERS` (per-request `SET LOCAL`) exists and stays unset, so the server default applies. The emulation runs decided it (2026-10-21, [record](../loadtests/results/2026-10-21-emulation.md) sections 5.2 and 5.3): `0` is faster at 0.25 CU (single-year p50 0.99 → 0.70 s, F66 compare p50 69.21 → 38.58 s) but slower at 2 CU (worst-case p99 3.03 → 3.53 s), and ADR-011 adopts it only if it also wins at 2 CU. If the compute is fixed at 0.25 CU, set `0` once its §9 proof passes ([ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13); ADR-013 Decision 9) | `test_database_unit.py::test_optional_parallel_workers_and_jit` |
| F39 | Fixed | The year pushdown (9769f52) is proven equivalent. The §9 HTTP run found that it caused a plan regression: a nested loop that re-ran the activity aggregate for each respondent. Materializing the CTE (9afd1b4) fixed it ([Results](#results)). Latency under emulation (2026-10-21, [record](../loadtests/results/2026-10-21-emulation.md)): at 0.25 CU the non-F66 worst cases took 21–22 s at p50, where the audit measured 47–52 s for the same requests; single-year estimates with replicate SEs took 0.99 s at p50 over 59 distinct specs (the audit's 6.95 s was one spec repeated, so the two are not comparable). The runs do not isolate how much of that is the pushdown, the release build of the serving database or the stack's Neon settings | `tests/unit/analytics/test_year_pushdown_sql.py`; `tests/integration/test_year_pushdown.py`; opt-in `tests/data_quality/test_query_plans.py` |
| F40 | Fixed | The scale-floored criterion is in `src/atus_pipeline/serving/equivalence.py` ([below](#equivalence-criterion-9)). The audit's extreme specifications seed every diff (`seed_specs`) | `tests/unit/test_serving_equivalence.py` (incl. `test_zero_variance_noise_passes_only_because_of_the_scale_floor`, `test_seed_specs_include_the_audit_extremes`) |
| F41 | Open until deploy | `scripts/replay_deployed.py` (URL-targeted, at most 40 requests, paced) and the list of unknowns exist. Both have run only against local APIs. Tracked in [deployment.md step 9](deployment.md#9-quota-budgeted-post-deploy-replay) and ADR-011 "Unknowns to measure after deploy" | `tests/unit/ops/test_replay_deployed.py` |
| F42 | Fixed (locally) | The API sends CSP `default-src 'none'; frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, CORP and, in production, HSTS. It sends no `server` header and gzips. The Worker sets its own headers, pinned as literal values in the edge tests (`edge/test/worker/helpers.ts`, `EXPECTED_SECURITY_HEADERS`; review round 2: those tests compared responses with the source's own constant, so deleting a header from it passed), and the SPA has `frontend/public/_headers`. Not yet checked on the real deployment ([deployment.md step 8](deployment.md#8-prove-the-deployment)) | `test_guard.py::test_security_headers_on_success_and_errors`, `::test_hsts_in_production`; `edge/test/worker/headers.test.ts`; `frontend/src/deploy/staticHeaders.test.ts` |
| F43 | Fixed | `ATUS_API_DOCS` is off in production, so the docs routes answer 404. The schema is committed as `frontend/openapi.json` by `scripts/export_openapi.py`, and the public site publishes that copy as a static asset at `/openapi.json`. The decision is recorded in [security.md "Production exposure decisions"](security.md#production-exposure-decisions) and `docs/api.md` "Production exposure" | `test_exposure.py` (docs off, schema exported, published copy current) |
| F44 | Fixed | Production refuses a missing or development `ATUS_DATABASE_URL` and missing or short secrets. It listens on `$PORT`, and `.env` is read only outside production (`src/atus_pipeline/config.py`, `api/serve.py`) | `test_config.py`; `test_serve_and_logging.py`; opt-in `tests/deploy/test_container_image.py::test_production_mode_refuses_to_start_without_its_settings` |
| F45 | Fixed | Production is same-origin through the Worker. Development CORS exposes the contract headers | `test_guard.py::test_cors_exposes_the_contract_headers` |
| F46 | Fixed | The HTTP exception handler keeps `exc.headers` | `test_guard.py::test_405_keeps_the_allow_header` |
| F47 | Fixed | `HEAD` is answered on GET routes | `test_guard.py::test_head_on_get_routes` |
| F48 | Fixed | No trailing-slash redirects: such paths get the 404 envelope | `test_guard.py::test_trailing_slash_is_not_redirected`; `test_server.py::test_odd_request_targets_get_envelopes_never_redirects` |
| F49 | Partly fixed | `limit_concurrency` (128, twice the app's own bound of 64 requests in flight; review round 3) sheds excess connections with 503 before any app code. The request head is limited to 16 KiB and keep-alive to 5 s. **Accepted:** uvicorn has no request-head read timeout, so slow clients rely on Render's proxy, which is unverified (ADR-012 "What this does not protect against") | `test_server.py::test_connections_beyond_the_limit_are_shed_before_the_app` |
| F50 | Fixed | Control characters in the query get a 422. An unhandled error is logged once, with one traceback | `test_guard.py::test_control_characters_in_query_are_422_without_database_work`; `test_protection_db.py::test_nul_in_search_is_422_not_500`; `test_serve_and_logging.py::test_unhandled_error_is_logged_once_with_the_request_id` |
| F51 | Fixed | 503 `data_not_loaded` with a generic message | `test_admission.py::test_unloaded_database_is_503_without_operator_hints`; `test_protection_db.py::test_migrated_but_empty_database_is_data_not_loaded` |
| F52 | Fixed | `client_connection_check_interval` is 5 s and `timeout_graceful_shutdown` is 20 s. On shutdown, the admission lanes and the database close to new work first, so an analysis still waiting for a lane gets 503 `overloaded` instead of taking the lane a cancelled query frees (review round 1: before, it started a query nothing cancelled); then running queries are cancelled (`Database.begin_shutdown`), detached computations get 5 s, and whatever still runs is cancelled again. uvicorn runs that lifespan shutdown only after it has cancelled the requests still running at the end of the 20 s window; such a request, an analysis and every request joined to it, now gets 503 `overloaded` from the request-context middleware instead of uvicorn's plain-text 500 (review round 3: the 503 above reached clients only under the in-process test client). Real-PostgreSQL tests cancel a running query through `cancel_active` and through leaving the app lifespan, with and without an analysis queued for the lane. After SIGKILL, a test shows the server ends the dead process's query (60 s `pg_sleep`, gone within 20 s; it fails with the check interval set to 0), which the review's round 1 found untested | `test_database_unit.py::test_analysis_and_metadata_timeouts`; `test_serve_and_logging.py::test_development`; `tests/integration/api/test_protection_db.py::TestShutdownCancel`, `::TestKilledProcess`; `tests/integration/api_prod/test_server.py::TestShutdownDuringAnAnalysis` (real uvicorn) |
| F53 | Fixed | One access line per request, with `disconnected`, `client_ip_prefix` and the edge's `request_id` when authenticated (ADR-014). `/metrics` counts them as `atus_client_disconnects_total{route}` | `test_server.py::test_abandoned_analysis_completes_is_cached_and_logged`; `test_observability.py::test_one_structured_line_per_request`; `tests/unit/api/test_edge_contract.py`; `test_observability.py::test_client_disconnects_during_an_analysis_are_counted` |
| F54 | Fixed | The tests now cover the pool, threads, statement counts, dead connections and limits (rows above). Playwright runs the projects desktop, mobile, firefox and webkit (`frontend/playwright.config.ts`). Stack-mode E2E runs through the Worker (`ci.yml` job `e2e-stack`). Slow `/meta`, version mismatch and 429 cases are in `frontend/e2e/service-states.spec.ts`. Firefox runs only in Linux CI ([deployment.md step 0](deployment.md#0-rehearse-the-whole-stack-locally)) | as listed |
| F55 | Fixed | Everything release-specific derives from one registry entry (d5831c7). The scripts take their years from the data and `/meta` (31d07d4). An AST guard keeps release literals out of the data, engine and validation packages. A new year reaches the frontend through `/meta` alone, and two tests feed it a `0326` `/meta` with years through 2026 (b2e44a5; [New ATUS year](#new-atus-year) runbook) | `tests/integration/test_release_checklist.py` (synthetic `0326`); `tests/unit/test_release_literals.py`; `tests/unit/test_benchmark_scripts.py`; `frontend/src/pages/newSurveyYear.test.tsx`; `frontend/e2e/new-survey-year.spec.ts` |
| F56 | Fixed | The image ships no migrations, and the API never changes the schema. `ATUS_MIGRATIONS_DIR` is for running outside a checkout | `test_packaging_files.py::test_no_migrations_or_tests_reach_the_image` |
| F57 | Fixed | A serving marker (schema comment, or a serving `data_version`) makes `migrate`, `load`, `validate-db` and `load_all` refuse (`src/atus_pipeline/serving/guard.py`; 1ed13af) | `test_serving_build.py::test_full_database_commands_refuse_the_serving_database`; `tests/integration/test_cli_data_commands.py` |
| F58 | Fixed | Every action is pinned by full SHA with a version comment. `checkout`, `setup-node` and `setup-python` are on v7. Dependabot updates the actions | `test_workflows.py::test_every_action_is_pinned_by_full_sha_with_a_version_comment` |
| F59 | Fixed | Explicit `permissions`, `concurrency` and `timeout-minutes`. Python 3.11 and 3.12. The Playwright report and traces are uploaded. OpenAPI and API-type drift checks, and actionlint (b7390ea) | `test_workflows.py::test_permissions_and_timeouts_are_explicit`, `::test_ci_backend_covers_both_pythons_and_the_openapi_drift` |
| F60 | Fixed | `scripts/e2e_backend.py` accepts only a local `atus_e2e[_<suffix>]` database and redacts the password (f1712d1) | `tests/deploy/test_e2e_backend_safety.py` |
| F61 | Fixed | The About page has a privacy and logs section | `frontend/src/pages/AboutPage.test.tsx` |
| F62 | Fixed | Metadata has `staleTime: Infinity`; loaded metadata is never refetched on focus or reconnect (only a failed query is retried on reconnect). In production it comes from the static snapshot. The API's `/meta` costs 0 statements while its snapshot is fresh (fe5f9dd) | `serviceStates.test.tsx` ("no background metadata refetch (F62)") |
| F63 | Fixed | "Try again" refetches every failed query | `serviceStates.test.tsx` |
| F64 | Fixed | An analysis is retried once on `overloaded`, `database_unavailable` or an unknown-code 503 (46a7384). On the server side, see F05 | `resilience.test.ts`; `serviceStates.test.tsx` |
| F65 | Fixed | A boot shell in `#root` with an unhashed `boot.css` fallback. `/assets/*` is immutable and HTML is `no-cache` (`frontend/public/_headers`) | `frontend/src/bootShell.test.tsx`; `staticHeaders.test.ts`; `service-states.spec.ts` (bundle fails to load) |
| F66 | Partly fixed | The broad selector is in the post-deploy replay (ad9f08e), in the seed specifications of every equivalence diff, and in the restore's worst-case EXPLAIN checks (`src/atus_pipeline/serving/checks.py`). **Accepted:** the API's heavy classifier and the governor's cost class ignore selector breadth (ADR-012 "Heavy classifier", ADR-015 "Cost-class blind spots"). Load tests (2026-10-21, [record](../loadtests/results/2026-10-21-emulation.md) section 5.3): at 0.25 CU all 18 tier-1 codes took 33.49–34.99 s at p50 pooled over 22 years or as a 23-year trend, and 69.21 s at p50 (max 71.02 s) compared by sex, 3.2 × pooled leisure; at 2 CU 2.67–2.99 s. They set the 85 s timeout's floor headroom (1.2 ×; [ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13)) | `test_replay_deployed.py`; `test_serving_equivalence.py::test_seed_specs_include_the_audit_extremes` |
| F67 | Fixed (unmeasured on Render) | The pool is lazy, so the API does no DB work at start-up (the image gate's `startup_db_quiet`). The SPA shows the waking notice and allows 150 s. The deploy waits up to 15 min (`scripts/ops/render_deploy.py wait`). Render's real spin-up is an ADR-011 unknown | `test_database_unit.py::test_pool_starts_without_connecting`; `tests/deploy/test_check_image.py`; the frontend cold-start tests |
| F68 | Accepted | The serving database is 0.63 GB, and the pushdown reads less. Memory pressure on Neon was not measured. Emulation (2026-10-21, [record](../loadtests/results/2026-10-21-emulation.md) section 5.7): at 0.25 CU the database's `docker stats` working set was at most 419 MiB of its 1 GiB, and its cgroup's `memory.peak` (page cache and shared buffers included) at most 567 MiB; the latencies there come from the CPU quota, not memory. Provider: [deployment.md step 9](deployment.md#9-quota-budgeted-post-deploy-replay) | — |
| F69 | Fixed (docs) | `docs/api.md`: the error table is complete, `database_unavailable` and `overloaded` are separate, `X-Cache: bypass` is documented, and the Phase 3 timing table is labelled historical. The docstrings of `api/dependencies.py` and `loading/loader.py` were rewritten, and the module docstring of `src/atus_pipeline/api/cache.py` describes the current `<release>:load-…` and `<release>:serving-…` versions (f8c57e7) | `tests/deploy/test_docs_accuracy.py` (the `cache.py` docstring) |
| F70 | Fixed (docs) | `docs/frontend.md` now covers the error mapping (`malformed_response` and the synthesized edge codes), the version tagging and the base URL | — |
| F71 | Fixed (docs) | README: the Node requirement (22.22.2+ on Node 22, 24.15+ on Node 24), the layout and documentation listings (ADR-001 to ADR-016, `frontend/`, `edge/`, `deploy/`). `docs/architecture.md` rewritten for the whole system, with the new-release claim matching the registry (02a438f). `docs/database.md` lists every `validation_results` writer (26e6e67). The deployment diagram is in [deployment.md "Architecture"](deployment.md#architecture) | — |

### Recorded facts I01–I14

| Id | Status | How Phase 5 uses it | Verified by |
|---|---|---|---|
| I01 | Applied | The builder aggregates activities to (`tucaseid`, `activity_code`), keeps `smallint`, stores rows in `activity_code` order, and asserts that every sum fits 0–1,440 (`src/atus_pipeline/serving/builder.py`) | `test_serving_build.py::test_the_serving_schema_keeps_the_contract_and_every_constraint`, `::test_the_served_rows_are_the_designed_subset` |
| I02 | Applied | The builder asserts that the omitted replicate rows are all-NULL or all-zero and that the weight filters exclude them | `test_serving_build.py::test_an_omitted_row_that_an_estimate_would_read_stops_the_build` |
| I03 | Applied | `src/atus_pipeline/serving/contract.py`; the builder asserts `missing_contract_columns()` | `tests/integration/test_serving_contract.py` |
| I04 | Applied | The builder's per-table hashes equal the prototype's (ADR-013 "Measured on the real full database"). The restore checks the physical order (`pg_stats.correlation` ≥ 0.99) | `tests/integration/test_serving_restore.py::test_the_restore_reproduces_the_build_exactly` |
| I05 | Applied | `atus_api` can only SELECT tables and defaults to read-only transactions; large objects need no privilege, so `roles.sql` removes the role's and the serving checks fail on any (review round 4, [security.md R16](security.md#accepted-risks)). The API opens every transaction with `BEGIN READ ONLY` | `test_snapshot_db.py::test_api_transactions_cannot_write`; `test_serving_restore.py`; opt-in `test_stack_roles.py` |
| I06 | Applied | `frontend/public/_headers` sets the CSP (`connect-src 'self'`), with a `<meta>` backstop. Every E2E test collects the CSP violations and Permissions-Policy errors of every page in its browser context and fails on one (`frontend/e2e/fixtures.ts`; review round 2: before, only the core-journeys test and the deployed browser smoke did, `frontend/e2e/helpers.ts`). Not yet checked on the real deployment | `staticHeaders.test.ts`; `frontend/e2e/deployed/starter.deployed.ts` |
| I07 | Applied | Secrets are redacted in log records and formatted output, tracebacks included. A malformed DSN is refused without being echoed | `test_serve_and_logging.py::test_create_app_redacts_the_database_password`; `test_observability.py::test_tracebacks_stay_on_one_line_and_secrets_are_redacted`; `test_exposure.py::TestNoInternalsInErrors`; `test_config.py::test_malformed_database_url_never_echoes_the_password` |
| I08 | Applied | `min_size=0` and no background refresh. Whether Neon suspends as documented is unverified (ADR-011 unknowns) | `test_database_unit.py::test_pool_starts_without_connecting`; `test_snapshot_db.py::test_db_free_routes_never_open_a_connection` |
| I09 | Applied | The body cap (F03) bounds hostile input. Memory under load (2026-10-21 emulation, [record](../loadtests/results/2026-10-21-emulation.md) section 5.7): the API process peaked at 76.1 MiB RSS (cgroup 59.3 MiB) of 512 MB in any run, with the 256-entry result cache full | see F03 |
| I10 | Applied | A transaction ends before its response starts | `test_admission.py::test_transaction_ends_before_the_response_starts` |
| I11 | Applied | ADR-014: never alert on `atus_db_pool_requests_waiting` alone | — |
| I12 | Applied | The scale floor of the criterion (F40) | see F40 |
| I13 | Partly | The version has one source: `pyproject.toml` reads `atus_pipeline.__version__` (3278134). `deploy.yml` deploys only `vMAJOR.MINOR.PATCH` tags and does not compare the tag with `__version__`. No tag exists yet | `test_workflows.py::test_deploy_runs_only_on_tags_or_by_hand` |
| I14 | Applied | Explore fetches the lexicon in parallel with `/meta` (`frontend/src/pages/ExplorePage.tsx`) | — |

### What remains open

- **Confirmation on Neon** of the numbers the fixed code gave under local emulation (F10, F35, F38, F66, F68, I09; [`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md), [ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13)): the emulation does not reproduce Neon's autoscaler, pageserver reads, wake from suspend or the Render↔Neon round trip, so the post-deploy replay must confirm the timeout and lanes, F66 included.
- **Measured only after a deploy** (F41, F42 and I06 on the real hosts, F67's Render spin-up, I08): [deployment.md steps 8–10](deployment.md#8-prove-the-deployment) and ADR-011 "Unknowns to measure after deploy".
- **Deferred or accepted by decision:** F15 (cache key on the resolved computation), F49 (no head-read timeout), F66 (cost class ignores selector breadth).
- **Owner settings** (F29, F31): CodeQL default setup, Dependabot alerts, and confirming secret scanning and push protection ([owner step 4.4](deployment.md#4-github-repository-nathan98000atus-stays-public)).

## Equivalence criterion (§9)

The serving database, the engine's year pushdown, the static snapshot and the edge cache must not change any served number, beyond the floating-point noise of summation order. This section defines when two answers are equivalent. `src/atus_pipeline/serving/equivalence.py` implements it, and ADR-013 Decision 10 refers here.

### Definition

Two answers to the same specification (from the full and the serving database, or from two renderings of a query) are equivalent when all of the following hold:

1. **Exactly equal, with the same JSON type.** This covers every result field except the three below: point estimates (`value`), `n_respondents`, `n_participants`, `weighted_population_per_day`, `days_in_period`, labels, years, the echoed `spec`, `warnings` and `analytics_version`. It also covers the key set at every level and the length of every list.
2. **Standard errors and CI bounds** (`standard_error`, `ci_lower`, `ci_upper`; `TOLERANT_FIELDS`):

   ```text
   |a - b| <= 1e-12 * max(|a|, |b|, S)        (REL_TOL = 1e-12)
   ```

   S is the largest absolute point value of the estimate the field belongs to, and of the estimates that enclose it. For a comparison's `difference`, S also includes both groups' estimates. If either value is not a number (for example `null`), the field is compared exactly. A non-finite value (NaN, ±inf) matches only the same non-finite value, and S is taken over finite point values only (review round 4: before, a NaN or infinite SE or CI bound passed as equal to any finite one).
3. **Errors are identical.** At engine level, the exception class and message must match (`"<Class>: <message>"`). Over HTTP, the status code and the whole error body (`code`, `message`, `details`) must match.
4. **Raw sufficient statistics** (`compare_statistics`, used to compare two SQL renderings). The row count, column count, year, counts and `numeric` sums are exactly equal, and so is the length of every replicate array. Each replicate `float8` sum is within `1e-12 ×` the largest finite absolute value in its replicate array, with the same non-finite rule as item 2.
5. **HTTP only** (`scripts/equivalence/httpdiff.py`):
   - These headers must be identical: `Content-Type`, `Cache-Control`, `X-Analytics-Version` and `X-Response-Format`.
   - Every Phase 5 answer's `X-Data-Version` must equal the version its server reported at start.
   - **Whitelisted** (may differ): the body keys `data_version` and `analysis_key` at any depth, and the headers `X-Data-Version`, `X-Analysis-Key`, `X-Request-ID`, `X-Cache`, `Server-Timing`, `Date` and `Content-Length`. Each has a written justification in `WHITELIST` and `WHITELISTED_HEADERS`.
   - **Deliberately compared exactly:** `/meta`'s `ingestion_run_id`, `last_ingested_at`, `release`, `respondents` and `years`. The serving build copies the source run's provenance.
   - **Phase 1–4 vs Phase 5 code on the same database (pair a–b):** a difference passes only when exactly one of eight named changes explains it. They are `validation_details_capped`, `preset_max_length`, `payload_too_large`, `unsupported_media_type`, `malformed_json_parser_limit`, `control_characters_rejected`, `lexicon_wording` and `health_reports_build` (`INTENDED_DIFFERENCES`). Domain errors have no such entry, so their code and message must be identical.

### Why these bounds

- **Point values, counts and populations get no tolerance.** They come from integer minutes and `numeric(18,6)` weights. Their sums are exact in any order, and the serving database keeps those types (ADR-013 Decision 6).
- **Replicate sums are `float8`, summed in plan order.** Any plan change reorders them: parallel workers, a different join, or the serving database's physical layout. Measured differences:
  - replicate sums, Phase 2–4 SQL vs the pushdown: up to 1.43e-14 relative on the full database and 1.11e-14 on the serving database (ADR-013 Decision 9). With parallel query off: 1.34e-14 (audit probe);
  - standard errors, two runs on the full database: up to 8.57e-14 relative;
  - standard errors, full vs serving: up to 1.28e-13 relative, and 6.24e-13 on near-universal participation (audit, "Brief statements corrected by measurement").
- **1e-12 sits above every measured value.** A real data change moves results by many orders of magnitude more. `tests/integration/test_equivalence_harness.py` shows that one changed episode, and one replicate weight off by 1e-6, are each caught.
- **The scale floor S (F40, I12).** An estimate whose true variance is 0 reports floating-point noise around 0 as its "standard error". Examples are all 18 tier-1 codes, which sum to 1,440 minutes, and a population compared with itself. On 1,440 minutes the audit measured 7.83e-12 vs 7.93e-12: 1.31e-2 relative, which a purely relative bound rejects. Scaled by S = 1,440, the difference is about 7e-17. The floor also widens the margin for near-universal participation, where the relative SE difference (6.24e-13) is within a factor of two of the bound.

### What it does not cover

- **Build identity.** `data_version`, `analysis_key` and the headers that carry them differ between any two databases by design. They identify a build; they are not statistics.
- **Performance.** Nothing in the harness is timed. Capacity answers (429, and 503 `overloaded`, `database_unavailable` or `daily_capacity_reached`) are retried, not compared. `analysis_timeout` and a missing answer count as failures. Performance is measured by the load tests ([`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md)).
- **Agreement with BLS.** The 24 benchmarks cover that: `atus validate-analytics`, `tests/data_quality/test_analytics_benchmarks.py`, `tests/data_quality/test_api_benchmarks.py`, and `scripts/replay_deployed.py` against a site.
- **The full database itself.** `atus validate-db` and `tests/data_quality/test_full_database.py` check it; both fail on a serving database by design.
- **Every possible specification.** Equivalence is shown for the specifications that ran: the seeded extremes, seeded random samples and the 544-specification corpus. It is not proven for all specifications.
- **A deployed host.** A local pass says nothing about one. The deploy smoke and the post-deploy replay apply the same criterion there.

### Where it is enforced

| Check | What it compares | When it runs |
|---|---|---|
| `tests/unit/test_serving_equivalence.py` | the criterion itself: exact fields, the tolerance, the scale floor, comparison scaling, non-finite values, errors, raw statistics (extra columns and replicates included), the seed specifications | CI (`ci.yml` backend) |
| `tests/unit/test_equivalence_harness.py` | the HTTP rules: whitelist, intended differences and their negative cases, headers, retries | CI |
| `tests/unit/ops/test_deploy_smoke.py`, `tests/unit/ops/test_replay_deployed.py` | the deploy-time comparisons: plan-order noise passes, a value outside the criterion fails | CI |
| `tests/unit/analytics/test_year_pushdown_sql.py` | the Phase 2–4 SQL itself: the current template's rendering without the pushdown equals, byte for byte, what the Phase 1–4 code (387fc2d) rendered, frozen in `tests/unit/analytics/phase4_queries.json` for shapes covering every branch of the query, so the checks below that render the Phase 2–4 SQL from the current template compare against the real one; the pushdown and `MATERIALIZED` are the only differences | CI (`ci.yml` backend) |
| `tests/integration/test_year_pushdown.py` | Phase 2–4 SQL vs the current SQL: raw statistics for the extremes plus 250 random specifications, and engine outputs for 120, on the randomized and the hand-computed fixtures | CI (integration) |
| `tests/integration/test_serving_build.py` | full vs serving fixture database: engine results and API responses | CI (integration) |
| `tests/integration/test_equivalence_harness.py` | real servers b and c, on a fixture full database and a serving database built from it; a mutated serving copy must be caught | CI (integration) |
| `atus compare-serving-db [--specs 200] [--seed 20261020] [--report FILE]` | engine-level randomized diff of the full database (`$ATUS_DATABASE_URL`) against the serving one (`$ATUS_SERVING_DATABASE_URL`). Both are read only with `jit=off`. It refuses one database on both sides, a serving database as the full side and an unmarked database as the serving side, and prints both data versions (review round 3; `test_serving_build.py::test_compare_refuses_to_compare_a_database_with_itself`, `::test_compare_fails_on_a_serving_copy_that_differs`) | each release, on the build and on the drill ([runbooks](#serving-db-build-and-restore)) |
| `scripts/equivalence/run.sh` | three-way HTTP replay: a = Phase 1–4 API (387fc2d) on the full database, b = Phase 5 on the full database, c = Phase 5 on the serving database. Corpus: `docs/evidence/phase5/equivalence/specs/` | each release, against its local [restore drill](#restore-drill), never against Neon (the harness refuses a database that is not on this machine); and after any engine SQL change ([`scripts/equivalence/README.md`](../scripts/equivalence/README.md)) |
| `ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_query_plans.py` | full database, opt-in: the seven hazard specifications scan `atus.activities` only inside the materialized CTE, answer within the 85 s budget, and match the Phase 2–4 sufficient statistics | after any engine SQL change |
| `scripts/ops/deploy_smoke.py --recompute 2` (`deploy.yml`) | two home-page starters served from the static snapshot vs recomputed live through the Worker, with request bytes unique to the run (a whitespace nonce), so no edge-cache entry can answer them; an `X-Cache: edge` answer fails, and a quota refusal is OWED ([Deploy](#deploy) step 6). Until review round 4 the bytes were the same on every deploy of a release, so from its second deploy on the edge cache answered (`smoke-recompute-answered-from-kv`) | every deploy |
| `scripts/replay_deployed.py --expected FILE` | every replayed response vs a recording from the full database | after a deploy, quota-budgeted |

### Results

All runs below were local. Nothing has been compared on a deployed host.

| Run | Scope | Result |
|---|---|---|
| Year pushdown, read only (ADR-013 Decision 9) | full database: 71 specifications, 202,272 values; serving database: 161 specifications, 530,892 values | counts and `numeric` sums identical; largest replicate-sum difference 1.43e-14 (full) and 1.11e-14 (serving) |
| `atus compare-serving-db`, restore drill of build `20261020T224252Z-a7420a5c` (ADR-013 Decision 8) | 161 specifications (extremes plus 150 random, seed 20261020), 9,671 values; full database vs the drill read as `atus_api` | all equivalent. Largest relative SE/CI difference 3.33e-13 on non-degenerate estimates; scaled differences within 1.66e-15 |
| HTTP, first run (c8f83a0; b and c at 1bb1d6f) | 544 specifications, 84 GETs ([`evidence/phase5/equivalence/`](evidence/phase5/equivalence/README.md)) | **543/544 and 84/84.** No statistic differed in any pair. The failure: `population-071`, where b (Phase 5, full database) answered 503 `analysis_timeout` at the harness's 600 s limit. Cause: a plan regression from the year pushdown on the full database. The planner underestimated the correlated filters (year, `pandemic_weight > 0`, diary dates) and chose a nested loop that re-ran the activity aggregate once per respondent. An EXPLAIN survey found the same plan for 32 specifications. Largest scaled difference 7.03e-15 (a–b), 4.02e-15 (b–c) |
| HTTP, supplementary run on ca2f5cc | same corpus | 544/544. The timeout is intermittent; the plan hazard was unchanged |
| Fix, 9afd1b4 | `WITH activity_minutes AS MATERIALIZED`: the aggregate is computed once, as in the Phase 2–4 plan. The same rows are aggregated and joined, so no value changes | `tests/data_quality/test_query_plans.py`; `tests/unit/analytics/test_year_pushdown_sql.py` |
| HTTP, final run ([`final-9afd1b4/`](evidence/phase5/equivalence/final-9afd1b4/README.md), committed in 928f50e) | 544 specifications, 84 GETs | **PASS: 544/544 and 84/84**, 0 mismatches, 0 server errors. a–b: 621 equivalent and 7 intended differences, all on GETs (`lexicon_wording` 5, `control_characters_rejected` 1, `health_reports_build` 1). b–c: 628 equivalent. Largest scaled SE/CI difference 4.24e-15 (a–b) and 2.79e-15 (b–c), against the 1e-12 bound. The summary records the code as 283f819: the run started from a working tree that already held the fix, which was committed as 9afd1b4 right after launch with no further edits |
| Deploy rehearsal ([deployment.md step 7](deployment.md#7-first-approved-deploy-ci)) | `deploy_smoke.py --require-snapshot --recompute 2` against `wrangler dev` and the restored release | both starters equivalent between the snapshot and the live answer |
| Replay rehearsal ([deployment.md step 9](deployment.md#9-quota-budgeted-post-deploy-replay)) | `replay_deployed.py`: full database (recording) vs the restored release as `atus_api` | 24/24 benchmarks; all 27 compared responses equivalent; largest scaled difference 1.59e-15 |

## Runbooks

### Conventions

- **Placeholders.** `SITE=https://atus.<account-subdomain>.workers.dev` is the public URL, the repository variable `PUBLIC_BASE_URL`. `ORIGIN=https://<service>.onrender.com` is the API host; it is never published.
- **Secrets** live only in the provider stores and in your files `~/.config/atus/edge.env` and `~/.config/atus/neon*.env` (mode 600; [configuration reference](deployment.md#owner-files-configatus-chmod-600)). Load a value into the environment without printing it, for example:

  ```bash
  export EDGE_OPS_TOKEN="$(grep '^OPS_TOKEN=' ~/.config/atus/edge.env | cut -d= -f2-)"
  ```

  Never put a secret on a command line, in chat or in an issue. Values in `neon*.env` that contain `?` or `&` are stored single-quoted; copy them without the quotes.
- **After every "Save and deploy"** (and any other Render deploy not made by `deploy.yml`): wait until Render → Events shows it live, then check that the API still runs the `live` build. Render does not document which image an image-backed service pulls then; with the Image URL at `:live` ([owner step 2.7](deployment.md#2-render-free-web-service)) it is the last digest a deploy verified, and `deep-check.yml` checks the build every 6 hours. By hand (listing package versions needs the `read:packages` scope: `gh auth refresh -s read:packages` once):

  ```bash
  build="$(GH_TOKEN="$(gh auth token)" python scripts/ops/github_ops.py live-build --owner nathan98000 --package atus-api | cut -d= -f2)"
  RENDER_ORIGIN="$ORIGIN" python scripts/ops/render_deploy.py wait --expect-build "$build"
  ```

  Never use Render's Manual Deploy instead of `deploy.yml`: it skips the smoke.
- **Quota.** Every request to `$SITE/api/...` is one Worker request (100,000 a day, account-wide). `/api/health` can wake Render, never Neon. Steps marked **(wakes Neon)** spend Neon compute.
- **Status.** None of these runbooks has run against a provider. Local rehearsals are cited where they exist.

### Deploy

The procedure is [deployment.md step 7](deployment.md#7-first-approved-deploy-ci). For the very first deploy, follow "Bootstrap" there.

1. **Check before you start:** CI is green on the commit (a push to `main`; a tag must point at a commit CI ran on, on `main`), and the release manifest names a published release. `deploy.yml`'s preflight refuses otherwise, and says what happened to the commit's CI. If it failed at a security check, see [The image scan blocks a deploy](#the-image-scan-blocks-a-deploy) or [A dependency audit blocks a deploy](#a-dependency-audit-blocks-a-deploy). If it is still running, wait for it and re-run the deploy. Not every commit on `main` gets a CI run: a push of several commits runs CI on its head only, and a run still waiting is cancelled when a newer push to `main` queues (`ci.yml`'s concurrency). For a cancelled run, re-run it (`gh run rerun <id>`) and then the deploy; otherwise tag a commit whose CI passed, such as the head of `main`:

   ```bash
   python scripts/ops/release_manifest.py check --require-published
   ```

2. **Start the workflow.** Either push a tag or run it by hand from `main`. The tag must be `vMAJOR.MINOR.PATCH`. It is not compared with `atus_pipeline.__version__`; keep them in step by hand.

   ```bash
   git tag -a v0.2.0 -m "<summary>" && git push origin v0.2.0
   # or
   gh workflow run deploy.yml --ref main -f reason="<why>"
   gh run watch
   ```

3. The `preflight`, `build`, `push` and `snapshot` jobs run unattended.
4. **Approve the `deploy` job** (Actions → the run → Review deployments → `production`). It then:
   - checks that the Cloudflare token still works (`wrangler deployments status`, read only) and stops, with nothing changed, if it does not;
   - records the rollback targets;
   - calls the Render deploy hook with the image digest and waits until `/health` reports this commit;
   - runs `wrangler deploy` with the release vars;
   - runs `deploy_smoke.py` (at most 30 requests) and one browser journey;
   - writes the rollback notes into the run summary.
5. **Check:** the run summary, Render → Events (the deploy is live), Cloudflare → Workers → `atus` → Deployments, and the edge monitor:

   ```bash
   python scripts/ops/monitors.py edge --base-url "$SITE"
   ```

6. **If a smoke check fails, the new version is already live:** the Render hook and the Worker deploy both come before the smoke. Go to [Rollback](#rollback), using the summary's table, unless the only rows that did not pass are OWED (the first item below). The smoke first waits up to a minute for the new release to reach the colo it talks to (check `propagation`), so a `snapshot` or `edge_health` failure after that wait is not propagation lag. If `propagation` shows WARN and only those two checks failed, run `python scripts/ops/monitors.py edge --base-url "$SITE"` once before rolling back: when it passes, the release arrived late and nothing needs rolling back, but the run is not finished (next item).
   - **OWED is not a reason to roll back.** When the quota governor refuses the smoke's live recompute (`daily_capacity_reached`, `capacity_paused` or `rate_limited`, as in a conservative-mode day's last hours or while the month's allowance is paused), the starters are reported `[OWED] ... not recomputed`, with the budget and the time it resumes, and the smoke ends `smoke: 0 failed, ... live recompute(s) owed` with exit status 3 (the run's error is titled "Live recompute owed (no rollback)"). Every other check passed and the release is serving: keep it. The job still fails, so `promote` does not tag a digest no live analysis has run on yet. Once the budget resumes (00:00 UTC for a daily budget; `resumes_at` for `capacity_paused`, which can be days away: `GET /api/_ops/budget`, as in [A quota is exhausted](#a-quota-is-exhausted)), finish the run as under "To keep the new version" below, whose re-run repeats the recompute. `capacity_unavailable` (the governor failed) and every other refusal stay FAIL. Review round 4, `smoke-fails-on-governor-refusal`; `tests/unit/ops/test_deploy_smoke.py`.
   - **A failed smoke skips `browser-smoke` and `promote`** (a failed browser journey, in `browser-smoke`, skips `promote`). Render then runs this run's digest while the GHCR tags `live` and `deployed-<commit>` still name the previous one. Until they move, `deep-check.yml` fails every 6 hours (`[FAIL] api_health: build <this commit>, expected <previous commit>`), and a Render deploy not made through the hook (the "Save and deploy" of a rotation, a release switch or a data rollback) can bring back the previous image, which the [Conventions](#conventions) check would then call correct.
   - **To keep the new version** (the late-arrival case above), finish the run: Actions → the run → **Re-run failed jobs** (`gh run rerun <run id> --failed`), and approve `deploy` if GitHub asks. Its confirmation lists every job it re-runs, the downstream ones included (github.blog/2022-03-16-save-time-partial-re-runs-github-actions, read 2026-10-21): it should be `deploy`, `browser-smoke` and `promote` (only the last two after a failed browser journey), and never `build` or `push`, which would make a new digest. A re-run of `deploy` calls the hook again (its log says `deploy hook accepted <digest>`: check that it is the digest under "This run" in the first attempt's table), repeats the smoke, then runs the browser journey and `promote`. Its own Rollback table names this deploy as "before this run"; keep the first attempt's. As in [Rollback](#rollback), first check that no other Deploy run is in progress or waiting for approval (one concurrency group). Otherwise run the browser smoke by hand ([deployment.md step 8](deployment.md#8-prove-the-deployment)) and tag this run's digest ("This run" in the table) with both `live` and `deployed-<commit>` by hand, as in [Rollback](#rollback).
   - **To roll back after a failed smoke**, roll back the API as well as the edge: `live` still names the API image from before this run, so no tag needs to move. If you roll back only the edge and keep the new API, tag its digest by hand as above.

"Re-run all jobs" builds and pushes a new digest of the same commit. `/health` cannot show that change, so the wait step warns instead of waiting; confirm the deploy in Render → Events.

### Rollback

The summary of every Deploy run has a "Rollback" table: the build, image digest and Worker versions that ran before it. The commands are also in [deployment.md "Rollback"](deployment.md#rollback).

- **API image.** Read the hook URL into the shell without echoing it (`read -rs RENDER_DEPLOY_HOOK_URL && export RENDER_DEPLOY_HOOK_URL`, then paste from your password manager). Then:

  ```bash
  python scripts/ops/render_deploy.py trigger --image-ref ghcr.io/nathan98000/atus-api@sha256:<previous digest>
  RENDER_ORIGIN="$ORIGIN" python scripts/ops/render_deploy.py wait --expect-build <previous commit sha>
  ```

  Then move the GHCR tag `live` back. `live` is the Image URL Render's service is configured with ([owner step 2.7](deployment.md#2-render-free-web-service)); until it moves, `deep-check.yml` fails on the build, and a Render deploy not made through the hook would bring back the image you rolled away from. After a deploy whose smoke failed, `promote` never ran and `live` still names the image you rolled back to: nothing moves ([Deploy](#deploy) step 6).
  - **Within 30 days of the Deploy run that deployed that digest:** Actions → that run → job `promote` → Re-run. GitHub re-runs a job only "up to 30 days after its initial run" (docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs, read 2026-10-21). **First check that no other Deploy run is in progress or waiting for approval** (`gh run list --workflow deploy.yml --limit 5`): `deploy.yml`'s deploys share one concurrency group (`deploy-production`), so the re-run would wait behind that run, possibly for days if it waits for approval, and a later Deploy dispatch would cancel the waiting re-run. Reject the pending run's deployment (that run → Review deployments → Reject) or cancel it (`gh run cancel <id>`), or move the tag by hand as below.
  - **Later, or while another Deploy run is pending, by hand** (what `promote` does; needs the `write:packages` scope on your gh token, `gh auth refresh -s write:packages` once):

    ```bash
    gh auth token | docker login ghcr.io -u Nathan98000 --password-stdin
    docker buildx imagetools create --prefer-index=false \
      --tag ghcr.io/nathan98000/atus-api:live ghcr.io/nathan98000/atus-api@sha256:<previous digest>
    docker buildx imagetools inspect ghcr.io/nathan98000/atus-api:live   # Digest: sha256:<previous digest>
    docker logout ghcr.io
    ```

    If `inspect` shows another digest, the tag names a new manifest: stop and point Render's Image URL at `ghcr.io/nathan98000/atus-api@sha256:<previous digest>` instead, until the next deploy's `promote` (then set it back to `:live`).

    A digest whose Deploy run never reached `promote` (a failed smoke whose release you keep, [Deploy](#deploy) step 6) has no `deployed-<commit>` tag yet: before `docker logout`, run the same `create` with `--tag ghcr.io/nathan98000/atus-api:deployed-<its commit sha>` as well, and `inspect` that tag too. `deep-check.yml` reads the commit it expects from the `live` digest's `deployed-` tag, and fails without one.

  GHCR keeps the newest 20 digests and `live` (`ghcr-cleanup.yml`). Render's dashboard can also roll back to an earlier deploy.
- **Edge** (SPA, snapshot, Worker and its vars; needs `npx --no-install wrangler login` or `CLOUDFLARE_API_TOKEN`):

  ```bash
  cd edge
  npx --no-install wrangler deployments list                      # find the previous version id
  npx --no-install wrangler rollback <version id> --message "rollback"
  cd .. && python scripts/ops/monitors.py edge --base-url "$SITE"
  ```

  - **Changed secrets.** If wrangler asks you to confirm because "The following secrets have changed since version … was deployed", confirming redeploys that version **with its old secret values**. After an edge-secret rotation, the old `EDGE_AUTH_SECRET` is no longer in the API's `ATUS_EDGE_SECRETS`, so every proxied request would get 403 `forbidden_origin`. An old `RENDER_ORIGIN`, or a revoked `NEON_API_KEY` (the governor then falls back to conservative mode), comes back the same way.
    - Decline, and pick a version deployed after the secret change (`npx --no-install wrangler deployments list`).
    - If only an older version will do, first make the API accept the old edge secret as well: Render `ATUS_EDGE_SECRETS=<current>,<EDGE_AUTH_SECRET_PREVIOUS from edge.env>`, "Save and deploy", wait until it is live. Then confirm.
    - Do not count on fixing it afterwards: `wrangler secret put` (4.148) refuses to edit a secret while a version other than the latest is deployed. Roll forward with a new deploy, then rotate again.
  - **Check one analysis through the site** as well as `monitors.py edge`, which never reaches the origin with the edge secret.

- **Keep the API and edge on the same data version.** If the two deploys carry different `DATA_VERSION`s, roll back the data binding as well (next item). Otherwise the edge cache stores nothing and the SPA shows its reload notice.
- **Browsers may hold HTML under rolled-back URLs.** Between a deploy and its rollback, a request for a file of the other version (`/snap/<snapshot_id>/…`, `/assets/<hash>.js`) gets the SPA's `index.html` with the one-year `immutable` Cache-Control of its path (`frontend/public/_headers`). The SPA re-fetches such a snapshot file once with `cache: "reload"`, so the snapshot recovers by itself. A bundle cached that way does not: that browser shows the boot fallback ("ATUS Explorer did not start") until its cache is cleared. Only a page that loaded during the gap and requested the other version's hashed bundle is affected; the SPA has a single entry bundle and no lazy chunks.
- **Data: the release switch in reverse** (ADR-013 "Rollback"). Steps 1 and 2 need no build, scan or CI, so they work even while [the image scan blocks every deploy](#the-image-scan-blocks-a-deploy), and they bring the API and the edge back to one release within minutes, instead of leaving them on different releases for a whole CI run and snapshot job.
  1. In Render → Environment, set `ATUS_DATABASE_URL` back to the previous project's `atus_api` DSN and `ATUS_EXPECTED_DATA_VERSION` to that release's `data_version`, together, then "Save and deploy", and wait until Render → Events shows it live.
  2. Roll the edge back right away, to the Worker version that ran before the release's deploy (the "Rollback" table of that Deploy run, "Before this run"; or `npx --no-install wrangler deployments list`): `cd edge && npx --no-install wrangler rollback <version id> --message "data rollback"`, as in the Edge item above. It restores the Worker's `DATA_VERSION`, `SNAPSHOT_ID` and the old snapshot, so the API and the edge agree again within minutes. If wrangler says secrets have changed since that version, read the Edge item first: after a release switch the changed secret is normally the new project's `NEON_API_KEY`, which the rollback then puts back as it was; decline if any other secret (an edge-secret rotation) is listed.
  3. Make `releases/current.json` on `main` name the previous release again (`git checkout <commit of the previous release> -- releases/current.json`). If the governor reads Neon usage, set `NEON_PROJECT_ID` in `edge/wrangler.jsonc` back too. Commit and push. Until this lands, every deploy from `main` would publish the newer release again.
  4. Wait until CI on that push has passed (`gh run watch`): `deploy.yml`'s preflight refuses a commit without a successful `ci.yml` run on a push to `main`. Then `gh workflow run deploy.yml --ref main -f reason="data rollback to <tag>"` and approve, so `main`, the Worker's code and the snapshot agree. It rebuilds and scans the image: if a red CI run or the scan blocks it, the rollback from steps 1-2 stays in place meanwhile.
  5. If the governor reads Neon usage: step 2's version may carry the previous project's `NEON_API_KEY`, but nothing else keeps it (wrangler cannot read a Worker secret back, and `edge.env` holds the newer project's key), so create a new project-scoped key for the previous project in Neon, load it with `npx --no-install wrangler secret put NEON_API_KEY` once step 4 has deployed (wrangler refuses to edit a secret while an older version is deployed), and revoke the newer project's key. Otherwise the governor may poll the previous project with the newer project's key: every poll fails, and once its last figures are a day old it runs in conservative mode (a daily ceiling of 2.903 CU-h instead of 10).
  6. Check that `curl -s "$SITE/api/_edge/health"` shows the previous `data_version`.

  Keep the newer project; it is deleted only with the owner's approval.
- **API host:** see [Render suspends the service](#render-suspends-the-service).

### The image scan blocks a deploy

`image.yml`'s Trivy step fails the image job on any HIGH or CRITICAL vulnerability in the image that has a fix, and when Trivy cannot download its vulnerability database. `ci.yml` then fails too (`e2e-stack` needs the image), so `deploy.yml`'s preflight refuses the commit, and `deploy.yml`'s own build (always from scratch) fails at the same step. Every deploy stops: code, a data release's switch, and the `deploy.yml` step of a data rollback.

1. **Read the scan step** (Actions → the run → job Image → "Scan the image"): the package, the vulnerability and its fixed version. No table but a download or network error is transient: re-run the job later.
2. **Fix forward when you can.**
   - A Debian package of the base image: bump the base digest (`python:3.12-slim@sha256:…` on both `FROM` lines of `Dockerfile` and in `ARG PYTHON_DIGEST`). Dependabot proposes it after its 7-day cooldown; by hand, `docker buildx imagetools inspect python:3.12-slim` shows the current digest. The Dockerfile never runs `apt-get upgrade`, so the fix arrives only with a rebuilt upstream image.
   - A Python wheel: bump it in the locks (`requirements/compile.sh --upgrade-package <name>`).
3. **Otherwise accept it for a short, recorded time.** Only when the advisory shows that the vulnerable code is not reachable in the API, add it to `.trivyignore` in a pull request: a comment with the date, who reviewed it and why, then the ID with an expiry at most 31 days out (`CVE-2026-12345 exp:2026-12-02`; the file's header has the format). Trivy stops ignoring it on that date; `tests/deploy/test_infra_review_r2.py` refuses an entry without a reason or with a later expiry. Remove the entry with the fix. The SARIF report still lists accepted entries.
4. **Rollbacks need no pipeline.** The API rolls back through the Render deploy hook and the edge through `wrangler rollback` ([Rollback](#rollback)); neither builds or scans. A data rollback's steps 1-2 are those two; its manifest commit and `deploy.yml` run can wait until the scan passes.

### A dependency audit blocks a deploy

`ci.yml` audits what the deployed artifacts run: `pip-audit` of `requirements/api.lock.txt` (job Supply chain), and `npm audit --omit=dev` of the SPA's runtime packages at high and above (job Frontend) and of the Worker's at every severity (job Edge Worker). Each fails on a known advisory whether or not a fixed version exists. The push run then fails, so `deploy.yml`'s preflight (`github_ops.py deploy-gate`) refuses the commit: every deploy stops, hotfixes and the `deploy.yml` step of a data rollback included. The running service is not affected. Each audit runs through `deploy/ci/audit_gate.py` with its own reviewed allowlist (review round 3; before, a disclosed advisory with no fix blocked every deploy until someone edited `ci.yml`).

1. **Read the failed step** (Actions → the run → the job → its audit step): the package, the advisory ID and whether a fixed version exists. For npm the gate prints a `[FAIL]` line per advisory; pip-audit prints its own table. An error from the registry or the advisory service (no table, no `[FAIL]` line) is transient: re-run the job.
2. **Fix forward when you can.**
   - Python: `requirements/compile.sh --upgrade-package <name>`, then commit the locks.
   - npm: `npm update <name>` in `frontend/` or `edge/` (or a new range in its `package.json`), then commit `package-lock.json`.
3. **Otherwise accept it for a short, recorded time.** Only when the advisory shows that the vulnerable code is not reachable in what that tree ships (the API, the SPA bundle, the Worker), add it in a pull request to the allowlist of the job that failed: `.pip-audit-ignore` (the API lock), `frontend/.npm-audit-ignore` or `edge/.npm-audit-ignore`. Write a comment with the date, who reviewed it and why, then the ID with an expiry at most 31 days out (`GHSA-xxxx-xxxx-xxxx exp:2026-12-02`). pip-audit takes a `GHSA-`, `CVE-` or `PYSEC-` ID and also matches an advisory's aliases; npm takes the `GHSA-` ID from the advisory's github.com/advisories URL. `deploy/ci/audit_gate.py` stops accepting an entry on its expiry date and refuses a file with an entry that has no reason or a later expiry (so does `tests/deploy/test_infra_review_r3.py`). Remove the entry with the fix: for npm, the gate notes an entry that no longer matches anything.
4. **Rollbacks need no pipeline**, as for the image scan ([The image scan blocks a deploy](#the-image-scan-blocks-a-deploy), step 4).

### Serving-DB build and restore

Background: ADR-013 Decisions 6–8 and [deployment.md steps 1, 2 and 5](deployment.md#1-build-the-serving-db-and-its-release-artifact). The full database is only read.

**Build** (your machine):

1. **Start a new, empty PostgreSQL 16 database.** For example, the stack's `db` service under its own compose project. It creates database `atus` owned by the non-superuser `neondb_owner`, on `127.0.0.1:5470` (or `ATUS_STACK_DB_PORT`):

   ```bash
   cp deploy/stack/stack.env.example .env.stack     # then fill in every empty ATUS_STACK_* secret; .env.* is gitignored
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # one fresh value per secret
   docker compose -p atus-build --env-file .env.stack -f docker-compose.prod.yml up -d --wait db
   ```

2. **Build and dump.** Export `ATUS_SERVING_DATABASE_URL` (the `neondb_owner` DSN of that database). `ATUS_DATABASE_URL` is the full database. Then:

   ```bash
   atus build-serving-db --artifact-dir ~/atus-artifacts/<release>-<UTC date> --report build-report.json
   atus compare-serving-db --specs 200 --report compare-report.json
   ATUS_DATABASE_URL="$ATUS_SERVING_DATABASE_URL" atus verify-serving-db \
       --manifest ~/atus-artifacts/<dir>/atus-serving-<release>-<build_id>.manifest.json --explain
   ```

   The build refuses a source that has no passing `validate-db` and `validate-analytics` results recorded for its loaded data. With `--artifact-dir` it first resolves and runs `pg_dump` (local, else the `postgres` image through Docker), so a missing client stops it while the target is still empty. It writes the dump, `.sha256` and manifest right after the build, and the `--report` before the dump. A built target refuses a second build, so if the dump still fails (a full disk, an image pull), drop and recreate the target as an empty database and run the build again. Run `atus validate-analytics` only on restored copies: it writes to `validation_results`.
3. **Check the size.** Compare the manifest's sizes with the provider limit. The restore enforces `--size-limit-bytes`, default 1,000,000,000.

**Restore into Neon** (owner; [deployment.md step 5](deployment.md#5-load-the-serving-db-into-neon-owner)). Use one new project per release, over the direct endpoint, never `-pooler`. Keep one secrets file per project, so that a rollback still has the previous project's DSN. Prefer the file to an exported `ATUS_OWNER_DATABASE_URL`: the restore takes the exported one first, and refuses when it names another database than the file's (the API role's credentials are written to the file).

```bash
# ~/.config/atus/neon-<release>-<build8>.env (mode 600) holds
#   ATUS_OWNER_DATABASE_URL='postgresql://neondb_owner:...@ep-....neon.tech:5432/atus?sslmode=verify-full&sslrootcert=system'
scripts/ops/restore_serving_db.sh --artifact ~/atus-artifacts/<dir>/atus-serving-<release>-<build_id>.dump \
    --secrets-file ~/.config/atus/neon-<release>-<build8>.env --report restore-report.json
```

This step **(wakes Neon)**. Do not pass `--verify-content-hash` on Neon: it re-reads every row, which costs about the data's size in egress. The script writes `ATUS_API_DB_PASSWORD` and `ATUS_API_DATABASE_URL` into the secrets file. The report has the checks, the metered size and each step's duration (`seconds`).

TLS needs the system's CA roots on your machine. The script finds them itself (macOS `/etc/ssl/cert.pem`) unless `SSL_CERT_FILE` names another bundle, and hands them to the `postgres:16` docker client when there is no local `pg_restore`/`psql` 16 or newer ([deployment.md, "TLS to the database"](deployment.md#tls-to-the-database)). On a machine without a system bundle, `export SSL_CERT_FILE=<CA bundle>` first. "certificate verify failed" means no roots were found (or the server is not the one named); never work around it with `sslmode=require`, which the restore upgrades to `verify-full` for any remote host anyway (review round 3).

**Restore after a lost project.** The release artifact is the backup.

1. Create a new Neon project ([owner steps 3.2–3.4](deployment.md#3-neon-free)).
2. Fetch and verify the published assets:

   ```bash
   python scripts/ops/release_manifest.py fetch --out ~/atus-artifacts/restore
   ```

3. Restore as above.
4. Set only `ATUS_DATABASE_URL` on Render ("Save and deploy"). The same artifact carries the same `data_version`, so `ATUS_EXPECTED_DATA_VERSION`, the Worker vars and the snapshot do not change.
5. Update `NEON_PROJECT_ID` and `NEON_API_KEY` if the governor uses them.

**Re-apply roles** on a restored serving database (for example after a role change in `scripts/ops/roles.sql`):

```bash
scripts/ops/restore_serving_db.sh --artifact <the release dump> --secrets-file <that project's file> --roles-only
```

It refuses a database without the serving marker. It also never replaces the API role's password unasked: when the secrets file holds no `ATUS_API_DB_PASSWORD` (a wrong `--secrets-file`, or none at that path while `ATUS_OWNER_DATABASE_URL` is exported) and the role exists, it stops before changing anything; pass the file the restore wrote, or `--rotate-api-password` to rotate on purpose. When it gives an existing role a new password, it first locks the role out (`NOLOGIN`) and ends its open sessions, and applies `roles.sql` (which sets `LOGIN` again) only once none is left; if one cannot be ended it stops there, the role still locked out, and says how to end them (review round 3). Its checks read the planner statistics from `pg_stats`, which a Neon suspend keeps, so it passes after a suspend too. Whenever `roles.sql` set a new password (`--rotate-api-password`, or a first run), the output says so in a note, whether the checks then passed, failed, or the run stopped with an error after `roles.sql` (the new password is in effect anyway), and only the file's `ATUS_API_DATABASE_URL` works from then on: update Render's `ATUS_DATABASE_URL`.

**Finish a restore that failed after `pg_restore`** (for example `roles.sql` refused by the provider: the error shows PostgreSQL's reason). `pg_restore` had committed, so rerunning the restore is refused ("the target database is not empty", with a pointer to this). Fix the cause, then run the same command with `--roles-only`: it applies `roles.sql`, writes the API credentials, runs `VACUUM (ANALYZE)` because the tables have no planner statistics yet, and runs every check. A new database (a new Neon project) is never needed for this.

### Data release

The normative procedure is ADR-013 "Release procedure on the free stack (§15)". Command by command:

1. **Full database.**

   ```bash
   atus download && atus extract && atus validate-source && atus migrate \
     && atus load --validate && atus validate-db && atus validate-analytics
   ```

   Then record the expected answers from a local API on the full database, and stop that API again: step 5 starts another one on the same port, which cannot bind while this one runs.

   ```bash
   atus api &
   python scripts/replay_deployed.py --base-url http://127.0.0.1:8000 --record ~/atus-artifacts/expected-<release>.json
   kill %1
   ```

2. **Build and prove:** [Serving-DB build and restore](#serving-db-build-and-restore), then the [Restore drill](#restore-drill), which includes the HTTP equivalence harness.
3. **Publish (owner).** Create an immutable GitHub release with the dump, its `.sha256` and the manifest. Then name it in the repository:

   ```bash
   gh release create data-<release>-<build_id> <dump> <dump>.sha256 <manifest> \
       --title "Serving database <release> (<build_id>)" --notes "<data_version>"
   python scripts/ops/release_manifest.py write --artifact-dir <artifact dir> --tag data-<release>-<build_id>
   python scripts/ops/release_manifest.py check --require-published
   ```

   Commit `releases/current.json` (and `NEON_PROJECT_ID` in `edge/wrangler.jsonc` if used). Data release tags do not match `v*`, so they deploy nothing.
4. **New Neon project and restore (owner):** [Serving-DB build and restore](#serving-db-build-and-restore). Never restore a second copy into the current project: two copies exceed its 1 GB.
5. **Smoke a local API against the new project (wakes Neon).** Start `atus api &` (development mode, loopback) with `ATUS_DATABASE_URL` set to that project's `ATUS_API_DATABASE_URL` (`verify-full` + `system`: `atus api` sets `SSL_CERT_FILE` to the system bundle when it is unset, as the restore does), then replay against it with the release's `data_version` (the manifest from step 3), and stop it:

   ```bash
   python scripts/replay_deployed.py --base-url http://127.0.0.1:8000 \
       --expected ~/atus-artifacts/expected-<release>.json \
       --expect-data-version "$(jq -r .data_version releases/current.json)" --report replay-local.json
   kill %1
   ```

   `--expect-data-version` fails the replay unless the new project answered. Without it, an API still running from step 1 (the full database) would answer instead, compare the full database with itself, and pass; the replay also fails when the site reports the data version the expected file was recorded from.

6. **Switch.**
   1. Push the commit from step 3. Wait until CI on that push has passed (`gh run watch`; `deploy.yml`'s preflight refuses the commit until then), then run `gh workflow run deploy.yml --ref main -f reason="data release <tag>"`.
   2. While `deploy` waits for approval (its snapshot is already built), set Render's `ATUS_DATABASE_URL` and `ATUS_EXPECTED_DATA_VERSION` (the manifest's `data_version`) together, "Save and deploy", and wait until Render → Events shows it live. Until the next step the API answers with the new version while every page load still reads the old snapshot, and the Worker stores nothing in KV. Every visitor who opens an analysis the snapshot does not hold — in an open tab, after a reload, or in a new tab — gets it from the new release next to starter examples and descriptions from the old one, and sees a notice that the service is switching data releases and that reloading does not change that yet (no "Reload page" button: a reload loads the same old snapshot). After `deploy` publishes the new snapshot, the notice in open tabs offers the reload within about a minute (each visible tab re-reads the pointer every minute), and the reload then helps. Keep this window short: approve `deploy` as soon as Render shows the switch live.
   3. If used, load the new project's `NEON_API_KEY` with `npx --no-install wrangler secret put NEON_API_KEY`.
   4. Approve `deploy`. Its wait step refuses to publish anything at the edge unless Render's `/health` reports the new `ATUS_EXPECTED_DATA_VERSION`, and its smoke checks `X-Data-Version`. From the push in 6.1 on, every deploy from `main` carries the new release: a hotfix approved before the Render switch fails at that wait (nothing is published at the edge).
7. **Verify (at most 40 requests; wakes Neon):**

   ```bash
   python scripts/replay_deployed.py --base-url "$SITE" --expected ~/atus-artifacts/expected-<release>.json \
       --expect-data-version "$(jq -r .data_version releases/current.json)" --report replay.json
   ```

8. **Keep the previous project** until the owner approves its deletion. Rollback is in [Rollback](#rollback), "Data".

### New ATUS year

A new multi-year file (for example 2003–2026, release `0326`) is a data release with extra steps first. They are in ADR-013 "New ATUS year procedure" and [docs/source-data.md "Adding the next release"](source-data.md#adding-the-next-release-eg-20032026-0326):

1. Read BLS's release notes and "Changes between data files".
2. Add `multiyear_release("0326", first_year=2003, last_year=2026, expected_rows={...})` to `_REGISTRY` in `src/atus_pipeline/sources.py`. The counts come from each new `*_info.txt`.
3. Generate the lexicon and review the diff against `activity_lexicon_0325.csv`. Add its counts to `tests/unit/test_lexicon_reference.py`. Review the presets in `src/atus_pipeline/analytics/spec.py` and the benchmark codes for retired or renamed codes.

   ```bash
   python scripts/extract_lexicon.py <lexiconnoex0326.pdf> data/reference/activity_lexicon_0326.csv
   ```

4. Run the pipeline with `ATUS_RELEASE=0326`.
5. **Benchmarks.** Run the 24 existing benchmarks unchanged. If BLS revised a published value, record the revision; never edit an expectation to make it pass. Add the new year's published Table A-1 values as new benchmarks in `src/atus_pipeline/analytics/benchmarks.py`.
6. Continue with the [Data release](#data-release) runbook. Re-check the serving database's size against the provider limit: release 0325 uses 65% of 10^9 B after a restore (ADR-013).
7. **Frontend.** Years, the default year and the year pickers come from `/meta`; in production that is the static snapshot, which `scripts/build_snapshot.py` builds from the release's `/meta`. No frontend change is expected. `frontend/src/pages/newSurveyYear.test.tsx` and `frontend/e2e/new-survey-year.spec.ts` serve a `0326` `/meta` with years through 2026 and check that every year control follows it and that 2020 stays a gap (b2e44a5). After the deploy, still check that the year pickers list the new year.

### Restore drill

Restore every release artifact once, as production will, into a fresh cluster with Neon's Postgres major and a non-superuser owner not named `atus`. Do this before publishing. ADR-013 Decision 8 records the 2026-10-20 drill (illustrative timings on a shared laptop). The timed drill ran alone on 2026-10-21: the restore tool took 15.6 s of wall time (`pg_restore` 8.0 s, `VACUUM (ANALYZE)` 1.3 s, the serving checks with the full content hash 5.2 s), all 22 checks passed, and `sum(pg_database_size)` was 649,903,180 B ([record, section 3](../loadtests/results/2026-10-21-emulation.md#3-a-restore-drill-timed)). It used the stack's unconstrained `postgres:16`, so the time does not transfer to Neon.

1. **A fresh cluster.** The stack's `db` service: `postgres:16`, `jit=off`, owner `neondb_owner`. Set `ATUS_STACK_DB_PORT` if another stack already uses port 5470.

   ```bash
   docker compose -p atus-drill --env-file .env.stack -f docker-compose.prod.yml up -d --wait db
   ```

2. **A drill secrets file.** In an editor, create `~/.config/atus/drill.env` (mode 600) with `ATUS_OWNER_DATABASE_URL='postgresql://neondb_owner:<ATUS_STACK_OWNER_PASSWORD>@127.0.0.1:<port>/atus'`.
3. **Restore and check.** The report holds each step's duration and the total.

   ```bash
   scripts/ops/restore_serving_db.sh --artifact <dump> --secrets-file ~/.config/atus/drill.env \
       --verify-content-hash --report drill-report.json
   ```

4. **Prove it.** Run `atus validate-analytics` and the benchmark tests with `ATUS_DATABASE_URL` set to the drill's owner DSN:

   ```bash
   atus validate-analytics
   ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_analytics_benchmarks.py tests/data_quality/test_api_benchmarks.py
   ```

   `compare-serving-db` prints both data versions, and refuses (exit 1, nothing compared) when both sides name the same database, when the full side is a serving database, or when the serving side has no serving marker: an `ATUS_DATABASE_URL` left on the drill would otherwise compare the drill with itself and pass (review round 3). Then, with `ATUS_DATABASE_URL` set back to the full database and `ATUS_SERVING_DATABASE_URL` set to the drill's `ATUS_API_DATABASE_URL`:

   ```bash
   atus compare-serving-db --specs 150 --report compare-drill.json
   scripts/equivalence/run.sh setup        # once: the 387fc2d baseline
   scripts/equivalence/run.sh --specs docs/evidence/phase5/equivalence/specs --out <dir> \
       --serving-secrets-file ~/.config/atus/drill.env --jobs 1
   ```

   Never run `atus validate-db` or `tests/data_quality/test_full_database.py` on the drill: they check the full database and fail by design.
5. **Tear down:**

   ```bash
   docker compose -p atus-drill --env-file .env.stack -f docker-compose.prod.yml down -v
   ```

   Then delete `drill.env`.
6. **Optionally smoke the image on the same dump**, after the teardown: the stack's `dump` profile restores it into a stack of its own and runs the smoke probe against it. Not while the drill runs: that stack's `db` publishes the same `127.0.0.1:${ATUS_STACK_DB_PORT}` as the drill's (5470 by default). Nor in the drill's own project (`-p atus-drill`) to share its database: there the stack's `roles` service would re-apply `roles.sql` with the stack's `ATUS_STACK_API_DB_PASSWORD`, replacing the `atus_api` password that `drill.env` holds. Set `ATUS_STACK_DUMP` in `.env.stack` to the dump, then:

   ```bash
   docker compose -p atus-drill-smoke --env-file .env.stack -f docker-compose.prod.yml --profile dump up -d --wait api
   docker compose -p atus-drill-smoke --env-file .env.stack -f docker-compose.prod.yml --profile smoke run --rm smoke
   docker compose -p atus-drill-smoke --env-file .env.stack -f docker-compose.prod.yml --profile dump --profile smoke down -v
   ```

   The teardown names both profiles: a plain `down -v` leaves the exited `restore` container behind. This is the same check as [deployment.md step 2](deployment.md#2-restore-drill).

### Incident first steps

1. **Find which tier fails.** These use at most 10 Worker requests (the deep check retries through a cold start) and none reach Neon:

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' "$SITE/"          # static floor: never invokes the Worker
   curl -sS "$SITE/api/_edge/health"                            # Worker alive; DATA_VERSION, SNAPSHOT_ID
   python scripts/ops/monitors.py deep --base-url "$SITE"       # /api/health -> Render /health (may wake Render)
   python scripts/ops/monitors.py quota --base-url "$SITE"      # governor budgets; needs EDGE_OPS_TOKEN
   ```

2. **Read the error code** of a failing request (browser network panel: `error.code`, `details.reason`, `details.budget`) and look it up below.
3. **Look at the providers:**
   - Render → the service → Events (deploys, restarts, "Image Pull Failed", suspension notices) and Logs. The logs are one JSON access line per request (`status`, `error`, `db_ms`, `request_id`, `edge_authenticated`) plus a metrics event every 5 minutes while requests arrive; Render keeps them for 7 days.
   - Cloudflare → Workers → `atus` → Logs (one line per `/api` request with outcome, origin status and `Server-Timing`) and Deployments.
   - Neon → the release project: compute state, Monitoring, usage (compute hours, network transfer, storage), any quota notice.
   - GitHub → Actions: the latest monitor runs, and the latest Deploy run's summary.
   - Email notices from Render, Neon, Cloudflare and GitHub.
4. **Ask the origin directly, if needed.** This bypasses the Worker and costs Render outbound. Export `ATUS_OPS_TOKEN` from `edge.env` first.

   ```bash
   curl -sS "$ORIGIN/health"                                               # build; touches nothing
   curl -sS -H "Authorization: Bearer $ATUS_OPS_TOKEN" "$ORIGIN/metrics"   # counters since the process started
   curl -sS -H "Authorization: Bearer $ATUS_OPS_TOKEN" "$ORIGIN/ready"     # data_version vs expected; reads the DB (can wake Neon) only when its snapshot is older than ATUS_API_SNAPSHOT_TTL_S (600 s), so check snapshot_age_s
   ```

5. **Contain:**
   - roll back if the incident started with a deploy;
   - rotate any secret that may have leaked;
   - for quota problems, follow [A quota is exhausted](#a-quota-is-exhausted).

   Do not redeploy blind.
6. **Write down** the times (UTC), the codes and what you changed. If a limit changes, update ADR-012 or ADR-015 and `docs/deployment.md`.

| Symptom | Likely cause | Next step |
|---|---|---|
| `$SITE/` fails | Cloudflare, or a broken Worker deploy | Cloudflare status; Deployments; edge [rollback](#rollback) |
| `/api/*` gives a non-JSON 429 (the SPA says "daily request allowance reached"), or Cloudflare's Error 1027 page with another status (a 5xx looks like a cold start to the SPA, another 4xx like a generic error) | Worker 100,000 requests/day used | [quota runbook](#a-quota-is-exhausted) |
| 503 `service_waking` | Render spinning up, or suspended (its answer then is undocumented; [security.md R13](security.md#accepted-risks)) | normal if it lasts about a minute; if it persists, Render → Events and [Render suspends the service](#render-suspends-the-service), and set the [pause switch](#pausing-the-api) |
| 503 `api_paused` | the owner's pause switch is on (`/api/_edge/health` shows `api_paused`) | intended while the API is paused; [Pausing the API](#pausing-the-api) to clear it |
| 502 `upstream_unavailable`, `reason` `not_configured` (Workers Logs: `origin_not_configured`, for either cause) | Worker secret `RENDER_ORIGIN` missing or invalid, or `EDGE_AUTH_SECRET` missing | `npx --no-install wrangler secret list` in `edge/` shows which name is absent; put it from `edge.env` (`wrangler secret put RENDER_ORIGIN` or `EDGE_AUTH_SECRET`) |
| `upstream_unavailable`, `reason` `connect_failed`, `circuit_open` or `platform_error` | Render down, suspended, or image pull failed | Render → Events; [Render suspends the service](#render-suspends-the-service) |
| `upstream_unavailable`, `reason` `origin_timeout` | a 524: the origin held the request past the proxy timeout | Render logs for `analysis_timeout` and `db_ms` |
| 403 `forbidden_origin` through the site | the Worker's `EDGE_AUTH_SECRET` is not in the API's `ATUS_EDGE_SECRETS` (a rotation step skipped) | [rotation](#rotating-tokens-and-secrets), edge secret |
| 503 `capacity_unavailable` | governor error, Durable Object quota, or `IP_HMAC_KEY` missing (`ip_hmac_key_not_configured` in Workers Logs) | Workers Logs; [quota runbook](#a-quota-is-exhausted) |
| 503 `daily_capacity_reached`; 429 `rate_limited` with `budget: "prefix"` or `"metadata"` | the governor's daily caps (by design), or the API's own backstop cap (`atus_daily_cap_used` on `/metrics`), which counts only analyses that reached the engine ([ADR-012](adr/ADR-012-load-and-quota-protection.md#daily-cap-process-wide-backstop)) and resets on a restart | quota report; [deployment.md "Scraping-burst scenarios"](deployment.md#scraping-burst-scenarios) |
| 503 `capacity_paused` | the governor's monthly Neon pacing | [quota runbook](#a-quota-is-exhausted) |
| 503 `database_paused` | Neon compute or egress quota exhausted | [quota runbook](#a-quota-is-exhausted) |
| 503 `database_unavailable` | Neon unreachable, wrong credentials or TLS failure | Neon console; Render logs (driver class, SQLSTATE); Render's `ATUS_DATABASE_URL`. The governor refunds these answers, so no network is locked out and the day's compute is not spent while the connection fails (ADR-015 amendments, review round 3); the API gives back its own daily-cap unit and analysis token for each (ADR-012, review round 4) |
| 503 `data_version_mismatch` | Render's `ATUS_EXPECTED_DATA_VERSION` differs from the database's | finish or reverse the release switch ([Rollback](#rollback), "Data") |
| 503 `data_not_loaded` or `schema_not_initialized` | `ATUS_DATABASE_URL` points at the wrong or an empty database | the DSN vs the restored project |
| 503 `overloaded` or `analysis_timeout` | load, or a slow plan | Render logs `db_ms`; `/metrics`; compare with the emulation's numbers ([`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md): at 0.25 CU sheds start at 4 concurrent clients, and the F66 compare takes 69–71 s of the 85 s timeout) |
| no monitor emails, no recent monitor runs | scheduled workflows disabled | [Scheduled workflows were disabled](#scheduled-workflows-were-disabled) |
| deep check fails with `[FAIL] api_health: build <X>, expected <Y>` | Render runs another image than the one tagged `live`: a deploy that failed after its Render hook (a smoke, or the wait or `wrangler deploy`; `promote` skipped), an API rollback whose tag has not moved yet, or a Render deploy not made through the hook that brought back another image. A deep check during a deploy may see the old build: re-run it first | if X is the build you mean to run, move the tags: [Deploy](#deploy) step 6, or [Rollback](#rollback) after an API rollback; otherwise redeploy the `live` digest through the hook ([Rollback](#rollback), API image, with build Y) |
| deep check fails at "Which build should run (the `live` tag)" | the run's token may not read the package's versions (401/403), or no version is tagged `live` | [The deep check cannot read the live tag](#the-deep-check-cannot-read-the-live-tag) |

### Rotating tokens and secrets

The procedures are in [deployment.md "Rotation"](deployment.md#rotation). `tests/deploy/test_packaging_files.py::test_every_secret_has_a_rotation_entry` keeps that list complete. Commands and checks:

| Secret | Commands | Check afterwards | Visitors |
|---|---|---|---|
| Edge secret (`EDGE_AUTH_SECRET` / `ATUS_EDGE_SECRETS`) | `scripts/ops/gen_edge_secrets.sh --rotate EDGE_AUTH_SECRET` (keeps the old value as `EDGE_AUTH_SECRET_PREVIOUS`, the value the Worker sends until the `wrangler secret put` below, and refuses another such rotation until `--finish-rotation`) → Render `ATUS_EDGE_SECRETS=<new>,<old>` (`<old>` = `EDGE_AUTH_SECRET_PREVIOUS`), Save and deploy, wait for live → in `edge/`: `grep '^EDGE_AUTH_SECRET=' ~/.config/atus/edge.env \| cut -d= -f2- \| npx --no-install wrangler secret put EDGE_AUTH_SECRET` → Render `ATUS_EDGE_SECRETS=<new>`, Save and deploy → `gen_edge_secrets.sh --finish-rotation` | `monitors.py deep`; one analysis on the site; `curl -s -o /dev/null -w '%{http_code}' "$ORIGIN/api/v1/meta"` still prints 403 | none, if the order is kept |
| Worker `OPS_TOKEN` (GitHub `EDGE_OPS_TOKEN`) | `gen_edge_secrets.sh --rotate OPS_TOKEN` → `grep '^OPS_TOKEN=' ~/.config/atus/edge.env \| cut -d= -f2- \| npx --no-install wrangler secret put OPS_TOKEN` → `grep '^OPS_TOKEN=' ~/.config/atus/edge.env \| cut -d= -f2- \| gh secret set EDGE_OPS_TOKEN --env ops` (and `--env production` if set there) | `gh workflow run quota-report.yml` | none |
| API `ATUS_OPS_TOKEN` | `gen_edge_secrets.sh --rotate ATUS_OPS_TOKEN` → Render env, Save and deploy | `/metrics` with the new bearer | none |
| `atus_api` password | `scripts/ops/restore_serving_db.sh --artifact <release dump> --secrets-file <project file> --roles-only --rotate-api-password` (it first locks the role out and ends its open sessions, then resets every setting of the role; review round 3) → copy the new `ATUS_API_DATABASE_URL` to Render's `ATUS_DATABASE_URL`, Save and deploy | the restore's `[PASS] restore: API role settings are roles.sql's alone`, `[PASS] restore: API role's earlier sessions ended` and `[PASS] serving: no large objects` (roles.sql prints how many large objects of the role it removed; review round 4) (if it stops because a session could not be ended, restart the compute: Neon → the branch → Postgres database → Computes → the compute's menu → Restart compute, which ends every connection; then run it again); `monitors.py deep`, then one uncached analysis (wakes Neon) | the API cannot connect between the two steps (503 `database_unavailable`); do it while idle |
| Cloudflare API token (90-day TTL) | a new Custom token with the same scope ([owner step 1.5](deployment.md#1-cloudflare-workers-free)) → check it in `edge/`: `read -rs CLOUDFLARE_API_TOKEN && export CLOUDFLARE_API_TOKEN` (paste; nothing is shown), `test -n "$CLOUDFLARE_API_TOKEN" && CLOUDFLARE_ACCOUNT_ID=<Account ID> npx --no-install wrangler deployments status` (authenticates, changes nothing; `wrangler deploy --dry-run` never authenticates, and without the `test -n` an empty paste would fall back to your OAuth login), `unset CLOUDFLARE_API_TOKEN` → `gh secret set CLOUDFLARE_API_TOKEN --env production` (paste at the prompt) → delete the old token. If it leaked (or the Cloudflare account did), then in this order ([deployment.md "Rotation"](deployment.md#rotation), item 5): look for versions you did not deploy with `npx --no-install wrangler deployments list`; replace them with a `deploy.yml` deploy of a new commit that sets a fresh `ASSET_SALT` (`openssl rand -hex 8`) in `frontend/src/deploy/assetNames.ts`, never with `wrangler rollback`; straight after, rotate every Worker secret, since code deployed with the token can read and set them all: `IP_HMAC_KEY` first, which invalidates every KV entry and moves the Worker to a fresh governor instance, so nothing that code wrote counts any more (ADR-015), then `OPS_TOKEN`, the edge secret `EDGE_AUTH_SECRET` and, if set, `NEON_API_KEY` with its restore into a new project (the rows here), and put `RENDER_ORIGIN` again; set the pause switch's `API_PAUSED_UNTIL` and `API_PAUSED_REASON` again with `deploy.yml` (`pause-api` or `resume-api`), since the token can change them ([Pausing the API](#pausing-the-api)). Do not rely on an `EPOCH` bump (its next value is public), and never roll back to a version from before the rotation | the next Deploy run, which repeats the read check before the Render hook (an expired or revoked token stops it there, nothing changed) and proves the deploy permission at `wrangler deploy` | none |
| Cloudflare analytics token (optional, 90-day TTL) | a new Custom token with Account Analytics: Read on this account ([owner step 4.3](deployment.md#4-github-repository-nathan98000atus-stays-public)) → `gh secret set CLOUDFLARE_ANALYTICS_TOKEN --env ops` (paste at the prompt) → delete the old token | `gh workflow run quota-report.yml`: the "Worker requests today" line passes | none |
| Render deploy hook | Render → Settings → Deploy Hook → Regenerate (the old URL stops at once) → `gh secret set RENDER_DEPLOY_HOOK_URL --env production` | the next Deploy run | none |
| `IP_HMAC_KEY` | `gen_edge_secrets.sh --rotate IP_HMAC_KEY` → `wrangler secret put IP_HMAC_KEY` piped as above, best soon after 00:00 UTC | `monitors.py quota` | the Worker moves to a fresh governor instance, so every count restarts and the day can admit up to its daily budgets again; every KV entry is orphaned and computed again on its next request |
| Neon owner password, `NEON_API_KEY` | reset in the Neon console and update the project's `neon*.env`; revoke and re-create the key, then `npx --no-install wrangler secret put NEON_API_KEY`. If either may have leaked (or the Cloudflare deploy token while the key was set), restore the release into a new project ([Serving-DB build and restore](#serving-db-build-and-restore), "Restore after a lost project"), switch Render's `ATUS_DATABASE_URL` to it, re-create the key for the new project, and ask to delete the old one. A content-hash check of the old project is no substitute: an owner can change what `atus_api` computes without changing any row (a row-level security policy, a `search_path` setting), and that check re-reads rows only (review round 3) | `monitors.py quota` (last poll error); after a leak, the new project's restore passed and `monitors.py deep` reads its data version | a leak: none, if the switch follows the release procedure |

After a suspected leak, rotate first: the secret, and every secret it exposes ([security.md, "Leaked tokens"](security.md#10-leaked-tokens)). Then look for use in Render logs, Workers Logs and Neon ([deployment.md "Rotation"](deployment.md#rotation)). What each leaked secret allows is in [security.md, "Leaked tokens"](security.md#10-leaked-tokens).

### A quota is exhausted

Every exhaustion degrades toward the static floor: the SPA, the snapshot (starters, presets, metadata, about 130 prewarmed analyses) and KV-cached results. With no card on file, no provider bills. **Never add a card, start a trial or upgrade.** The limits and what visitors see are in [deployment.md "Free-tier budget"](deployment.md#free-tier-budget) and ["Failure modes and degradation"](deployment.md#failure-modes-and-degradation).

| Quota | Signal | Action |
|---|---|---|
| Worker requests, 100,000/day, account-wide | Before: the daily quota report's "Worker requests today" line above 70% of the pro-rated allowance (only with the optional analytics token, owner step 4.3). After: `/api` answers a non-JSON 429 (the SPA's daily-allowance page), or Cloudflare's Error 1027 page with another status, which the SPA retries as a cold start (5xx) or shows as a generic error (4xx) | Wait for 00:00 UTC. Find the source in Workers Logs (deployment.md scenarios S1–S3). Nothing at $0 can rate-limit before the Worker runs (ADR-011 accepted risk 4) |
| KV reads or writes | none visible; more misses | wait for 00:00 UTC |
| Durable Object requests or rows written | 503 `capacity_unavailable` for new analyses | wait for 00:00 UTC; `/api/_ops/budget` may fail too |
| Governor daily caps | 503 `daily_capacity_reached` (`budget` `global`, `heavy`, `cu_day`, `egress_day`, `metadata`); 429 `budget: "prefix"` (a network's 60 analyses, 300 admissions or fifth of the day's compute, which its metadata lookups spend too when they wake Neon) or `"metadata"`. Metadata lookups get the same compute and egress denials as analyses | Working as designed; wait for 00:00 UTC. If real visitors often hit `cu_day`, `egress_day` or their network's compute share without a Neon key, give the governor Neon's usage figures: commit the project id as `NEON_PROJECT_ID` in `edge/wrangler.jsonc` and deploy it with `deploy.yml` (a dashboard value does not survive the next deploy: `keep_vars` is not set), then load the project-scoped key with `npx --no-install wrangler secret put NEON_API_KEY` ([owner step 3.6](deployment.md#3-neon-free)). The governor polls only with both, so check that `/api/_ops/budget` reports the mode `ground_truth`, not `conservative`. With fresh usage figures the daily ceiling is 10 CU-h instead of 2.903, and egress is checked against Neon's own month-to-date figure (ADR-015). `metadata` means clients are sending query strings or unknown codes: the SPA never does; check that the deploy set `SNAPSHOT_ID` (the Worker answers plain metadata GETs from the snapshot only then) |
| Governor monthly pacing | 503 `capacity_paused` (`budget` `egress` or `cu_month`) with `resumes_at` | Wait for `resumes_at`. If it happens every month, apply the calibration lever ([deployment.md step 10](deployment.md#10-week-1-calibration)): lower the compute's maximum CU and set `NEON_MAX_CU` in `edge/src/constants.ts` to match |
| Neon compute (100 CU-h) or egress (5 GB) | Before: with `NEON_API_KEY`, the daily quota report's "Neon CU-h, month to date" line above 70% of the governor's pacing allowance, or its egress line above 70% of the pro-rated 5 GB. Without the key nothing automated warns (look at the Neon console). After: 503 `database_paused`; the SPA's paused page | Wait for the next Neon consumption period (Neon console, or the Neon `period_end` in `/api/_ops/budget` when `NEON_API_KEY` is set). Then find out why the governor did not stop first |
| Neon storage (1 GB per project) | owner writes fail (`validate-analytics`); the API is unaffected | Never keep two copies in one project; a release goes into a new project |
| Render outbound (5 GB/month) | Before: nothing automated (no monitor can read it without an account-wide Render API key), and nothing prompts you: the weekly look at the workspace's usage is an item of the [owner checklist](deployment.md#owner-checklist), which you open yourself (the schedule check's run summary holds it, but a passing run sends no email). After: services suspended until the 1st (Render email or dashboard). Render's answer while suspended is undocumented: the Worker answers `service_waking` or `upstream_unavailable`, and the SPA shows the waking notice, then "still not answering … may be paused" — it cannot tell a suspension from a cold start ([security.md R13](security.md#accepted-risks)) | Set the [pause switch](#pausing-the-api) until the 1st (UTC) with reason `render_quota`: visitors then get one page saying the API is paused until then. Wait. Find the cause in Render logs (direct hits show `edge_authenticated: false` and 403) and in `/api/_ops/budget` (`metadata.origin_calls`, `analyses.refunded`; ADR-015 "Render outbound through the Worker"). Never pay; never recreate the service |
| Render pipeline minutes | deploys wait; the running service keeps serving | Wait for the next month. Image deploys build nothing; whether pulls count is undocumented |
| Render instance hours | cannot run out with one service | never add a second Free web service |
| Workers Logs | edge logs stop until 00:00 UTC | none needed |

**Levers that need no code change** (Render env, "Save and deploy"): `ATUS_API_DAILY_ANALYSIS_CAP` (minimum 1), `ATUS_API_RATE_ANALYSIS` and `ATUS_API_MAX_INFLIGHT`. They are per process and reset on every restart. The governor's limits are constants in `edge/src/constants.ts`; changing them is a code change and a deploy. The [pause switch](#pausing-the-api) stops everything that would reach Render at the edge without a code change; it is meant for a known API outage (a Render suspension, maintenance), not for pacing a quota.

### Render suspends the service

This means Render's discretionary suspension for "uncommonly high" traffic that the service initiates (ADR-011 accepted risk 1). It is not the monthly bandwidth pause, which ends on the 1st.

1. **Confirm** in Render → the service, and in Render's email. The deep monitor fails, and analyses answer `service_waking` or `upstream_unavailable`: what a suspended service answers is undocumented. Visitors see the waking notice for 150 s per novel analysis, then "The analysis service is still not answering … may be paused" ([security.md R13](security.md#accepted-risks)).
2. **Set the pause switch** with reason `render_suspended` and `paused_until=unknown` ([Pausing the API](#pausing-the-api)): visitors then get one page saying the API is paused, instead of those waits. Clear it with `resume-api` when the service is back.
3. **Do not:**
   - pay, add a card or upgrade;
   - recreate the service, or create another one in this or a new workspace. Render's AUP forbids attempts to "bypass, circumvent, or defeat" its restrictions (ADR-011 "Must-not-do list").

   Delete nothing without the owner's approval.
4. **What keeps working:** the static SPA, the snapshot and KV-cached results. Neon and the release are unaffected.
5. **Owner decision: fail over to the ADR-011 fallback** (Vercel Hobby; [ADR-011 "Fallback"](adr/ADR-011-hosting.md#fallback-the-runner-up-p3s-api-host-vercel-hobby)). **The fallback is not implemented** ([ADR-011 amendment](adr/ADR-011-hosting.md#fallback-p3-vercel-hobby-a-documented-procedure-not-ready)). The repository has none of its preconditions: no Vercel entry module, no `vercel.json`, no `deploy-vercel` job, no Vercel budgets or in-flight gating in the governor, no origin mode with the protection-bypass header. The Worker reads its origin only from the secret `RENDER_ORIGIN` (`edge/src/upstream.ts`), not `API_ORIGIN` or `GATE_INFLIGHT`. `wrangler secret put RENDER_ORIGIN` can point it at another HTTPS origin, but for Vercel that alone is not enough. Failing over is therefore implementation work first, then ADR-011's switch procedure, which includes the owner accepting Vercel's terms and stopping at any card prompt.
6. Until then, the site serves only the static floor and cached results, and the paused page for anything else.

### Pausing the API

The owner's switch for a known API outage: Render suspended the service until next month (its outbound allowance used up) or at its discretion, or it is down for maintenance. What a suspended Render service answers is undocumented, so without the switch visitors get the cold-start flow, up to 150 s of waking notices per novel analysis ([security.md R13](security.md#accepted-risks)). With it, the Worker answers every request that would reach Render with 503 `api_paused` and the date, without calling Render or the governor, and the SPA shows one page: "The analysis service is paused", why, until when, and what still works. The static site, the snapshot (starters, presets, metadata, prewarmed analyses), KV-cached results, `/api/_edge/health` and `/api/_ops/budget` keep working.

```bash
# Render's outbound allowance is used up: paused until the 1st of next month (UTC midnight)
gh workflow run deploy.yml --ref main -f operation=pause-api -f paused_until=2026-12-01 -f pause_reason=render_quota
# A discretionary suspension, no end known
gh workflow run deploy.yml --ref main -f operation=pause-api -f paused_until=unknown -f pause_reason=render_suspended
# Clear it
gh workflow run deploy.yml --ref main -f operation=resume-api
```

Then approve the `production` environment. Only job `api-switch` runs: it checks the inputs (`scripts/ops/api_pause.py secrets`), changes the optional Worker secrets `API_PAUSED_UNTIL` and `API_PAUSED_REASON` with `wrangler secret bulk` (one new Worker version with the same code, site and snapshot; nothing is built and Render is not called, so it works while Render is suspended), and waits until `/api/_edge/health` reports the new `api_paused` state (`api_pause.py verify`, at most 6 requests).

- **It never waits behind a Deploy run.** `pause-api` and `resume-api` have their own concurrency group (`deploy-production-switch`; review round 3), so a Deploy run that is in progress or waiting for approval (a data release leaves one waiting while Render is switched) neither delays nor cancels them. GitHub keeps one pending run per group: if you dispatch the switch several times while one of its runs is still going (waiting for approval counts), only the latest runs after it, so the last values you dispatched win; reject a waiting run whose values you have since corrected. A deploy running at the same time keeps the switch (next item), but its post-deploy smoke then fails at `api_paused`; deploy again after `resume-api`.

- **`paused_until`** is a UTC date (`YYYY-MM-DD`, meaning midnight) or time (`YYYY-MM-DDTHH:MMZ`), or `unknown`. From that time on the Worker forwards requests again by itself, with no new version: give the time Render's allowance resets. With `unknown`, the switch stays on until `resume-api`.
- **`pause_reason`** picks the wording: `render_quota` (the host used up this month's allowance; new analyses resume when it resets), `render_suspended` (the host suspended the service), `maintenance`.
- **Deploys keep it.** `wrangler deploy` keeps every secret, the switch included. A deploy's post-deploy smoke goes through the Worker, so while the switch is on it fails at once (`[FAIL] api_paused`, with `api_health`, the starters and the origin lock not checked, so it sends Render nothing; `scripts/ops/deploy_smoke.py`). Clear the switch (`resume-api`) before deploying once Render is back.
- **Monitors** report it as `WARN`, not as a failure: `edge-check.yml` from `/api/_edge/health`, and `deep-check.yml` stops after one `api_paused` answer instead of waiting out its budget.
- **Edge rollbacks** bring back the switch of the version rolled back to ([deployment.md "Rollback"](deployment.md#rollback), step 4). After a rollback, `wrangler secret bulk` refuses to change secrets until the latest version is deployed again.

### A provider changes its free tier

1. **Notice periods.** Neon's terms treat continued use as consent and allow termination on 30 days' notice. GHCR promises at least a month's notice. Render may suspend "for any reason". Workers Logs changes on 2026-12-01 (already in the budget). Decide before a change takes effect.
2. **Compare the terms.** Re-read the cited pages ([ADR-011 "Cited free-tier terms"](adr/ADR-011-hosting.md#cited-free-tier-terms-all-accessed-2026-10-20); each has a URL and an access date) and write down what changed.
3. **Recompute the budget:** the [deployment.md budget tables](deployment.md#free-tier-budget) (limit, projected use, headroom, on exhaustion) and the Neon CU-hour scenarios. Then the values that encode a limit:
   - the governor constants in `edge/src/constants.ts` (`NEON_MONTHLY_CU_HOURS`, `DAILY_CU_HOURS_*`, `PACING_*`, `EGRESS_STOP_BYTES`, `GLOBAL_DAILY_ANALYSES`, `HEAVY_DAILY_ANALYSES`, `PREFIX_DAILY_*`, `NEON_MAX_CU`);
   - the API defaults (ADR-012);
   - the restore's `--size-limit-bytes`;
   - the quota report's 70% threshold (`scripts/ops/monitors.py`);
   - the Workers static-asset limits checked in `deploy.yml`.
4. **If $0 without a card still holds:** change the constants, the budget table and ADR-011's citation (new date), then deploy.
5. **If it no longer holds** (a card required, a quota too small, a limit that would bill): stop and report to the owner. The options are in ADR-011 "Rejected alternatives" and "Fallback". A new hosting choice is a new ADR that supersedes ADR-011.

### The deep check cannot read the live tag

`deep-check.yml` compares the build `/health` reports with the commit of the GHCR digest tagged `live`, read with the run's own `GITHUB_TOKEN` (`packages: read`). Its step "Which build should run" fails the check when that lookup cannot work, because a check that silently stopped comparing would never warn that Render runs another image:

- **HTTP 401 or 403.** The token may not read the package's versions. GitHub does not document that a repository's `GITHUB_TOKEN` can list a user-owned package's versions ([ADR-011](adr/ADR-011-hosting.md), "Not exercised against GHCR"), so the first deep check after owner step 4.1 is the test. In GitHub → your profile → Packages → `atus-api` → Package settings → **Manage Actions access**, add `Nathan98000/ATUS` with the Read role, then `gh workflow run deep-check.yml`. `ghcr-cleanup.yml` lists the same versions, so it fails the same way.
- **No version is tagged `live`** (or the package is missing, or the `live` digest has no `deployed-<commit>` tag, which the check reads the same way). The monitors start only after the first deploy's `promote` job tagged one, so this means the tag was deleted, `promote` never passed, or `live` was moved by hand without `deployed-<commit>`: re-run `promote` of the last good Deploy run, or move the tags by hand as in [Rollback](#rollback).
- **No reply, 429 or 5xx.** GitHub is having a moment: the step passes with an empty build, the health check runs without it and the run shows the warning "Build not compared". Nothing to do unless it repeats.

To read the tag yourself: `GH_TOKEN="$(gh auth token)" python scripts/ops/github_ops.py live-build --owner nathan98000 --package atus-api --require-live`.

### Scheduled workflows were disabled

GitHub disables scheduled workflows after 60 days without repository activity, and it can delay or drop scheduled runs. `schedules-check.yml` (weekly) fails when a schedule is not active, when a monitor's last scheduled run is far older than its cadence, or when `main` has had no commit for 28 days (pushes to Dependabot's branches do not count: GitHub does not say whether they are activity). When it fails, or monitor runs stop:

```bash
gh workflow list --all                      # which workflows are disabled
gh workflow enable edge-check.yml           # repeat for each disabled one: deep-check.yml,
                                            # quota-report.yml, ghcr-cleanup.yml, schedules-check.yml
gh workflow run schedules-check.yml         # confirm, or locally:
GH_TOKEN="$(gh auth token)" python scripts/ops/github_ops.py schedules --repo Nathan98000/ATUS
```

If the cause was inactivity, push a commit to `main`. Failure emails for a scheduled workflow go to the user who last changed its `cron` line ([owner step 4.4](deployment.md#4-github-repository-nathan98000atus-stays-public)).
