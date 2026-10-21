# ADR-015: Edge result cache, static snapshot and quota governor

**Status:** Accepted (2026-10-20) · Phase 5 · implemented and tested locally; not deployed

**Related:** [ADR-011](ADR-011-hosting.md) (hosting and request flow; this ADR finalizes its "Edge tiers" and "Quota governor" sections), [ADR-012](ADR-012-load-and-quota-protection.md) (the API's own, second layer of protection), [ADR-009](ADR-009-shareable-urls.md) (the stable request serialization), [`../deployment.md`](../deployment.md) (traffic model and free-tier budget).

**Scope and honesty.** Nothing here is deployed. Numbers are one of three kinds:

- **measured** in local tests or the local end-to-end run (counts and sizes only; no timings were taken);
- **PROJECTION**, from the traffic model in [`../deployment.md`](../deployment.md), labelled as such;
- **cited** free-tier terms, read on the official pages on 2026-10-20 (see "Cited terms").

Code: `edge/src/cache.ts`, `edge/src/governor.ts`, `edge/src/budget.ts`, `edge/src/admission.ts`, `edge/src/ops.ts`, `scripts/build_snapshot.py`. Tests: `edge/test/worker/{cache,governor,governor-flow,budget,admission,ops,integration}.test.ts`, `tests/unit/test_build_snapshot.py`, `tests/integration/test_build_snapshot_api.py`.

## Context

The brief's §13 allows a persistent result cache only with an ADR covering write privileges, versioning by `data_version`, size bounds and quota cost. ADR-011 chose three cache tiers in front of a sleeping Render instance and a Neon database whose month-to-date compute is the scarcest resource, and a Durable Object governor that admits the analyses that would reach them. It left the details to this ADR.

What drives the design:

- **Every avoided origin call avoids a Render spin-up (~1 min, documented) and possibly a Neon wake.** A Neon wake session costs at least 2 CU × 300 s = 600 CU-s (0.167 CU-h) under the conservative autoscaler assumption (scenario C in `deployment.md`).
- **Statistics must not change.** A cache may only replay bytes the origin produced for the same data, analytics version and response format.
- **Free tiers fail closed, never bill** (ADR-011). Each tier must stay inside its own free quota and fail toward the next tier.

## Decision

### Tiers

| Tier | Holds | Key | Written by | Read by | Survives |
|---|---|---|---|---|---|
| **L0 static snapshot** `/snap/<SNAPSHOT_ID>/` | every metadata response (meta, activities, presets, population metadata, all 556 activity details) and the prewarmed analyses, byte for byte | GET: the API path; POST: the request hash `k` | the deploy workflow only, through `scripts/build_snapshot.py`, from the exact deployed image read-only against the restored release dump | the SPA, through the manifest; the Worker, for metadata GETs without a query string ("Metadata at the edge") | everything, including the Worker's daily limit and Render or Neon pauses |
| **L1 Workers KV** | analysis responses (≤ 16 KiB) | `r1/<CACHE_SCOPE>/<k>` | the Worker only, through its `RESULT_CACHE` binding, under the store gate | the Worker, before the governor | Render spin-down and Render/Neon monthly pauses; not the Worker's daily limit |
| **L2 API in-process LRU** | recent results | operation, canonical spec, analytics version and data version (`src/atus_pipeline/api/cache.py`); keying on the resolved computation is deferred (F15, ADR-012) | the API | the API | nothing (lost on spin-down) |

### Keys and version scope

- **Request hash** `k = sha256_hex(path + "?" + query + "\n" + body)`: `path` as sent (e.g. `/api/v1/analysis/estimate`), `query` the raw query string without `?` (empty from the SPA), `body` the exact UTF-8 bytes. The SPA, the Worker and the snapshot builder compute the same value; a shared test vector is pinned in the Worker and builder tests.
- **`CACHE_SCOPE = sha256_hex(EPOCH|API_VERSION|ANALYTICS_VERSION|DATA_VERSION|RESPONSE_FORMAT)[:16]`**, from the Worker vars the deploy sets. The git SHA is deliberately not in it, so a code-only deploy keeps the keyspace. `EPOCH` (a Worker var, `"1"`) is the manual lever that orphans every entry at once. The scope is reported by `/api/_edge/health` and `/api/_ops/budget`. Every input is public, so the scope says which release an entry is for, not who wrote it: that is the entry's tag ("KV store gate").
- **`SNAPSHOT_ID = <api_build[:12]>-<sha256_hex(DATA_VERSION)[:16]>`**: a new image or a new data release gives a new snapshot directory.
- **Cache off without release vars.** With an empty `DATA_VERSION` or `ANALYTICS_VERSION` (any deploy that did not pass them), or without `IP_HMAC_KEY`, the Worker neither reads nor writes KV.

### KV store gate

The Worker stores a response only when all of these hold (`storeHeadersAllow` plus the body check):

1. a POST to `/api/v1/analysis/{estimate,trend,compare}`;
2. status exactly 200, `Content-Type: application/json`;
3. origin `X-Cache` is `miss`: the API computed it for this request, on an admission whose count the governor keeps. Never `hit` (refunded from the global count, so byte variants of one spec could write without bound; review round 1), `bypass` (computed during a data reload) or `join`;
4. `X-Data-Version` equals the Worker's `DATA_VERSION`;
5. `X-Analytics-Version` equals the Worker's `ANALYTICS_VERSION` (stricter than ADR-011's list: the scope includes it, so a mismatch would mislabel the entry);
6. `X-Response-Format` equals the Worker's `RESPONSE_FORMAT`;
7. the body arrived complete and is at most 16 KiB (measured maximum 8,383 B, ADR-011).

Only when conditions 2–6 hold does the Worker buffer the body (up to 16 KiB + 1 byte) before answering; a longer body is streamed on unchanged. A body that breaks mid-read becomes a 502 `upstream_unavailable` and is not stored. The entry holds the body bytes and, as KV metadata (≤ 1,024 B), the content type, the three version values, the `X-Analysis-Key`, the store time and a tag: HMAC-SHA256 over the KV key, those fields and the SHA-256 of the body, under HMAC-SHA256(`IP_HMAC_KEY`, `"atus-kv-entry-tag"`) (review round 4). TTL 30 days. The write runs through `waitUntil` after the response; a failed write is ignored. A failed read, an entry whose metadata does not match the deployment, an entry whose tag does not verify, or an empty or oversized value is a miss.

A hit returns the stored bytes with status 200, `X-Cache: edge`, the stored version headers and analysis key, a new `X-Request-ID` and the security headers. It touches neither the governor nor the origin.

### Static snapshot (L0)

