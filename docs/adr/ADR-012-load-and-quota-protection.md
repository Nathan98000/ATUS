# ADR-012: Load and quota protection in the API

**Status:** Accepted (2026-10-20) · Phase 5 · API layer implemented and tested; values calibrated on the emulation profile on 2026-10-21 (see "Calibration (§13)"); confirmation on Neon pending (§16 replay)

**Related:** [ADR-011](ADR-011-hosting.md) (hosting, request flow, process model), [ADR-015](ADR-015-edge-cache-snapshot-and-governor.md) (edge cache, static snapshot and the quota governor), [ADR-014](ADR-014-observability.md) (observability). Audit findings are in [`../operations.md`](../operations.md).

## Context

On the $0 stack, abuse does not cost money. It costs the month's availability: Neon's 100 CU-hours and 5 GB egress, Render's 5 GB outbound, Cloudflare's 100k requests per day (ADR-011). The audit measured what an unprotected API does with that budget:

- nothing bounded one request's database time, and a disconnected client's query ran to completion (F01, F52);
- about 50 concurrent requests stalled the whole API in a threadpool and pool convoy, and every failure looked like "database unavailable" (F02, F34, F35);
- a 100 MB body raised memory from 54 to ~500 MB, and a 930 KB body produced a 9.7 MB 422 (F03, F04);
- there was no rate limit, no client identity and no trusted proxy chain; the provider hostname bypasses anything that lives only at the edge (F07);
- after the database closed its connections, each pooled connection produced one 503 (F05);
- every cache hit and every 422 ran SQL before answering, so even free answers could wake a sleeping database (F14);
- the cache key is trivially bypassed by respelling a request (F15).

ADR-011 makes the Worker's quota governor the **primary** defense (durable daily and monthly budgets, fail closed; finalized in ADR-015). This ADR is the **second, independent layer inside the API**. It exists because the onrender.com hostname cannot be disabled, because a Worker misconfiguration must not expose the database, and because a single 0.1-CPU process must stay responsive no matter what reaches it.

## Decision

### Order of checks

Every request passes these stages in order. Each rejection happens before routing, so it costs no dependency, no pool connection and no SQL statement.

| # | Stage | Rejection |
|---|---|---|
| 1 | declared `Content-Length` > `ATUS_API_MAX_BODY_BYTES` (64 KiB) | 413 `payload_too_large` (`Connection: close`) |
| 2 | edge authentication (every path except `/health`; `/ready` and `/metrics` also pass with a valid ops bearer) | 403 `forbidden_origin` |
| 3 | client identity, then the per-client **request** bucket (every path except `/health`) | 429 `rate_limited` |
| 4 | a `POST` (or a body on another method) that is not `application/json` | 415 `unsupported_media_type` |
| 5 | the body is read with the same cap, also for chunked bodies | 413 `payload_too_large` |
| 6 | control characters in query parameter names or values | 422 `validation_error` |
| 7 | schema and domain validation of the body | 400 / 422 |
| 8 | data routes: the in-process data snapshot (read only when older than its TTL) and the expected data version | 503 `data_version_mismatch` |
| 9 | analyses only: cache lookup, single-flight join | — |
| 10 | analyses that will compute: the engine's dry run up to its first statement | 422 (`unknown_activity`, `invalid_spec`, `unsupported_analysis`, ...) |
| 11 | analyses that will compute: daily cap, per-client **analysis** bucket | 503 `daily_capacity_reached`, 429 `rate_limited` |
| 12 | admission lane (bounded wait), then a pool connection | 503 `overloaded` |
| 13 | the query, under per-transaction limits and a request deadline | 503 `analysis_timeout`, `overloaded`, `database_paused`, `database_unavailable` |

The code is `api/middleware.py` (1–6), `api/snapshot.py` (8), `api/analysis_runner.py` (9–12) and `api/database.py` (12–13). Everything up to stage 11 costs no SQL statement while the snapshot is fresh.

### Edge authentication and client identity

