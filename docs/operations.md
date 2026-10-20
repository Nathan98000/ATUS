# Operations

This document covers how the ATUS platform is run in production on the $0 stack: what was found before deploying, the statistical equivalence criterion between the full database and the deployed serving database, and the operational runbooks.

At this stage it holds only the Phase 5 audit. The audit describes the system as it was at commit 387fc2d, before any Phase 5 change. The equivalence criterion and the runbooks will be written during Phase 5 implementation.

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

## Equivalence criterion (§9)

To be completed in Phase 5 implementation. The audit input is F40.

## Runbooks

To be completed in Phase 5 implementation.
