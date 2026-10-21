# ADR-011: $0 hosting on Cloudflare Workers, Render Free and Neon Free

**Status:** Accepted (2026-10-20) · Phase 5

**Related:** ADR-012 (load and quota protection), ADR-013 (data releases and the serving DB), ADR-014 (observability) and ADR-015 (persistent result cache) are written during Phase 5 implementation. Where this ADR sets a constraint that belongs to one of them, it says "to be finalized in ADR-01x".

**Scope and honesty.** Nothing described here is deployed yet. Every number is one of two kinds:

- **measured**, from the Phase 5 audit ([`../operations.md`](../operations.md) and [`../evidence/phase5/01-audit-timed.md`](../evidence/phase5/01-audit-timed.md));
- a **PROJECTION**, labelled as such, with its assumptions stated.

Free-tier terms were read on official pages on 2026-10-20. A researcher and an independent checker verified them, and the five proposals re-verified them again. They are listed with URLs under "Cited free-tier terms". Where the fact sheet disagrees with the brief's §3, the fact sheet wins.

## Context

### Hard constraints

1. **$0, forever, with no payment card at any step.** The only acceptable failure mode for a quota is "paused until it resets", never billing.
2. **No purchased domain.** Provider hostnames only.
3. **Statistics unchanged.** Everything served must match the full database under the §9 equivalence criterion.
4. **The owner does all account actions.** The owner creates every account, accepts every terms page and authenticates every CLI.

### Measured facts that drive the design

| Fact | Value | Audit ref |
|---|---|---|
| API memory | ~70 MB peak (VmHWM 70,364 kB after 300 distinct analyses) of 512 MB | I09 |
| API container cold start at 0.1 CPU, before the platform's own spin-up | first `/health` p50 7.65 s; first `/meta` up to 11.04 s with the DB at 0.25 CU | F67 |
| Share of request time spent in the DB at 0.25 CU (`jit=off`) | ≥ 98.4% of every uncached request (≥ 99.4% for worst cases); the API adds 7–17 ms (96–189 ms for trends) | 7.9 |
| Worst cases at Neon's 0.25 CU floor (`jit=off`) | pooled 22 years p50 51.7 s; 23-year trends p50 46.9 / 48.5 s; read as a band of ~33–52 s. With JIT on: 64–71 s | F10, F37 |
| Worst cases at 2 CU | 2.7–3.4 s | timed (b) |
| Single-year estimate with SE | p50 6.95 s at 0.25 CU, 0.38 s at 2 CU | timed |
| Saturation | DB CPU saturates from 2 concurrent uncached analyses at 0.25 CU; at 16 concurrent, 6 × 503 after 10 s | F35 |
| Probes (adoption needs the §9 proof) | parallel query off: 40.6 / 39.5 s → 19.2 / 19.1 s; year pushdown: single-year 2.90 / 3.39 s → 0.50 / 0.90 s | F38, F39 |
| Costliest specs on the serving DB | broad tier-1 selectors (all 18 codes pooled over 22 years) cost ~2.6× pooled leisure (illustrative) | F66 |
| Statements per request today | uncached estimate/trend 9, compare 10, cache hit 3, `/meta` 5, `/ready` 4; every cache hit and 422 touches the DB | F14 |
| Serving DB prototype | 598.4 MiB database; cluster sum 650,458,708 B ≈ 0.65 GB; dump 336.5 MB | I04 |
| Analysis response size | p50 1,108 B, p95 8,317 B, max 8,383 B (300 responses); `/api/v1/activities` is 70,851 B uncompressed | timed, F42 |
| Pool behaviour | no idle traffic; `pool.open()` connects at start, which wakes the DB on every API cold start; a dead pooled connection gives one 503 per connection | I08, F05 |
| Writes | the API never writes, so a read-only role works | I05 |

**Not emulated** (F41): Neon pageserver reads, wake from suspend, how fast autoscaling reacts, Render↔Neon round-trip time, Render's real CPU share and its spin-up.

### What the free tiers force

- **Size.** The 1,980 MiB full database fits no no-card managed Postgres free tier. The ~0.65 GB serving DB fits Neon (1 GB per project) and Prisma (2 GB per workspace).
- **Sleep.** Every free API host sleeps or scales to zero:
  - Render spins a service down after 15 idle minutes and takes about a minute to spin it back up.
  - Neon suspends after 5 idle minutes, and this cannot be disabled.
- **Quota horizons.** Every quota pauses rather than bills, but the length of the pause differs:
  - Cloudflare Worker requests and KV operations reset at 00:00 UTC each day.
  - Render and Neon quotas reset monthly.
  - Vercel waits "until 30 days have passed".
  - Render may suspend a Free service for "uncommonly high" traffic that the service itself initiates, and names accessing an external database as an example. The only documented way back is a paid plan.
- **Share links.** The public hostname is baked into every share link (ADR-009). It must therefore sit in front of every component that might change.

### Corrections to the brief's §3 that this decision relies on

- **Edge caching.**
  - The Cache API's workers.dev restriction was removed from Cloudflare's docs (2025-03-05). The Cache API is still per data center, so it is not used here.
  - Workers Cache (`[cache] enabled`) is available on Free, but turning it on bills requests that are normally free, including static assets. It must stay off.
- **After 100k Worker requests per day.** Requests matching `run_worker_first` get a 429 (static-assets billing page), or an Error 1027 page whose HTTP status is undocumented.
- **Render.**
  - The 5 GB of outbound bandwidth is per Hobby workspace and includes API→database traffic.
  - The brief left out a sentence: the only documented way back from a service-initiated-traffic suspension is a paid plan.
  - The reported card prompts appeared mostly when deploying, not only at sign-up.
  - Creating an extra workspace asks for a payment method.
- **Neon.**
  - The 90-day inactivity deletion applies only to the deprecated Azure regions, so no database keep-alive is needed in AWS.
  - New Free projects default to autoscaling 0.25–2 CU.
  - Every new connection resets the 5-minute suspend timer.
  - Storage is metered as `logical_size` summed across branches. Whether "1 GB" means 1e9 bytes or 1 GiB is undocumented.
  - Waking can take "a few seconds".
- **Prisma.** All Free limits are workspace-wide. Free storage went from 500 MB to 1.01 GB to 2 GB within October 2026. The FAQ calls Free "evaluation only".
- **Vercel Hobby.** Besides Active CPU, Hobby also caps Provisioned Memory at 360 GB-h, which keeps counting while a function waits on the DB, and invocations at 1M. Going over pauses the feature for 30 days.
- **GitHub.** Node 20 was removed from hosted runners on 2026-09-23.

## Decision

Adopt proposal **P5** from the judge panel: Cloudflare Workers edge, Render Free API, Neon Free database, a static-plus-KV result cache and a Durable Object quota governor. It is amended by the grafts listed under "Judge panel".

### Components and hostnames

| Role | Provider and plan | Hostname |
|---|---|---|
| Public origin: SPA, static result snapshot, `_headers` | Cloudflare Workers Free, static assets | `https://<worker>.<account-subdomain>.workers.dev` (recommended `<worker>` = `atus`); the only hostname in share links |
| `/api/*` proxy, edge authentication, response headers | Cloudflare Workers Free, the same Worker script | same origin |
| Persistent result cache | Workers KV Free, one namespace | binding only |
| Quota governor | one Durable Object with SQLite storage, Workers Free | binding only |
| API | Render Free web service (0.1 CPU / 512 MB), image-backed, in the Hobby workspace created at sign-up | `https://<service>.onrender.com`. Recommended service name `atus-api-<6 random chars>`; Render may add a suffix. Never linked or published |
| Database | Neon Free, Postgres 16, AWS US East 1 (`aws-us-east-1`), autoscaling 0.25–2 CU, direct endpoint, one project per data release | `ep-<endpoint-id>.us-east-1.aws.neon.tech:5432` (never the `-pooler` host) |
| Image registry | GitHub Container Registry, public package | `ghcr.io/nathan98000/atus-api@sha256:<digest>` |
| CI/CD and monitors | GitHub Actions in the public repo; environment `production` with a required reviewer | — |
| Backup artifact | GitHub Releases, with release immutability on | `github.com/Nathan98000/ATUS/releases` |

In short:

- One workers.dev origin serves the SPA. It also serves a build-time static snapshot of all metadata and about 130 prewarmed analyses, free and without a request quota.
- Everything else goes through an origin-locked Worker. The Worker checks a KV cache keyed on the exact request bytes, then a quota governor in a Durable Object, before forwarding `/api/*` to a single Render Free instance.
- That instance reads the ~0.65 GB serving DB on Neon Free over the direct endpoint.

### Request flow

1. **Browser to Cloudflare.** The browser connects over TLS with Cloudflare's workers.dev certificate. HSTS comes from `_headers`, and `.dev` is HSTS-preloaded.
2. **Static paths.** These are `/`, `/explore`, `/analysis/*`, `/about`, `/assets/*` and `/snap/<SNAPSHOT_ID>/**`. A request for a file that exists (`/`, `/assets/*`, `/snap/…`) never invokes the Worker and never counts toward the 100k/day limit, and neither does a navigation (`Sec-Fetch-Mode: navigate`) that matches no file, such as `/explore`, `/about` or `/analysis/*`, which have no file of their own.
   - Unknown paths get `index.html` with a 200 (SPA mode).
   - **Corrected in review round 2:** a non-navigation request that matches no file does invoke the Worker and is metered (Cloudflare's [SPA routing](https://developers.cloudflare.com/workers/static-assets/routing/single-page-application/) page, read 2026-10-21: "navigation requests will not invoke the Worker script", and "Requests are only billable if a Worker script is invoked"). That is a link-preview unfurler, crawler or `curl` fetching a share link or an SPA route, the deploy smoke's deep link, and a tab still on the previous release fetching a `/snap/` file the new deploy no longer has. The Worker hands such a request to its assets binding (`edge/src/index.ts`), which answers with the SPA fallback; once the 100k/day are spent, it gets the platform's 429 or Error 1027 instead. Under `wrangler dev` (4.148) even a plain `curl` was handled as a navigation in review round 2, so the local stack cannot show this ("Unknowns").
   - The SPA reads all metadata from `/snap/…`: `/meta` content, population metadata, the activity lexicon and details, and presets. It also reads from there any analysis listed in the snapshot manifest.
3. **`/api` and `/api/*`.** These match `run_worker_first` (both forms are listed) and invoke the Worker.
   1. **Allowlist.** The routes below are allowed. Anything else gets a v1 404 envelope with no origin call, and `/ready`, `/docs`, `/redoc` and `/openapi.json` are never routed.
      - GET `/api/v1/{meta, activities, activities/presets, activities/<code>, population/metadata}`;
      - POST `/api/v1/analysis/{estimate, trend, compare}`;
      - GET `/api/health`, which goes to origin `/health`;
      - handled by the Worker itself: GET `/api/v1/wake` (prewake), GET `/api/_edge/health` (no origin call) and GET `/api/_ops/budget` (bearer token).
   2. **Limits.** A body over 64 KiB gets a 413 envelope. A POST must be `application/json`.
   3. **Upstream headers.** The Worker builds a fresh upstream header set from an allowlist:
      - forwarded from the client: `Content-Type`, `Accept`, `If-None-Match`;
      - set by the Worker: `Accept-Encoding: gzip` (always, whatever the client accepts: the runtime decodes the body, and an uncompressed answer costs several times the Render outbound; review round 1), `X-Request-ID` (generated), `X-ATUS-Client-IP` (copied from `CF-Connecting-IP`), `X-ATUS-Edge-Auth` (a Worker secret) and `User-Agent: atus-edge/<version>`.

      Client-sent `X-ATUS-*`, `X-Forwarded-*`, `Forwarded`, `X-Real-IP`, `Cookie` and `Authorization` headers are never forwarded.
   4. **Edge cache lookup (analysis POSTs).** The Worker reads the body bytes once and computes `k = sha256(path + "?" + query + "\n" + body)`. It then looks up KV key `r1/<CACHE_SCOPE>/<k>`. On a hit it returns the stored bytes with `X-Cache: edge`, and Render and Neon are not touched.
   5. **Governor admission.** On a miss the Worker calls `admit(ip_prefix_hmac, cost_class)` on the governor.
      - A denial returns a 429 `rate_limited` or a 503 `daily_capacity_reached` / `capacity_paused`, with `Retry-After`. Render is not contacted.
      - If the governor is unreachable, the Worker fails closed: uncached analyses get 503 `capacity_unavailable`, and everything else keeps working.
   6. **Forward.** Admitted requests go to `fetch(RENDER_ORIGIN + path)`.
      - The Worker sets no timeout of its own. Its only retry is one attempt when the connection fails before any response headers arrive; all routed calls are idempotent reads.
4. **Worker to Render.** Render sits behind its own Cloudflare zone, so this is a cross-zone hop. Render's load balancer ends TLS and forwards plain HTTP to the container on `$PORT` (10000). This is the only non-TLS hop, and Render documents it.
5. **API (one uvicorn process).** The request passes through these stages in order:
   - body cap;
   - edge-auth check: constant-time, with two accepted values for rotation. Failure returns 403 `forbidden_origin` with 0 DB statements; `/health` is exempt;
   - client IP taken from `X-ATUS-Client-IP`;
   - per-IP token buckets;
   - validation and cache lookup against the in-process `data_version`, with 0 DB statements;
   - single-flight join of identical in-flight requests;
   - concurrency lanes;
   - engine;
   - Neon.
6. **API to Neon.** psycopg 3 connects over TLS to the direct endpoint as role `atus_api`, with `sslmode=verify-full` and SNI. The role is SELECT-only and carries the role defaults listed below.
7. **Response path.**
   - The API adds security headers, `X-Data-Version`, `X-Analytics-Version`, `X-Analysis-Key`, `X-Cache` and `X-Request-ID`, and gzips responses of 1 KB or more.
   - The Worker passes JSON envelopes through byte for byte.
   - It converts responses that are not API envelopes into JSON 503s: Render's hibernate 503 or HTML loading page becomes `service_waking` (`Retry-After: 5`), and a Cloudflare 52x becomes `upstream_unavailable`.
   - It sets its own security headers, because `_headers` does not apply to Worker responses: `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, HSTS, `nosniff`, `Referrer-Policy`, CORP `same-origin` and `Cache-Control: no-store`.
   - It strips `server`, `x-render-*` and `rndr-id`.
   - Through `waitUntil` it then stores the response in KV if the store gate allows it, and settles the governor charge.

**Prewake.** The SPA sends GET `/api/v1/wake` only on user intent: entering Explore, or opening a share link that is not in the snapshot.

- The Worker races `fetch(origin /health)` against 1.5 s and replies `{origin: warm|waking}`. If the origin is waking, the fetch stays alive through `waitUntil`, for at most the 30 s Cloudflare allows after the response; a longer spin-up is recorded as still waking.
- A wake is sent at most once per isolate per 10 minutes once the origin answered warm, and at most once a minute while it was waking or unknown, so a stale "waking" does not keep the cold-start notice up (review round 1). The Worker var `PREWAKE=off` turns the feature off.
- A wake never touches Neon.

**Coldest path for a novel analysis.** PROJECTION: Render spin-up (~1 min, documented) + app start (7–11 s, measured; whether Render's minute includes it is unknown) + Neon wake (a few hundred ms to a few s) + the query (0.4–3.4 s if the autoscaler reaches 2 CU; up to ~52 s at 0.25 CU). That is about 65–80 s in the typical case and up to ~125 s in the worst.

- Prewake overlaps the spin-up with the time the user spends building a spec.
- The SPA shows the waking message in a polite live region when a metadata request has been pending for 3 s, when an attempt fails as waking, or while a prewake that answered "waking" is under 90 s old ([frontend.md](../frontend.md)). An analysis has no 3 s timer of its own. The SPA retries within a 150 s budget and honours `Retry-After`.

### Proxy chain and client-IP trust

```text
visitor ──TLS──▶ Cloudflare edge (sets CF-Connecting-IP = visitor)
        ──▶ Worker (copies it into X-ATUS-Client-IP; adds X-ATUS-Edge-Auth)
        ──TLS──▶ Render's Cloudflare zone (cross-zone hop: CF-Connecting-IP becomes the
                 fixed Worker address 2a06:98c0:3600::103; X-Forwarded-For is appended to,
                 so its leftmost entry is spoofable)
        ──▶ Render LB (TLS ends) ──HTTP──▶ API