- **Shared secret.** `X-ATUS-Edge-Auth` must equal one of `ATUS_EDGE_SECRETS` (at most two values: current and next, for rotation). Every accepted value is compared with `hmac.compare_digest`, so timing reveals neither the secret nor which slot matched. Production refuses to start without a secret, and production secrets must be at least 32 characters.
- **Disabled in development** when `ATUS_EDGE_SECRETS` is empty.
- **`/health` is exempt.** The platform's health check reaches the container directly, and liveness touches nothing.
- **Operator endpoints.** `/ready` and `/metrics` pass with a valid `Authorization: Bearer <ATUS_OPS_TOKEN>` alone, because the Worker never routes them. Without one they are refused like any other path (ADR-014).
- **Client identity.** The client is `X-ATUS-Client-IP` only when edge authentication succeeded; without edge authentication it is the socket peer. An authenticated request whose header is missing or malformed (the Worker omits it when Cloudflare gave it no usable address) is charged to one shared bucket, `edge:unknown`, as the governor charges such requests to its one `unknown` prefix, and logs no address. It is never the socket peer: behind the edge that is Render's proxy, whose address would be logged as the client's and whose nodes would split or merge unrelated visitors.
  - `X-Forwarded-For`, `Forwarded`, `CF-Connecting-IP` and `True-Client-IP` are never read. uvicorn's proxy-header handling is off and no forwarding peer is trusted.
  - IPv4 addresses are keyed whole. IPv6 addresses are keyed by their /64, the smallest prefix one subscriber normally controls.
  - In development only (edge auth disabled, which production refuses), loopback peers and in-process test clients are not per-client limited. This keeps local tools, the E2E backend and the HTTP benchmark replays usable. The daily cap and the lanes still apply.
- **`X-Request-ID`** from the edge is kept only when authenticated and matching `^[A-Za-z0-9._-]{8,64}$`. Otherwise the API generates one.

### Rate limits (per client, token buckets)

| Bucket | Variable | Default | Charged for |
|---|---|---|---|
| requests | `ATUS_API_RATE_REQUESTS` | `120/60` (burst 120) | every request except `/health` |
| analyses | `ATUS_API_RATE_ANALYSIS` | `6/60` (burst 6) | only an analysis that will actually compute: not cache hits, not single-flight joiners, not requests rejected earlier |

- A rejection is 429 `rate_limited` with `Retry-After` (seconds until a token is available, at least 1), `details.retry_after_seconds` and `details.scope: "client"`.
- An analysis token is refunded when the request is then shed (`overloaded`, `daily_capacity_reached`) before computing.
- **Memory is bounded.** Each bucket set is an LRU of at most 10,000 clients (about 2 MB). Evicting a bucket forgets its debt, so a client whose bucket was pushed out by 10,000 newer clients starts again with a full bucket. That is the price of a fixed memory bound, and one reason the edge governor stays primary.

### In-process data snapshot: answers that cost nothing (F14)

The API keeps what it needs to know about the served data in memory (`api/snapshot.py`): the data version, the latest ingestion run, the loaded years, the respondent count, the activity lexicon, and an `ActivityResolver` built from it.

- **One consistent read.** All of it is read in one `REPEATABLE READ`, read-only metadata transaction (11 statements) when a request needs it. Concurrent requests share one read. There is no background refresh, so an idle API never wakes the database.
- **Metadata reads and the quota (wave 2).** Each read can wake a suspended Neon compute, so how often it happens is a quota question, not only a freshness one:
  - **Pinned.** With `ATUS_EXPECTED_DATA_VERSION` set (production), a snapshot of exactly that version never expires. In production the data never changes in place (a release switch points the API at a new database, which redeploys it), so once a process has read its snapshot, no metadata request, cache hit or 4xx, however many arrive, reads the database again. Only an analysis that sees the version change (reload guard) forces a new read. `atus_snapshot_pinned` reports it.
  - **Otherwise** (development, or production without the expected version, which the API logs as a warning at start-up) the snapshot is read again once it is older than `ATUS_API_SNAPSHOT_TTL_S` (600 s in production, 2 s in development).
  - **Stale if error.** If a read fails while an earlier snapshot exists that was never invalidated, requests are answered from that snapshot instead of failing, and the database is tried again only after `min(30 s, TTL)`. Cache hits, metadata and 4xx keep working through a database outage or a monthly pause; uncached analyses still need the database and fail with their usual 503. `atus_snapshot_stale_serves_total` counts these answers. `/ready` is strict: it uses neither a pinned nor a stale snapshot, so it reports the database as it is now.