Layout (all static assets under the SPA's `dist`, so a request for one of these files never invokes the Worker; a non-navigation request for a file that is not there, such as a tab still on the previous snapshot asking for one of its files, does: ADR-011 "Request flow", corrected in review round 2):

```text
snap/current.json                    pointer (no-cache): snapshot_id, data_version, analytics_version,
                                     api_version, response_format, generated_at
snap/<SNAPSHOT_ID>/manifest.json     the same fields, api_build, "get": {path: file},
                                     "post": {k: {path, file, analysis_key, x_cache}},
                                     "files": {file: {sha256, bytes}}
snap/<SNAPSHOT_ID>/get/...           exact GET bodies
snap/<SNAPSHOT_ID>/post/<k>.json     exact analysis bodies
```

`scripts/build_snapshot.py`:

- calls a running API with the edge secret (sent only as `X-ATUS-Edge-Auth`, never printed; plain HTTP only to loopback hosts; no redirects followed; no proxies);
- takes the analysis list from `--requests` files or runs the SPA's own generator (`frontend/scripts/snapshot-requests.mjs`) in both of its passes: the prewarm list, then the canonical `spec` of each response, so share links copied from a prewarmed result hit too;
- refuses any answer that is not a 200 with a JSON body, and any analysis computed during a data reload (`X-Cache: bypass`, which the KV gate refuses too). 429 and 503 answers with `Retry-After` are retried within a total wait budget (`--max-wait`, default 600 s), so the build API should run with generous per-client limits;
- requires `X-Data-Version`, `X-Analytics-Version` and `X-Response-Format` on every analysis response, and `X-Data-Version` on `/api/v1/meta`, `/api/v1/activities` and every activity detail (the API sends them on all of these), and one value of each across all responses. `--data-version` and `--response-format` only add expectations the API must meet; they never stand in for a missing header;
- with `--skip-refused` (the local stack's fixture data only), leaves out an analysis the API refuses with a 422 domain error instead of failing; the SPA then asks the API, which answers the same. A release snapshot is built without it, so every prewarmed spec must succeed on the release;
- writes into a temporary directory, verifies every file's sha256 and size against the manifest, then moves the snapshot into place and writes `current.json` last. `--verify DIR` re-checks a published tree.

Measured on the fixture database with the real 0325 lexicon (local run): 560 metadata responses (4 + 556 activity details), 564 files with two analyses, 2.4 MB on disk; the largest files were the manifest (110,715 B) and `/api/v1/activities` (70,851 B). Workers static assets allow 20,000 files and 25 MiB per file (ADR-011).

The SPA reads the manifest and never probes for a file, because the SPA fallback answers a missing path with `index.html` and a 200.

**Metadata at the edge.** The Worker answers `GET /api/v1/meta`, `/activities`, `/activities/presets`, `/population/metadata` and `/activities/<code>` (a 2-, 4- or 6-digit code) from the same files (`edge/src/snapshot.ts`): the bytes as built, `Content-Type: application/json`, the API's version headers (`X-Data-Version` from the Worker's `DATA_VERSION`, except on presets and population metadata; `X-Analytics-Version` on `/meta`), a new `X-Request-ID` and the security headers. It does so only for a request without a query string, and only when `SNAPSHOT_ID`, `DATA_VERSION` and `ANALYTICS_VERSION` are set and `SNAPSHOT_ID` ends in `sha256_hex(DATA_VERSION)[:16]`, so the version it adds is the snapshot's own. A file the snapshot does not hold comes back from the assets binding as `index.html`, which is a miss. Every miss (a query string, a code not in the lexicon, no snapshot deployed) goes to the origin only as a governed metadata lookup ("Quota governor"). Before review round 1 every metadata GET was forwarded: each cost Render outbound bandwidth (the lexicon is 70,851 B, ~8.5 KB gzipped), and one client replaying it at the Worker's 100,000 requests a day would have spent Render's 5 GB a month in under a week, after which Render suspends the service until the next month.

### Quota governor

One SQLite-backed Durable Object, named `global-` followed by HMAC-SHA256(`IP_HMAC_KEY`, `"atus-governor-instance"`) truncated to 128 bits (review round 4; before, plainly `global`), so rotating `IP_HMAC_KEY` moves the Worker onto a fresh instance. The Worker calls it for analysis POSTs that missed KV, and for metadata GETs the snapshot could not answer. Static files, KV hits, snapshot answers, health and wake never call it.

**Who is charged.** An HMAC of the client network: IPv4 `/24` (also for IPv4-mapped IPv6), IPv6 `/48`, from `CF-Connecting-IP`; requests without a parseable address share one `unknown` bucket. The HMAC is `HMAC-SHA256(HMAC-SHA256(IP_HMAC_KEY, "atus-ip-prefix|<UTC date>"), prefix)` truncated to 128 bits, computed in the Worker. The governor never sees an address. The derived key changes every UTC day, but `IP_HMAC_KEY` itself changes only when rotated, so whoever holds it can derive any day's key. Prefix rows are deleted at the first event after 00:00 UTC (a midnight alarm makes sure that happens without traffic), and the Durable Object's point-in-time recovery can still restore them for 30 days (review round 2; [`../security.md`](../security.md) C17).

**Cost class.** Heavy when `len(years) × (2 for a compare, else 1) ≥ 8`, the API's lane rule (ADR-012); also heavy when the body is not JSON with a `years` list (the API rejects those, and they are then refunded).

**Budgets, in the order they are checked:**

| # | Budget | Limit | Denial | `details.scope` | `Retry-After` / `resumes_at` |
|---|---|---|---|---|---|
| 1 | per client network | 60 uncached analyses per UTC day, and 300 admissions including refunded ones | 429 `rate_limited` (`prefix`) | `client` | next 00:00 UTC |
| 2 | global | 600 per UTC day | 503 `daily_capacity_reached` (`global`) | `global` | next 00:00 UTC |
| 3 | heavy | 300 heavy per UTC day | 503 `daily_capacity_reached` (`heavy`) | `daily` | next 00:00 UTC |
| 4 | per client network's compute | a fifth (`PREFIX_DAILY_CU_SHARE`) of row 5's ceiling, in compute charged to the network | 429 `rate_limited` (`prefix`) | `client` | next 00:00 UTC |
| 5 | Neon compute today (charged by the model below) | 10 CU-h with fresh ground truth, else (100 × 0.9) / 31 = 2.903 CU-h | 503 `daily_capacity_reached` (`cu_day`) | `daily` | next 00:00 UTC |
| 6 | Neon egress today, without ground truth only | 4.5 GB / 31 ≈ 145 MB, from per-analysis estimates ("Model error") | 503 `daily_capacity_reached` (`egress_day`) | `daily` | next 00:00 UTC |
| 7 | Neon egress month to date (ground truth only) | stop at 4.5 GB (4.5 × 10⁹ B) | 503 `capacity_paused` (`egress`) | `monthly` | end of Neon's consumption period |
| 8 | Neon compute month to date (ground truth only) | below `min(85, 10 + 75 × elapsed share)` CU-h | 503 `capacity_paused` (`cu_month`) | `monthly` | when the line reaches the need; the period end above 85 |

Every denial also carries `details.budget` (in brackets above; `metadata` for a lookup's own counts) and `details.retry_after_seconds`. Checks 4–8 include the candidate's own provisional charge: 780 CU-s for a fresh session, and its egress estimate. That is an estimate, not a bound. A settle without `Server-Timing` (a Cloudflare 524 always is one, and arrives more than 90 s after admission) is charged from admission to the settle plus the 300 s tail, so the request that crosses a compute budget can pass it by 2 CU for every second its settle comes later than 90 s after admission: at most about 1,020 CU-s, for a settle just before the 10-minute lease expiry. With `Server-Timing` the settled window is sized from the reported DB time instead, `[end − db − 5 s, end + 300 s]`; it still ends later than the provisional one when the answer comes more than 90 s after admission (a Render spin-up first), so such a settle can overshoot too. The excess is in the model's counter (a conservative over-charge, not compute Neon is known to have used).

**Each network's share of the compute (row 4).** The counts in row 1 do not bound compute. An isolated analysis is charged its whole wake session, 2 CU × (DB time + 5 s + 300 s) ≈ 610 CU-s. Before review round 1, one network that spaced cheap analyses about 7 minutes apart, so that none shared a session, reached the conservative ceiling after 17 of them and the ground-truth ceiling after 59, both under its 60; every other network then got `daily_capacity_reached` until 00:00 UTC. Now every settled or expired analysis's compute (the part of the union it added, the same amount the day is charged) is also charged to its network, and admission adds what the network's own in-flight analyses and the candidate would add beyond the windows already charged. A fifth of the ceiling gives one network about 3 isolated sessions a day in conservative mode (2,090 CU-s) and about 11 with ground truth (7,200 CU-s). Analyses inside one session share its window, but the window grows with the session: a network running novel analyses without a break (a minute apart) is refused after about 11 minutes a day in conservative mode and about 54 with ground truth (`governor.test.ts`, "a real visitor's continuous run"), where before it could run its 60. It then gets 429 `rate_limited` (`budget: prefix`) until 00:00 UTC; results in the static snapshot and the KV cache still work. That is the price of this budget on the default, conservative deploy; the Neon key's ground truth multiplies it by about five. At least five networks are now needed to spend the day's compute.

**Metadata lookups.** A metadata GET the snapshot could not answer ("Metadata at the edge") is admitted by `admitLookup`: at most 200 a day per client network (429 `rate_limited`, `scope: client`) and 2,000 a day in all (503 `daily_capacity_reached`, `scope: global`), both with `budget: metadata`, until 00:00 UTC. Each costs Render outbound. A warm API answers it from memory (`db;dur=0`), but the first data request on a freshly started API process (after every Render spin-down) runs the process's snapshot read (11 statements, 108,005 B) on Neon and can wake it. So a lookup also passes rows 4–8 like an analysis, with its candidate's provisional window and an egress estimate of 128 KiB (`SNAPSHOT_READ_EGRESS_BYTES`), and gets the same denials (`budget` `prefix`, `cu_day`, `egress_day`, `egress`, `cu_month`), worded for metadata. It holds a lease that the Worker settles with the API's `Server-Timing`: `db;dur=0` or not reached is free; DB time is charged its window and the egress estimate, to the day and to the network, as for an analysis; a lost settle expires and is charged like an analysis's. A lookup's counts are never refunded. Before review round 2 a lookup was only counted, so one network sending one after each spin-down (about every 16 minutes) woke Neon about 90 times a day with no charge, past the daily ceiling, its compute share and the monthly stop; it is now refused after about 3 such wakes a day in conservative mode (its fifth of the ceiling, `metadata.test.ts`). The SPA never needs a lookup.

The second limit in row 1 exists because refunds (below) give the 60 back: without it, one network whose analyses the API sheds (its own per-client rate limit answers 429 after 6 a minute) or that arrive while Render wakes could be admitted without bound, and every admission plus its settle writes up to 11 governor rows when it ends refunded that way (Workers Free allows 100,000 a day). 300 is about ten cold starts' worth of SPA retries (150 s at `Retry-After: 5`, ~30 attempts each).

**Compute charge model** (the conservative bridge of ADR-011; `budget.ts`). The compute is assumed to run at its maximum, 2 CU, from the moment DB work starts until 300 s after it ends. The charge is 2 CU × the length of the union of those windows:

- **Provisional, at admission:** each in-flight analysis is assumed to occupy `[admitted, admitted + 90 s + 300 s]` (90 s is the API's deadline), 780 CU-s for a fresh session. Only the part of the union outside the windows already charged counts, so concurrent admissions share one window instead of costing a session each.
- **At settle:** the DB work is taken to span `[max(admitted, end − db − 5 s), end]`, where `db` is the API's `Server-Timing: db;dur=` and 5 s covers the connect and a Neon wake. Without `Server-Timing` it starts at admission, which also charges a Render spin-up: an over-charge, never an under-charge. The charge is the part of `[start, end + 300 s]` not charged yet, at 2 CU. The governor keeps the charged windows themselves (merged, back to the oldest time a live lease can reach, 10 minutes), not just the end of the last one, so analyses that settle out of order (a long one that started first and finished last) are charged their whole union.
- **Never settled** (the Worker invocation died, or Cloudflare cancelled it after its client disconnected): after 10 minutes the lease is charged as if the API had kept the DB busy to its deadline, `[admitted, admitted + 90 s + 300 s]`, as part of the union like any settle, and its counts are kept. Leases admitted together share one window (780 CU-s for a fresh session), as they would have had they settled. Before review round 1 each expired lease was charged 780 CU-s on its own, so 13 abandoned analyses closed the conservative day. The Worker queues the settle with `waitUntil` before it calls the origin, so a client that leaves early loses it only when the origin answers more than 30 s later (Cloudflare's limit for such work).

**Settling and refunds.** The Worker reports how the origin call ended:

| Outcome | Examples | Day and heavy counts | Network count | Compute and egress |
|---|---|---|---|---|
| `not_reached` | Render waking, platform error page other than a 524, connection failure | refunded | refunded | not charged |
| `shed` | API `overloaded`, `rate_limited`, `daily_capacity_reached`; and the data-availability refusals, which return no result and depend on the deployment's state, not on the request: `database_paused`, `database_unavailable`, `data_version_mismatch`, `data_not_loaded`, `schema_not_initialized` (all but the first can come after statements ran: a version change detected after computing, a connection that broke after the pool gave it; their DB time is charged) | refunded | refunded | charged unless `db;dur=0`, which the API reports when it got no connection at all (refused, wrong credentials, TLS, a connect timeout); never for `database_paused` (Neon refused the connection) |
| `rejected` | API 400, 413, 415, 422 | refunded, unless `db;dur` > 0: a 422 found only by the statistics query (`insufficient_data`) ran it in full and keeps its counts, so the heavy cap bounds that egress | kept (bounds invalid-request floods) | charged unless `db;dur=0` |
| `reused` | 2xx with `X-Cache: hit` or `join`, on the first attempt | refunded, unless `db;dur` > 0 (as for `rejected`) | kept | charged unless `db;dur=0` |
| `computed` | 2xx `miss`/`bypass`, `analysis_timeout`, an unexpected 500, a Cloudflare 524; and a `hit`/`join` that came back on the Worker's retry, which may be the first attempt's own computation (its connection dropped before the headers), charged from admission | kept | kept | charged |

Refunding waking and shed outcomes matters because the SPA retries during a cold start for up to 150 s. A 524 from Render's Cloudflare zone is charged: the origin took the request and kept it past the proxy read timeout, and the API runs an admitted analysis to the end even when its client is gone. While the per-isolate circuit breaker skips the origin, analyses get 503 `upstream_unavailable` before any governor call.

`upstream_unavailable` carries `details.reason`, because only some causes mean the API may have taken the request: `origin_timeout` (a 524) and `broken_response` (the API's body broke off) may have computed it, so the SPA retries such an analysis once, when it joins the running computation or hits the API's cache; `connect_failed`, `circuit_open`, `platform_error` (a platform page, a redirect, an unreadable envelope) and `not_configured` never reached the API and are retried like a cold start within the 150 s budget.

The network count (the 60) is refunded for `not_reached` and `shed`, but every admission still counts toward the network's 300 admissions a day.

**Ground truth (optional).** With the `NEON_API_KEY` secret (a project-scoped key) and the `NEON_PROJECT_ID` var, an alarm reads `GET https://console.neon.tech/api/v2/projects/{id}` (`compute_time_seconds` / 3600 = CU-h month to date, `data_transfer_bytes`, `consumption_period_start` and `_end`). It polls at most every 5 minutes and only while analyses arrived in the last 5 minutes, so the compute is awake anyway. Between polls, usage is bridged with the same conservative charges, counted from the *previous* successful poll (Neon's figures lag), plus the in-flight provisional window. Ground truth earns the 10 CU-h daily ceiling while it is at most 24 h old; the monthly checks apply whenever ground truth for the current period exists. A failed poll keeps the last ground truth and records only a status or error name (`http_401`, `TimeoutError`); the key goes only into the `Authorization` header.

**Fail closed.** If the governor errors or does not answer within 5 s, uncached analyses and metadata lookups get 503 `capacity_unavailable` (`Retry-After: 60`). KV hits, metadata the snapshot answers, health, wake and static files are unaffected. A failed settle never fails a request.

**Ops.** `GET /api/_ops/budget` with `Authorization: Bearer <OPS_TOKEN>` (constant-time comparison) returns the edge's versions and cache scope and the governor's report: mode, today's counts and refunds, active prefixes and the largest prefix count (no prefixes or addresses), compute today (settled, provisional, ceiling), a rolling 31-day sum, month-to-date figures with the last poll error, and a used/limit ratio per budget for the daily quota-report workflow. Any other request to that path, including a wrong method, gets the same 404 as an unknown route.

### Write privileges

- **L0:** only the deploy workflow, by publishing static assets. Files are immutable per `SNAPSHOT_ID`.
- **L1:** only the Worker, through its binding, and only under the gate. No runtime holds a KV API token. The API cannot write. The Cloudflare API token that deploys the Worker may also be able to write KV, and code deployed with it can write through the binding and read every Worker secret, `IP_HMAC_KEY` included, so it can tag entries under any scope. An `EPOCH` bump is no purge then (its next value is public). If the token leaks, replace that code with your own and rotate every Worker secret, `IP_HMAC_KEY` first: no entry tagged under the old key verifies any more (review rounds 2 and 4; [`../deployment.md`](../deployment.md#rotation), "Rotation", item 5).
- **Governor:** only the Worker, through its binding. The ops endpoint only reads. A version deployed with a leaked token can write an instance's storage as it likes, and that storage outlives it; the rotated `IP_HMAC_KEY` names a fresh instance, and every instance drops what this code could not have written (review round 4: amendments).
- **L2:** the API process.

### Quota cost

Workers KV (Free; cited terms below):

| Operation | Free limit per day | Use | Bound |
|---|---|---|---|
| reads | 100,000 (failed and missing-key reads count) | one per analysis POST that reaches the Worker with release vars set. PROJECTION 45/day typical, 450 on the spike day | ≤ Worker requests, whose limit is also 100,000: reads cannot run out first |
| writes | 1,000 (different keys); 1 write/s per key | at most one per admitted analysis whose response passes the gate (`X-Cache: miss` only). PROJECTION 30/day typical; on the spike day only what the governor admits, 34–123 of 300 (`deployment.md`, "The spike day under the governor") | ≤ 600 by the governor's global cap (60% of the limit): a `miss` settles as `computed`, whose count is never refunded |
| deletes, lists | 1,000 each | none (entries expire by TTL) | 0 |
| storage | 1 GB | ≤ 16 KiB value + ≤ 1 KiB metadata per entry, 30-day TTL | 600 × 30 × 17 KiB ≈ 0.31 GB (31%). PROJECTION 1.3–10 MB |

Durable Objects on Workers Free (SQLite backend only; cited terms below). Row counts are **measured** from the SQLite cursors in `governor.test.ts` (index rows included, as billed):

| Quantity | Free limit per day | Use | Bound |
|---|---|---|---|
| requests (incl. alarm invocations) | 100,000 | 2 per admitted analysis or metadata lookup (admit, settle), 1 per denial, ≤ 1 midnight alarm, ≤ 288 polls only while traffic is active (with a Neon key), ops reads. PROJECTION ~95/day typical | ≤ analysis POSTs that miss KV + 2 × the metadata lookups + ~900. It can pass 100,000 only on a day the Worker's own 100,000 requests are spent, when `/api` is down anyway |
| rows written | 100,000; each `setAlarm` is one row; deletes count | admission ≤ 8; settle ≤ 4, or 5 when it is refunded and also charged compute (a data-availability refusal with DB time, or an API answer without `Server-Timing`), and ≤ 3 when it is refunded with `db;dur=0`, which covers every refund a client can cause; metadata lookup ≤ 7 to admit and ≤ 4 to settle (1 when it reports `db;dur=0`); denial 0 (measured); plus the midnight purge and ≤ 2 per poll | ≤ ~10,000 at the 600 cap (≤ 10%), and ≤ 22,000 for the 2,000 lookups. Refunded admissions do not count toward the 600; one network can make at most 300 a day (≤ 3,600 rows). About 28 networks doing so at once could spend the day's 100,000 (see "What this does not protect against") |
| rows read | 5,000,000 | denial ≤ 2 (measured); admission ≤ ~15 plus the in-flight leases | ≤ ~1.5M even if all 100,000 Worker requests were uncached analyses |
| duration | 13,000 GB-s | active only while handling calls | 0.125 GB × 86,400 s = 10,800 GB-s even if never idle |
| storage | 5 GB per account | 40 day rows, ≤ one row per active prefix, in-flight leases, one state row | < 1 MB |

Static snapshot: free and unmetered requests; about 600–900 files per version (fixture: 564 with two analyses; ~130 prewarmed analyses plus their canonical twins in production), under the 20,000-file limit.

### Staleness across releases

- **Data release** (ADR-013): the deploy sets a new `DATA_VERSION`, so `CACHE_SCOPE` and `SNAPSHOT_ID` change. Old KV entries are never read again and expire within 30 days (the storage bound includes them). The old snapshot directory disappears with the deploy; a tab still holding the old manifest gets `index.html` for a missing file and falls back to the API.
- **Mixed window** (API switched, Worker not yet deployed): the API reports the new `X-Data-Version`, the Worker still holds the old one, so the gate stores nothing. Existing old-version entries are still served, labelled with their own (old) version; the SPA shows its reload or "switching" notice on a version mismatch. It compares both versions: `X-Data-Version`, and the analytics version (`X-Analytics-Version` and each result's `analytics_version`) against the page's `/meta`, so a release that changes only `ANALYTICS_VERSION` is noticed as well (review round 2; before, only the data version was compared and such a release mixed two statistical implementations on one page under the old tag). The same holds after a failed edge step or an API-only rollback, and for a tab left open across a deploy.
- **Code-only deploy**: the scope is unchanged, so entries survive. Any change to analysis response bytes must bump the API's `RESPONSE_FORMAT` (it is part of both the gate and the scope). `EPOCH` is the manual purge.
  - **Preset definitions are not in the scope.** Requests name presets (`{"activity": {"preset": "watching_tv"}}`), so an entry keyed by such a request keeps the numbers of the definition it was computed with. Redefining a preset's codes in `ACTIVITY_PRESETS` (`src/atus_pipeline/analytics/spec.py`), or removing one, is therefore a statistical change and must bump `ANALYTICS_VERSION` ([analytics.md](../analytics.md)); changing only a label must bump `RESPONSE_FORMAT`. `tests/unit/analytics/test_preset_vocabulary.py` pins every preset's codes and label to that pair of versions and fails on any other change (review round 2); a new preset name needs no bump.
- **Neon project per release**: each release has its own project, so `NEON_PROJECT_ID` and the project-scoped `NEON_API_KEY` must be switched with the release. Until then the governor reads the old project's usage.

### Why no canonicalization at the edge

A JSON round trip is lossy (`1e400` becomes `null`, and `1.0` becomes `1`), so two different requests could map to one key and a malformed request could receive another spec's 200. Keying on exact bytes is always safe. The SPA sends the stable sorted-keys serialization (ADR-009), so starters, presets and share links hit. Semantic deduplication stays in the API (ADR-012, F15).

### Rejected alternatives

- **Cache API.** Its contents "do not replicate outside of the originating data center", and `put` resolves "regardless of whether the cache successfully stored the response". A per-colo cache would mostly miss on a site with a few dozen visits a day.
- **Workers Cache** (`[cache]`). Turning it on bills requests that are otherwise free, static assets included (ADR-011). CI asserts there is no `cache` block.
- **A cache table in Neon.** Every hit would spin up Render and wake Neon (≥ 600 CU-s per session under scenario C), cost Render outbound bandwidth, and need a write privilege that breaks the read-only `atus_api` role.
- **Metadata in KV.** The static snapshot serves it with no quota at all and survives the Worker's daily limit.
- **The Rate Limiting binding as the governor.** It is "local to the Cloudflare location", so it cannot enforce global daily caps (ADR-011).
- **Governor state in KV.** KV is eventually consistent (60 s or more) and allows one write per second per key, so concurrent admissions would race.

## Consequences

- **Statistics are unchanged.** An L1 hit is a byte-identical copy of an earlier origin response for the same globally unique data version, analytics version and response format (tested byte for byte, including gzip-encoded origins). L0 entries are the exact bytes of the deployed image on the release data.
- **Conservative mode is tight. PROJECTION.** Without a Neon key the daily ceiling is 2.903 CU-h = 10,452 CU-s, about 17 fresh wake sessions at the 600 CU-s minimum. Under `deployment.md`'s traffic model (16 Neon sessions a day, ~1.9 analyses 60 s apart, about 2 × 360 = 720 CU-s each) a typical day needs about 3.2 CU-h. So in conservative mode the last one or two sessions of a typical day would get `daily_capacity_reached`, while static and KV-cached results keep working. That is the price of charging every second at 2 CU without evidence. The daily quota report does not alert on it: its "Neon CU-h today" line is informational, because a typical day fills the conservative ceiling by design (review round 2; [`../deployment.md`](../deployment.md#monitoring-and-alerting-0)). The remedies are the project-scoped Neon key (ground truth: 10 CU-h a day, paced monthly) or ADR-011's calibration lever (a lower maximum CU, with the `NEON_MAX_CU` constant in `edge/src/constants.ts` changed to match).
- **The spike day is mostly refused, in both modes. Simulated.** `deployment.md`'s spike day sends 300 uncached analyses over about 12 h, one every 144 s. Each arrives inside the previous one's 300 s tail, so the charge runs without a break: 24 CU-h against a ceiling of 2.903 or 10 CU-h, while Neon itself would use about 3 CU-h under scenario A. Played through the Worker against the governor (`edge/test/worker/spike-day.test.ts`), conservative mode serves 34 of 300 (first refusal after 4,896 s) and fresh ground truth 123 of 300 (after 17,712 s). The rest get `daily_capacity_reached` (budget `cu_day`); static and cached results keep working ([`../deployment.md`](../deployment.md#the-spike-day-under-the-governor)). Charging less than 2 CU per second, or deriving the daily ceiling from the monthly pacing headroom, would serve more; neither is implemented.
- **`Server-Timing` matters.** Until the API sends `db;dur=` on every response (and `db;dur=0` when it touched no DB), each settled analysis is charged from admission, including any Render spin-up. In the local run against the API branch of 2026-10-20 the header was absent.
- **Operations.** One more secret (`IP_HMAC_KEY`) and one optional secret (`NEON_API_KEY`), one var (`NEON_PROJECT_ID`), and `/api/_ops/budget` for the owner and the daily quota report.

## Failure modes

| Failure | What happens | Visitors see |
|---|---|---|
| KV read fails or KV reads exhausted | treated as a miss; the governor decides | slower answer, or a governor denial |
| KV write fails or writes exhausted | ignored | nothing; the result is not edge-cached |
| KV eventual consistency (60 s or more between locations) | an extra miss, one more governed analysis | nothing; never a wrong result (keys are version-scoped) |
| Two identical requests miss at once | both are admitted; the API's single-flight joins them (`join` is refunded); the second write to the same key may fail (1/s) and is ignored | nothing |
| Governor errors, times out (5 s) or its quota is spent | uncached analyses fail closed | 503 `capacity_unavailable`; everything else works |
| Settle lost (the invocation died, or was cancelled more than 30 s after its client disconnected) | the lease expires after 10 minutes and is charged to the API's deadline as part of the union, counts kept; leases lost together share one window | possibly an earlier `daily_capacity_reached` that day |
| Neon poll fails | last ground truth kept; after 24 h the conservative ceiling applies; the error name shows in `/api/_ops/budget` | possibly earlier `daily_capacity_reached` |
| Snapshot build hits an error response | the build fails; nothing is published | the previous deploy stays live |
| Mid-body failure while buffering for KV | 502 `upstream_unavailable` (`reason: broken_response`); nothing stored; the analysis is charged as computed | a retryable error; the SPA retries it once (it joins or hits the API's cache) |
| uvicorn's own connection-limit 503 (plain text `Service Unavailable`) | 503 `overloaded`, `Retry-After: 10`, `scope: global`; refunded (the API never ran) | the API's own "busy" copy |

## What this does not protect against

- **The Worker's account-wide 100,000 requests per day.** Replaying KV hits (5 req/s for ~5.6 h) spends them without touching the governor; `/api` is then down until 00:00 UTC. Only static files survive. On `workers.dev` at $0 nothing can rate-limit before the Worker runs.
- **Distributed abuse within the global caps.** A few networks can use up the day's shared budgets: ten at their 60 spend the 600 uncached analyses, five sending heavy analyses spend the 300 heavy, and five spacing analyses so that each is charged its own wake session spend the compute ceiling (each network is held to a fifth of it, row 4). Real visitors then get `daily_capacity_reached` until 00:00 UTC. One network alone no longer can. About 28 networks, each making its 300 admissions a day that end refunded (requests the API sheds, or sent while Render wakes), could also spend the governor's 100,000 rows written; the governor then fails closed (`capacity_unavailable`) until 00:00 UTC.
- **Shared networks.** Everyone behind one IPv4 `/24` (a campus, a carrier NAT) shares 60 analyses a day and a fifth of the day's compute (about 11 minutes of continuous novel analyses in conservative mode, row 4); an attacker with many `/48`s gets that much each.
- **Direct-origin traffic.** Requests to the onrender.com host bypass the Worker; ADR-012's layer in the API answers them (403 without the edge secret).
- **Model error.** The charge model is an upper bound only while the compute's maximum is at most `NEON_MAX_CU` (2 CU, a constant in `edge/src/constants.ts`) and every DB-touching request goes through the governor. Neon's availability-check wakes are not counted (ground truth catches them), nor is a connection attempt that woke Neon but failed anyway, after the API's 15 s connect budget or refused by the compute itself (a database name that does not exist, say): the API got no connection, reports `db;dur=0`, and the answer is refunded (review round 3). Ground truth catches these too; without it, they last only as long as the misconfiguration or the outage, which `database_unavailable` reports. Egress between polls, and the whole daily egress budget without ground truth (row 6), uses per-analysis estimates of 64 KiB light and 512 KiB heavy (`EGRESS_ESTIMATE_BYTES`). They are sized from DB-to-API bytes measured on the full database in review round 1 ([byte counts](../evidence/phase5/review-r1-bytes/README.md)): 61,892 B for a broad 7-year light analysis (pooled or trended), 392,464 B for a 22-year compare, and up to 242,394 B for one side of a narrow 22-year population, about a quarter more per year than the broad one; a narrow 7-year light analysis was not measured and may pass 64 KiB. They are estimates, not bounds. The per-process snapshot read (108,005 B at each API cold start) is charged as 128 KiB to a metadata lookup that reports DB time, but not to an analysis that triggers it (its estimate covers its own query); ground truth corrects both. Without ground truth the heavy cap and row 6 are the only egress bounds, and the 4.5 GB monthly stop needs ground truth.
- **Cost-class blind spots.** Single-year broad selectors (F66) are "light" for the heavy count, though their real compute time is still charged.
- **DB-touching GET routes.** The Worker answers the metadata routes from the static snapshot; they reach the API only as governed lookups (a query string, a code the snapshot does not hold, no snapshot deployed). They no longer keep Neon awake: with `ATUS_EXPECTED_DATA_VERSION` set, the API's in-process snapshot is pinned and never re-read for its age, and every lexicon search, non-ASCII terms included, is answered in memory (ADR-012, "Metadata reads and the quota"). What remains is one snapshot read per API process, on its first data request, which can wake Neon after each Render spin-down. Through the Worker that request is an analysis or a lookup, and since review round 2 both are charged their wake session when the API reports DB time, against every compute budget. Before, lookups were only counted: a client that timed one after each spin-down caused about 4 uncharged Neon wakes an hour (PROJECTION: at most about 2 CU-h a day at 0.25 CU), which in conservative mode no governor figure showed. What remains is the charge model's error (above).
- **Render outbound through the Worker.** Bounded per day as follows. PROJECTION from measured sizes:
  - analyses that keep their counts: ≤ 600 × ~40 KB (SQL text to Neon plus the gzipped answer), ≈ 0.74 GB a month;
  - metadata lookups: ≤ 2,000 × ~9 KB (the whole lexicon, gzipped; the Worker always asks for gzip), ≈ 0.6 GB a month;
  - `/api/health`, which is not governed: 571 B each from the production-mode API itself (505 B of headers, a 66 B body; [counted in review round 2](../evidence/phase5/review-r2-bytes/README.md)), ≈ 1.8 GB a month even if one client spent the Worker's whole 100,000 requests a day on it for a month. Render's proxy headers come on top and are unmeasured. (Review round 2 replaced an earlier estimate of half a kilobyte per answer, which no measurement supported.)

  That is about 3.1 GB of the 5 GB in the worst case before Render's own headers, so no single client can suspend Render for the month as long as Render adds less than about 600 B to each answer (every 100 B more adds 0.31 GB a month); ADR-011 "Unknowns" measures it after deploy. Admissions that end refunded (Render waking, the API shedding, a 4xx decided before any statement, a reuse) are bounded per network only, at 300 a day with answers of a few KB each, about 30 MB a month per network. It would take dozens of networks, sustained for the whole month, to spend the rest (R1 in `../security.md`). Direct requests to onrender.com are outside the Worker (ADR-011, accepted risk 8).

## Verification

- Worker tests (Vitest; counted on 2026-10-21 at f208719 with `vitest list`: 420 tests across the edge suite, 390 in workerd and 30 in Node; later commits add more, and CI's test report has the current count): the KV key format against independent hash vectors, every gate condition, byte-exact replay, cache-off and KV-failure paths; every governor budget and denial envelope, refunds per outcome, the compute model (including analyses settling out of order), lease expiry, ground truth polling cadence, pacing and egress stops, the midnight purge, failing closed (including a 5 s timeout), the hidden ops endpoint; real-socket integration from a gzip-encoding origin through KV. The row counts above come from `governor.test.ts`.
- Snapshot builder (counted on 2026-10-21 at f208719 with `pytest --collect-only`): 60 unit tests against a fake API, and 3 integration tests against the real API on the fixture database (every file byte-identical to a fresh API response; an API 422 refuses the build).
- Local end-to-end, 2026-10-20: `wrangler dev` (local mode, no account) with the built SPA and a snapshot, and the API from the WS-A branch on the fixture database. Checked: SPA fallback for deep links; static assets and `/snap/**` served without invoking the Worker (a probe Worker answering 418 saw only `/api` and `/api/*`); `/api` proxying with security headers and no `server` header; KV miss then a byte-identical `X-Cache: edge` hit; the 61st uncached analysis from one network (60 admitted) refused with 429 `rate_limited` and `Retry-After` to midnight without an origin call; a KV hit still served after that; 404 for `/api/_ops/budget` without the bearer token. Wrangler's telemetry stayed off ("Dispatching disabled"); its only external request was its own npm update check. A second, independent run (verification review, same day, API from the WS-A branch with `Server-Timing`) also checked: the snapshot built with the generator path given relative to the working directory; an edge hit byte-identical to the snapshot entry for the same request; an API 422 envelope byte-identical through the edge; an uncached analysis charged from its `Server-Timing` (one 600 CU-s session); no store when the API's data version differs from the Worker's; with the API's default 6-per-minute limit, its 429s refunded and the network refused at the edge after 300 admissions; against a scratch echo origin, the exact upstream header set with forged client `X-ATUS-*`, `X-Forwarded-*`, `Forwarded`, `X-Real-IP`, `Cookie` and `Authorization` headers dropped, origin `Set-Cookie`, `x-render-*`, `rndr-id` and CORS headers dropped, and a 524 answered as 502 and charged as computed.
- Not verifiable locally: real KV propagation, the Durable Object's behaviour under Cloudflare's eviction and quotas, the Neon endpoint's real responses and lag, Workers CPU per request.

## Cited terms (accessed 2026-10-20)

| Term | URL | Quote or value |
|---|---|---|
| KV Free: 100,000 reads, 1,000 writes, 1,000 deletes, 1,000 lists per day; 1 GB; failures, not billing; missing-key reads count | https://developers.cloudflare.com/kv/platform/pricing/ | "All operations incur charges, including fetches for non-existent keys." |
| KV: 1 write per second to the same key; key 512 B; value 25 MiB; metadata 1,024 B; minimum `cacheTtl` 30 s | https://developers.cloudflare.com/kv/platform/limits/ | "1 per second" |
| KV is eventually consistent | https://developers.cloudflare.com/kv/concepts/how-kv-works/ | "may take up to 60 seconds or more to be visible" (ADR-011) |
| Durable Objects Free: 100,000 requests, 13,000 GB-s, 5 million rows read, 100,000 rows written per day, 5 GB; alarms count as requests; `setAlarm` is a row written; deletes are rows written | https://developers.cloudflare.com/durable-objects/platform/pricing/ | "Includes HTTP requests, RPC sessions, WebSocket messages, and alarm invocations." |
| Durable Objects Free: SQLite backend only; 5 GB per account; per-object storage stated as 10 GB in the table and 1 GB in the FAQ (we use < 1 MB) | https://developers.cloudflare.com/durable-objects/platform/limits/ | "Only Durable Objects with SQLite storage backend are available." |
| Cache API is per data center; `put` resolves either way | https://developers.cloudflare.com/workers/runtime-apis/cache/ | "the contents of the cache do not replicate outside of the originating data center" |
| Neon project endpoint: `compute_time_seconds`, `data_transfer_bytes`, `consumption_period_start` / `_end`; Bearer API key | https://api-docs.neon.tech/reference/getproject | "Egress traffic from the Neon cloud to the client for given project over the billing period." |
| `compute_time_seconds` is CU-seconds; divide by 3600 for CU-hours; Free plans read usage from the project endpoint | https://neon.com/docs/introduction/usage-calculations | "Divide by 3600 for CU-hours." |

## Amendments (implementation, 2026-10-21)

The decisions are unchanged. This section records where the implementation differs from the text above, and why. Nothing is deployed.

- **`Server-Timing` is sent.**
  - The API adds `Server-Timing: db;dur=…, app;dur=…` to every response (`src/atus_pipeline/api/middleware.py`; [ADR-014](ADR-014-observability.md)).
  - The Worker relays it and logs it (`edge/src/proxy.ts`, `edge/src/index.ts`), and the governor sizes the settle charge from `db` as specified.
  - The sentence under "Consequences" saying the header was absent describes the first local run only.
- **Where L0 is built.** In `.github/workflows/deploy.yml`, job `snapshot`, before the `production` approval and before any deploy step. Its steps:
  1. It downloads the release named in the committed `releases/current.json` and verifies it ([ADR-013](ADR-013-data-releases-and-serving-db.md), amendments).
  2. It restores the dump into a `postgres:16` service as a non-superuser owner, with `scripts/ops/roles.sql`.
  3. It runs the pushed digest read-only as the `atus_api` role: `--read-only`, production mode, the release's `ATUS_EXPECTED_DATA_VERSION`, and per-client limits raised for the builder.
  4. It builds the SPA and the snapshot (`scripts/build_snapshot.py`).
  5. It hands `frontend/dist` to the `deploy` job as the artifact `site-dist`, kept 30 days so that a late approval still finds it.

  A local rehearsal against release 0325 is recorded in [`../deployment.md`](../deployment.md#7-first-approved-deploy-ci), step 7. On fixture data, `scripts/smoke_stack.sh --edge` (and the `ci.yml` job `e2e-stack`) builds a snapshot with `--skip-refused`.
- **`NEON_PROJECT_ID` is a committed var.**
  - It is empty in `edge/wrangler.jsonc`, and `deploy.yml` does not set it. A deploy therefore runs the governor in conservative mode until the owner commits the project id and loads `NEON_API_KEY` with `wrangler secret put`.
  - The same edit is due at every release that moves to a new project ("Staleness across releases").
  - `keep_vars` is not set, so a value set only in the Cloudflare dashboard would not survive the next deploy.
- **Who reads the ops report.**
  - `.github/workflows/quota-report.yml` reads `/api/_ops/budget` daily with `EDGE_OPS_TOKEN`, and fails above 0.7 of a pro-rated budget (`scripts/ops/monitors.py`).
  - `.github/workflows/edge-check.yml` reads `/api/_edge/health` and compares its `snapshot_id` with `/snap/current.json`.
- **The SPA's fallback.** The SPA reads metadata from the snapshot only while the snapshot is readable. Any problem reading it falls back to the API (`frontend/src/api/snapshot.ts`). The Worker answers those GETs from the same snapshot when it can read it; otherwise they become counted lookups, which can trigger the per-process snapshot read described under "DB-touching GET routes".
- **Verified locally only.** Everything under "Verification" ran under `wrangler dev` in local mode, with KV and the Durable Object simulated on disk.
  - In the production-mode stack, the Worker reaches the API through the loopback relay `deploy/edge/origin-relay.mjs`. The relay exists only in that stack ([ADR-011](ADR-011-hosting.md), amendments).
  - The CI job that repeats this check (`e2e-stack`) has no recorded run.
  - The items under "Not verifiable locally" remain unverified.

## Amendments (review round 1, 2026-10-21)

The review's confirmed edge findings changed the decision text above in place; this list says where. Nothing is deployed. Each change has a Worker test that failed before it (`edge/test/worker/`; 468 tests in the edge suite after the change and its verification, from 420).

- **Per-network compute share** (budget row 4; `governor.test.ts`, "each network's share of the day's compute"). One network spacing cheap analyses could spend the whole daily compute ceiling within its 60. The share also holds a real visitor's continuous run to about 11 minutes a day in conservative mode (row 4, "a real visitor's continuous run").
- **Metadata at the edge and metadata lookups** ("Static snapshot (L0)", "Quota governor"; `metadata.test.ts`). Every metadata GET used to reach Render, with the client's query string and `Accept-Encoding`, with no cache or governor. The Worker now also asks the origin for gzip on every call (`headers.test.ts`, `integration.test.ts`).
- **Expired leases charged as part of the union, settle queued before the origin call** ("Compute charge model", "Failure modes"; `governor.test.ts`, `governor-flow.test.ts`). Each expired lease used to cost a full 780 CU-s, and the settle was queued only after the origin answered.
- **Daily egress budget without ground truth; refusals that ran the statistics query keep their counts; measured egress estimates** (rows 6–7, "Settling and refunds", "Model error"; `governor.test.ts`, "Neon egress without ground truth"). In conservative mode nothing bounded Neon egress, and 422s that ran the whole query were refunded from the heavy cap.
- **KV stores `X-Cache: miss` only** ("KV store gate"; `cache.test.ts`). Hits were stored while refunded, so KV writes were not bounded by the 600.
- **Data-availability refusals settle as `shed`** ("Settling and refunds"; `admission.test.ts`, `governor-flow.test.ts`). `database_paused` was charged ~610 CU-s per attempt and kept its counts.
- **Provisional charge is an estimate** (under the budget table). The text promised that the request crossing a budget could not pass it; a settle later than 90 s without `Server-Timing` can.
- **Prewake** (`edge/src/wake.ts`, [ADR-011](ADR-011-hosting.md) "Prewake"). An isolate that saw the origin waking re-probes after a minute, not 10; `wake.test.ts`.

## Amendments (review round 2, 2026-10-21)

Confirmed findings of the second review round. Nothing is deployed. Each change has a test that failed before it.

- **Metadata lookups are charged when they reach the database** ("Metadata lookups", the quota tables, "Model error", "DB-touching GET routes"; `governor.test.ts`, "metadata lookups that reach the database"; `metadata.test.ts`, "charge a lookup that woke the database"). A lookup was only counted, but the first one on a freshly started API process runs its snapshot read on Neon: one network sending a lookup after each Render spin-down woke Neon about 90 times a day with no charge, also past the daily ceiling, its compute share, the pacing line and the 85 CU-h stop. Lookups now pass the compute and egress checks and are settled like analyses.
- **Preset definitions are pinned to the cache scope's versions** ("Staleness across releases"; `tests/unit/analytics/test_preset_vocabulary.py`). Redefining a preset needed no version bump, so KV kept its old numbers for up to 30 days.
- **The owner's pause switch** (`edge/src/pause.ts`, [ADR-011](ADR-011-hosting.md) amendments, "Worker details"; `pause.test.ts`). It answers 503 `api_paused` after the L0 and L1 tiers and before the governor, so snapshot metadata and KV hits keep working and a paused API spends no governor rows.

## Amendments (review round 3, 2026-10-21)

Confirmed findings of the third review round. Nothing is deployed. Each change has a test that failed before it.

- **A failed database connection is refunded, not charged** ("Settling and refunds", "Model error"; `admission.test.ts`, "reports database_unavailable without a connection"; `governor-flow.test.ts`, "lets a run of failed database connections lock out neither a network nor the day"; on the API, `tests/unit/api/test_edge_contract.py::TestServerTimingForTheGovernor::test_a_request_that_got_no_connection_reports_no_database_time`, `test_database_unit.py::TestCheckout`, and the outage test in `tests/integration/api/test_snapshot_db.py`). The API timed a failed connect as DB time, and the Worker settled 503 `database_unavailable` as computed: it kept the counts and charged a wake session (about 610 CU-s at the model's 2 CU; the SPA's retry merged into it) while Neon was unreachable, refused the credentials or failed TLS. In conservative mode a network trying every 6 minutes (an attempt and the SPA's retry) got 429 from its fourth attempt, and about 16 networks closed the day's compute for everyone until 00:00 UTC, also after the connection was fixed. The API now counts a transaction's time only once the pool gave it a connection (`observability.database_time`), and `database_unavailable` settles as `shed`; a connection that broke after the checkout still reports its DB time and is charged.
- **The governor's key is tested against forged forwarding headers** (`governor-flow.test.ts` and `metadata.test.ts`, "whatever forwarding headers the client adds"; `deploy/stack/edge_probe.py`, check `governor_denial`). The code keyed on `CF-Connecting-IP` already, but no test sent a forged header to the governor: keying it on `X-Forwarded-For` passed every Worker test ([security.md](../security.md), abuse case 5).

## Amendments (review round 4, 2026-10-21)

Confirmed finding of the fourth review round. Nothing is deployed. Each change has a test that failed before it.

- **What a compromised Worker version leaves behind no longer survives the recovery** ("Keys and version scope", "KV store gate", "Quota governor", "Write privileges"; `compromise.test.ts`; [`../deployment.md`](../deployment.md#rotation), "Rotation", item 5; [`../security.md`](../security.md) C6, C10 and R16). The documented response to a leaked Cloudflare token was to rotate the secrets and bump `EPOCH`. Code deployed with the token could leave state that outlived both:
  - **KV entries for the next scope.** Every scope input is public (`/api/_edge/health`), `EPOCH` is a small integer, and a read checked only the metadata against those public values, so entries planted for `EPOCH` 2 with no TTL were served, as `X-Cache: edge`, by the very bump meant to purge them. Every entry now carries an HMAC tag under a key derived from `IP_HMAC_KEY`, over its key, metadata and body, and an entry whose tag does not verify is a miss; the recovery rotates `IP_HMAC_KEY`, so no entry that code wrote verifies, wherever it was written. An entry copied under another request's key, or given another body, no longer verifies either.
  - **Governor state.** A Durable Object's storage outlives every Worker version, and the governor trusted it: rows for future days at the caps refused those days, a charged window to the end of time switched off the daily compute ceiling and each network's share, and planted Neon ground truth was used even with no Neon key configured. The instance is now named by an HMAC of `IP_HMAC_KEY`, so the rotation moves the Worker onto a fresh one. Every instance also deletes rows dated after today and impossible leases when it starts (every deploy restarts it), treats a count that is not a non-negative number as spent, and on every load drops charged windows that end later than any settle could reach and Neon ground truth that no poll could have produced; ground truth counts only while a Neon key is configured. A rotation of `IP_HMAC_KEY` therefore also restarts the day's counts (the day of a rotation can admit up to its daily budgets again) and orphans every KV entry.
  - **Browser caches.** `/assets/*` names are content hashes of the public source and are cached as immutable for a year, so such a version could serve its own files under the names the recovery build would have. The recovery deploys a new commit with a fresh `ASSET_SALT` (`frontend/src/deploy/assetNames.ts`, `assetNames.test.ts`), which changes every bundle name, and a new `SNAPSHOT_ID`. A browser that kept such a version's page itself, or a service worker it registered, is beyond the origin's reach ([`../security.md`](../security.md) R16).