```

- The API trusts `X-ATUS-Client-IP` only when `X-ATUS-Edge-Auth` is valid. It ignores `X-Forwarded-For`, `CF-Connecting-IP` and `True-Client-IP`.
- uvicorn's proxy-header handling is off: `forwarded_allow_ips` is empty, never `*`.
- The onrender.com URL cannot be disabled without a custom domain. Direct hits on it therefore get a minimal 403 with zero DB work.
- Raw IPs live only in API memory, for the token buckets.
  - The governor stores HMAC(daily-rotating key, /24 or /48 prefix) and purges it daily.
  - Logs keep IPs truncated to /24 or /48 (to be finalized in ADR-014).
- What actually reaches the app is measured after deploy with an ops-only header echo (see "Unknowns").

### Edge tiers and the persistent result cache (to be finalized in ADR-015)

| Tier | Holds | Survives | Key and version scope | Written by |
|---|---|---|---|---|
| **L0 static snapshot** `/snap/<SNAPSHOT_ID>/` | all SPA metadata, about 556 activity-detail files, about 130 prewarmed analyses (4 starters, presets, tier-1 trends, compares by sex, pandemic windows), and a manifest | everything, including Worker daily exhaustion and Render or Neon pauses | `SNAPSHOT_ID = <api_build12>-<sha256(DATA_VERSION)[:16]>`; the manifest is keyed by the same request hash the Worker computes | CI at each deploy. It restores the sha256-verified release dump into `postgres:16` with the production role settings, then runs the exact deployed image digest read-only against it |
| **L1 Workers KV** | runtime analysis results | Render spin-down and Render or Neon monthly pauses; not Worker daily exhaustion | `r1/<CACHE_SCOPE>/<k>`, where `CACHE_SCOPE = hash(EPOCH, api_version, ANALYTICS_VERSION, DATA_VERSION, RESPONSE_FORMAT)`. The git SHA is deliberately excluded, so code deploys don't orphan the keyspace | the Worker, through `waitUntil` |
| **L2 API in-process LRU** | recent results while the instance is awake | nothing (lost on spin-down) | operation, canonical spec, analytics version and data version (`src/atus_pipeline/api/cache.py`); keying on the resolved computation (F15) is deferred (ADR-012) | the API |

- **KV store gate.** The Worker writes to KV only when all of these hold:
  - a POST to one of the three analysis paths;
  - status 200 with a JSON content type;
  - origin `X-Cache` is `miss` (ADR-015: `hit` is refunded from the global count, so storing it left KV writes unbounded);
  - `X-Data-Version` equals the Worker's `DATA_VERSION`;
  - the origin's response-format header equals the Worker's `RESPONSE_FORMAT`;
  - the body is complete and ≤ 16 KiB (measured maximum 8,383 B).

  Errors are never stored. Entries expire after 30 days. A failed KV read is treated as a miss, and a failed write is ignored.
- **Invariant: statistics are unchanged.**
  - An edge hit is a byte-identical copy of an earlier origin response for the same globally unique data version, analytics version and response format.
  - Snapshot entries come from the same image on the same data. Their only difference from a live response is plan-order noise, which the §9 criterion allows.
  - The post-deploy smoke test recomputes at least 2 snapshot entries live and diffs them under §9.
- **No canonicalization at the edge.** A JSON round trip is lossy (`1e400` becomes `null`), which would let a malformed request receive another spec's 200. The SPA instead sends the stable sorted-keys serialization (ADR-009), so starters and share links hit. Semantic deduplication stays in the API (F15).
- **Not used:**
  - the Cache API: it is per data center, and `put` resolves even when nothing was stored;
  - Workers Cache: it bills static requests;
  - a cache table in Neon: every hit would wake Render and Neon, and it would break the read-only role.

### Quota governor (to be finalized in ADR-012)

- **Where it lives.** A single Durable Object named `global`, using the SQLite backend on Workers Free.
  - Its state survives Render restarts and spin-downs.
  - It does not depend on the API host, which keeps the fallback swap (below) cheap.
  - The API keeps its own in-flight lanes and per-IP buckets as a second, independent layer.
- **What it admits.** Only uncached analyses; static and KV hits never call it. It charges provisionally at admission, settles when the request completes, and refunds shed or waking outcomes.
- **Starting budgets** (final values in ADR-012):
  - **Neon CU-hours, ground truth.** Month-to-date usage comes from Neon's `GET /projects/{id}` (`compute_time_seconds`, `data_transfer_bytes`, `consumption_period_start`).
    - A Durable Object alarm polls it at most every 5 minutes, and only while uncached analyses arrived in the last 5 minutes, when the compute is awake anyway.
    - Admission continues while month-to-date plus the bridge estimate stays below `min(85, 10 + 75 × elapsed share of the consumption period)`.
    - There is also a ceiling of 10 CU-h per UTC day.
  - **Bridge estimate.** Between polls, and as the whole estimate when no ground truth is available, busy time and the full 300 s idle tail are charged at the compute's maximum (2 CU). This holds until at least 7 days of reconciled data justify a smaller factor.
  - **Without ground truth.** The daily ceiling drops to `(100 × 0.9) / 31 ≈ 2.9` CU-h. That way no 30- or 31-day window can exceed the quota, wherever Neon's reset falls, as long as everything that wakes Neon through the Worker is charged: analyses, and since review round 2 the metadata lookups that reach a freshly started API process (ADR-015, "Metadata lookups"). The model's own error is in ADR-015 ("Model error").
  - **Neon egress.** Stop at 4.5 GB month-to-date (ground truth only), and admit at most 300 heavy analyses per day. Without ground truth each day's estimated egress is held to 4.5 GB / 31 (ADR-015).
  - **Global.** At most 600 uncached analyses per UTC day. This also bounds KV writes (only computed answers are stored). It bounds Render outbound only for analyses that keep their count; refunded admissions are bounded per network, and metadata lookups by their own cap (ADR-015, "Render outbound through the Worker").
  - **Per IP prefix** (/24 or /48): at most 60 uncached analyses per day, and a fifth of the day's compute ceiling (ADR-015).
  - **Metadata.** The Worker answers metadata GETs from the static snapshot; the rest reach Render as lookups, at most 200 per prefix and 2,000 in all per day, each charged compute like an analysis when the API reports DB time (ADR-015).
- **Failure behaviour.** The governor fails closed for uncached analyses and metadata lookups only. Static content, KV hits and the metadata the snapshot answers keep working.
- **Credentials.** The Neon key is a **project-scoped** API key, stored as a Worker secret and used only by the alarm.
  - Such a key can edit one project but cannot delete it, and only organization admins can create one. Whether the Free plan offers it is undocumented.
  - A personal or organization-wide key is never stored in any runtime.
  - Without a key the governor runs in the conservative mode above.
- **Visibility.**
  - `/api/_ops/budget` is answered by the Worker itself, behind the bearer `OPS_TOKEN`.
  - A daily GitHub workflow fails, and so emails the owner, when any budget is above 70% of its pro-rated value.

### API process model

- **Process.**
  - One uvicorn process, with `WEB_CONCURRENCY=1` set explicitly, `--no-server-header`, proxy headers off, listening on `$PORT`.
  - Measured memory is ~70 MB of 512 MB.
  - The anyio thread limiter is 16.
  - All DB work runs off the event loop, so `/health` answers within Render's 5 s health-check window even during a 50 s query. After 15 s of failed checks Render stops routing to the instance; after 60 s it restarts it.
  - Requests waiting on a long analysis cannot shut the check out either. Single-flight joiners take no lane and no token and wait as long as the computation, so before review round 3 about 63 of them filled uvicorn's `limit_concurrency` (64), and uvicorn answered `/health` with its own 503 for as long as the analysis ran. The app now serves at most `max(32, 4 × ATUS_API_THREADS)` (64) requests besides `/health` at once and answers the next one 503 `overloaded` itself, and uvicorn's bound is twice that (128), so ordinary traffic leaves room for `/health` (`tests/integration/api_prod/test_server.py::TestHealthWhileRequestsWait`). Idle or slow connections beyond 128 still fill uvicorn's bound (ADR-012, "What this does not protect against").
- **Pool** (`psycopg_pool`):
  - `min_size=0`, so the API makes no DB connection at start and a Render spin-up does not wake Neon;
  - `max_size=4`;
  - `max_idle=240 s`, shorter than Neon's 300 s suspend;
  - `check=ConnectionPool.check_connection`, which fixes F05 (measured: a dead connection is replaced in 11.2 ms);
  - `connect_timeout=10 s`, TCP keepalives, and a 5 s acquire timeout.
- **Concurrency lanes.**
  - At most 2 uncached analyses in flight, of which at most 1 heavy.
  - A request waits at most 5 s for a lane, then gets 503 `overloaded` with `Retry-After`.
  - Cache hits and single-flight joins bypass the lanes.
  - An admitted analysis runs to completion even if the client disconnects (bounded by the deadline) and is cached. A retry after a proxy 524 therefore joins the running computation or hits the cache.
- **In-process snapshot.** The API keeps `data_version`, years, respondent count and the lexicon in memory.
  - It is built on the first request that touches the DB and refreshed at most every 10 minutes.
  - Cache hits, 400 and 422 responses, and metadata then cost 0 DB statements (F14). That means fewer Neon wakes.
- **Bounded work** (provisional values; final in ADR-012 after the §13 calibration and the post-deploy replay):
  - role `atus_api` defaults: `statement_timeout=85s` as a backstop, `lock_timeout=5s`, `idle_in_transaction_session_timeout=30s`, `jit=off`;
  - an API deadline of 90 s that cancels the query;
  - after measurement, the timeout becomes `clamp(3 × p99 measured on Neon including the F66 broad selectors, 30 s, 85 s)`;
  - every limit stays at or below 100 s, under Cloudflare's 125 s Proxy Read Timeout, in case it applies to the Worker→Render hop (undocumented).
- **Exposure.**
  - A 64 KiB body cap, enforced in pure ASGI before any DB checkout, and capped 422 bodies (F03, F04).
  - `/docs` and `/redoc` are off; `openapi.json` is published as a static asset.
  - `/ready` is token-gated and not routed by the Worker.

### Database: Neon

- **Plan and region.** Neon Free, Postgres 16, region `aws-us-east-1`, database `atus`, paired with Render's Virginia region.
  - Render does not publish which cloud region backs each of its regions, so the pairing is an assumption; the round-trip time is measured after deploy.
  - Regions cannot be changed later on either provider. If Render does not offer Virginia for Free, use Ohio with `aws-us-east-2`, or Oregon with `aws-us-west-2`. Decide before the Neon project is created.
- **Direct endpoint, not the pooler.** Reasons:
  - Neon recommends direct connections for long-running analytics queries.
  - The pooler's `query_wait_timeout` is 120 s, and it rejects startup parameters.
  - The app owns at most 4 connections, against `max_connections` of 104 at 0.25 CU and 839 at 0.25–2 CU.
  - Prepared statements and role-level settings work unchanged.
- **TLS.** `sslmode=verify-full`, `sslrootcert=system` (needs libpq ≥ 16; SNI needs libpq ≥ 14). The image build checks this. If it fails, use `verify-full` with a bundled CA file. Never use `require` alone.
- **Compute: autoscaling, minimum 0.25 CU, maximum 2 CU.** Set this on the production branch's compute itself; changing the project defaults does not change existing computes.
  - Basis: worst cases measured at 2.7–3.4 s at 2 CU versus 47–52 s at 0.25 CU. The UX criteria carry the most weight with the panel.
  - **Calibration lever** (ADR-012). If reconciled CU-hours per wake session exceed the budget, roughly 0.1 CU-h per session under the traffic model in [`../deployment.md`](../deployment.md), then either:
    - lower the maximum to 1 CU (only after a 1 CU emulation run), or
    - fix the compute at 0.25 CU. Accounting becomes exact, but worst cases are back to 47–52 s until the §9-gated optimizations land.
- **Roles.**
  - `neondb_owner` (created by the console) restores and validates.
  - `atus_api` is created in SQL: LOGIN, SELECT only, password of at least 60 bits.
  - Read-only is enforced by privileges, and a test proves the role cannot write.
- **Projects and branches.**
  - Each data release gets its own project, with its own 1 GB and its own 100 CU-h.
  - There is one production branch. No child branches, read replicas or instant restores: storage counts each branch's logical size, and a restore leaves an extra root branch behind.
- **Optimizations, only with the §9 proof:**
  - role-level `max_parallel_workers_per_gather=0`, adopted only if it also wins at 2 CU in emulation;
  - year pushdown in the activity CTE.
- **No database keep-alive.** The project is in an AWS region, and the inactivity deletion is Azure-only. Branch archiving is accepted (see Risks).

### Deploy: the exact CI image, by digest

- **Build.** CI builds one `linux/amd64` image, tests it (production compose, emulation profile, Playwright), scans it with Trivy (pinned to a commit) and pushes it to public GHCR.
- **Deploy.** `deploy.yml` runs on a `v*` tag or on manual dispatch, in environment `production` with a required reviewer. It:
  1. calls the Render deploy hook with `imgURL=ghcr.io/nathan98000/atus-api@sha256:<hex>`, URL-encoded. Every part except the digest must match the image configured on the service;
  2. polls origin `/health` until the reported build equals `GITHUB_SHA`;
  3. generates the static snapshot with that same digest;
  4. runs `wrangler deploy`, which publishes the assets (including the snapshot), the Worker, and the vars `DATA_VERSION`, `ANALYTICS_VERSION`, `RESPONSE_FORMAT` and `SNAPSHOT_ID`;
  5. runs a smoke test of at most 40 requests.
- **The only deploy path.**
  - Image-backed Render services never auto-deploy.
  - No Git repository is connected on Render, and Workers Builds is never connected.
  - CI asserts the wrangler config: no `[cache]` block, `preview_urls = false`, `workers_dev = true`, and `run_worker_first` containing both `/api` and `/api/*`.
- **Rollback.**
  - API: call the hook with the previous digest. Render keeps no images, so GHCR keeps the last 20 digests.
  - Edge: `wrangler rollback`.
  - Data: the reverse switch in ADR-013.
- **Host build rejected.** §16 requires deploying the exact image that was scanned.

### Keep-alive policy

- **None for Neon, none for Render.**
- Render wakes only from:
  - real `/api` traffic;
  - user-intent prewake;
  - the deep monitor every 6 hours, which hits `/health` and never the DB.
- Render spins down after 15 idle minutes, so the monitor does not keep it awake (about 1 h/day, PROJECTION).
- No monitor ever touches the database. Final cadence in ADR-014.

### Release switch (to be finalized in ADR-013)

1. Restore the new serving dump into a **new Neon project** over the direct endpoint (`pg_restore --no-owner --no-acl`, client ≥ 16). Then apply the roles script, run `VACUUM (ANALYZE)`, check the size, check the EXPLAIN plans of the worst cases, and smoke-test with a local API.
2. CI builds the snapshot for the new `DATA_VERSION` but does not deploy it yet.
3. The owner changes the Render env vars `ATUS_DATABASE_URL` and `ATUS_EXPECTED_DATA_VERSION` and clicks "Save and deploy".
   - The API answers 503 `data_version_mismatch` if the DB version is not the expected one.
   - Until step 4, the Worker stores nothing (version gate), and the SPA shows a reload banner when it sees a version header mismatch.
4. The deploy workflow publishes the Worker vars and the new snapshot.

Rollback is the same sequence in reverse. The previous project is kept until the owner approves its deletion.

The zero-failure proof against the fixture DB belongs to ADR-013. Whether an env-var change deploys with zero downtime on Render Free is unverified; the Worker's single connect-failure retry covers the gap.

### Must-not-do list (configurations that could bill, break terms or break links)

- Never add a payment method on any provider. Never accept a trial, or Neon's "$20 credits". Stop and report at any card prompt.
- Never create a second Render workspace (its creation flow asks for a payment method), and never run a second Free web service in the workspace.
- Never enable Workers Cache (`[cache]`), Workers Paid or R2. Never connect Workers Builds or the Pages Git integration, and never connect a Git repository on Render.
- Never keep-alive ping Render or Neon.
- Never create Neon branches, read replicas or instant restores on a serving project.
- Never use a `render.yaml`. If one is ever introduced, it must say `plan: free`, because a missing `plan` gets the paid default.
- Never recreate a Free service on Render to get around a Render-imposed suspension. The AUP forbids attempts to "bypass, circumvent, or defeat any access or usage restrictions".
- Never put a personal or organization-wide Neon or Render API key in any runtime.
- Never load-test a provider. All load tests run locally under emulation (§13).

## Judge panel

### Proposals

| Key | Architecture |
|---|---|
| P1 | Cloudflare Workers (SPA + `/api` proxy + KV cache) → Render Free (image by digest) → Neon Free 0.25–2 CU; an in-API governor paced against Neon's actual month-to-date CU-hours |
| P2 | Cloudflare Workers + KV → Render Free → Prisma Postgres Free (PG17); designed to minimize SQL operations (1–2 statements per uncached analysis) with persisted daily caps |
| P3 | Cloudflare Workers + KV + Durable Object quota gate → Vercel Hobby (Python, Fluid compute, iad1) → Neon Free fixed at 0.25 CU; every daily cap = quota × safety / 31 |
| P4 | Cloudflare Workers SPA + `/api` proxy + KV → Tailscale Funnel → the owner's MacBook (full 1,980 MiB DB, native PG16) |
| P5 | P1 tuned for the measured worst cases: build-time static snapshot + byte-exact KV cache + SQLite Durable Object governor + intent-based prewake |

No proposal was disqualified. Every one carries a conditional card risk:

- Render's Terms reserve a $1 authorization hold, and card prompts were reported in 2024–25.
- Vercel's and Tailscale's official pages say nothing about cards.

Each of these is a stop-and-report condition, not a confirmed violation.

### Scores per judge

Each judge scored every criterion from 1 to 5 using its own weights. All judges' factual spot-checks were made against the fact sheet and official pages on 2026-10-20.

**Judge 1: cost, quota and terms risk.** Total = Σ score × weight; maximum 87.5.

| Criterion | Weight | P1 | P2 | P3 | P4 | P5 |
|---|---|---|---|---|---|---|
| zero_cost_no_card (includes billing-shaped failures, terms, paid-only restores) | 3 | 3 | 2 | 3.5 | 3 | 3 |
| quota_risk_blast_radius | 3 | 3 | 3 | 4 | 4 | 4 |
| db_fit_headroom | 1.5 | 3 | 3 | 3 | 4 | 3 |
| share_link_stability | 1.5 | 4 | 4 | 4 | 4 | 4 |
| portability | 1.5 | 4 | 4 | 4 | 4 | 4 |
| no_domain | 1 | 5 | 5 | 5 | 5 | 5 |
| ops_simplicity | 1 | 3 | 3 | 2 | 1 | 2 |
| cold_start_and_ui | 1 | 3 | 3 | 4 | 2 | 4 |
| worst_case_latency | 1 | 3 | 2 | 2 | 4 | 3 |
| logs_metrics | 1 | 4 | 3 | 3 | 2 | 4 |
| backup_restore | 1 | 4 | 3 | 4 | 3 | 5 |
| security_headers | 0.5 | 4 | 4 | 4 | 4 | 4 |
| tls | 0.5 | 4 | 3 | 4 | 4 | 4 |
| **Total** | | **60.5** | **54** | **63** | **60** | **64.5** |
| Rank | | 3 | 5 | 2 | 4 | 1 |

**Judge 2: operations and longevity for a junior engineer.** Weights in %. Total = Σ weight × score / 5; maximum 100.

| Criterion | Weight | P1 | P2 | P3 | P4 | P5 |
|---|---|---|---|---|---|---|
| ops_simplicity | 18 | 3 | 2.5 | 2.5 | 1 | 2.5 |
| share_link_stability | 11 | 4 | 4 | 4 | 4 | 4 |
| logs_metrics | 11 | 4 | 3.5 | 3 | 2 | 4 |
| backup_restore | 11 | 4 | 3 | 3.5 | 3 | 4.5 |
| portability | 11 | 4 | 4 | 3.5 | 4 | 4 |
| quota_risk_blast_radius | 8 | 3.5 | 3 | 3.5 | 4 | 4.5 |
| zero_cost_no_card | 6 | 4 | 4 | 3.5 | 3 | 4 |
| db_fit_headroom | 6 | 4 | 3 | 4 | 3 | 4 |
| cold_start_and_ui | 6 | 3.5 | 3 | 4 | 2 | 4 |
| worst_case_latency | 6 | 3 | 2 | 2 | 4 | 3 |
| no_domain | 2 | 5 | 5 | 5 | 5 | 5 |
| security_headers | 2 | 4 | 4 | 4 | 4 | 4 |
| tls | 2 | 4 | 3.5 | 4 | 4 | 4 |
| **Total** | | **74.2** | **65.1** | **66.8** | **58.2** | **75.7** |
| Rank | | 2 | 4 | 3 | 5 | 1 |

**Judge 3: user experience, performance and security.** Total = Σ score × weight; maximum 95. Ties are broken on the lens-core subtotal: cold start + latency + security + TLS + quota.

| Criterion | Weight | P1 | P2 | P3 | P4 | P5 |
|---|---|---|---|---|---|---|
| cold_start_and_ui | 3 | 3 | 3 | 4 | 2 | 4 |
| worst_case_latency | 3 | 3 | 2 | 2 | 4 | 3.5 |
| security_headers (incl. IP trust, origin lock, attack surface) | 2.5 | 4 | 3 | 5 | 2 | 4 |
| tls | 2 | 4 | 3 | 4 | 5 | 4 |
| quota_risk_blast_radius | 2 | 3 | 3 | 3.5 | 4 | 4 |
| zero_cost_no_card | 1 | 4 | 3 | 3 | 4 | 4 |
| ops_simplicity | 1 | 3 | 3 | 2 | 1 | 2 |
| db_fit_headroom | 1 | 4 | 3 | 4 | 3 | 4 |
| share_link_stability | 1 | 4 | 4 | 4 | 4 | 4 |
| logs_metrics | 1 | 4 | 3 | 3 | 3 | 4 |
| no_domain | 0.5 | 5 | 5 | 5 | 5 | 5 |
| backup_restore | 0.5 | 4 | 3 | 4 | 4 | 4 |
| portability | 0.5 | 4 | 4 | 3 | 4 | 4 |
| **Total** | | **67.5** | **56.5** | **67.5** | **62.5** | **73** |
| Rank | | 3 (tie-break 42 vs 45.5) | 5 | 2 | 4 | 1 |

### Aggregation and ranking

The judges used different weights and maxima, so each total was normalized to a percentage of that judge's maximum. The primary aggregate is the equal-weight mean of the three percentages. The Borda rank sum (1 = best) is a cross-check.

| Proposal | Judge 1 % | Judge 2 % | Judge 3 % | **Mean %** | Rank sum | Outcome |
|---|---|---|---|---|---|---|
| P5 | 73.7 | 75.7 | 76.8 | **75.4** | 3 | **Winner**: first for every judge and under both methods |
| P1 | 69.1 | 74.2 | 71.1 | 71.5 | 8 | Merged into the winner; not a separate fallback (see below) |
| P3 | 72.0 | 66.8 | 71.1 | 69.9 | 7 | **Runner-up and documented fallback** |
| P4 | 68.6 | 58.2 | 65.8 | 64.2 | 13 | Rejected |
| P2 | 61.7 | 65.1 | 59.5 | 62.1 | 14 | Rejected |

The winner is unanimous. Second place is not:

- P1 leads P3 by 1.5 points on the normalized mean.
- P3 leads P1 by one on the rank sum.

P1 is not a separate fallback, for two reasons:

- P5 is P1's provider set plus two tiers, the static snapshot and the Durable Object governor, and the judges note that P5 "degrades to P1 behaviour" when a tier is removed.
- Every condition that could block P5 blocks P1 in exactly the same way: a Render card prompt, Render's discretionary suspension with its paid-only restore, and Render's terms.

P1's distinctive ideas are therefore grafted into the decision. P3 is the highest-scoring proposal whose API host does not share those Render risks, so it is the documented fallback.

### Ideas grafted onto P5

| Graft | From | Why |
|---|---|---|
| Governor paces against Neon's actual `compute_time_seconds` and `data_transfer_bytes` (`GET /projects/{id}`), polled by a Durable Object alarm only while traffic is active | P1; judges 1 and 3 | P5's model of CU cost is an upper bound only if the autoscaler behaves as assumed, and the Neon docs do not support that assumption. P5's daily GitHub reconciliation can be delayed, dropped, or disabled after 60 days |
| Conservative bridge (charge the whole 300 s tail at max CU) until ≥ 7 days of reconciled data; daily ceiling = quota × 0.9 / 31 when ground truth is missing | judge 1; P3 | Accounting stays safe until it has been measured |
| Fail closed for DB-touching forwards when the governor is unavailable; static and KV keep serving | P3; judges 1 and 3 | P5 failed open to limits held in API memory, and those reset on every Render restart |
| `data_version` bound to the deployment (`ATUS_EXPECTED_DATA_VERSION`, 503 `data_version_mismatch`) | P3; judge 2 | Prevents a mismatched API/DB pair during an env-var switch whose behaviour on Render Free is unverified |
| Ops-frugal access: in-process snapshot of `data_version`, years and lexicon, so cache hits, 4xx and metadata cost 0 DB statements; `db_statements` counted per request | P2; judges 1, 2 and 3 | Every avoided DB touch avoids a Neon wake, which costs ≥ 75 CU-s at 0.25 CU |
| Worker answers `/api/_edge/health` itself; routine monitors cost 0 Render wakes and 0 Neon | P2; judge 2 | Monitoring should not spend quota |
| No DB-touching monitor at all (P5's daily DB check removed) | judge 1 | Brief §10: DB wakes only from real traffic |
| Daily quota-report workflow that fails above 70% of the pro-rated budget, plus a check for auto-disabled schedules (planned monthly; implemented as a weekly workflow, ADR-014 amendments) | P3; judges 1 and 2 | $0 alerting that does not depend on dashboards |
| KV key scope uses `api_version`, `ANALYTICS_VERSION`, `DATA_VERSION`, `RESPONSE_FORMAT` and a manual `EPOCH`, not the git SHA | P1; judge 2 | P5 orphaned the whole KV keyspace on every code deploy |
| Single-flight joins; admitted analyses complete and are cached on disconnect; one Worker retry on connection failure before headers; meta-CSP backstop in `index.html`; CORP `same-origin` on `/api` | P1; judge 3 | A retry after a 524 never pays twice; `_headers` on the SPA fallback is unverified |
| Allowlisted upstream header set; HMAC(daily key, IP prefix) in the Durable Object; `Server-Timing` (db/app) logged at the edge | P3; judge 3 | Smaller trust surface; latency attribution that Render Hobby lacks |
| Per-isolate circuit breaker (skip the origin for 30 s after 2 failures) and an explicit Worker `User-Agent` | P4; judge 3 | Avoids hammering a sleeping or suspended origin |
| Render suspension recovery = fail over to a different provider by changing one Worker var, never recreate on Render | P2, P3, P4; judge 1 | The AUP's "bypass, circumvent, or defeat any access or usage restrictions" clause |
| Random suffix on the never-published origin name | P3 | Makes it harder for direct-origin traffic to spend Render bandwidth (see Risks) |
| F66 broad selectors added to the §13 emulation matrix and the post-deploy replay | judge 3 | Timeouts sized only on pooled leisure would cut broad selectors at 0.25 CU |
| Explicit must-not-do list with CI assertions | P1, P5; judge 1 | Turns the configurations that could bill into failing tests |

## Rejected alternatives

- **P1, Workers + Render + Neon with no static floor or persistent governor.** It shares every one of the winner's provider risks, and its strengths are grafted in. Without the static snapshot, all metadata lives in KV with a 7-day TTL. A long origin outage, or Worker quota exhaustion, then blanks even the home page. Its own scenario C projects 105–110 CU-h a month at typical traffic. Its governor falls back to an estimate that lives in API memory and is lost on every spin-down.
- **P2, Prisma Postgres Free.**
  - Prisma's FAQ calls Free "intended for evaluation only and is not meant for production workloads".
  - All limits are workspace-wide, and running out of operations stops every database in the workspace until the next calendar month.
  - Official sources contradict each other on how raw-SQL statements are counted.
  - Credentials cannot be made read-only, so the §9 role requirement can only be approximated.
  - Compute size is unpublished, so worst-case latency cannot be emulated (§13).
  - Free storage changed twice in one week in October 2026.
  - Its operation-frugal access pattern is grafted.
- **P3, Vercel Hobby API.** This is the runner-up, kept as the fallback rather than the primary because:
  - Vercel is silent on whether Hobby needs a card;
  - going over quota pauses the feature "until 30 days have passed", the largest blast radius on the menu;
  - use is limited to non-commercial personal projects;
  - it cannot deploy the scanned OCI image by digest (a §16 deviation);
  - runtime logs are kept 1 hour, with no Drains on Hobby;
  - at its launch configuration of a fixed 0.25 CU, worst cases take 47–52 s.
- **P4, self-hosting on the owner's MacBook via Tailscale Funnel.**
  - Availability depends on one fanless laptop with FileVault on and a disk that is 95% full (verified locally by judge 2).
  - The required `tailscaled` variant is "only recommended for unattended installs managed by experienced macOS system administrators".
  - Funnel is in beta with unpublished bandwidth limits.
  - A public process runs on a personal machine with LAN reach.
  - The origin cannot be deployed through the approved CI workflow.
  - It has the best measured worst-case latency (13.7–24 s with JIT on this Mac), but that does not outweigh the above. Its circuit breaker and stale-offline ideas are grafted.
- **FastAPI Cloud Hobby.** Its terms (effective 2026-08-12) limit use to "your own internal use ... not ... for the benefit of any third party". The request timeout and region are undocumented, it is a beta, and its 0.1 vCPU gives the measured 7–11 s process start.
- **Oracle Cloud Always Free.** A credit card is required at sign-up (constraint 1).
- **Cloudflare Pages instead of Workers static assets.** Cloudflare now directs new projects to Workers. Pages lacks Workers Logs and the Rate Limiting binding. Both share the 100k/day limit.
- **GitHub Pages.** It has no real SPA fallback (deep links return 404) and no custom headers.
- **Neon's pooled endpoint.** See "Direct endpoint" above.
- **A fixed 2 CU or fixed 1 CU Neon compute.** With the 5-minute idle tail billed at the fixed size, P5 projects these at ~107 CU-h a month at realistic traffic (fixed 2 CU) and ~108 at twice that traffic (fixed 1 CU), over the 100 CU-h quota.
- **Supabase Free, Render Postgres Free, Aiven Free, Fly.io, Koyeb, Railway, Heroku, Hugging Face Spaces, PythonAnywhere, Cloudflare Python Workers, Netlify credits, quick tunnels, ngrok, Dev Tunnels.** The brief's §3 already verified these as unsuitable, and no proposal revived them.

## Fallback: the runner-up (P3's API host, Vercel Hobby)

**When to use it.** Only on an owner decision, after one of these:

- Render shows a card prompt (stop first; constraint 1);
- Render suspends the service for service-initiated traffic, which only a paid plan can restore;
- Render changes its Free terms in a way that breaks the budget.

Vercel has its own conditions, which the owner must accept first:

- the non-commercial-use clause;
- Vercel's silence on whether Hobby needs a card;
- the 30-day over-limit pause.

**Kept constant across the switch:** the workers.dev origin and every share link, the static snapshot and KV (`CACHE_SCOPE` does not change), the Durable Object governor, and Neon.

**Preconditions that implementation must keep ready** (finalized during implementation):

1. A Vercel entry module that exposes the same ASGI app, plus a `vercel.json` with `git.deploymentEnabled=false`, `regions=["iad1"]` and a function `maxDuration` of 120 s or more. Both are tested in CI by importing the module, without needing a Vercel account.
2. The governor's in-flight leases (`GATE_INFLIGHT=on`). Vercel can run several instances, so in-process lanes are no longer global.
3. Vercel budgets in the governor, each with a daily ceiling of quota × 0.9 / 31, because the pause anchor is "30 days":
   - 4 Active CPU-hours,
   - 360 GB-h of Provisioned Memory,
   - 1M invocations,
   - Fast Origin Transfer.
4. A Worker origin mode that adds the `x-vercel-protection-bypass` header.
5. A disabled `deploy-vercel` job that runs `vercel build` and then `vercel deploy --prebuilt`.

**Switch procedure:**

1. The owner creates a Vercel Hobby account with email or GitHub, and stops at any card prompt.
2. `npx vercel login`, then `npx vercel link` from the CLI with no Git import. Name the project `atus-api-<8 random chars>`. Settings → Git must show no connected repository.
3. Settings → Functions: region `iad1`, Fluid compute on.
4. Settings → Deployment Protection: Vercel Authentication with scope **All Deployments**. Create a Protection Bypass for Automation secret.
5. Production environment variables, marked Sensitive: the same Neon `atus_api` DSN, the edge secret, `ATUS_EXPECTED_DATA_VERSION` and `ATUS_ENV=production`.
6. A Vercel token scoped to the Hobby team, expiring in 90 days. Store it with the org/project ids and the bypass secret as GitHub `production` secrets.
7. Run the deploy workflow with target `vercel` (approval required). Smoke-test the deployment URL with the bypass header.
8. Store the bypass secret as a Worker secret. Set the Worker vars `API_ORIGIN=https://<project>.vercel.app`, the Vercel origin mode and `GATE_INFLIGHT=on`. Deploy the Worker, then run a smoke test of at most 40 requests.
9. Leave the Render service as it is. Do not recreate it, and do not delete it without the owner's approval.

**Reverting:** restore the Worker vars to the Render origin.

**Recorded deviation:** on Vercel the deploy uses CI-built prebuilt output, not the scanned OCI image (§16).

**If Neon is the blocker** (for example a card prompt at sign-up), neither ranked fallback avoids it. The choice between P2 (Prisma, with its terms tension) and P4 (self-hosting) is an owner decision.

## Cited free-tier terms (all accessed 2026-10-20)

Status codes:

- **V**: verified on the official page by the researcher and the checker (fact sheet).
- **A**: official but ambiguous or silent.
- **P**: re-verified by a proposal or judge on 2026-10-20; not in the fact sheet.

### Cloudflare

| Term | URL | Quote or value | Status |
|---|---|---|---|
| Workers Free: 100,000 requests per day per account, reset at 00:00 UTC | https://developers.cloudflare.com/workers/platform/limits/ | "daily request limit of 100,000 requests, resetting at midnight UTC" | V |
| Over the limit: Error 1027 (HTTP status undocumented); `run_worker_first` requests get 429 | same; https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/ | "will receive a 429 (Too Many Requests) response instead of falling back to static asset serving" | V / A |
| Static asset requests are free and unlimited | https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/ | "Requests to static assets are free and unlimited." | V |
| 10 ms CPU per request; network waits not counted; 1102 maps to HTTP 503 | https://developers.cloudflare.com/workers/platform/limits/ | "Waiting on network requests ... does not count toward CPU time." | V |
| No duration limit for HTTP Workers; no subrequest time limit | same | "There is no set time limit on individual subrequests." | V |
| 125 s Proxy Read Timeout; unclear whether it applies to Worker subrequests | https://developers.cloudflare.com/fundamentals/reference/connection-limits/ | "Proxy Read Timeout \| 125 \| 524" | A |
| `_headers` does not apply to Worker-generated responses | https://developers.cloudflare.com/workers/static-assets/headers/ | "not applied to responses generated by your Worker code" | V |
| SPA fallback; `run_worker_first` arrays need Wrangler ≥ 4.20; at most 20,000 files and 25 MiB per file | https://developers.cloudflare.com/workers/static-assets/routing/single-page-application/ ; https://developers.cloudflare.com/workers/platform/limits/ | "Workers will serve the contents of the `/index.html` file with a `200 OK` status." | V |
| KV Free: 100k reads, 1k writes, 1k deletes and 1k lists per day; 1 GB; failures, not billing | https://developers.cloudflare.com/kv/platform/pricing/ | "further operations of that type will fail with an error" | V |
| KV is eventually consistent (≥ 60 s) | https://developers.cloudflare.com/kv/concepts/how-kv-works/ | "may take up to 60 seconds or more to be visible" | P |
| Durable Objects on Free, SQLite backend only: 100k requests/day, 13,000 GB-s/day, 100k rows written/day, 5 GB | https://developers.cloudflare.com/durable-objects/platform/pricing/ | "Only Durable Objects with SQLite storage backend are available." | P |
| Subrequests (fetch, KV, DO) are not billed as requests | https://developers.cloudflare.com/workers/platform/pricing/ | "Cloudflare does not bill for subrequests you make from your Worker." | P |
| Workers Logs Free: 200k events/day, 3 days; from 2026-12-01: 0.5 GB/day, 7 days, ingestion stops at the cap | same; https://developers.cloudflare.com/observability/pricing/ | "Cloudflare stops ingesting new data when the account reaches its daily limit" | V |
| Workers Cache bills requests that are normally free | https://developers.cloudflare.com/workers/cache/ | "every request to your Worker is charged at the standard Workers request rate, including requests that are normally free" | A |
| Version URLs are on by default; `preview_urls = false` turns them off | https://developers.cloudflare.com/workers/versions-and-deployments/version-urls/ | "If `workers_dev` is enabled, Version URLs are enabled by default." | V |
| Cross-zone hop: CF-Connecting-IP becomes the Worker address | https://developers.cloudflare.com/fundamentals/reference/http-headers/ | "set to the Worker client IP address '2a06:98c0:3600::103'" | V |
| workers.dev subdomains may be renamed | https://www.cloudflare.com/service-specific-terms-developer-platform/ | "for any or no reason" ... "will attempt to provide you with at least one week prior notice" | V |
| workers.dev is meant for hobby projects | https://developers.cloudflare.com/workers/configuration/routing/workers-dev/ | "intended for personal or hobby projects that aren't business-critical" | V |
| Granular Workers roles: deploying an existing Worker needs Editor on it | https://developers.cloudflare.com/workers/authorization/workers/ | "Deploy an existing Worker \| `wrangler deploy` \| `Editor` for that Worker" | V |
| Rate Limiting binding is local to each location (so it cannot enforce global caps) | https://developers.cloudflare.com/workers/runtime-apis/bindings/rate-limit/ | "local to the Cloudflare location" | V |
| No card | https://www.cloudflare.com/products/workers/ | "Start building for free — no credit card required." | V |

### Render

| Term | URL | Quote or value | Status |
|---|---|---|---|
| Free web service: 0.1 CPU / 512 MB; a `render.yaml` without `plan` gets a paid plan | https://render.com/docs/blueprint-spec | "`free`: 0.1 CPU / 512 MB RAM" | V |
| Spins down after 15 idle minutes; spin-up takes about a minute | https://render.com/docs/free | "This process takes about one minute." | V |
| 750 Free instance hours per workspace per month; suspension when exhausted | same | "Render grants 750 Free instance hours to each workspace per calendar month" | V |
| May restart a Free service at any time | same | "Render might restart a Free web service at any time." | V |
| Hobby outbound bandwidth 5 GB, including service-initiated traffic | https://render.com/docs/outbound-bandwidth | "Network communication initiated by any Render service ... over the public internet" | V |
| Bandwidth exhausted with no card: suspended until next month | https://render.com/docs/free | "Render instead suspends all of your Free services for the remainder of the month." | V |
| Discretionary suspension; restore only by a paid plan | same | "you can restore it by moving it to any paid compute plan." | V |
| Image deploys: any reachable registry, by digest, no stored images, no auto-deploy | https://render.com/docs/deploying-an-image | "Render does not store previously pulled Docker images." | V |
| Deploy hook `imgURL` | https://render.com/docs/deploy-hooks | "All components of `imgURL` besides the tag or digest must match your service's default image URL." | V |
| Health checks: pass within 5 s; routing stops after 15 s of failures; restart after 60 s | https://render.com/docs/health-checks | "If an instance fails consecutive health checks for 60 seconds, Render automatically restarts the instance." | V |
| Hobby logs and metrics kept 7 days; latency percentiles need Pro | https://render.com/docs/logging ; https://render.com/docs/service-metrics | "Response latency metrics require a Pro workspace plan or higher." | V |
| The onrender.com subdomain cannot be disabled without a custom domain | https://render.com/docs/blueprint-spec | "requires the service to have at least one custom domain" | V |
| Load balancer ends TLS and forwards HTTP; traffic is fronted by Cloudflare | https://render.com/docs/web-services ; https://render.com/docs/uptime-best-practices | "All inbound requests to Render web services pass through Cloudflare" | V |
| Regions cannot be changed after creation; cloud mapping is unpublished | https://render.com/docs/regions | "Render doesn't currently support changing the region" | V / A |
| No card for free resources | https://render.com/docs/your-first-deploy | "No payment is required." | V |
| Terms allow a $1 authorization hold | https://render.com/terms (Last Modified 2026-07-10) | "a temporary authorization hold of up to one U.S. dollar ($1.00)" | V |
| Staff in 2024 said card prompts happen for verification | https://web.archive.org/web/20250125073101/https://community.render.com/t/free-but-credit-card-first/24630 | "Sometimes, we may request card details for verification purposes." | V |
| AUP: no bypassing usage restrictions | https://render.com/acceptable-use (2025-08-22) | "Attempt to bypass, circumvent, or defeat any access or usage restrictions" | A |
| An additional workspace asks for a payment method | https://render.com/docs/team-members | "including specifying a plan type and payment method" | V |
| Render API keys reach every workspace (so none is created) | https://api-docs.render.com/reference/authentication.md | "An API key provides access to *all* workspaces you belong to." | V |

### Neon

| Term | URL | Quote or value | Status |
|---|---|---|---|
| 1 GB per project, 20 GB per account | https://neon.com/docs/introduction/plans | "Free plan users get 1 GB of Postgres storage per project" | V |
| Raised from 0.5 GB on 2026-10-02 | https://neon.com/docs/changelog/2026-10-02 | "1 GB of database storage per project, up from 0.5 GB" | V |
| Metered as `logical_size` across branches | https://neon.com/docs/introduction/usage-calculations | "caps storage per project based on `logical_size` across branches" | A |
| Over the storage cap, storage-increasing operations fail | https://neon.com/docs/introduction/plans | "causes operations that increase storage ... to fail" | V |
| 100 CU-hours per project per month | same | "Free: 100 CU-hours/project/month" | V |
| New projects default to autoscaling 0.25–2 CU; project defaults don't change existing computes | https://neon.com/docs/guides/autoscaling-guide | "does not alter the autoscaling configuration for existing computes" | V |
| The autoscaler uses the 1-minute load average; downscale speed undocumented | https://neon.com/docs/guides/autoscaling-algorithm | "Every 5 seconds, the autoscaler-agent checks the 1-minute load average" | P |
| Scale to zero after 5 minutes, cannot be disabled | https://neon.com/docs/introduction/plans | "Free: 5 min inactivity; cannot disable" | V |
| Each new connection resets the timer | https://neon.com/docs/manage/computes | "Each connection resets the scale to zero timer." | V |
| Egress 5 GB per project, outbound only; exceeding it suspends compute | https://neon.com/docs/introduction/network-transfer | "Exceeding this suspends your compute until the next billing cycle" | V |
| Exhausted quotas suspend; nothing is billed | https://neon.com/faqs/free-plan-limits-and-quotas | "Existing connections drop and new ones can't open." | V |
| No card; permanent plan; the "$20 credits" link must not be used | https://neon.com/pricing | "The Free plan is permanent (not a trial); no credit card required." | V |
| Inactivity deletion applies to Azure only | https://neon.com/docs/import/azure-regions-deprecation | "Projects in AWS regions are not affected." | V |
| Branch archiving (root not exempt) | https://neon.com/docs/guides/branch-archiving | "Expect slow query response times." | A |
| Pooler is transaction mode, `query_wait_timeout` 120 s; direct recommended for long analytics | https://neon.com/docs/connect/connection-pooling | "Long-running analytics queries \| Direct" | V |
| On Free, usage is read via the project endpoint | https://neon.com/docs/introduction/usage-calculations | "you can track usage with the project detail and branch detail endpoints" | V |
| Project-scoped API keys can edit one project but not delete it; created by org admins | https://neon.com/docs/manage/api-keys | "cannot delete the project or manage who can access it" | P |
| Terms may change; continued use counts as consent | https://neon.com/platform-terms (2026-10-14) | "Databricks may make changes to the Agreement, this Schedule, and pricing" | V |
| SNI needs libpq ≥ 14 | https://neon.com/docs/connect/connection-errors | "SNI support was added to `libpq` ... in Postgres 14" | V |
| Scheduled update restarts on Free | https://neon.com/docs/manage/updates | "On the Free plan, updates are scheduled and applied automatically." | V |
| Idle availability-check wakes; CU-hour cost unspecified | https://neon.com/docs/introduction/compute-lifecycle | "Neon occasionally activates your compute to check for data availability." | A |

### GitHub

| Term | URL | Quote or value | Status |
|---|---|---|---|
| Actions free on standard runners in public repos | https://docs.github.com/en/billing/concepts/product-billing/github-actions | "free ... for public repositories that use standard GitHub-hosted runners" | V |
| No payment method means usage is blocked, not billed | same | "usage is blocked once you use up your quota" | V |
| GHCR is currently free | https://docs.github.com/en/billing/concepts/product-billing/github-packages | "Container image storage and bandwidth for the Container registry is currently free." | V |
| First publish of a package is private | https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry | "When you first publish a package, the default visibility is private." | A |
| Release assets under 2 GiB each | https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases | "Each file included in a release must be under 2 GiB." | V |
| Immutable releases | https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases | "Release assets cannot be modified or deleted" | V |
| Required reviewers: public repos only on Free | https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments | "required reviewers are only available for public repositories" | V |
| Scheduled workflows are disabled after 60 days of inactivity | https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows | "automatically disabled when no repository activity has occurred in 60 days" | V |
| Scheduled runs can be delayed or dropped | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows | "some queued jobs may be dropped" | V |

### Fallback and rejected hosts

| Term | URL | Quote or value | Status |
|---|---|---|---|
| Vercel Hobby: 4 Active CPU-h, 360 GB-h, 1M invocations; over a limit, wait 30 days | https://vercel.com/docs/plans/hobby | "you will have to wait until 30 days have passed" | V |
| Vercel functions: 2 GB / 1 vCPU, 300 s maximum, `iad1` by default | https://vercel.com/docs/functions/limitations | "Hobby: 300s default and maximum." | V |
| Vercel: non-commercial only; no load testing without authorization | https://vercel.com/docs/limits/fair-use-guidelines | "Hobby teams are restricted to non-commercial personal use only." | V |
| Vercel: card requirement not stated | https://vercel.com/docs/accounts | sign-up via email or a Git provider only | A |
| Vercel: All Deployments protection on all plans | https://vercel.com/docs/deployment-protection | "Protecting all deployments is available on all plans" | P |
| Vercel overwrites X-Forwarded-For | https://vercel.com/docs/headers/request-headers | "we currently overwrite the X-Forwarded-For header" | P |
| Prisma: Free is for evaluation; limits are workspace-wide | https://www.prisma.io/docs/postgres/faq ; https://www.prisma.io/docs/full-stack-tutorial | "intended for evaluation only and is not meant for production workloads" | V |
| Prisma credentials cannot be made read-only | https://www.prisma.io/docs/postgres/database/connecting-to-your-database | "You can't make a credential read-only" | V |
| FastAPI Cloud: internal use only | https://fastapicloud.com/legal/terms | "for your own internal use, and not on behalf of or for the benefit of any third party" | A |
| Tailscale Funnel is beta; Personal plan is non-commercial | https://tailscale.com/kb/1223/funnel ; https://tailscale.com/pricing | "only suitable for non-commercial use" | V |
| Oracle requires a card | https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm | "most users need a mobile phone number and a credit card" | V |

## Risks

### Accepted risks (no $0 mitigation removes them)

1. **Render discretionary suspension.** Render may suspend a Free service for "uncommonly high" traffic that the service initiates, with accessing an external database as the named example. That describes this design. No threshold is published, and the only documented restore is a paid plan.
   - **Mitigation:** DB traffic stays small and governed (PROJECTION: ~0.05 GB/month of Render outbound at realistic traffic; ≤ 0.74 GB of DB traffic at the governor's caps, since every analysis that reaches the database keeps its count; about 3.1 GB of all Render outbound through the Worker in the worst case before Render's own response headers, which are unmeasured; review round 2 corrected the earlier 2.8 GB, ADR-015 "Render outbound through the Worker"). The static snapshot and KV keep serving.
   - **Recovery:** switch to the fallback by changing a Worker var. Never pay, and never recreate the service on Render to get around the suspension.
2. **Cloudflare may rename the workers.dev subdomain** "for any or no reason", attempting a week's notice. Every share link would break. Only a custom domain removes this risk, and constraint 2 excludes one. The owner chooses the subdomain once.
3. **Card prompts.** Render's Terms reserve a $1 authorization hold, and prompts were reported at deploy time in 2024–25. Vercel's and Tailscale's pages say nothing either way. If a prompt appears: stop, report and decide (constraint 1).
4. **Workers Free's 100k requests per day are account-wide**, and any client sending about 1.2 requests per second exhausts them.
   - **Effect:** `/api` is down until 00:00 UTC, including KV-cached results.
   - **What still works:** the static SPA and the snapshot (starters, presets, metadata, prewarmed analyses).
   - On workers.dev at $0, nothing can rate-limit before the Worker is invoked.
5. **Neon autoscaler idle-tail behaviour.**
   - If the compute stays at 2 CU through the 5-minute tail (scenario C in [`../deployment.md`](../deployment.md)), typical traffic projects to ~120 CU-h a month (PROJECTION), above the 100 CU-h quota.
   - The governor then pauses uncached analyses late in the month, with an honest message.
   - The week-1 calibration lever is to lower the maximum CU or fix 0.25 CU.
6. **Free-tier terms can change.**
   - Neon (Databricks) can change terms, with continued use counting as consent, and can terminate on 30 days' notice.
   - Render's Terms allow suspension "for any reason".
   - GHCR's free policy comes with at least one month's notice of change.
   - Workers Logs pricing changes on 2026-12-01.
7. **A project-scoped Neon key in a Worker secret** has editor rights on the project. If the Cloudflare account or the Worker code were compromised, the database could be altered. Recovery is a restore from the release dump.
8. **Direct-origin traffic.** The onrender.com URL cannot be disabled. Every direct 403 still costs a few hundred bytes of Render outbound. A sustained direct flood of about 10 requests per second for a month would approach the 5 GB budget (PROJECTION: 26.8M requests × ~300 B ≈ 8 GB). Render would then pause the service until the next month.
   - **Mitigation:** an unpublished name with a random suffix, a minimal 403 response, and Render's own Cloudflare DDoS layer.
9. **Plain HTTP inside Render**, between its load balancer and the container (documented by Render).
10. **Mixed-version windows** of a few minutes during deploys and data releases. Results always carry their own `data_version`, and the version gate prevents cache poisoning.
11. **A Render suspension is presented as a cold start until the owner sets the pause switch** (review rounds 1 and 2; [`../security.md`](../security.md#accepted-risks), R13). What a suspended Free service answers is undocumented, so the Worker maps it to `service_waking` or `upstream_unavailable`, and the SPA waits out its 150 s budget per novel analysis, then says the service is still not answering and may be paused until next month. Since review round 2 the owner can set the edge's pause switch once they know (`deploy.yml` operation `pause-api`; `edge/src/pause.ts`): every request that would reach Render then gets 503 `api_paused` with the date, and the SPA shows one page saying the API is paused and until when. Nothing sets it automatically. The static floor keeps serving either way.

### Other risks and their mitigations

- **What a Worker fetch receives while Render spins up is undocumented.** It could be a held request or an empty 503 carrying `x-render-routing: dynamic-hibernate-error-503`. Both are mapped to `service_waking`.
- **The coldest worst case** (spin-up ~70 s + a query at 0.25 CU) can exceed a 125 s proxy read timeout, if one applies, giving a 524 once. The retry then joins the single-flight computation or hits the cache.
- **Neon branch archiving** after quiet days slows the first query by an unmeasured amount.
- **Neon compute update restarts** drop connections. `check_connection` replaces them; a test terminates backends and expects no 5xx.
- **The governor's model can drift.** Ground-truth polling and reconciliation correct it, and the owner can apply the brake manually.
- **GitHub scheduled workflows** auto-disable after 60 days without repo activity and may be delayed or dropped. A weekly workflow, `schedules-check.yml`, fails, and so emails the owner, when a schedule is disabled, a monitor has stopped running, or `main` has had no commit for 28 days; it re-enables nothing, so re-enabling stays manual. If it is disabled itself, the owner's monthly look at the Actions tab is the backstop ([owner checklist](../deployment.md#owner-checklist)). (Amended in review round 2: this line said a monthly check by hand covers it; review round 1 made the check a weekly workflow, ADR-014 amendments.)
- **`_headers` may not apply to the SPA-fallback `index.html`.** A meta CSP is the backstop; check with curl after deploy. Under `wrangler dev`, whose asset worker is Cloudflare's, the rules are chosen by request path after the fallback: a missing `/assets/*` or `/snap/<id>/*` file gets `index.html` with that path's one-year `immutable` Cache-Control (review round 1; the SPA re-fetches such a snapshot file once with `cache: "reload"`, [`../frontend.md`](../frontend.md#static-snapshot)).
- **KV eventual consistency** costs extra misses, never wrong results, because keys are version-scoped.
- **Snapshot coverage of real traffic is unknown.** If most users build novel specs, the latency gain is limited to the home and starter paths.
- **Complexity for a junior engineer:** four tiers plus a governor. Every tier fails toward the next one, has its own tests, and is covered by a runbook.

## Consequences

- **Share links.** They depend only on the workers.dev hostname. The API host, the database project and the data release can all change behind the Worker without breaking a link.
- **Quota failures.** Whatever runs out (Worker daily, KV, governor caps, Render monthly or discretionary, Neon monthly), the home page, the starter analyses, presets, metadata and about 130 prewarmed analyses keep working as static files. A browser loads them without invoking the Worker: its page loads are navigations and its fetches name files that exist. A non-navigation request for a path without a file (an unfurler or crawler fetching a share link, a stale tab's missing `/snap/` file) invokes it, and gets the platform's 429 or Error 1027 once the Worker's daily requests are spent (review round 2; "Request flow").
- **What novel analyses pay.** They carry all the cold-start and quota cost, and their messages are honest: waking, rate-limited, daily capacity reached, paused until a date.
- **Statistics are untouched.** Caches only replay origin responses for the same globally unique version. Cost optimizations (parallel query off, year pushdown) need the §9 proof first.
- **New code.**
  - a Worker of roughly 300–400 lines (proxy, KV gate, governor, prewake);
  - snapshot generation in CI (projected 10–15 extra CI minutes per deploy, unmeasured);
  - API changes for edge auth, lanes, the in-process snapshot and a lazy pool.
- **New secrets** to rotate: the edge secret (two values accepted during rotation), the ops token, the IP HMAC key, the Cloudflare API token, the Render deploy hook and, optionally, the Neon project key.
- **New error codes** are additive v1 changes. Candidates: `rate_limited`, `overloaded`, `analysis_timeout`, `daily_capacity_reached`, `capacity_paused`, `capacity_unavailable`, `database_paused`, `data_version_mismatch`, `payload_too_large`, `forbidden_origin`, plus the Worker-generated `service_waking` and `upstream_unavailable`. Names are finalized in ADR-012.
- **The owner's steps** are in [`../deployment.md`](../deployment.md#owner-account-setup-19). Implementation then fills the from-scratch deploy guide and the configuration reference there.

## Unknowns to measure after deploy (quota-budgeted)

**Total budget:** at most 150 Worker requests, at most 3 Neon CU-h (3% of a month, PROJECTION), at most 15 Render wakes, and negligible bandwidth. No load tests.

| Unknown | Method | Budget |
|---|---|---|
| What a Worker fetch gets while Render spins up (status, headers, hold time), and the total spin-up for this image | ≤ 5 cold requests 20+ min apart | 5 Worker requests, 0 Neon |
| Whether Render's own health checks count as inbound traffic (does the service still spin down at 15 min?) | watch the Events timeline | 0 |
| Headers that reach the app through the Worker (`CF-Connecting-IP`, `X-Forwarded-For`, `Rndr-Id`), and whether `X-ATUS-Client-IP` arrives unchanged | ops-only echo build, removed afterwards | ≤ 5 requests |
| Whether the 125 s proxy read timeout applies to Worker→Render | ops-token DB-free sleep endpoint (130 s), once | 1–2 requests |
| Neon autoscaler ramp and downscale; `compute_time_seconds` per wake session | ≤ 6 worst-case runs including F66 broad selectors, each followed by idle-to-suspend | ≤ 1.5 CU-h |
| Real latency matrix on Neon 0.25–2 CU (≤ 10 worst cases + typical); calibrate `statement_timeout`, deadline and lanes | URL-targeted replay script (§16) | ≤ 30 requests, ≤ 1 CU-h |
| Render↔Neon round-trip time; Neon wake latency; unarchive delay after a quiet day | `Server-Timing` on probes | ≤ 10 requests |
| Whether `_headers` applies to the SPA-fallback `index.html`; whether `/api` without a trailing path matches | curl a deep link | 0 Neon |
| Whether a non-navigation GET of a path without a file invokes the Worker, as Cloudflare documents (review round 2; `wrangler dev` cannot show it) | one `curl` of `/explore` without `Sec-Fetch-Mode: navigate`, then the Worker's request count in its metrics | 1 Worker request |
| Metered Neon storage after restore; 1e9 B or 1 GiB (`branch_logical_size_limit`) | read-only API GET | 1 API call |
| Whether `GET /projects/{id}` wakes the compute or unarchives the branch; freshness of `compute_time_seconds`; whether Free can create project-scoped keys | observe while suspended | a few API calls |
| Whether `ALTER ROLE atus_api SET ...` is accepted on Neon; whether `sslrootcert=system` works with the image's libpq | restore script and image build | 0 |
| Render outbound and Neon `data_transfer_bytes` per uncached analysis (checks the ~40 KB and ~80 KB estimates) | dashboards after the replay; for the ungoverned `/api/health`, the headers Render adds to the 571 B the API sends (`curl -sv "$ORIGIN/health"`; ADR-015 "Render outbound through the Worker", review round 2) | 1 Render request |
| Render behaviour during an env-var switch; whether image pulls consume pipeline minutes; hook digest encoding | the first release switch; Billing page | 0 |
| GHCR visibility after the first `GITHUB_TOKEN` push | packages page | 0 |
| Worker CPU ms per request; Workers Logs events per request; whether invocation logs, off until this is known ([ADR-014](ADR-014-observability.md)), would record client IPs or headers | the Worker's metrics; Workers Logs | 0 |
| Minimal Cloudflare token for `wrangler deploy` with assets, KV and a Durable Object binding | dry run | 0 |
| Whether Render offers Virginia for Free | the creation form, before creating the Neon project | 0 |
| Cost of Neon's availability-check wakes (`compute_time_seconds` drift with no traffic, 3–7 days) | project GET | 0 |
| Cannot be tested without exhausting a quota: the exact response after 100k/day (a 429, or a 1027 page whose status is undocumented) and Neon's error when exhausted | never induced. Only a non-JSON 429 gets the SPA's daily-allowance page; a 1027 page with a 5xx status is retried as a cold start and one with another 4xx is a generic error. Neon's error maps to `database_paused` | — |

## Amendments (implementation, 2026-10-21)

The decision is unchanged. This section records where the implementation (code, workflows and scripts at the commit that adds it) differs from the text above, and why. Nothing is deployed: the owner accounts do not exist yet, `releases/current.json` says `"status": "unpublished"`, and `edge/wrangler.jsonc` still holds the placeholder KV namespace id, which the deploy job refuses. Nothing below describes a live service.

### Cross-references

- The labels "to be finalized in ADR-012" on "Quota governor" and "final values in ADR-012" on its "Starting budgets" name the wrong ADR. The governor, its budgets and the edge tiers are finalized in [ADR-015](ADR-015-edge-cache-snapshot-and-governor.md), whose title is "Edge result cache, static snapshot and quota governor" (broader than the "persistent result cache" under **Related**).
- [ADR-012](ADR-012-load-and-quota-protection.md) holds the API's own limits, the "Bounded work" values (calibrated on the emulation profile on 2026-10-21, its "Calibration (§13)") and the API's error codes. [ADR-013](ADR-013-data-releases-and-serving-db.md) finalizes "Release switch". [ADR-014](ADR-014-observability.md) finalizes the IP truncation in logs and the monitor cadence.
- [ADR-016](ADR-016-short-share-links.md) (short share links, deferred) was added.
- Error code names: the API's codes are in ADR-012 "Error mapping". The Worker-generated `service_waking`, `upstream_unavailable`, `capacity_unavailable` and `capacity_paused` are specified in ADR-015 and defined in `edge/src/proxy.ts`, `edge/src/ops.ts` and `edge/src/governor.ts`; `api_paused` (review round 2) in `edge/src/pause.ts`. The client-facing table is `docs/api.md`, "Errors".

### Deploy workflow (`.github/workflows/deploy.yml`)

- **Order.** "Deploy" lists hook, wait, snapshot, `wrangler deploy`, smoke. The workflow runs the jobs `preflight`, `build` (`image.yml`, no build cache), `push`, `snapshot`, `deploy`.
  - The snapshot is built in its own job, from the pushed digest, before the `production` approval and before any deploy step, so a failed restore or snapshot deploys nothing.
  - The `deploy` job then runs hook, wait, `wrangler deploy` and the smoke.
  - The snapshot's inputs are the ones in the "Edge tiers" table: the release dump restored into `postgres:16` with `scripts/ops/roles.sql`, and the digest run read-only. Only the order changed.
- **Which release.** The dump is the one named in the committed `releases/current.json` (`scripts/ops/release_manifest.py`). It is downloaded and checked against the recorded sizes and SHA-256s. `preflight` refuses to run unless the file says `published`. Reason: a deploy can only serve a release that someone committed.
- **Triggers.** Only `vMAJOR.MINOR.PATCH` tags, and manual runs from `main`. `tests/deploy/test_workflows.py` asserts the triggers and the per-job permissions.
- **CI gate** (amendment, review round 1). `preflight` runs `scripts/ops/github_ops.py deploy-gate`: a successful `ci.yml` run on a push to `main` must exist for exactly `GITHUB_SHA`, and the commit must be on `main` (a tag can point at any commit). Before, the only guard was the runbook's "CI is green", while the deploy itself runs only the Worker's config tests and the SPA build. Not every commit on `main` gets a run (a multi-commit push runs CI on its head; a newer push cancels a run still waiting), so the refusal says whether the commit's run is unfinished, was cancelled (re-run it) or never came (tag a commit with a green run) (amendment, review round 2).
- **Smoke.** "At most 40 requests" is split in two:
  - `scripts/ops/deploy_smoke.py --max-requests 30`, which recomputes 2 snapshot entries live and checks the origin lock;
  - one Chromium journey (`frontend/playwright.deployed.config.ts`), capped at 6 `/api` requests in `frontend/e2e/deployed/starter.deployed.ts`.
- **Wait.** `scripts/ops/render_deploy.py wait` allows 15 minutes. `/health` reports the commit, not the digest, so a re-run that pushes a new digest of a commit already running cannot be observed. The step then warns instead of passing.
- **Rollback targets.** The job summary records the previous build, its GHCR digest and the previous Worker versions. `.github/workflows/ghcr-cleanup.yml` (weekly) keeps the newest 20 digests.
- **Render's configured image** (amendment, review round 1). The deploy hook's `imgURL` deploys a digest without changing the service's Image URL ("for future deploys, your service continues to use the tag or digest in its settings", render.com/docs/deploying-an-image, read 2026-10-21), so the bootstrap digest would come back on any deploy not made through the hook, and fail once the cleanup deleted it. After a deploy passes its smoke, the job `promote` tags its digest `live` and `deployed-<commit>` (same manifest, `imagetools create --prefer-index=false`, digest re-checked). The service's Image URL is `:live` (owner step 2.7), the cleanup never deletes `live`, and `deep-check.yml` compares `/health`'s build with `live`'s commit (`github_ops.py live-build`). The previous digest in the rollback notes comes from `deployed-<commit>` (`github_ops.py deployed-image`), not from `sha-<commit>`, which a re-run of the running commit moves before the lookup. Not exercised against GHCR or Render: the tag move is checked by digest in the job itself, and whether "Save and deploy" pulls the configured image is undocumented (with `:live` it does not matter). Nor is it documented that the run's `GITHUB_TOKEN` may list a user-owned package's versions, so `deep-check.yml` fails, rather than skipping the comparison, when the lookup is refused (401/403) or finds no `live` tag; its first run after the monitors start is the test (amendment, review round 2; operations.md "The deep check cannot read the live tag").
- **What the pushed image passed.** In a deploy run, `image.yml` lints, gates the image, runs `tests/deploy` against it (TLS to Postgres included), smokes the production stack plain and under emulation, and runs Trivy.
  - The Playwright suite does not run in `deploy.yml`. It runs in `ci.yml`: job `e2e` against the vite-preview build and the API from source, and job `e2e-stack` on `ci.yml`'s own cached image build of the same commit. `e2e-stack` also runs the deploy's own gates, `deploy_smoke.py` and the browser smoke, against the local edge (amendment, review round 1), and `preflight` requires that run to have passed (the CI gate above).
  - "Tests it (… Playwright)" therefore holds for the commit, not for the exact pushed bytes.
  - The projected 10–15 extra CI minutes for the snapshot remain unmeasured.
- **Trivy** is a release binary pinned by version and by the archive's SHA-256, not an action pinned to a commit. The reason is in the comment in `.github/workflows/image.yml`: the action's tags were hijacked in March 2026 (GHSA-69fq-xp46-6x23).
- **Must-not-do assertions.** CI asserts:
  - `edge/wrangler.jsonc` (`edge/test/node/config.test.ts`): no `cache` block, no environments, routes or custom domains, `workers_dev: true`, `preview_urls: false`, `run_worker_first` with both forms, SPA fallback, required secrets;
  - in `tests/deploy/test_workflows.py`: deploy triggers and permissions, and that no other workflow can deploy or push.

  Not asserted by any test: the absence of a `render.yaml`, Render auto-deploy off, no Git connection on Render, Workers Builds not connected, no card, no Neon branches. These remain owner checks ([`../deployment.md`](../deployment.md#owner-account-setup-19), and step 8 there).
- **Environments.** Besides `production`, the daily quota report uses a GitHub environment `ops` that holds `EDGE_OPS_TOKEN`.

### Fallback (P3, Vercel Hobby): a documented procedure, not ready

- None of the five "Preconditions that implementation must keep ready" exists in the repository:
  - no Vercel entry module, no `vercel.json` and no import test;
  - no `GATE_INFLIGHT` in-flight gating in the governor (it tracks leases only to charge compute);
  - no Vercel budgets;
  - no origin mode that sends `x-vercel-protection-bypass`;
  - no `deploy-vercel` job.

  They were not built in Phase 5.
- The Worker reads its origin only from the secret `RENDER_ORIGIN` (`edge/src/index.ts`, `edge/src/upstream.ts`). `API_ORIGIN` and `GATE_INFLIGHT` are read by no Worker code; `deploy/edge/entrypoint.sh` no longer passes them as vars, and the configuration reference no longer lists them (2026-10-21).
- **Consequence.** "Recovery: switch to the fallback by changing a Worker var" (accepted risk 1) and the graft "fail over to a different provider by changing one Worker var" do not hold for the current code.
  - After a Render suspension, only static content and KV-cached results keep working.
  - That lasts until the preconditions are built and the switch procedure is run.
  - Pointing the Worker at another HTTPS origin is `wrangler secret put RENDER_ORIGIN` (a secret, not a var). For Vercel, that alone is not enough: the bypass header and global in-flight gating are missing.

### Worker details

- **Size.** At 0066649, `edge/src` was 14 TypeScript files and 3,279 lines, comments included (`wc -l`). "Consequences" projected roughly 300–400 lines. (Review round 2 dated the figure: it first said 13 files and 2,816 lines, true at 92aac34, before review round 1 added `edge/src/snapshot.ts`.)
- **Error mapping** (`edge/src/proxy.ts`, `edge/src/routes.ts`, `edge/src/index.ts`):
  - `upstream_unavailable` is a 502, or a 503 while the circuit breaker is open. It carries `details.reason`: `connect_failed`, `circuit_open`, `platform_error`, `origin_timeout` (a 524), `broken_response` or `not_configured`. Reason: only some causes mean the API may have computed the request, and the SPA retries those once.
  - uvicorn's own plain-text 503 becomes `overloaded`.
  - A wrong method on a known route gets 405 `method_not_allowed`. The hidden ops route answers 404 instead.
  - An analysis POST that is not JSON gets 415 `unsupported_media_type`.
- **Headers.** The Worker's security headers also include `Permissions-Policy` (`edge/src/http.ts`).
- **Prewake.** The answer is `{origin: warm|waking|unknown, wake: sent|recent|off|circuit_open}` (`edge/src/wake.ts`).
- **Pause switch** (review round 2, `edge/src/pause.ts`). Two optional Worker secrets, `API_PAUSED_UNTIL` (a UTC date or time, or `unknown`) and `API_PAUSED_REASON`, set and cleared only by `deploy.yml`'s operations `pause-api` and `resume-api` (job `api-switch`, behind the `production` approval) with `wrangler secret bulk`. While set and not past, every request that would reach Render (analyses and lookups after the edge tiers, `/api/health`, the prewake) gets 503 `api_paused` with `Retry-After` and `details.until`/`reason`, before the governor. They are secrets rather than vars only because secrets can change without a rebuild and survive every `wrangler deploy`; a var would be reset by the next deploy.
- **KV and governor** are as finalized in ADR-015. These differ from the text above:
  - the store gate also requires `X-Analytics-Version`;
  - a client prefix is also capped at 300 admissions a day, refunded ones included;
  - the compute charge is sized from the API's `Server-Timing: db;dur=`.
- **Failing closed.** Analysis POSTs and, since review round 1, the metadata GETs the static snapshot cannot answer (counted lookups) are governed, and both fail closed. The Worker answers the other metadata GETs from the snapshot without the governor. `/api/health` and the prewake are not governed (ADR-015, "What this does not protect against").
- **Neon key.** `NEON_PROJECT_ID` is committed empty in `edge/wrangler.jsonc` and `deploy.yml` does not set it, so a first deploy runs the governor in conservative mode.
- **Unknowns tooling.** The "ops-only echo build" and the "ops-token DB-free sleep endpoint (130 s)" under "Unknowns" do not exist in `src/` or `edge/src/`. They must be written, and removed again, before those rows can be measured. The URL-targeted replay exists: `scripts/replay_deployed.py`, with a hard budget of 40 requests and the F66 broad selector included.

### API and database details

- **In-process snapshot.** With `ATUS_EXPECTED_DATA_VERSION` set (production), the snapshot is pinned and never re-read for its age; "refreshed at most every 10 minutes" no longer applies. A stale-if-error fallback was added (ADR-012). Reason: every re-read can wake Neon.
- **OpenAPI.** `/openapi.json` is off in production as decided. The schema is committed as `frontend/openapi.json` (`scripts/export_openapi.py`, drift-checked in `ci.yml`), and the site publishes that copy as a static asset at `/openapi.json`, as decided ("Exposure"). It sits outside `/api`, so serving it runs neither the Worker nor the API.
- **TLS check.** The libpq-version and `sslrootcert=system` checks are in `tests/deploy/test_container_image.py`, which the image job runs after the build; the Dockerfile itself checks nothing. The image sets `SSL_CERT_FILE`, because the libpq inside the psycopg binary wheel has no usable default CA path ([`../deployment.md`](../deployment.md#tls-to-the-database)).
- **Optimizations.**
  - `max_parallel_workers_per_gather` is a per-request API setting (`ATUS_API_MAX_PARALLEL_WORKERS`, unset by default), not a role default (`scripts/ops/roles.sql`).
  - The year pushdown was adopted with the §9 proof, as a `MATERIALIZED` CTE (ADR-013, amendments).
- **Calibration.** The §13 emulation runs (2026-10-21) kept the statement timeout, the lanes and the parallel-worker setting at their code defaults ([ADR-012, "Calibration (§13)"](ADR-012-load-and-quota-protection.md#calibration-13)). The post-deploy replay must confirm them on Neon, and the maximum CU still awaits week-1 Neon data. The measurements this ADR quoted are superseded: see [Calibration (§13): superseded measurements](#calibration-13-superseded-measurements) below.

### Release switch

- Step 2 ("CI builds the snapshot … but does not deploy it yet") is the `snapshot` job of a `deploy.yml` run. That run needs `releases/current.json`, updated for the new release, committed first. No snapshot-only workflow exists.
- Step 4 is the same run's `deploy` job, after approval. It also redeploys the API image by digest through the hook.
- Data rollback restores the two Render variables, then restores the previous `releases/current.json` on `main` and runs `deploy.yml` from `main` ([operations.md, "Rollback"](../operations.md#rollback)). Preflight runs only `main` or a `vX.Y.Z` tag, and an old commit's run would roll the code back too.
- The governor's `NEON_PROJECT_ID` and `NEON_API_KEY` must move with the project (ADR-015).

### Verified locally only

- **The local Worker.** The production-mode local stack (`docker-compose.prod.yml`, profile `edge`) runs the Worker under `wrangler dev` in local mode (`deploy/edge/Dockerfile`). KV and the Durable Object are simulated on disk, and no Cloudflare account is involved.
- **The local relay.** The Worker accepts a plain-HTTP origin only on loopback (`edge/src/upstream.ts`). In the stack, a TCP relay (`deploy/edge/origin-relay.mjs`) therefore forwards 127.0.0.1:10000 to the API container. The relay exists only there. Production has no relay: `RENDER_ORIGIN` is the HTTPS onrender.com URL.
- **Browsers.** Chromium, WebKit and Firefox pass locally ([`../frontend.md`](../frontend.md)).
  - Playwright's Firefox does not launch natively on the development machine (macOS 27), so there it runs in the official Playwright Linux image (`scripts/e2e_in_container.sh`; vite-preview mode, 13 of 13 on 2026-10-21). CI runs it natively on Linux (`ci.yml` jobs `e2e` and `e2e-stack`); no stack-mode Firefox result exists yet.
  - No CI run of the Phase 5 workflows is recorded in the repository, and the remote-tracking `origin/main` in this clone is the Phase 4 commit `387fc2d`.
  - The post-deploy browser smoke runs in Chromium only.

### Calibration (§13): superseded measurements

The decision is unchanged. The §13 load tests ran locally on 2026-10-21 against the production-mode stack with the real serving release, the API at Render Free's 0.1 CPU / 512 MB and the database at Neon Free's 0.25 CU floor and 2 CU ceiling ([`../../loadtests/results/2026-10-21-emulation.md`](../../loadtests/results/2026-10-21-emulation.md), "the record"). [ADR-012, "Calibration (§13)"](ADR-012-load-and-quota-protection.md#calibration-13) takes its decisions from them. Nothing ran on Neon, Render or Cloudflare.

Their numbers supersede three parts of this ADR, which stay above as written: the audit's measurements of the code at 387fc2d.

**1. "Measured facts that drive the design".** For the current code:

| Fact | Above (audit, 387fc2d) | Now (record section) |
|---|---|---|
| API memory | ~70 MB peak | peak RSS (VmHWM) at most 76.1 MiB, cgroup peak at most 59.3 MiB, of 512 MB, with the 256-entry result cache full (5.7). Also "~70 MB" under "API process model" |
| API cold start at 0.1 CPU, before the platform's own spin-up | first `/health` p50 7.65 s; first `/meta` up to 11.04 s | first `/health` p50 13.12 s (DB at 0.25 CU) and 12.88 s (2 CU), max 13.85 s; first `/api/v1/meta` p50 13.72 s and 13.21 s, max 14.28 s. Start-up costs 1.3–1.4 CPU-seconds of import (4). Also "the measured 7–11 s process start" under "Rejected alternatives" |
| Share of request time in the DB | ≥ 98.4% at 0.25 CU | 98.7–99.1% of uncached single-year latency at 0.25 CU, 96.6–97.0% at 2 CU, 99.7–99.9% for the worst cases; the API's `Server-Timing` `app` time 1.1–2.6 ms per worst case at p50 (5.8) |
| Worst cases at 0.25 CU | ~33–52 s (pooled 22 years p50 51.7 s) | pooled 22-year leisure and 23-year trends 21.35–21.81 s at p50 (max 25.80 s); the F66 broad selector 33.49–34.99 s pooled or trended (max 42.02 s), and 69.21 s compared by sex (max 71.02 s); p99 of all 30 requests 70.52 s (5.3). Also "47–52 s" under "Rejected alternatives" (P3 at a fixed 0.25 CU) |
| Worst cases at 2 CU | 2.7–3.4 s | 2.03–2.99 s at p50 by kind, p99 3.03 s (5.3) |
| Single-year estimate with SE | p50 6.95 s at 0.25 CU, 0.38 s at 2 CU | p50 0.99 s at 0.25 CU (p99 4.43 s) and 79 ms at 2 CU (p99 211 ms) over 59 distinct corpus specs (5.2). Not the same request: the audit repeated one spec (`010101`, 2025), so the two p50s are not a like-for-like speed-up |
| Costliest specs | broad tier-1 selectors ~2.6× pooled leisure (illustrative) | the F66 broad selector pooled or trended 1.6 × pooled leisure (W1), and compared by sex about 3.2 × W1, at 0.25 CU (5.3) |
| Saturation | DB CPU saturates from 2 concurrent at 0.25 CU; at 16, 6 × 503 | through the real lanes: at 0.25 CU throughput peaks at 1 concurrent client (1.14 analyses/s), `503 overloaded` starts at 4 clients and reaches 246 of 354 requests at 16; at 2 CU it plateaus at about 24/s from 2 clients, with no 503 up to 16 (5.4) |
| Parallel query off | 40.6 / 39.5 s → 19.2 / 19.1 s (probe) | `ATUS_API_MAX_PARALLEL_WORKERS=0`: at 0.25 CU the F66 compare 69.21 → 38.58 s and a single-year estimate 0.99 → 0.70 s at p50; at 2 CU slower, worst-case p99 3.03 → 3.53 s (5.2, 5.3). Left unset (ADR-012) |
| Serving DB size | cluster sum 650,458,708 B (prototype) | the release artifact restored: `sum(pg_database_size)` 649,903,180 B, 65% of 1,000,000,000 B; dump 336,549,536 B (3) |

**Later measurement: cold start (2026-10-21).** The cold-start row above is for the image at 9afd1b4, which shipped no compiled standard library. A later A/B ([`../../loadtests/results/2026-10-21-cold-start-ab.md`](../../loadtests/results/2026-10-21-cold-start-ab.md)) measured at 0.25 CU, n=8, under the same method:
- the image at e5ca1fd, which precompiles the standard library (commit 5844ab7): first `/health` p50 5.76 s (max 6.51 s), first `/api/v1/meta` p50 6.12 s (max 6.84 s), 0.57–0.67 CPU-seconds of start-up;
- an image built before that commit, for comparison: first `/health` p50 12.69 s.

The "App start" term of the coldest-path projection below still uses the earlier image's figures. It was not recomputed.

The year pushdown is adopted (ADR-013). The statements per request and the pool's behaviour changed with the implementation, not the calibration: [ADR-012](ADR-012-load-and-quota-protection.md) and [`../api.md`](../api.md#statements-per-request).

**2. "Coldest path for a novel analysis"** (Request flow), recomputed. **PROJECTION**, from these terms in sequence (the API wakes Neon only on its first database request):

- Render spin-up: about a minute (documented, unmeasured for this image). Whether it includes the app start is still unknown.
- App start: 13.2–13.7 s at p50 to the first `/api/v1/meta`, at most 14.28 s, at 0.1 CPU. Measured with the database already awake; it includes the first snapshot read.
- Neon wake: a few hundred ms to a few seconds (documented, unmeasured).
- The query. At 2 CU: 0.079 s at p50 for a single-year estimate, at most 3.04 s for a worst case. At 0.25 CU: 0.99 s at p50 (at most 4.60 s) for a single-year estimate, at most 25.80 s for the non-F66 worst cases, and at most 71.02 s for the F66 compare. The statement timeout (85 s) and the deadline (90 s) cap it. How fast a freshly woken compute leaves 0.25 CU is unmeasured (F41), so the floor's figures are the conservative ones.

| Novel analysis | Coldest path (PROJECTION) |
|---|---|
| single-year estimate | about 60–82 s: 60 s + 0–14.3 s + 0.3–3 s + 0.08–4.6 s |
| pooled or trend worst case, non-F66, at 0.25 CU | up to about 103 s: 60 s + 14.3 s + 3 s + 25.8 s |
| F66 compare at 0.25 CU (the slowest measured request) | about 134–148 s: 60 s + 0–14.3 s + 3 s + 71.0 s, so about 148 s when Render's minute excludes the app start, about 134 s when it includes it |
| bound: any analysis cut off by the 90 s deadline | about 167 s (60 s + 14.3 s + 3 s + 90 s), answered with 503 `analysis_timeout` |

This replaces "about 65–80 s in the typical case and up to ~125 s in the worst".

- **The 125 s proxy read timeout.** If it applies to the Worker→Render hop (an unknown above) and Render holds a request through its spin-up (also unknown), the F66 compare's cold path exceeds it. The visitor then gets a 524, which the Worker answers as `upstream_unavailable` (`origin_timeout`). The API still finishes and caches the analysis, and the SPA's one retry for that reason joins it or hits the cache. This updates "The coldest worst case (spin-up ~70 s + a query at 0.25 CU)" under "Risks".
- **The SPA's 150 s analysis budget** ([`../frontend.md`](../frontend.md#cold-start-retries-and-service-states)). About 148 s only just fits; the 167 s bound does not. A visitor who runs out of budget sees a time-out, while a computation that completes is cached for "Try again".
- **Prewake** still overlaps the first two terms with the time a visitor spends building a spec.

**3. The compute basis** ("Database: Neon"). The decision stands: autoscaling, minimum 0.25 CU, maximum 2 CU. Its basis now reads: worst cases at 2.03–2.99 s (p50) at 2 CU against 21.35–69.21 s at 0.25 CU, the F66 compare included; the floor is 10–23 × slower. The calibration lever's cost changes too. At a fixed 0.25 CU the worst cases take 21–22 s, the F66 broad selector 33–35 s, and its compare 69.21 s at p50 (71.02 s max), not "47–52 s". With `ATUS_API_MAX_PARALLEL_WORKERS=0`, once its §9 proof passes, the compare drops to 38.58 s at p50 (40.71 s max), as ADR-012's week-1 rules say.