- **What it makes free.** While it is usable, analysis cache hits, every 400/413/415/422, `/meta`, `/activities*` (search included) and `/population/metadata` run no statement. Analyses that compute reuse its resolver and years (an additive, optional argument of the engine's constructor), so a computation runs only its statistics queries and the version recheck.
- **The lexicon search** runs in Python with results identical to the `ILIKE` it replaced, for every term: names are stored as the database lowered them, and a term is lowered with the database's own `lower()` of every character of every Unicode block that has case mappings, a table read with the snapshot (one statement). Every other code point lowers to itself: a unit test proves it for Python's Unicode data, an integration test for the database's collation (every valid code point). The table equals the database's `lower()` of a whole term only when the database lowers one character at a time, as libc collations (Neon's, the stack's) and PostgreSQL's builtin provider do; a probe with a final capital sigma checks this at every read, and a database that lowers by context (ICU) sends search terms to the original SQL. An integration test compares both paths on the real 0325 lexicon for thousands of terms, more than 200 of them non-ASCII (`décor`, `DÉCOR`, the dotted capital I, the Kelvin sign, ...).
- **Dry run.** Before a computing request is charged, the real engine runs it on the snapshot's resolver and years with a stand-in connection that stops it at its first database use. An analytics error raised before that point (unknown activity code, year not loaded, 2020 under the multi-year weights) is exactly the error the computation would raise first, so it is answered as the usual 422 without a token, a lane or a statement. Nothing else changes; the dry run never produces a result.
- **Reload guard.** An uncached analysis still re-reads the data version inside its own transaction after computing. If it differs from the snapshot's, the snapshot is invalidated and the result is returned once, uncached (`X-Cache: bypass`). A snapshot read that was in progress during the invalidation is handed to its waiters but not kept as fresh.
- **Deployment binding.** With `ATUS_EXPECTED_DATA_VERSION` set (production), a snapshot of another version makes every data route answer 503 `data_version_mismatch` (`Retry-After: 60`, `scope: "global"`), and a version change detected by the recheck refuses the result the same way instead of returning it. `/population/metadata` and `/activities/presets`, which do not depend on the data, keep working. This is ADR-011's release-switch guard: an API pointed at the wrong database serves nothing from it.

The per-request statement counts, verified against PostgreSQL's own statement log, are in [`../api.md`](../api.md#statements-per-request).

### Daily cap (process-wide backstop)

- At most `ATUS_API_DAILY_ANALYSIS_CAP` (600) uncached analyses are admitted per UTC day in this process. This mirrors ADR-011's global governor cap.
- Over the cap: 503 `daily_capacity_reached`, `details.scope: "daily"`, and `Retry-After` set to the seconds until 00:00 UTC. Cached results stay available.
- The count lives in memory, so a restart resets it. The governor's count persists.

### Admission lanes

- At most `ATUS_API_MAX_INFLIGHT` (2) uncached analyses compute at once, and at most `ATUS_API_MAX_INFLIGHT_HEAVY` (1) of them heavy. A heavy analysis takes the heavy lane first, then a general one, so a light analysis never holds the heavy lane and the two cannot deadlock.
- A request waits at most `ATUS_API_ADMISSION_WAIT_S` (5 s) in total, then gets 503 `overloaded` (`Retry-After: 10`, `details.scope: "global"`).
- **The lane is taken before a pool connection is checked out.** The pool (4 connections) is therefore never exhausted by analyses, and metadata reads and cache lookups always find a connection. A test proves that a shed analysis never asks the pool for a compute connection.
- Cache hits and single-flight joiners bypass the lanes.

**Heavy classifier** (`api/protection.py:is_heavy`). An analysis's database cost is dominated by its statistics queries, which scale with the number of survey years scanned. On the emulation of Neon's 0.25 CU floor (2026-10-21, "Calibration (§13)"), one analysis at a time: a single-year estimate with replicate SEs 0.99 s at p50 (max 4.60 s), the demo mix's pooled 8–22-year estimates 5.79–8.39 s, and the worst cases' pooled 22 years and 23-year trends 21.35–21.81 s at p50 (W1–W3). The audit had measured ~7 s, ~52 s and ~47–49 s on an earlier build (operations.md, timed matrix (a)). A compare runs one statistics query per group. The rule is:

```text
year_queries = len(years) × (2 if compare else 1)
heavy        = year_queries ≥ 8
```

It is a pure function of the validated spec, so the same request is always in the same lane. It ignores `variance`, because point-only requests were not measured separately and are therefore treated as conservatively as replicate ones. Broad activity selectors (F66) are not distinguished; the timeout and the lanes bound them (at 0.25 CU they take 33–35 s at p50 for one group, and 69.2 s at p50, 71.0 s at most, for a compare by sex).

### Single-flight and disconnects

- Concurrent requests with the same cache key share one computation. The first computes (`X-Cache: miss`); the others wait for it (`X-Cache: join`) and receive the same bytes or the same error. Errors are never cached.
- The computation runs as a task detached from the request that started it. If that client disconnects, an admitted computation still completes (bounded by the timeout and deadline below) and is cached, so a retry after a proxy timeout hits the cache or joins instead of paying twice (ADR-011).
- The disconnect is detected and logged ("client disconnected before the response", plus `client_disconnected=1` on the access line).

### Bounded work per request

Every request's database work runs in one transaction, started as `BEGIN READ ONLY` (the API never writes, I05; a write fails even under a role that could write). Its first statement applies the limits with `set_config(name, value, true)`, the function form of `SET LOCAL`. The values end with the transaction, so they are safe behind a transaction-mode pooler. Session `SET` and startup `options` are never used.

| Parameter | Value | Source |
|---|---|---|
| `statement_timeout` | 85,000 ms for analyses, 10,000 ms for metadata | `ATUS_API_STATEMENT_TIMEOUT_MS`, `ATUS_API_META_STATEMENT_TIMEOUT_MS` |
| `lock_timeout` | 5 s | constant (ADR-011 role default) |
| `idle_in_transaction_session_timeout` | 30 s | constant (ADR-011 role default) |
| `client_connection_check_interval` | 5 s | constant: a killed API process does not leave its query running (F52) |
| `jit` | `off` | `ATUS_API_JIT` (Neon has no LLVM; F37) |
| `max_parallel_workers_per_gather` | server default: unset in production ("Calibration (§13)") | `ATUS_API_MAX_PARALLEL_WORKERS` (§9-gated; F38) |

Further bounds:

- **Request deadline.** An analysis gets a wall-clock deadline of `statement_timeout + 5 s` (90 s by default, ADR-011's API deadline). When it passes, the running query is cancelled with `cancel_safe()`. This bounds a compare's two statistics queries together, not only each statement. The timer can never cancel a connection after it went back to the pool.
- **Timeouts are never cached.** A cancelled query (statement timeout or deadline) becomes 503 `analysis_timeout` with `Retry-After: 60`.
- **Early release.** The transaction ends and its connection returns to the pool before the response is sent (I10).

### Pool

- `min_size=0`: the API opens no connection at startup, so a Render spin-up never wakes Neon (I08).
- `max_size` 4, `max_idle` 240 s, both from the environment (F17). psycopg_pool shrinks by one idle connection per `max_idle` period.
- libpq `connect_timeout` (10 s) and TCP keepalives (30 s idle, 10 s interval, 3 probes; F36). `application_name=atus-api`.
- `prepare_threshold` comes from `ATUS_API_PREPARE_THRESHOLD`: psycopg's default 5, or `none` behind a transaction-mode pooler (F18).
- **Stale connections** (F05). Every checkout runs `ConnectionPool.check_connection`. When a connection fails the check, the other idle connections died the same way (a restart or a suspend), so the pool is drained and the request gets a fresh connection. psycopg_pool 3.3 waits about 1 s before the next attempt after a failed check. A test terminates every API backend and then expects only 200s.
- **Checkout failures are classified, not lumped together** (F34):
  - every connection busy → wait `ATUS_API_POOL_TIMEOUT_S` (5 s) → 503 `overloaded`;
  - a new connection is needed → wait up to pool timeout + connect timeout, because a waking database is slow to accept. But fail as soon as an attempt made during this wait has failed (an older failure says nothing about a database that may have recovered and is only slow to wake): Neon's quota refusals become 503 `database_paused` (`Retry-After: 3600`, `scope: "monthly"`), anything else becomes 503 `database_unavailable` (`Retry-After: 5`).
  - A failed background connection attempt is retried for at most `connect_timeout + pool_timeout`. After that, the next request starts a fresh attempt instead of waiting out a long backoff.
- The pool's `requests_waiting` gauge counts waiters that already timed out (I11), so nothing here or in alerting relies on it alone.

### Error mapping

| Condition | Response |
|---|---|
| `QueryCanceled` on an analysis (statement timeout, deadline, shutdown) | 503 `analysis_timeout`, `Retry-After: 60` |
| `QueryCanceled` on a metadata read | 503 `overloaded` |
| `LockNotAvailable` (55P03), `DeadlockDetected` (40P01), SQLSTATE class 53 (e.g. `DiskFull` from a 64 MiB `/dev/shm`, F33; too many connections) | 503 `overloaded`, `Retry-After: 10` |
| text matching Neon's quota or disabled-endpoint refusals | 503 `database_paused`, `Retry-After: 3600` |
| any other `OperationalError` or `InterfaceError` (lost connection, admin shutdown, refused) | 503 `database_unavailable`, `Retry-After: 5` |
| migrated but empty database (no succeeded run, or no respondents) | 503 `data_not_loaded`, generic message, `Retry-After: 300` (F51) |
| the database serves another data version than `ATUS_EXPECTED_DATA_VERSION` | 503 `data_version_mismatch`, `Retry-After: 60` |
| any other driver error | 500 `internal_error`, traceback logged once (F50) |

**Neon's refusal texts.** Neon does not document them. The matched markers come from its proxy's user-facing messages ("endpoint is temporarily unavailable. Check your quotas and/or contact our support.", "endpoint is disabled"; `neondatabase/neon`, `proxy/src/control_plane/messages.rs`) and from user reports of the control plane's text ("Your account or project has exceeded the compute time quota", "Your project has exceeded the data transfer quota"). Matching is deliberately narrow; an unmatched refusal is still a 503, as `database_unavailable`.

All of these are additive v1 codes in the unchanged envelope. Temporary conditions carry `Retry-After` and `details.retry_after_seconds`, plus `details.scope` where a limit applies to a client (`client`), the service (`global`), the day (`daily`) or the month (`monthly`). Every error response is `Cache-Control: no-store`.

### Request size and shape

- **Body cap** (F03): 64 KiB, enforced in pure ASGI on the declared length and again while reading, for chunked bodies too.
- **Capped 422s** (F04): at most 20 errors per response (`details.truncated` and `details.error_count` when more). Each location part is capped at 64 characters and each message at 200. Request values are never echoed. A body with 500 unknown keys (one key 5,000 characters long) now gets a 422 under 8 KB (asserted by a test).
- `preset` has `max_length` 64.
- **Responses** of 1 KB or more are gzipped for clients that accept it.

### Process

These settings live in `api/serve.py`, which `atus api` runs.

- **One uvicorn process.** The protection state, cache and single-flight map are per process.
- **Listening.** On `$PORT`, on 0.0.0.0 in production and 127.0.0.1 in development.
- **Protocol.** Proxy headers off, `forwarded_allow_ips` empty, no `server` header, websockets off, h11 with a 16 KiB request-head limit.
- **Threads.** The anyio thread limiter is set to `ATUS_API_THREADS` (16) at startup. Routes are async and do their database work in exactly one worker-thread hop per transaction, so DB-free routes never wait for a thread (F02).
- **Connections.** Keep-alive 5 s. `limit_concurrency` is 4 × threads (64): beyond it uvicorn answers 503 before any app code runs (F49).
- **Shutdown.** `timeout_graceful_shutdown` is 20 s, below Render's 30 s grace. On shutdown, queries still running are cancelled and detached computations get up to 5 s before the pool closes (F52).

### Relation to the edge governor (ADR-015)

The two layers count different things and fail differently:

| | Edge governor (ADR-015) | This layer |
|---|---|---|
| Lives in | one Durable Object; survives Render restarts and spin-downs | API memory; reset on every restart |
| Counts | uncached analyses per client network (60 per day), globally (600), heavy (300), Neon compute and egress from ground truth | per-client requests and computing analyses per minute, 600 computing analyses per UTC day, concurrent computations |
| Client key | HMAC of the /24 or /48 prefix, daily key | the address from `X-ATUS-Client-IP` (IPv6 by /64) |
| Sees | requests that reach the Worker | every request, including direct hits on onrender.com |
| On failure | fails closed for uncached analyses (`capacity_unavailable`) | protects one process regardless of the edge |

- The governor uses the same heavy rule (`len(years) × (2 for a compare) ≥ 8`), so both layers agree on what is expensive.
- It sizes its compute charge from `Server-Timing: db;dur=` (ADR-014). Requests this layer answers without the database (hits, 4xx, sheds) report `db;dur=0` and are not charged compute.
- The daily caps are the same number on purpose: the governor's is durable, this one is the backstop when the Worker is misconfigured or bypassed.

## What this does not protect against

- **Distributed abuse.** Per-client buckets do not stop many IPs. The daily cap and the edge governor bound the total. A flood from many IPs can still use up the day's analysis capacity and push legitimate users to cached and static content.
- **Bucket eviction.** An attacker cycling through more than 10,000 addresses evicts other clients' buckets, which then start full again.
- **Direct-origin bandwidth.** A direct request to onrender.com still costs a minimal 403 of Render outbound bandwidth (ADR-011, accepted risk 8).
- **Idle and slow connections.** uvicorn has no request-head read timeout. A client that opens connections and sends nothing holds them until a proxy in front drops them. `limit_concurrency` bounds the memory this can pin, but a client holding 64 idle connections to the container would make uvicorn shed everyone else with 503. In production every connection reaches the container through Render's proxy, which is expected to buffer or terminate client connections. This is unverified (ADR-011 unknowns).
- **Restarts.** All in-process state (buckets, daily count, cache) resets on restart or spin-down. The governor at the edge is the durable layer.
- **Cache-key spelling** (F15). Respelling a request (label, `confidence_level`, prefix versus leaf codes) still misses the cache. It now costs an analysis token and a lane, so the bypass is bounded. Keying the cache on the resolved computation needs result assembly to change, which belongs to the analytics layer and is deferred.
- **A stale snapshot** answers for up to its TTL (600 s) after data changed in place, or indefinitely while pinned. In production the data never changes in place: a release switch points the API at a new database and restarts it (ADR-013). Uncached analyses detect a change themselves (reload guard), which unpins the snapshot.
- **Snapshot reads after a cold start.** Each new process reads its snapshot once, on its first data request, also when that is only a cache hit or a metadata request, and with the database suspended it wakes it. Render spins the API down after 15 idle minutes, so a client sending one metadata request every ~16 minutes could cause about 4 such reads an hour. PROJECTION: at Neon's 0.25 CU minimum and its 5-minute suspend tail, about 0.02 CU-h each, so at most about 2 CU-h a day if sustained all day. The governor does not count these; Neon's ground truth (ADR-015) and the daily quota report would show them. The SPA itself never causes them: it reads metadata from the static snapshot. Serving the metadata routes from the static snapshot at the edge would remove this path; it is not implemented.
- **Dry-run reach.** It covers what the engine checks before its first query. A compare whose second group alone is invalid is still found invalid by the computation, after the first group's query (the dry run must not change which error a request gets).
- **Search on a context-lowering database.** Only when the database lowers text by context (an ICU collation, which neither Neon's default nor the stack uses) do search terms run the lexicon SQL (3 statements), bounded by the request bucket only. A cased script added in a Unicode version newer than the code-point table would be missed by the table, but it lowers into its own new letters, which no lexicon name contains, so no result could change.

## Calibration (§13)

**Source.** [`../../loadtests/results/2026-10-21-emulation.md`](../../loadtests/results/2026-10-21-emulation.md) (the "record" below), regenerated by `loadtests/report.py` from the exports in `loadtests/results/raw/`.

- The runs were local, on 2026-10-21 between 03:53 and 07:14 UTC, each one alone on the machine.
- They used the production-mode stack with the real serving release artifact (0325, `a7420a5c`) restored into it, the API at Render Free's 0.1 CPU / 512 MB, and the database at Neon Free's 0.25 CU floor and at its 2 CU ceiling.
- The API ran production's settings, except for the daily cap and the per-run overrides the record lists.
- Nothing ran on Neon, Render or Cloudflare.

**The rule** (ADR-011 "Bounded work"): `statement_timeout = clamp(3 × p99 of the worst cases including the F66 broad selectors, 30 s, 85 s)`. Every other value is chosen from the throughput, shed and latency measurements below.

### Decisions (2026-10-21)

Every value stays at its code default, so nothing needs to be set on Render. Because no value changed, no confirmation run was needed: the configuration that was measured is the production configuration. No ADR-011 decision changes either. Where ADR-011 quotes the audit's 0.25 CU figures for an earlier build (47–52 s), these measurements supersede them.

| Setting | Decision | Evidence (record section) |
|---|---|---|
| `ATUS_API_STATEMENT_TIMEOUT_MS`, and the `atus_api` role default in `scripts/ops/roles.sql` | **85000 (85 s), unchanged.** The request deadline stays 90 s. | Worst cases W1–W6, 30 requests one at a time (§8). **0.25 CU:** p99 70.52 s, 3 × p99 = 211.6 s, clamped to **85 s**. **2 CU:** p99 3.03 s, 3 × p99 = 9.1 s, clamped to 30 s. The compute autoscales up from 0.25 CU, and a request can arrive while it is still there, so the floor governs. |
| `ATUS_API_MAX_INFLIGHT` | **2, unchanged** | **2 CU:** successful analyses per second peak at 2 in flight: 24.19/s, against 15.78/s at 1. With the cap opened to 8, 4 and 8 in flight fell to 18.26/s and 11.27/s (§5.4, §5.5). **0.25 CU:** 1 in flight gives more single-year throughput (1.14/s against 0.70/s at 2). But on the same API-view mix visits, one lane shed 20 of 405 requests, 10 of them light, while two lanes shed 11, all heavy (§5.6). |
| `ATUS_API_MAX_INFLIGHT_HEAVY` | **1, unchanged** | **0.25 CU:** two heavy analyses at once took 2.6–3.6 × their solo p50 each: W1–W3 60.40–78.76 s, and W4/W5 87.85–106.24 s, past 85 s (§5.5). **2 CU:** a second heavy lane would cost little (2.05–3.24 s each). The API cannot see the current compute size, so the floor decides. |
| `ATUS_API_ADMISSION_WAIT_S` | **5, unchanged** | **0.25 CU:** a light analysis alone takes 0.99 s at p50 and 3.19 s at p95, so 5 s covers a queue of one behind a light request. With two lanes, no light analysis was shed in either mix: all 11 sheds in the API mix were heavy requests waiting for the heavy lane (§5.6). A heavy analysis holds that lane for 5.33–71.02 s, so admitting a request queued behind one would need a wait of that order. Shed requests were answered after 5.01 s at p50 (max 5.58 s), with `Retry-After: 10`, and the SPA retries an `overloaded` answer once (§5.2, §5.4). **2 CU:** a queued request waited under 1 s for a lane (time outside the database at 16 clients: p99 0.84 s, max 0.95 s), and nothing was shed. |
| `ATUS_API_MAX_PARALLEL_WORKERS` | **unset (server default), unchanged** | **0.25 CU:** `0` is faster. Single-year p50 goes from 0.99 to 0.70 s, W6 p50 from 69.21 to 38.58 s, and worst-case p99 from 70.52 to 40.54 s. **2 CU:** `0` is slower. Single-year p95 goes from 142 to 209 ms, worst-case p99 from 3.03 to 3.53 s, and W6 p50 from 2.99 to 3.44 s (§5.2, §5.3). ADR-011 adopts `0` only if it also wins at 2 CU, and the §9 proof for it has not been run. |
| Heavy classifier (`HEAVY_YEAR_QUERIES`) | **8, unchanged** | No measured request was misclassified (next section). |
| `ATUS_API_POOL_MAX_SIZE` | **4, unchanged** | Analyses hold at most 2 connections, because the lanes come first. No run had a pool timeout or a pool error (§5.4, §9). |
| Edge governor (ADR-015): 300 heavy and 600 uncached analyses per UTC day | **unchanged** | These caps budget Neon egress and CU-hours. The runs measured latency, CPU and memory, not egress or CU-hours per analysis, so they give no reason to change them. |

### Heavy classifier check

`report.py --lanes` sorts every analysis that computed (main phase, 200, `X-Cache: miss`) by the lane the API's rule gives it, and reports its `Server-Timing` database time, which excludes the wait for a lane (record §13).

**The rule separated light from heavy in every run.**

- **0.25 CU, one analysis at a time** (uncached at 1 client, the worst cases, the API mix with one lane): light requests used at most 4.60 s of database time (single-year estimates; compares 2.63 s). Heavy ones used at least 5.79 s (an 8–22-year pooled estimate).
- **0.25 CU, two lanes busy** (API mix): light at most 5.18 s, heavy at least 5.33 s.
- **2 CU:** light at most 0.23 s, heavy at least 0.54 s.
- **Medians at 0.25 CU:**
  - light: 0.91–1.37 s for estimates and 1.35–3.02 s for compares;
  - heavy: 6.48–29.91 s for pooled estimates, 11.14–23.61 s for trends, and 69.20 s for the F66 compare.

**The margin at the boundary is narrow at 0.25 CU.** The costliest single-year spec (4.60 s) costs nearly as much as the cheapest 8-year pooled estimate (5.33–5.79 s).

**Not measured.** Requests of 2–7 year queries: pooled ranges of 2–7 years, compares over 2–3 years, and trends shorter than 8 years. The harness sends none, so the threshold's immediate neighbourhood is untested. With no misclassification measured, the threshold stays at 8, and the governor keeps the same rule.

### At the 0.25 CU floor, said plainly

- **Headroom.** 85 s is only 1.2 × the F66 compare's slowest request (71.02 s). It is 2.0 × the slowest single-group F66 request (42.02 s) and 3.3 × the slowest non-F66 worst case (25.80 s). The target of 3 × p99 cannot be reached at the floor without more CPU or parallel workers off.
- **Contention.** The heavy lane prevents two heavy analyses from running together; with two at once, the F66 requests took 88–106 s. A light analysis beside a heavy one still shares the 0.25 vCPU, and no run measured one beside an F66 compare. In the API mix, on the same visit plan, 23-year trends used up to 28.54 s of database time with two lanes against 21.61 s with one. PROJECTION: a slowdown of that size would take the F66 compare past 85 s.
- **What the user gets.** Such a request ends as `503 analysis_timeout` with `Retry-After: 60`, never as a wrong or cached answer.
- **At 2 CU** the same requests take about 3 s.

### What stays open

- **The Neon replay (§16, ADR-011 "Unknowns to measure after deploy").** It must confirm these values on the real compute, F66 included.
- **Not emulated:**
  - how fast Neon's autoscaler leaves 0.25 CU, which decides how often the floor numbers apply;
  - pageserver reads and wake from suspend;
  - the Render↔Neon round trip;
  - Render's CPU-share mechanism;
  - the amd64 cloud CPU (these runs used arm64 on an Apple M4).
- **Sample sizes.** n=5 per worst-case kind, and one run per profile, so run-to-run variance is unknown.
- **Settings.** The stack's `effective_cache_size` was 4 GB at every profile, and Neon's value may differ.

### Week-1 rules (after the replay; no code change needed)

- **Timeout.** Apply the clamp to the replay's worst cases, F66 included. Lower 85 s only if those requests ran on a compute at its 0.25 CU minimum and 3 × their p99 is below 85 s. A replay that saw only a scaled-up compute says nothing about the floor, which these measurements show governs.
- **Lanes.** Change them only if production logs show light analyses shed (`overloaded`) or `analysis_timeout` under ordinary traffic.
- **Parallel workers.** If the owner fixes the compute at 0.25 CU (ADR-011's calibration lever), set `ATUS_API_MAX_PARALLEL_WORKERS=0` once the §9 proof for it passes. At the floor it is the better setting by the measurements above: the F66 compare drops to 38.58 s at p50 (40.71 s max), giving 2.1 × headroom under 85 s.

## Consequences

- Rejected and cached requests cost no analysis capacity, and while the data snapshot is fresh they cost no SQL either (F14): cache hits, 4xx and the metadata routes run 0 statements, an uncached estimate 4 (6 in PostgreSQL's log, with `BEGIN` and `COMMIT`), a compare 5 (7). These are counted per request by the API and verified against PostgreSQL's `log_statement=all` in a test.
- Clients see specific, actionable errors with `Retry-After`. The frontend maps each code to its own message.
- Every protection named here has a test:
  - unit tests in `tests/unit/api/`: `test_guard.py`, `test_admission.py`, `test_protection.py`, `test_database_unit.py`, `test_config.py`, `test_serve_and_logging.py`, `test_snapshot.py` (snapshot, zero-statement paths, dry run, expected data version);
  - integration tests against PostgreSQL in `tests/integration/api/test_protection_db.py` and `test_snapshot_db.py` (lexicon equivalence, unchanged statistics, read-only transactions, statement counts), and `test_statement_log.py` (PostgreSQL's own statement log, in a throwaway `postgres:16` container);
  - a production-configured uvicorn server in `tests/integration/api_prod/test_server.py`.
