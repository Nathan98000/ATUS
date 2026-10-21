# ADR-014: Observability on free tiers

**Status:** Accepted (2026-10-20) · Phase 5 · API side implemented and tested; monitors and the quota report belong to the CI and edge work (ADR-011, ADR-015)

**Related:** [ADR-011](ADR-011-hosting.md) (hosting, request flow, keep-alive policy), [ADR-012](ADR-012-load-and-quota-protection.md) (load and quota protection), ADR-015 (edge cache, snapshot and quota governor). Audit findings are in [`../operations.md`](../operations.md). The HTTP details are in [`../api.md`](../api.md#observability).

## Context

The platform runs on free tiers only (ADR-011), so observability has to fit what those tiers keep and must not spend the quotas it watches:

- **Render Free (Hobby workspace)** keeps logs and service metrics for 7 days; latency percentiles need a Pro workspace. Nothing scrapes a `/metrics` endpoint.
- **Workers Logs** keep 3 days (200,000 events per day) until 2026-11-30, then 7 days (0.5 GB per day). Ingestion stops at the daily cap.
- **GitHub Actions** scheduled workflows are free in a public repository, can be delayed or dropped, and are disabled after 60 days without repository activity. A failed workflow emails the owner when notifications are set up (deployment guide).
- **Every database touch can wake Neon** (≥ 75 CU-s at 0.25 CU per wake, ADR-011). A monitor or readiness probe that reaches the database more often than every 5 minutes would keep it awake all month (operations.md, `/ready` analysis).

Brief §12 requires structured JSON logs with `request_id`, route, status, latency, cache status and `analysis_key`; no full specs or bodies; client IPs treated as personal data (truncated to /24 or /48, or a keyed HMAC rotated at least daily); retention bounded by the providers; metrics for requests, latency, cache, in-flight analyses, rejections, timeouts and the pool; health checks; and $0 alerting.

What the audit found on the API side:

- `/ready` was public, uncached, ran 4 statements per call and returned operator hints (F16).
- uvicorn's access log printed full client addresses, and every response carried `server: uvicorn` (F42).
- Every cache hit and every 422 ran SQL (F14); nothing counted it.
- No internals leaked in responses, but a `%` in the database password reached the log through psycopg_pool (I07).
- psycopg_pool sends no idle traffic (I08), so a quiet API lets Neon sleep, as long as nothing else polls.

## Decision

### Logs: one JSON object per line, on stderr

Production (`ATUS_ENV=production`) logs JSON (`ATUS_LOG_FORMAT=json`); development logs text. Every record has:

| Field | Meaning |
|---|---|
| `ts` | UTC timestamp, millisecond precision, `Z` suffix |
| `level`, `logger`, `message` | as in Python logging |
| `exc_type`, `exc` | only with an exception; the traceback is one JSON string, so a record never spans lines |

Records may add structured fields (`extra={"fields": {...}}`), which the JSON format puts at the top level. The access line and the metrics event below use this.

**The access line.** The request-context middleware writes exactly one record per request (logger `atus.api.access`, level INFO, ERROR for an unhandled exception). It replaces uvicorn's access log, which is switched off in `atus api` and in `create_app`.

| Field | Content |
|---|---|
| `event` | `"access"` |
| `request_id` | the edge's `X-Request-ID` when edge-authenticated and well formed, otherwise generated; also returned in the response |
| `method` | as received (`HEAD` stays `HEAD`) |
| `route` | the route template, e.g. `/api/v1/activities/{code}`; `unmatched` for unknown paths. Requests rejected before routing still get their template |
| `path` | the path as received, still percent-encoded and cut at 200 characters (a decoded newline cannot forge a line). Never the query string |
| `status` | the HTTP status (500 for an unhandled exception) |
| `latency_ms` | receipt of the request to the end of the response |
| `db_ms`, `db_statements` | time inside database transactions, and statements sent, for this request (below) |
| `cache` | `X-Cache` of an analysis: `hit`, `miss`, `join` or `bypass`; otherwise null |
| `analysis_key` | `X-Analysis-Key` of an analysis; otherwise null |
| `error` | the envelope's error code, e.g. `rate_limited`; null on success |
| `client_ip_prefix` | the client's IPv4 /24 or IPv6 /48 network (an IPv4-mapped IPv6 address counts as IPv4); null when unknown |
| `edge_authenticated` | whether the request carried a valid `X-ATUS-Edge-Auth` |
| `disconnected` | the client went away before the response started |

The text format writes the same facts in one line: `request_id=… METHOD path -> status in N ms db=N ms/N cache=… [error=…] [client_disconnected=1]`.

**Never logged:** request or response bodies, analysis specs, query strings (search terms), headers, full client addresses, secrets. The client's address that the access line truncates is the one the rate limiter uses (ADR-012): `X-ATUS-Client-IP` from an authenticated edge (none when an edge request lacks it), the socket peer otherwise.

**Errors.** An unhandled exception is logged once, with its traceback and `request_id` (field and message); uvicorn's duplicate record is dropped (F50). Rejections log their code at INFO. Database failures log the driver class, SQLSTATE and the first line of the server message, never the SQL.

**Secrets.** The database password, edge secrets and ops token are replaced by `***` in records of the API's, psycopg's and uvicorn's loggers, and again in the formatted output of the handler the API installs, which covers tracebacks (I07). A malformed `ATUS_DATABASE_URL` is refused at startup without being echoed.

### Client IPs and retention

- **Raw addresses live only in API memory**, as keys of the per-client token buckets: an LRU of at most 10,000 clients per bucket, lost on every restart or spin-down (ADR-012).
- **Logs and metrics never see more than the /24 or /48 prefix.** A truncated prefix is not reversible to one subscriber the way a plain hash of an IPv4 address is (brief §12). The edge governor keeps its own per-prefix counters as an HMAC with a daily-rotating key (ADR-015).
- **Retention is the providers' retention**: Render 7 days for API logs; Workers Logs 3 days, then 7 days from 2026-12-01, for the Worker's. Nothing is shipped elsewhere, and the API writes no files. Raising retention means paying for a plan or a drain, which constraint 1 of ADR-011 forbids.
- The About page states what is logged and for how long (F61; frontend).

### Database accounting per request

The request-context middleware puts a counter in a context variable for each request. Worker threads, the detached analysis computation and the shared snapshot refresh inherit it, so their database work is charged to the request that caused it; a single-flight joiner pays nothing.

- `db_statements` counts every statement the API sends through psycopg: the pool's connection check (an empty query), the per-transaction limits, the queries. PostgreSQL additionally logs the `BEGIN` and `COMMIT` psycopg sends around each transaction.
- `db_ms` is wall time inside `Database.transaction`, from asking the pool for a connection to giving it back. A waking database shows up here, not as application time.

The per-request statement budget, verified against PostgreSQL's own statement log in a test (`tests/integration/api/test_statement_log.py`), is in [api.md](../api.md#statements-per-request). In short: cache hits, every 4xx, `/meta`, `/activities*`, `/population/metadata`, `/health` cost 0 statements while the in-process data snapshot is fresh; an uncached estimate or trend costs 4 (6 in the server log), a compare 5 (7).

### `Server-Timing`

Every response carries `Server-Timing: db;dur=<ms>, app;dur=<ms>`: the request's `db_ms`, and the rest of its time up to the response start. The Worker logs it, and its governor uses `db` to size the compute charge of an analysis; a shed, rejected or cached answer with `db;dur=0` is not charged at all (ADR-015). It is the only latency attribution available on Render's free plan, which has no percentiles.

### Metrics

`GET /metrics` serves the Prometheus text format (0.0.4), hand-written in `api/metrics.py` instead of adding `prometheus_client` as a runtime dependency. Label values come only from bounded vocabularies; no client input becomes a label. Counters reset when the process restarts.

| Series | Kind | Source |
|---|---|---|
| `atus_http_requests_total{route,method,status}` | counter | access record |
| `atus_http_request_duration_seconds{route}` | histogram, 5 ms to 120 s | access record |
| `atus_http_errors_total{code}` | counter | access record |
| `atus_analysis_cache_total{result}` | counter: `hit`, `miss`, `bypass`, `join` | access record |
| `atus_db_statements_per_request{route}` | histogram, 0 to 21 | access record |
| `atus_analyses_in_flight`, `atus_analyses_heavy_in_flight` | gauge | admission lanes |
| `atus_admission_waits_total`, `atus_admission_shed_total` | counter | admission lanes |
| `atus_client_disconnects_total{route}` | counter | clients that disconnected before their response (analyses) |
| `atus_rate_limit_rejections_total{bucket}` | counter: `request`, `analysis` | token buckets |
| `atus_daily_cap_rejections_total`, `atus_daily_cap_used`, `atus_daily_cap_limit` | counter, gauges | daily cap |
| `atus_analysis_computations_total`, `atus_analysis_joins_total` | counter | analysis runner |
| `atus_analysis_cache_entries` | gauge | result cache |
| `atus_query_timeouts_total` | counter: statement timeout, request deadline, shutdown | database layer |
| `atus_snapshot_refreshes_total`, `atus_snapshot_refresh_failures_total`, `atus_snapshot_age_seconds` | counters, gauge | data snapshot |
| `atus_db_pool_{pool_min,pool_max,pool_size,pool_available,requests_waiting}` | gauge | `psycopg_pool.get_stats()` |
| `atus_db_pool_*_total` (requests, waits, errors, connections, lost, bad returns) | counter | `psycopg_pool.get_stats()` |
| `atus_build_info{build}` | gauge, 1 | `ATUS_BUILD_SHA` |

Do not alert on `atus_db_pool_requests_waiting` alone: psycopg_pool counts waiters that already timed out (I11).

**Metrics events in the log.** Nothing on the free stack scrapes `/metrics`, so the process also writes one record with `event: "metrics"` every 5 minutes, holding the headline counters (responses by status class, error codes, cache outcomes, computations, joins, lane waits and sheds, rate-limit and daily-cap rejections, query timeouts, pool size and errors, snapshot age, uptime). It writes one only if requests arrived since the previous event, so a quiet process writes nothing. The event touches no database, and Render keeps it for the same 7 days as the access lines.

### Health checks

| Endpoint | Who calls it | Touches | Answer |
|---|---|---|---|
| `GET /health` (Worker: `/api/health`, prewake `/api/v1/wake`) | Render's health check, the deep monitor, the deploy workflow | nothing: no database, no edge authentication, no worker thread | `{"status": "ok", "build": "<ATUS_BUILD_SHA>"}`, `Cache-Control: no-store` |
| `GET /ready` | operators, with `Authorization: Bearer <ATUS_OPS_TOKEN>` | the data snapshot: the database only when the snapshot is older than its TTL | 200 with `data_version`, `expected_data_version`, `build`, `snapshot_age_s`; or the ordinary 503 envelope (`database_unavailable`, `schema_not_initialized`, `data_not_loaded`, `data_version_mismatch`) |
| `GET /metrics` | operators, same bearer | memory only | Prometheus text |
| `GET /api/_edge/health` | the edge monitor | the Worker only (ADR-015) | Worker-generated |

- `/health` reports the build so the deploy workflow can wait until the new image answers (ADR-011).
- `/ready` is not the platform's health check: a readiness probe that reached the database would keep Neon awake. Its remediation hints (`atus migrate`, `atus load`) go to the log, not to the response (F16).
- Without a valid bearer, or with no `ATUS_OPS_TOKEN` configured, `/ready` and `/metrics` answer exactly like a path that does not exist: 404 `not_found`, or 403 `forbidden_origin` where edge authentication refuses every unknown path. A valid bearer is enough on its own, without the edge secret, because the Worker never routes these paths and an operator reaches the origin directly. Neither is in the OpenAPI schema.

### Monitors and alerting (cadence from ADR-011)

Cadence and budget as in [`../deployment.md`](../deployment.md) (traffic model and free-tier budget):

| Check | Schedule | Calls | Wakes | Per month (PROJECTION) |
|---|---|---|---|---|
| Edge check | every 30 min (`:17`, `:47`) | `/api/_edge/health`, answered by the Worker | nothing behind the Worker | 1,488 runs, 1 Worker request each |
| Deep check | every 6 h (`17 */6 * * *`) | `/api/health` → origin `/health` | Render (spin-up), never Neon | 124 runs, 1 Worker request and up to 1 Render wake each |
| Quota report | daily | the Worker's `/api/_ops/budget` | nothing behind the Worker | 31 runs, 1 Worker request each |
| Schedule check | monthly runbook | re-enables auto-disabled workflows | — | — |

- **No monitor touches the database.** The only check that reaches the API calls `/health`, so Neon wakes only for real traffic (brief §10).
- **Alerting is a failed workflow**, which emails the owner. The quota report fails when any budget is above 70% of its pro-rated value (ADR-011).
- The deep check does not keep Render awake: it spins down after 15 idle minutes and the check comes every 6 hours (ADR-011 keep-alive policy).

## What this does not provide

- **History beyond the retention.** 7 days of API logs and 3 to 7 days of Worker logs. No dashboards beyond what Render and Cloudflare show for free.
- **Percentiles from the platform.** Render's free plan has none. They can be computed from the access lines (`latency_ms`) or the `/metrics` histograms while the process is up, or from `Server-Timing` at the edge.
- **Continuity across restarts.** Every counter, histogram and the metrics event start from zero after a restart or spin-down; the access lines are the durable record.
- **Scraping.** `/metrics` is for an operator's curl. When the service sleeps, nothing answers, and the first request wakes it.
- **Tracing.** There is no distributed tracing; `X-Request-ID` correlates a Worker log line with the API's access line.
- **Proof of the providers' behaviour.** Whether Render's health checks count as traffic, and what the Worker's logs record, are measured after deploy (ADR-011, "Unknowns").

## Consequences

- An operator can answer "what happened to request X" from one access line (and the Worker's line with the same `X-Request-ID`), and "how is the process doing" from `/metrics` or the latest metrics event, without any paid tool.
- Logs hold no personal data beyond /24 and /48 prefixes, for at most the providers' retention.
- Statement counts are visible per request and per route, so a regression that makes cache hits or 4xx touch the database shows up in `atus_db_statements_per_request` and fails `test_statement_log.py`.
- Tests: `tests/unit/api/test_observability.py` (access record, IP truncation, JSON format, `Server-Timing`, operator endpoints, exposition format, metrics events), `tests/integration/api/test_snapshot_db.py` (statement counts, pool series), `tests/integration/api/test_statement_log.py` (PostgreSQL's own count), `tests/integration/api_prod/test_server.py` (a production process logging JSON on stderr).
- No timing, latency or load numbers were measured for this ADR.

## Amendments (implementation, 2026-10-21)

The decisions are unchanged. This section records where the implementation differs from the text above, and why. Nothing is deployed, so no monitor has run against a deployment.

### Monitors (status line: "monitors and the quota report belong to the CI and edge work")

- **Implemented as workflows.** The monitors are `.github/workflows/edge-check.yml`, `deep-check.yml`, `quota-report.yml` and `schedules-check.yml`. They run `scripts/ops/monitors.py` and `scripts/ops/github_ops.py`.
  - `tests/deploy/test_workflows.py` asserts each one's cadence and manual trigger.
  - It also asserts that no monitor touches the database, and that the site monitors are skipped until the repository variable `PUBLIC_BASE_URL` exists.
- **Edge check: 4 requests per run, not 1.**
  - It fetches the SPA shell (with its security headers), `/api/_edge/health`, and the snapshot pointer and manifest.
  - Only `/api/_edge/health` invokes the Worker, so the Worker requests per month are as projected.
  - It also fails when the Worker's `SNAPSHOT_ID` differs from `/snap/current.json`.
- **Deep check: 1–8 requests per run** (`--wake-timeout 240 --max-requests 8`). It retries through a cold start instead of failing on the first `service_waking`.
  - The bound is 8 × 124 = 992 Worker requests a month, not 124.
  - Each run still wakes Render at most once and never wakes Neon.
- **Quota report.**
  - It runs daily at 22:05 UTC, so it reads the governor's counters before their 00:00 UTC reset.
  - It runs in the GitHub environment `ops`, with the Worker's ops token stored as `EDGE_OPS_TOKEN`, and alerts at a threshold of 0.7.
  - The pro-rating rules are in [`../deployment.md`](../deployment.md#monitoring-and-alerting-0). The per-network cap is reported but never alerts.
- **Schedule check: a weekly workflow (Monday 09:41 UTC, off the hour), not a runbook step.** It re-enables nothing: with a read-only token (`actions: read`) it can only fail, and so email the owner, when:
  - a scheduled workflow is not active;
  - a monitor's last scheduled run is too old;
  - the default branch has had no commit for 28 days. (Amended in review round 1: it read the repository's `pushed_at` and ran monthly at minute 0. Dependabot's weekly branch pushes keep `pushed_at` fresh, GitHub does not say whether they count as activity, and one dropped monthly run removed the only warning.)

  Re-enabling a workflow stays manual (Actions → workflow → "Enable workflow"). If the schedule check itself has been disabled, it cannot alert.

### Statement budget: "every 4xx"

"Every 4xx" costs 0 statements only when the 4xx is decided before the statistics query. `insufficient_data`, and the 422 for a diary-date window outside the requested years, are found by the statistics query itself (`src/atus_pipeline/analytics/engine.py`), so those requests run it first and report `db;dur` above 0. [api.md](../api.md#statements-per-request) has the qualified table.

### Metrics

Two series were added after the table above: `atus_snapshot_pinned` (gauge) and `atus_snapshot_stale_serves_total` (counter). They report the pinned and stale-if-error data snapshot of [ADR-012](ADR-012-load-and-quota-protection.md) (`src/atus_pipeline/api/metrics.py`).

### The Worker's log line

This ADR describes only the API's logs. The Worker writes one JSON line per `/api` request (`edge/src/index.ts`).

- **Fields:**
  - `event`, `request_id`, `method`, the route name, `status`, `outcome` and `reason`;
  - `cache` (`edge`, `miss`, `error`, `off`) and `governor` (`admitted`, `denied:<budget>`, `unavailable`);
  - `upstream_status`, `attempts`, the origin's `server_timing` and `duration_ms`;
  - an error name, when there is one.
- **Never logged:** bodies, client IPs, prefix hashes, secrets or the origin hostname.
- **Correlation.** Its `request_id` is the `X-Request-ID` that the API logs.

### Workers invocation logs

`edge/wrangler.jsonc` enables `observability.logs.invocation_logs`. Whether Cloudflare's invocation records include the client IP or request headers is unverified ([ADR-011](ADR-011-hosting.md), "Unknowns").

- **What is verified.** "Logs and metrics never see more than the /24 or /48 prefix" is verified only for the API's records and the Worker's own line.
- **If they do hold full addresses.** They are kept for Workers Logs' retention, unless `invocation_logs` is turned off.
