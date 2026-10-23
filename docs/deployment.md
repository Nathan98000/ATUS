# Deployment

This is the **first version** of the deployment document. It was written with the hosting decision ([ADR-011](adr/ADR-011-hosting.md)) on 2026-10-20.

**Status: not deployed.**

- Nothing here has been verified against a live URL. The README will show a live URL only after it has been checked with curl and Playwright.
- Numbers come in two kinds:
  - **measured**: from the Phase 5 audit ([`operations.md`](operations.md), [`evidence/phase5/01-audit-timed.md`](evidence/phase5/01-audit-timed.md)), which measured the code before the fixes, and from the local load tests of the current code under free-tier emulation ([`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md));
  - **PROJECTION**: arithmetic on the traffic model below, with its assumptions stated.
- Free-tier terms were read on the official pages listed in each table, all accessed 2026-10-20. ADR-011 has the full citation list with quotes.
- **Environment-variable and secret names are final.** The [configuration reference](#configuration-reference) lists them; tests hold them to the code, the templates and the workflows.

## Architecture

```mermaid
flowchart LR
  U["Browser"] -->|"HTTPS"| EDGE{"Cloudflare edge: path?"}

  subgraph CF["Cloudflare Workers Free: WORKER.ACCOUNT-SUBDOMAIN.workers.dev"]
    EDGE -->|"files that exist: /, /assets/*, /snap/*; navigations to /explore, /analysis/*, /about"| S["Static assets: SPA, snapshot of metadata and prewarmed results, _headers. Free, not metered"]
    EDGE -->|"/api, /api/*; any non-navigation request that matches no file"| W["Worker: allowlist, 64 KiB cap, edge auth, security headers"]
    W -->|"get / gated put"| KV[("Workers KV: result cache")]
    W -->|"admit / complete"| DO[["Durable Object: quota governor (SQLite)"]]
  end

  W -->|"HTTPS + X-ATUS-Edge-Auth + X-ATUS-Client-IP"| LB

  subgraph RD["Render Free web service, Hobby workspace"]
    LB["Render load balancer: TLS ends"] -->|"HTTP on PORT 10000"| API["API container: 1 uvicorn process, pool max 4"]
  end

  API -->|"TLS verify-full, direct endpoint, role atus_api"| DB[("Neon Free: Postgres 16, aws-us-east-1, 0.25 to 2 CU")]
  DO -.->|"usage poll while active: GET /projects/ID"| NAPI["Neon API"]

  subgraph GH["GitHub Free, public repo"]
    CI["Actions: CI, deploy behind approval, monitors"]
    REG[("GHCR: atus-api image")]
    REL[("Releases: serving DB dump")]
  end

  CI -->|"push image"| REG
  REG -.->|"pulled by digest via deploy hook"| API
  CI -->|"wrangler deploy: SPA, snapshot, Worker"| S
  REL -.->|"owner-run restore script"| DB
```

**Trust boundaries:** browser → Cloudflare edge → Worker → Render load balancer → API → Neon. The full request flow, proxy chain and client-IP rules are in ADR-011. The essentials:

- Only the Worker's copy of `CF-Connecting-IP` is trusted, and only when it arrives with a valid `X-ATUS-Edge-Auth`.
- The API ignores `X-Forwarded-For` and Render's own `CF-Connecting-IP`. Because the Worker reaches Render through another Cloudflare zone, the latter is always the Worker address `2a06:98c0:3600::103`.
- Requests that skip the Worker and hit onrender.com directly get a minimal 403 with zero database work.

## Providers and hostnames

Placeholders stay until the owner picks the names.

| Component | Provider and plan | Hostname | Public? |
|---|---|---|---|
| SPA, static snapshot, `/api` proxy | Cloudflare Workers Free (static assets + one Worker) | `https://<worker>.<account-subdomain>.workers.dev`; recommended `<worker>` = `atus` | **Yes.** The only hostname in share links |
| Result cache | Workers KV Free, one namespace | none (binding) | No |
| Quota governor | Durable Object (SQLite backend) on Workers Free | none (binding) | No |
| API | Render Free web service (0.1 CPU / 512 MB), image-backed | `https://<service>.onrender.com`; recommended `<service>` = `atus-api-<6 random chars>` | Reachable but never linked; secret-gated (403) |
| Database | Neon Free, Postgres 16, `aws-us-east-1`, direct endpoint | `ep-<endpoint-id>.us-east-1.aws.neon.tech:5432` | No (TLS + password; `atus_api` is read-only) |
| Image registry | GHCR, public package | `ghcr.io/nathan98000/atus-api@sha256:<digest>` | Yes (image only, no secrets or data) |
| Backup artifact | GitHub Releases, immutable | `https://github.com/Nathan98000/ATUS/releases` | Yes (public BLS data) |
| CI/CD and monitors | GitHub Actions, public repo | — | — |
| Fallback API (**not provisioned, not implemented**) | Vercel Hobby: a documented manual procedure whose preconditions are not built (ADR-011 "Fallback" and its amendment) | `https://<project>.vercel.app` | Would be protected by Vercel Deployment Protection |

## Realistic demo traffic (the model the budget uses)

Everything in this section is **PROJECTION**. It is an assumption about how a portfolio demo is used, not a measurement.

| Parameter | Assumed value |
|---|---|
| Visits | 30 per typical day, over about 12 waking hours, plus one spike day per month with 300 visits (for example, a social-media share). A month = 30 typical days + 1 spike day = 1,200 visits |
| Static requests per visit | about 10 (HTML, JS, CSS, snapshot files). Unmetered |
| Analyses viewed per visit | 3: 1.5 from the static snapshot, 0.5 KV hits (repeated share links), 1.0 uncached (reaches Render and Neon) |
| Wake calls per visit | 0.5 (entering Explore, or opening a share link that is not in the snapshot) |
| Worker requests per visit | 2.0 = 1.5 analysis POSTs + 0.5 wake calls. Metadata is static |
| Uncached analyses | 1,200 per month; 30% multi-year ("heavy") |
| Visits with at least one uncached analysis | about 60%, i.e. 18 per typical day |
| Neon wake sessions | 16 per typical day (visits within 5 minutes merge) |
| Render wake sessions | 14 per typical day (visits within 15 minutes merge) |
| Monitoring | edge check every 30 minutes (48 per day; 1 Worker request each, no Render or Neon wake); deep check every 6 hours (4 per day; 1–8 Worker requests, its retries while Render wakes (`--max-requests 8`), + a Render `/health` wake, no DB); daily quota report (1 Worker request) |

## Free-tier budget

Columns:

- **Limit**: the provider's documented allowance.
- **Projected use**: PROJECTION under the traffic model above.
- **Headroom**: how much of the limit is left.
- **On exhaustion**: what the provider does.
- **Degradation**: what visitors experience.

Every source was accessed on 2026-10-20.

### Cloudflare (Workers Free)

| Quota | Limit | Projected use | Headroom | On exhaustion | Degradation |
|---|---|---|---|---|---|
| Worker requests (account-wide) | 100,000/day, reset 00:00 UTC ([limits](https://developers.cloudflare.com/workers/platform/limits/)) | up to 141 on a typical day (60 from visits + up to 81 from monitoring: 48 edge checks, 4 deep checks of 1–8 requests, 1 quota report); up to 681 on the spike day (refused analyses count too); up to ~4,910/month. Not counted: a non-navigation request for a path that matches no file also invokes the Worker (link-preview unfurlers and crawlers fetching a share link, `curl`, the deploy smoke's deep link, a stale tab's `/snap` file after a deploy; ADR-011 "Request flow"); the model has no figure for them | ≥ 99% even on the spike day | `run_worker_first` requests get 429 ([billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/)) or an Error 1027 page with an undocumented status, until 00:00 UTC. Never billed | Static SPA and snapshot keep working (home, starters, presets, metadata, ~130 prewarmed analyses). Novel and KV-cached analyses fail. The SPA names only a non-JSON 429 from `/api` "daily request allowance reached; resets at 00:00 UTC", shown in local time. A 1027 page with another status is not recognised: a 5xx is retried as a cold start (`upstream_unavailable`, up to 150 s) and then shown as a temporary problem, and any other 4xx as a generic error |
| Static asset requests | free and unlimited; 20,000 files and 25 MiB per file per version ([billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/), [limits](https://developers.cloudflare.com/workers/platform/limits/)) | ~12,000 requests/month; ~750 files per version | unlimited requests; ~96% of the file cap free | n/a | n/a. This tier is the degradation floor for every other quota |
| Worker CPU per request | 10 ms; network, KV and DO waits not counted ([limits](https://developers.cloudflare.com/workers/platform/limits/)) | expected well under 1 ms (SHA-256 of ≤ 64 KiB; one JSON parse of the ≤ 64 KiB request body for its cost class, and of an upstream error body of up to 256 KiB; an HMAC of the client prefix; header work). **Unmeasured** | expected large; measure in the Worker's metrics | Error 1102 (HTTP 503) for that request; occasional overruns tolerated | one failed request, which the SPA retries |
| KV reads | 100,000/day ([KV pricing](https://developers.cloudflare.com/kv/platform/pricing/)) | 45/day typical, 450 on the spike day (1 per analysis POST) | ≥ 99% | reads fail until 00:00 UTC | treated as a miss: more traffic to the origin, still under the governor. Reads can never run out before Worker requests do |
| KV writes | 1,000/day; 1 write/s per key ([KV pricing](https://developers.cloudflare.com/kv/platform/pricing/)) | 30/day typical (1 per computed 200 response, `X-Cache: miss`). On the spike day only what the governor admits: 34–123 of its 300 uncached analyses ([The spike day under the governor](#the-spike-day-under-the-governor)) | ≥ 87% on the spike day. The governor's cap of 600 uncached analyses/day keeps writes ≤ 600 (≥ 40%): only computed answers are stored, and their count is never refunded | writes fail until 00:00 UTC; the Worker ignores the error | none visible: new results are not edge-cached for the rest of the day |
| KV storage | 1 GB per account | 1.3–10 MB (≤ 1,200 live entries × 1.1–8.4 KB, measured response sizes; 30-day TTL) | > 99%. Hard bound: 600 writes/day × 30 days × 17 KiB (16 KiB value cap + 1 KiB metadata) ≈ 0.31 GB (69% free) | writes fail | as for KV writes |
| Durable Object requests | 100,000/day, SQLite backend only ([DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)) | ~95/day typical, at most ~760 on the spike day (2 per uncached analysis + 2 per metadata lookup, which the SPA never needs + alarm polls + ops reads; a refused analysis makes 1, and most of the spike day's are refused: [The spike day under the governor](#the-spike-day-under-the-governor)) | ≥ 99% | operations fail until 00:00 UTC | fail-closed: uncached analyses and metadata lookups get 503 `capacity_unavailable`; static, KV and snapshot metadata keep working |
| Durable Object duration | 13,000 GB-s/day | far below the bound | hard bound 10,800 GB-s/day even if the object never idles (128 MB × 86,400 s), ≥ 17% headroom | operations fail | as above |
| Durable Object rows written / storage | 100,000 rows/day; 5 GB | ~150 rows/day typical, ~1,350 on the spike day; < 1 MB stored | ≥ 98% | writes fail | as above |
| Workers Logs | 200,000 events/day, 3-day retention, until 2026-11-30. From 2026-12-01: 0.5 GB/day, 7 days ([Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [observability](https://developers.cloudflare.com/observability/pricing/)) | up to 141 events/day typical, 681 on the spike day: invocation logs are off (`invocation_logs: false`, ADR-014), so the only events are the Worker's own line, one per `/api` request (`edge/src/index.ts`), at most one per Worker request above | ≥ 99% | after Dec 1, ingestion stops until 00:00 UTC; before then, undocumented | none for users; the edge logs for that day are lost |
| Workers Builds (build minutes) | 3,000 min/month | **0**: never connected; wrangler deploys from GitHub Actions | n/a | n/a | n/a |

### Render (Free web service, Hobby workspace)

| Quota | Limit | Projected use | Headroom | On exhaustion | Degradation |
|---|---|---|---|---|---|
| Free instance hours | 750 per workspace per calendar month ([free](https://render.com/docs/free)) | ~220 h: 14 wake sessions/day × ~25 min (1 min spin-up + activity + 15 min idle) ≈ 5.8 h, plus 4 deep checks × ~16 min ≈ 1.1 h, plus ~14 h on the spike day | ≥ 70%. One service can use at most 744 h, so it cannot run out. **Rule: never add a second Free web service** | all Free web services suspended until next month | cannot happen with one service |
| Outbound bandwidth (includes API→DB traffic the service initiates; inbound not billed) | 5 GB per Hobby workspace per month ([outbound bandwidth](https://render.com/docs/outbound-bandwidth)) | ~0.05 GB: 1,200 uncached × ≤ 40 KB (SQL text to Neon, measured 18–37 KB, plus a gzipped response ≤ 3 KB) + ~2 MB of health and wake responses. Metadata costs nothing here: the Worker answers it from the static snapshot | ~99%. Bounds through the Worker (PROJECTION, ADR-015 "Render outbound through the Worker"): analyses that keep their count 600/day × 40 KB × 31 ≈ 0.74 GB; metadata lookups 2,000/day × ~9 KB × 31 ≈ 0.6 GB; ungoverned `/api/health` ≤ ~1.8 GB even at the Worker's 100,000 requests every day (571 B per answer from the production-mode API itself, [counted in review round 2](evidence/phase5/review-r2-bytes/README.md); Render's proxy headers come on top, unmeasured). ≈ 3.1 GB in all, so one client cannot spend it as long as Render adds less than about 600 B to each answer (every 100 B adds 0.31 GB); measured after deploy (ADR-011 "Unknowns"). Refunded admissions (≤ 300 per network per day, a few KB each) are bounded per network only (S6, [R1](security.md#accepted-risks)) | with no card on file, all services suspended until next month ([free](https://render.com/docs/free)). **With a card, overage would be billed at $0.15/GB, so a card is never added** | what a suspended service answers is undocumented; the Worker maps any non-API answer to `service_waking` or `upstream_unavailable`, so the SPA cannot tell a suspension from a cold start. Each novel analysis waits out the 150 s retry budget behind the waking notice, then says the service is still not answering and that its free hosting may be paused until next month. Once the owner sets the pause switch (`deploy.yml` operation `pause-api`, until the 1st), every novel analysis gets one page saying the API is paused and until when, with no retries ([security.md R13](security.md#accepted-risks); [operations.md "Pausing the API"](operations.md#pausing-the-api)). Static snapshot and KV keep working |
| Discretionary suspension for "uncommonly high" service-initiated traffic | no published threshold; restore only via a paid plan ([free](https://render.com/docs/free)) | ~1.6 MB/day of API→DB traffic (PROJECTION); ≤ ~24 MB/day at governor caps | unknown | indefinite suspension | as above, but indefinite. Runbook: [Render suspends the service](operations.md#render-suspends-the-service). Failing over to the Vercel fallback is an owner decision and a manual procedure whose preconditions are not implemented (ADR-011 amendment), so until it is built only the static floor and KV-cached results serve. Never recreate the service on Render; never pay |
| Pipeline minutes | 500/month (Hobby) ([free](https://render.com/docs/free)) | **0 builds**: image-backed. Whether image pulls count is undocumented | ~100% | new builds and deploys disabled for the rest of the month; the running service keeps serving | none for users; deploys wait |
| Logs and metrics | 7 days on Hobby; latency percentiles need Pro ([logging](https://render.com/docs/logging), [metrics](https://render.com/docs/service-metrics)) | structured app logs plus a periodic metrics event (ADR-014) | n/a | n/a | n/a |

### Neon (Free)

| Quota | Limit | Projected use | Headroom | On exhaustion | Degradation |
|---|---|---|---|---|---|
| Compute | 100 CU-hours per project per month ([plans](https://neon.com/docs/introduction/plans)) | Scenario A ~17, B ~45–80, C ~120 CU-h (see "Neon CU-hour scenarios") | A 83%, B 20–55%, **C none** | compute suspended until the next billing period; connections drop. Never billed ([FAQ](https://neon.com/faqs/free-plan-limits-and-quotas)) | prevented by the governor: uncached analyses get 503 `capacity_paused` with `Retry-After`/`resumes_at` once the paced allowance is used (hard stop at 85 CU-h). If Neon suspends anyway, the API returns `database_paused` and the SPA shows a "new analyses paused until <date>" page. Static and KV keep working |
| Public network transfer (egress, outbound only) | 5 GB per project per month ([network transfer](https://neon.com/docs/introduction/network-transfer)) | ~0.15 GB: 1,200 analyses × ~80 KB average + per-process snapshot reads (108,005 B each). DB-to-API bytes **measured** on the full database ([review round 1, 2026-10-21](evidence/phase5/review-r1-bytes/README.md)): 9,523 B for a 1-year estimate, 61,892 B for a 7-year trend, 193,120 B for a 22-year pooled estimate or trend, 392,464 B for a 22-year compare; a 422 that runs the statistics query first transfers as much (376,495 B for a 22-year compare) | ~97%. Governor: heavy analyses ≤ 300/day, 422s that ran the query included. With ground truth (the optional Neon key) a hard stop at 4.5 GB month to date; **without it** (the first deploy) no monthly figure exists, and each day is held to 4.5 GB / 31 ≈ 145 MB of estimated egress (64 KiB per light, 512 KiB per heavy analysis; ADR-015) | compute suspended until the next billing period | as for CU-hours |
| Storage (`logical_size` across branches) | 1 GB per project, 20 GB per account ([plans](https://neon.com/docs/introduction/plans), [usage](https://neon.com/docs/introduction/usage-calculations)) | **measured** 0.65 GB (cluster sum 650,458,708 B, local prototype). +~26 MB per new ATUS year (PROJECTION: 598 MiB / 23 years) | ≥ 35% if the cap is 1e9 B (unit undocumented) | storage-increasing writes fail; reads continue; no deletion, no billing | none for serving (the API is read-only); only owner tasks that write (`validate-analytics`) fail. Each release goes into a new project |
| Projects / branches | 100 projects, 20 GB account total; 10 branches per project | 1 project (2 during a release switch, ≈ 1.3 GB); 1 branch | large | creation fails | none for serving |
| Scale to zero | fixed at 5 min, cannot be disabled | n/a | n/a | n/a | the first query after idle waits for a wake: a few hundred ms to a few seconds, longer after a branch is archived (unmeasured) |

### GitHub (Free, public repository)

| Quota | Limit | Projected use | Headroom | On exhaustion | Degradation |
|---|---|---|---|---|---|
| Actions minutes (standard runners) | free and unmetered for public repos; 6 h per job, 20 concurrent jobs ([Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)) | CI per PR; deploys (+10–15 min each for snapshot generation, unmeasured); ~1,650 scheduled runs/month (1,488 edge checks + 124 deep checks + 31 quota reports + 4–5 GHCR cleanups + 4–5 schedule checks) | unmetered | n/a. Without a card, metered items are blocked, never billed | none |
| Scheduled workflows | disabled after 60 days without repo activity; runs may be delayed or dropped ([disable rule](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows), [events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)) | 5 schedules: `edge-check.yml` (`:17`/`:47`), `deep-check.yml` (`17 */6 * * *`), `quota-report.yml` (daily), `ghcr-cleanup.yml` (weekly), `schedules-check.yml` (weekly) | n/a | monitors stop silently | none for users. Outages go unalerted until a schedule is re-enabled by hand ([operations.md](operations.md#scheduled-workflows-were-disabled)): the weekly schedule check fails, and so emails you, while it still runs; if it is disabled too, only the monthly look at the Actions tab in the [owner checklist](#owner-checklist) catches it |
| GHCR storage and bandwidth | "currently free" (≥ 1 month's notice of change) ([Packages billing](https://docs.github.com/en/billing/concepts/product-billing/github-packages)) | last 20 digests kept (image size to be measured) | n/a | policy change announced | none; fallback is another public registry plus an `imgURL` change |
| Release assets | < 2 GiB per file; no total size or bandwidth limit ([about releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)) | one 336.5 MB dump + sha256 per data release (measured prototype size) | 83% under the per-file cap | n/a | n/a |

### Neon CU-hour scenarios

All values are **PROJECTION**. They exist because the autoscaler's behaviour during the 5-minute idle tail is undocumented.

Neon's algorithm page says the autoscaler reads the 1-minute load average every 5 s and targets ≤ 90% CPU. It does not say how fast the compute scales back down.

Common assumptions:

- 480 Neon wake sessions a month (16/day × 30), each with ~1.9 uncached analyses about 60 s apart;
- busy time at 2 CU of ~1 s per analysis. Measured in the [2026-10-21 emulation](../loadtests/results/2026-10-21-emulation.md): 0.079 s p50 for a single-year estimate, 2.0–3.0 s p50 for the worst cases. The audit measured 0.38 s and 2.7–3.4 s on an earlier build;
- spike day: compute awake for about 12 h. This is an upper bound: the governor serves only part of the spike day ([The spike day under the governor](#the-spike-day-under-the-governor)), so the compute sleeps sooner;
- ~1 CU-h reserve for restore and smoke tests.

| Scenario | Idle-tail behaviour | Per session | Month |
|---|---|---|---|
| A | drops to 0.25 CU as soon as a query ends | 75 CU-s tail + 15 CU-s think time + ~4 CU-s busy ≈ 0.026 CU-h | ≈ 12.5 + 3.2 (spike) + 1 ≈ **17 CU-h** |
| B | stays raised while the 1-minute load average decays (~2–3 min) | + ~210–400 CU-s ≈ 0.08–0.14 CU-h | ≈ **45–80 CU-h** |
| C | stays at 2 CU through the whole tail | ≈ 2 × (300 + 60) CU-s ≈ 0.2 CU-h | ≈ 96 + 24 (spike) ≈ **120 CU-h**, over quota; the governor stops at 85, so uncached analyses pause late in the month |
| Fixed 0.25 CU (fallback lever) | constant | ≈ 0.25 × (300 + 60 + ~23 s busy) ≈ 0.027 CU-h | ≈ **17 CU-h**, but worst cases take 21–22 s pooled or trended, 33–35 s for the F66 broad selector, and 69.2 s for its compare by sex (p50; max 71.0 s; [2026-10-21 emulation](../loadtests/results/2026-10-21-emulation.md)) |

These figures line up with proposal P1's independent scenario analysis (A 17, B 45–55, C 105–110).

**Week-1 calibration.** Read `compute_time_seconds` per wake session from `GET /projects/{id}`. If it exceeds about 0.1 CU-h, lower the compute's maximum to 1 CU (after a 1 CU emulation run) or fix it at 0.25 CU. ADR-012 "Calibration (§13)" keeps this lever. If the compute is fixed at 0.25 CU, set `ATUS_API_MAX_PARALLEL_WORKERS=0` once its §9 proof passes (step 10 of the deploy guide).

### Governor starting parameters

Finalized in [ADR-015](adr/ADR-015-edge-cache-snapshot-and-governor.md) (the governor's values are constants in `edge/src/constants.ts`) and, for the API's own limits, [ADR-012](adr/ADR-012-load-and-quota-protection.md).

| Budget | Starting value |
|---|---|
| Neon CU-h month-to-date (ground truth, polled from the Neon API only while traffic is active) | admit while < min(85, 10 + 75 × elapsed share of the consumption period) |
| Neon CU-h per UTC day | ≤ 10. Without ground truth: ≤ (100 × 0.9)/31 ≈ 2.9, with every wake session charged at max CU for its whole 300 s tail |
| Neon egress | with ground truth: stop at 4.5 GB month-to-date. Without it: ≤ 4.5 GB / 31 per UTC day, estimated at 64 KiB per light and 512 KiB per heavy analysis. Heavy analyses ≤ 300/day either way, 422s that ran the statistics query included |
| Global uncached analyses | ≤ 600 per UTC day (also bounds KV writes, which store computed answers only, and the Render outbound of analyses that reach the database) |
| Per IP prefix (/24 or /48, HMAC with a daily key) | ≤ 60 uncached analyses per UTC day, ≤ 300 admissions including refunded ones, and ≤ a fifth of the day's Neon CU-h ceiling charged to its analyses. That fifth lets one network run novel analyses without a break for about 11 minutes a day without the Neon key, about 54 with it (ADR-015, row 4) |
| Metadata GETs the static snapshot cannot answer (a query string, an unknown code) | ≤ 200 per IP prefix and ≤ 2,000 in all per UTC day reach Render; the Worker answers the rest from the snapshot. Each also passes the compute and egress budgets above, and one that reports DB time (a freshly started API process's snapshot read, after a Render spin-down) is charged like an analysis |
| Governor unreachable | fail closed after 5 s: uncached analyses and metadata lookups get 503 `capacity_unavailable` (`Retry-After: 60`); static, KV hits and snapshot metadata keep working |
| API in-flight (in-process, global on one Render instance) | ≤ 2 uncached, ≤ 1 heavy; wait ≤ 5 s, then 503 `overloaded` (calibrated on the emulation profile, ADR-012 "Calibration (§13)") |
| API per-IP token buckets | uncached analyses: 6 per 60 s (burst 6); every request except `/health`, analyses included: 120 per 60 s (`ATUS_API_RATE_ANALYSIS`, `ATUS_API_RATE_REQUESTS`; ADR-012) |

### The spike day under the governor

**PROJECTION, simulated against the real governor** (`edge/test/worker/spike-day.test.ts`: the Worker and the Durable Object in workerd, a controlled clock, an origin that answers every analysis with 1 s of DB time; nothing measured on a provider).

The governor charges every admitted analysis at 2 CU (`NEON_MAX_CU`) for its DB time plus the whole 300 s idle tail (`NEON_IDLE_TAIL_MS`). The spike day's 300 uncached analyses arrive about 144 s apart, inside the previous analysis's tail, so the charge never stops: 2 CU × 12 h = 24 CU-h against a daily ceiling of 2.903 CU-h (no Neon key) or 10 CU-h (fresh ground truth). Neon itself would use about 3 CU-h under scenario A. The daily ceiling therefore refuses most of the day:

| Governor mode | Served | First refusal | Refused with 503 `daily_capacity_reached` (budget `cu_day`) |
|---|---|---|---|
| conservative (no `NEON_API_KEY`) | 34 of 300 | after 4,896 s (about 1.4 h) | 266 (89%) |
| ground truth (fresh Neon usage, mid-period) | 123 of 300 | after 17,712 s (about 4.9 h) | 177 (59%) |

- **What visitors see.** On the day a share link spreads, most visitors' novel analyses get "Today's free analysis capacity is used up; resets at <time>". The snapshot, prewarmed results and KV-cached results keep working.
- **Arrival pattern.** Bunched arrivals share tails and fare better. Arrivals more than 300 s apart fare worse: each is charged its whole tail (about 600 CU-s at 1 s of DB time, against about 290 CU-s at this spacing), so a day whose first analyses are sparse gets even fewer served before the ceiling.
- **The budget tables above.** Their spike-day figures for KV, the Durable Object, Render and Neon assume every analysis is served, so they are upper bounds. Worker requests are spent either way.
- **What would serve more.** Loading the project-scoped `NEON_API_KEY` at the first deploy (owner step 3.6) raises the spike day from 34 to 123 served. Charging less than 2 CU per second needs evidence from the week-1 calibration (step 10). Deriving the daily ceiling from the monthly pacing headroom is not implemented.

## Scraping-burst scenarios

All values are **PROJECTION** from the limits above. Nothing here was load-tested against a provider, and nothing will be: §13 load tests run locally under emulation.

| Scenario | What happens | Blast radius |
|---|---|---|
| **S1. One client sends novel specs at 5 req/s for 2 h** (36,000 requests) | Worker: 36% of the day's 100k. Each request misses KV, and the governor admits ~60 (per-prefix daily cap). The rest get a 429 from the Worker with `Retry-After` to 00:00 UTC, without touching Render. Neon: ≤ 60 analyses, arriving together in about one wake session; the network's fifth of the daily CU-h ceiling caps their compute either way. KV writes ≤ 60 | Other visitors unaffected; Worker headroom for the day shrinks by 36% |
| **S1b. One client spaces cheap novel specs ~7 min apart** (so each is its own wake session, ~610 CU-s) | The network is charged each session's compute; after about 3 (conservative ceiling) or 11 (ground truth) it gets 429 `rate_limited` until 00:00 UTC, its fifth of the ceiling spent. Before review round 1 it got 17 or 59 and then spent the whole ceiling | Other visitors unaffected; at least five such networks are needed to spend the day's compute (R1) |
| **S2. Distributed scraper** (≥ 200 /24 prefixes) sending novel specs at 3 req/s, sustained | The global cap (600 uncached/day) and the daily CU ceiling are reached within hours. After that, real visitors' novel analyses get 503 `daily_capacity_reached` until 00:00 UTC. The Worker's 100k is exhausted after ~9.3 h, and `/api` then returns 429/1027 until 00:00 UTC | One UTC day per attack day for `/api`. The static SPA and snapshot keep working throughout. If it repeats daily for a month: Neon stays ≤ 85 CU-h with ground truth (paced) and ≤ 90 CU-h without (2.903 a day), by the governor's charge model, which covers analyses and the metadata lookups that wake Neon alike (ADR-015, "Model error" for its limits), and Neon egress held to 4.5 GB (without ground truth only by per-analysis estimates, ADR-015 "Model error"); Render outbound through the Worker ≤ ~3.1 GB before Render's own response headers, which are unmeasured (ADR-015), plus a few KB per refunded admission. **No month-long pause** from this pattern, but novel analyses are mostly unavailable while the attack lasts |
| **S3. Replay of cached specs** (KV hits) at 5 req/s | Only Worker requests and KV reads are spent. Both run out after ~5.6 h, with the same 100k limit. Render and Neon untouched | `/api` down until 00:00 UTC; static floor intact |
| **S4. Direct hits on the onrender.com origin**, skipping the Worker | Each gets a minimal 403 with 0 DB work. It still wakes Render (instance hours, which cannot run out) and costs a few hundred bytes of Render outbound. ~10 req/s sustained for a month ≈ 26.8M × ~300 B ≈ 8 GB, which would exceed 5 GB | Accepted risk (ADR-011): services paused until next month. Mitigated by an unpublished name with a random suffix, a minimal 403 body and Render's own Cloudflare DDoS layer |
| **S5. Replay of metadata GETs** (e.g. `/api/v1/activities?n=<i>`) at 5 req/s | Plain paths are answered by the Worker from the static snapshot: no Render call. Query strings and unknown codes reach Render as lookups, ≤ 200 per network and ≤ 2,000 in all a day (~9 KB of Render outbound each at most, gzipped); the rest get 429/503 with `budget: metadata` until 00:00 UTC. A warm API answers them from memory; the first one after a Render spin-down runs the new process's snapshot read on Neon, and the governor charges that wake session to the network and the day like an analysis's, so a network timing lookups to the spin-downs gets about 3 such wakes a day without the Neon key, then 429 `budget: prefix`. Before review round 1 every such GET reached Render, and one client could spend Render's 5 GB a month in about 5.5 days; before review round 2 the wakes were uncharged (about 90 a day) | Worker requests, and Neon compute within the governor's budgets. Render outbound from lookups ≤ ~0.6 GB a month however long it lasts |
| **S6. Many networks spending their refunded admissions** (requests the API sheds, or answers from its cache, ≤ 300 per network a day) | Each costs Render a few KB of outbound and no Neon work; none counts toward the 600 | about 30 MB a month per network: dozens of networks sustained all month would be needed to suspend Render until the 1st (R1) |

**What this protection does not cover:** a determined distributed attacker can always spend the account-wide 100k Worker requests per day. On a workers.dev hostname at $0, nothing can rate-limit before the Worker is invoked.

## Failure modes and degradation

| Event | How it shows up | What visitors see | Recovery |
|---|---|---|---|
| Render spun down (normal, after 15 idle minutes) | first `/api` call waits about a minute, or gets Render's hibernate 503, which the Worker maps to `service_waking` | home, starters, presets and metadata are instant (static). "Waking up the analysis service — free hosting sleeps when idle; this can take up to a minute" appears (polite live region, no fake progress bar) when a metadata request has been pending for 3 s, when an attempt fails as waking (`service_waking`, `upstream_unavailable` or `database_unavailable`), or while a prewake that answered "waking" is under 90 s old ([frontend.md](frontend.md)). A novel analysis that Render simply holds while it spins up has no 3 s timer: it shows the notice only through that prewake (sent, at most once per 10 minutes per page, on entering Explore and before an analysis the snapshot does not hold). The SPA retries for up to 150 s, honouring `Retry-After` | automatic |
| Neon suspended (normal, after 5 idle minutes) | first query waits a few hundred ms to a few s | absorbed in the analysis wait | automatic |
| Neon branch archived after quiet days | slow first query (unmeasured) | same waiting message | automatic |
| Neon compute update restart | dropped connections | none: `check_connection` replaces them (test) | automatic |
| Worker 100k/day exhausted | a non-JSON 429 from `/api`, or Cloudflare's Error 1027 page, whose status is undocumented | for a non-JSON 429, a full-page notice for analysis features: "daily request allowance reached; resets at <local time of 00:00 UTC>". A 1027 page with a 5xx status is retried as a cold start (`upstream_unavailable`) and then shown as a temporary problem; with another 4xx, as a generic error. Static content and prewarmed results still work | 00:00 UTC |
| KV reads or writes exhausted | KV errors in the Worker | none (miss, or not cached) | 00:00 UTC |
| Durable Object error or quota exhausted | governor call fails | novel analyses get 503 `capacity_unavailable`; everything else works | 00:00 UTC, or a fix |
| Governor daily cap reached | 503 `daily_capacity_reached` | "Today's free analysis capacity is used up; resets at <time>". Snapshot and cached results work. On the traffic model's spike day this refuses 59–89% of the novel analyses ([The spike day under the governor](#the-spike-day-under-the-governor)) | 00:00 UTC |
| Governor monthly pacing or hard stop | 503 `capacity_paused` with `resumes_at` | "New analyses paused until <date> (free database allowance)". Snapshot and cached results work | allowance resumes; or the owner applies the calibration lever |
| Neon CU-h or egress exhausted anyway | connection failures, which the API maps to 503 `database_paused` | the same paused page | the next Neon billing period |
| Neon storage over the cap | owner writes fail | none | free space, or restore into a new project |
| Render bandwidth exhausted | an undocumented answer, which the Worker maps to `service_waking` or `upstream_unavailable`, as for a cold start; Render's email | until the owner sets the pause switch: for each novel analysis, the waking notice for up to 150 s, then "The analysis service is still not answering … its free hosting may be paused, for example until next month" with "Try again" ([security.md R13](security.md#accepted-risks)); those retries count toward the network's 300 daily admissions, so after roughly 10–20 such analyses a visitor sees the per-network daily-limit page instead. Once it is set (`pause-api`, until the 1st, reason `render_quota`): one page, "The analysis service is paused", with the date, and no retries. Snapshot and KV work | next month: the switch clears itself at its date; find the cause in the logs |
| Render discretionary suspension | same symptoms; Render email or dashboard notice | same, but indefinite; with the switch set (`render_suspended`, `unknown`), the paused page without a date | **owner decision** ([runbook](operations.md#render-suspends-the-service)): the ADR-011 fallback is a documented manual procedure, not implemented; no Worker var selects the API origin. Until it is built, the static floor and KV-cached results serve. Never pay; never recreate on Render |
| Render restart (can happen any time) | in-process caches and token buckets lost | none (edge tiers and the governor persist) | automatic |
| Card prompt at any provider | sign-up or deploy screen | — | **stop and report** (constraint 1); owner decision |
| GHCR image unavailable | Render "Image Pull Failed" on a restart or deploy | Render serves nothing; snapshot and KV still work | republish the image or switch registry; redeploy by digest |
| GitHub schedules auto-disabled | no monitor runs | none (no alerts) | re-enable them by hand ([operations.md](operations.md#scheduled-workflows-were-disabled)). The weekly schedule check fails first, unless it is disabled too; then only the [owner checklist](#owner-checklist)'s monthly look at the Actions tab notices |
| workers.dev subdomain renamed by Cloudflare | notice email (Cloudflare "will attempt" a week's notice) | every old share link breaks | no $0 fix; update the README; links regenerate on the new host |
| API/edge version skew after a deploy | header mismatch | SPA banner: "A new version is available — reload" | automatic after the reload |

## Owner account setup (§19)

**Rules for every step.**

- Choose only Free plans. Never add a payment method, start a trial or redeem credits.
- **If any page asks for a card, a trial or credits, stop and report it.** Do not continue around it.
- Never paste a secret into chat. Generated secrets are written by scripts into gitignored files outside the repo (for example `~/.config/atus/*.env`, `chmod 600`). Tell Claude only that a step is done, or which variable is set.
- The order matters. Some steps need artefacts from implementation (the first CI image, the restore script); each step says so.

**Before you start, decide:**

1. **The Cloudflare account subdomain.** It appears in every share link, and changing it later breaks all of them. It must be 63 characters or fewer, using letters, digits and dashes. It must not contain another person's or organisation's name; your GitHub handle is a good choice.
2. **The Worker name** (recommended `atus`). The public URL becomes `https://atus.<subdomain>.workers.dev`.

### 1. Cloudflare (Workers Free)

1. Sign up at `dash.cloudflare.com` with email and password, then verify the email. The account starts on Workers Free. Do **not** subscribe to Workers Paid, and do not enable R2, Workers Cache or any paid add-on.
2. **Workers & Pages → account subdomain:** set the subdomain you chose. ⚠ Every share link depends on it.
3. *(After implementation provides `wrangler.jsonc`.)* On your Mac, from the repo:
   1. `cd edge && npm ci`, then, from `edge/`, `npx --no-install wrangler login`. `npm ci` installs the wrangler locked in `edge/package-lock.json` (4.20 or newer is required), and `--no-install` makes `npx` refuse to download any other version: this login stores an OAuth token with broad scopes on your Cloudflare account on the Mac, so it must come from the reviewed lockfile, never from whatever the registry serves that day (the repository root has no `package.json`, so `npx` run there downloads wrangler). Run every later `wrangler` command the same way, from `edge/`. Once the deploys run (CI uses its own scoped token), `npx --no-install wrangler logout` removes the login until you next need it.
   2. `npx --no-install wrangler kv namespace create <KV_NAME> --update-config=false`, from `edge/`. Keep `--update-config=false`: without it wrangler (4.148) asks whether to add the namespace to `edge/wrangler.jsonc`, and its default answers (yes, and a binding named after `<KV_NAME>`) append a second KV binding next to `RESULT_CACHE` and leave the placeholder in place; `edge/test/node/config.test.ts` allows only `RESULT_CACHE`, so CI on your push fails and preflight refuses the first deploy. With the flag it only prints a snippet (whose binding name you ignore) and edits nothing; `--binding RESULT_CACHE` would write the id itself but re-indents the whole file (rehearsed on 2026-10-21 with the locked wrangler against a loopback stand-in for Cloudflare's API). The namespace id is not secret: replace the placeholder `id` of the `RESULT_CACHE` binding in `edge/wrangler.jsonc` with the snippet's `id`, then commit and push that change to `main` before you dispatch the first deploy run ("Bootstrap", step 2, under step 7 of the deploy guide). The deploy job checks out the commit the run was dispatched from and refuses the placeholder.
   3. Generate the secrets: `scripts/ops/gen_edge_secrets.sh --render-origin https://<service>.onrender.com` writes `~/.config/atus/edge.env` (mode 600) and prints, without any value, the commands below. The Render origin exists only after step 2.3; until then run the script without `--render-origin` and re-run it later.
   4. Build what the first deploy publishes, from a checkout of the commit you dispatched: `wrangler.jsonc` publishes `../frontend/dist`, which a fresh clone does not have (it is gitignored). Run `(cd frontend && npm ci && VITE_API_BASE_URL= npm run build)` (same-origin, as CI builds it), then `cd edge && npm ci`, so that `npx` runs the wrangler locked in `edge/package-lock.json`. The script prints both commands too. This first publish has no snapshot; the approved deploy replaces it ("Bootstrap", step 5).

      Then run the first `npx --no-install wrangler deploy` yourself, from `edge/`. Creating a Worker needs account-level Admin. This creates the Worker, its static assets and the Durable Object migration. `wrangler.jsonc` marks `EDGE_AUTH_SECRET`, `OPS_TOKEN`, `IP_HMAC_KEY` and `RENDER_ORIGIN` as required, so this first deploy passes them with `--secrets-file`, filtered to the Worker's names exactly as the script prints it (the file also holds the API's `ATUS_OPS_TOKEN`, which never goes to the Worker). The printed command copies those names into a private temporary file that it removes afterwards: a process substitution (`--secrets-file <(grep ...)`) fails on macOS, because wrangler reads the file in a child process that cannot open `/dev/fd/N`. Later changes use `npx --no-install wrangler secret put <NAME>`, piped from the file:
      - `EDGE_AUTH_SECRET`
      - `OPS_TOKEN`
      - `IP_HMAC_KEY`
      - `RENDER_ORIGIN` (after step 2.3)
      - optionally `NEON_API_KEY` (step 3.6)
4. **Worker → Settings → Builds:** leave it **disconnected**. Never use "Connect to Git". The config sets `preview_urls = false`, so no extra Version URLs exist.
5. **Account API Tokens → Create Token → Custom token** for CI:
   - Workers role **Editor**, scoped to the single Worker `<worker>`.
   - Account resources: only this account. TTL 90 days.
   - If the UI forces a template, use **Edit Cloudflare Workers** restricted to this account. It is broader: it includes KV, R2 and routes.
   - **Check the token before you save it**, from `edge/`, once the Worker exists (step 3.4). Not with `wrangler deploy --dry-run`: a dry run never authenticates (wrangler 4.148 skips its login check and stops before any API call), so it passes with any token. This check authenticates and changes nothing:

     ```bash
     read -rs CLOUDFLARE_API_TOKEN && export CLOUDFLARE_API_TOKEN   # paste the token, then Enter; nothing is shown
     export CLOUDFLARE_ACCOUNT_ID=<Account ID>
     test -n "$CLOUDFLARE_API_TOKEN" && npx --no-install wrangler deployments status   # must print the Worker's current deployment
     unset CLOUDFLARE_API_TOKEN
     ```

     The `test -n` matters: with an empty `CLOUDFLARE_API_TOKEN` (nothing pasted), wrangler falls back to your own OAuth login from step 3.1 and the check would pass without testing the token. It proves that the token is active, belongs to this account and can read the Worker. Whether it may also deploy (the Editor role) shows only when it deploys: the first approved deploy's `wrangler deploy` (Bootstrap, step 5, which sends Render the digest it already runs, so a failure there leaves nothing half-switched). If that step fails with an authentication or permission error, add **Account Settings: Read** to the token, update the secret and re-run the job. Every Deploy run repeats the read check before the Render hook and stops there, with nothing changed, once the token has expired or been revoked.
   - Save the token as GitHub environment secret `CLOUDFLARE_API_TOKEN`, and the Account ID (Workers overview sidebar) as `CLOUDFLARE_ACCOUNT_ID`.

### 2. Render (Free web service)

1. Sign up at `dashboard.render.com`. Use the **Hobby workspace created automatically** at sign-up. **Never create another workspace**: that flow asks for a payment method. If any card prompt appears, stop.
2. **Region check, before creating anything on Neon.** Open New → Web Service, choose **Free** as the instance type, and note which regions are offered. **Cancel without creating.** Tell Claude whether **Virginia** is offered. If it is, Neon goes to AWS US East 1. If not: Ohio pairs with AWS US East 2, Oregon with AWS US West 2.
3. *(After the first deploy run has pushed the image and the GHCR package is made Public, step 4.5; see "Bootstrap" under step 7 of the deploy guide.)* **New → Web Service → Existing Image:**
   - Image URL: `ghcr.io/nathan98000/atus-api@sha256:<digest>`, the digest the `push` job of that run prints in its summary (public; no credentials).
   - Name: `atus-api-<6 random chars>`. Never publish this hostname. Store the resulting URL as the Worker secret `RENDER_ORIGIN` and the GitHub secret `RENDER_ORIGIN`.
   - Region: as agreed in step 2. It cannot be changed later.
   - Instance type: **Free**. Health check path: **`/health`**.
4. **Environment** ([API](#api-render--environment)):
   - `ATUS_ENV=production`
   - `ATUS_DATABASE_URL`: the `atus_api` direct DSN with `sslmode=verify-full&sslrootcert=system`, from the file the restore script writes (step 3.5)
   - `ATUS_EDGE_SECRETS`: the same value as `EDGE_AUTH_SECRET`
   - `ATUS_EXPECTED_DATA_VERSION`
   - `ATUS_OPS_TOKEN`: the `ATUS_OPS_TOKEN` line of `~/.config/atus/edge.env` (step 1.3)
   - `WEB_CONCURRENCY=1`
5. **Settings → Deploy Hook:** copy the URL into the GitHub environment secret `RENDER_DEPLOY_HOOK_URL`. Regenerate it if it ever leaks. Confirm that **no Git repository is connected**; image-backed services never auto-deploy.
6. Do **not** create a Render API key: it grants access to every workspace you belong to. Enable email notifications for failed deploys and service events. Never add a second Free web service to this workspace.
7. *(After the first deploy run's `promote` job has passed.)* **Settings → Image → Image URL:** `ghcr.io/nathan98000/atus-api:live`, then save. Deploys keep going through the hook by digest; this setting is what any other deploy uses (Manual Deploy, "Deploy latest reference", and possibly "Save and deploy"). Render documents that a hook's `imgURL` does not change it ("your service continues to use the tag or digest in its settings", render.com/docs/deploying-an-image, read 2026-10-21), so left at the bootstrap digest it would bring that image back, and fail once `ghcr-cleanup.yml` deletes it. `deploy.yml`'s `promote` job moves `live` to each digest whose deploy passed the smoke, `ghcr-cleanup.yml` never deletes it, and `deep-check.yml` alerts when `/health` reports another build. It also fails when it cannot read the `live` tag (a 401/403, or no tag), never silently skipping the comparison; its first run after step 4.1 shows whether the run's `GITHUB_TOKEN` may read the package's versions ([operations.md](operations.md#the-deep-check-cannot-read-the-live-tag)).

### 3. Neon (Free)

1. Sign up at `console.neon.tech` on the **Free** plan. Ignore "Claim $20 in free credits" and every upgrade prompt. If any card prompt appears, stop.
2. **Create project** `atus-<release>-<build8>`:
   - **Postgres 16**;
   - cloud **AWS**, region **US East 1 (N. Virginia)**, or the pairing from step 2.2;
   - database name `atus`.

   Keep the default owner role `neondb_owner`.
3. **Branches → production → Computes → Edit:** autoscaling **min 0.25 CU, max 2 CU**, then Save. Edit the compute itself: changing the project defaults does not change an existing compute. Scale to zero stays at the fixed 5 minutes.
4. Do **not** create branches or read replicas, and do not run an instant restore on this project. Each would add to the project's 1 GB.
5. *(After implementation provides the restore script.)*
   1. **Connect**, with connection pooling **off** (the direct host, without `-pooler`). Copy the `neondb_owner` connection string into a new file for this project, `~/.config/atus/neon-<release>-<build8>.env` (named like the project), as `ATUS_OWNER_DATABASE_URL=...`. Neon's string carries `sslmode=require`, which encrypts but verifies no server; the restore never uses it that way: for a remote host it always connects with `sslmode=verify-full&sslrootcert=system` (keeping `channel_binding`), so the string can be pasted as is (review round 3). Keep that file outside the repo, `chmod 600`. Use one file per Neon project and keep the previous project's file until that project's deletion is approved: a data rollback needs its `atus_api` DSN ([operations.md](operations.md#serving-db-build-and-restore)).
   2. Run the restore script yourself, with `--secrets-file` naming that file (step 5 of the deploy guide). It runs `pg_restore --no-owner --no-acl` with a client of version 16 or newer (a local one, or the `postgres:16` image through docker, pinned by the digest every other `postgres:16` in the repository uses; on macOS no client install is needed), creates `atus_api` with a generated password of at least 60 bits, applies grants and role settings, runs `VACUUM (ANALYZE)` and checks the metered size. It writes the `atus_api` DSN back into the same file. Nothing is printed or pasted.
6. *(Optional; strengthens the quota governor.)* **Organization settings → API keys → Create → Project-scoped key** for this project only, if the Free plan offers it.
   - Load it with `npx --no-install wrangler secret put NEON_API_KEY`. The project id goes in as the Worker var `NEON_PROJECT_ID`.
   - Never use a personal or organization-wide key here.
   - The key must be re-created for each release project.

### 4. GitHub (repository `Nathan98000/ATUS`, stays public)

The workflows in `.github/workflows/` read exactly these names; nothing else is needed.

1. **`PUBLIC_BASE_URL`** = `https://<worker>.<account-subdomain>.workers.dev`, set twice:
   - **now, as an environment variable of `production`** (step 4.2, "Environment variables"). The deploy job runs in that environment and reads it there: GitHub gives an environment variable precedence over a repository variable of the same name: "the environment-level variable takes precedence" ([configuration variable precedence](https://docs.github.com/en/actions/reference/workflows-and-actions/variables), accessed 2026-10-21).
   - **only after step 8 has verified the site, as a repository variable** (**Settings → Secrets and variables → Actions → Variables → New repository variable**). The monitors run as soon as it exists: they decide whether to run before they enter an environment, so only a repository variable starts them. Set earlier, every edge check would fail, and email you, every 30 minutes until the approved deploy has published the Worker and the snapshot.
2. **Settings → Environments → New environment `production`:**
   - Required reviewers: yourself, with **Prevent self-review OFF** (otherwise nobody can approve).
   - Deployment branches and tags: "Selected branches and tags": `main` and the tag pattern `v*`.
   - Secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `RENDER_DEPLOY_HOOK_URL`, `RENDER_ORIGIN`. Optionally `EDGE_OPS_TOKEN` (the Worker's `OPS_TOKEN`): the post-deploy smoke then also reads the governor's budget.
   - Environment variables: `PUBLIC_BASE_URL` (step 4.1).
3. **New environment `ops`**, used by the daily quota report:
   - No reviewers. Deployment branches: `main` only.
   - Secret: `EDGE_OPS_TOKEN` (the Worker's `OPS_TOKEN`). `scripts/ops/gen_edge_secrets.sh` prints the `gh secret set` commands for it, here and optionally in `production`, each reading the value from `edge.env`.
   - *(Optional; the early warning for the Worker's 100,000 requests a day.)* Secrets `CLOUDFLARE_ANALYTICS_TOKEN` and `CLOUDFLARE_ANALYTICS_ACCOUNT`. The token is a Cloudflare Custom token with the single permission Account → **Account Analytics** → **Read** on this account (My Profile → API Tokens → Create Token), with a 90-day TTL like the deploy token; never the deploy token. The account is the Account ID (Workers overview sidebar, the value of `CLOUDFLARE_ACCOUNT_ID`). The daily quota report then judges the account's Worker requests today against the pro-rated 100,000 (one query to Cloudflare's GraphQL Analytics API). Without them the report shows that line as SKIP. Whether the `workersInvocationsAdaptive` dataset answers on Workers Free is not documented: the first report after you add them shows it (a failure names Cloudflare's error).
   - `ATUS_OPS_TOKEN` and `GHCR_IMAGE` are **not** GitHub settings. No workflow reads the origin's `/ready` or `/metrics` (the post-deploy smoke checks the origin lock instead, which touches no database), and the workflows name the image repository themselves.
4. **Repository settings:**
   - **Actions → General → Workflow permissions:** "Read repository contents and packages permissions". Every job asks for what it needs; only deploy.yml's `push` and `promote` jobs and the GHCR cleanup get `packages: write`.
   - **Actions → General:** if your settings offer "Require actions to be pinned to a full-length commit SHA", turn it on. Every workflow already pins by SHA, and `tests/deploy/test_workflows.py` enforces it.
   - **General → Releases:** enable **release immutability**.
   - **Advanced Security:** enable CodeQL default setup, Dependabot alerts and Dependabot security updates (`.github/dependabot.yml` adds weekly version updates). Secret scanning and push protection are on by default for public repos; confirm they are.
   - **Your profile → Settings → Notifications → System → Actions:** "Email", with "Only notify for failed workflows". This is what turns a failed monitor into an email. GitHub sends a scheduled workflow's notifications to the user who last changed its `cron` line, so the monitors' cron lines should last be changed in commits you push.
5. *(After the first push to GHCR; see "Bootstrap" under step 7 of the deploy guide.)* **Packages → `atus-api` → Package settings:**
   - **Change visibility → Public.** This cannot be undone; the image contains no secrets or data.
   - **Manage Actions access:** `Nathan98000/ATUS` with role **Admin**. Packages pushed by the repository's own workflow normally get it; the weekly cleanup (`ghcr-cleanup.yml`) needs it to delete old versions.

### 5. Verification

1. Push a docs-only commit to `main`. Confirm that **nothing deploys**: check Render → Events and Cloudflare → Worker → Deployments.
2. Tell Claude that the secrets above are set, by name only.
3. Approve the first run of the `deploy` workflow in the `production` environment.
4. After that deploy, check the origin lock: `curl -s -o /dev/null -w '%{http_code}' https://<service>.onrender.com/api/v1/meta` must print `403`.

## From-scratch deploy guide

**Status.** ADR-011 fixes the order below, and every step is written. Steps 0 to 3 and 6 were run locally (functional checks only); step 0 covers the public origin too (the Worker, the SPA and the static snapshot). `ci.yml` is configured to run step 0 on every pull request (step 3), but no run is recorded yet ([roadmap](roadmap.md#remaining-for-the-definition-of-done), item 1). The snapshot and deploy jobs of step 7 and the replay script of step 9 were rehearsed locally against the real release artifact (functional; no provider contacted). What remains is the owner's: the [account steps](#owner-account-setup-19) and steps 4–10 below.

Nothing here has been run against a provider.

| Step | Who acts | Needs | Produces |
|---|---|---|---|
| 0. Rehearse locally | anyone | Docker (and Node for `--edge`) | a passing stack smoke, image gate and lint; the edge probe and the stack-mode E2E |
| 1. Serving DB and release artifact | owner's machine | the full local DB (read only) | a checksummed serving dump, its `data_version`, the §9 equivalence proof |
| 2. Restore drill | owner's machine | step 1 | a restored, validated copy and its restore notes |
| 3. The image | CI (and locally, for rehearsal) | the repository | `ghcr.io/nathan98000/atus-api@sha256:<digest>`, gated and scanned |
| 4. Accounts | owner | — | Cloudflare, Render, Neon and GitHub set up as in §19 |
| 5. Load Neon | owner | steps 1 and 4 | the serving DB on Neon; the `atus_api` DSN in that project's `~/.config/atus/neon-<release>-<build8>.env` |
| 6. First Worker deploy | owner | step 4 and `edge/` | the Worker, its KV namespace, Durable Object and secrets |
| 7. First approved deploy | CI, after the owner approves | steps 3, 5 and 6 | the API on Render by digest, the snapshot, the Worker vars |
| 8. Prove it | owner and Claude | step 7 | curl and Playwright evidence; then the README's live URL |
| 9. Post-deploy replay | Claude, quota-budgeted | step 8 | the measurements ADR-011 lists as unknown |
| 10. Week-1 calibration | owner and Claude | step 9 | the final limits in ADR-012 |

### 0. Rehearse the whole stack locally

Everything below runs on your machine, binds only to `127.0.0.1`, and calls no provider.

- **Functional smoke of the production stack** (builds the image, loads the hand-computed fixture dataset, probes the API from inside the stack network, removes everything):

  ```bash
  scripts/smoke_stack.sh                 # add --emulation for the free-tier CPU/RAM limits
  ```

  The probe (`deploy/stack/smoke_probe.py`) checks:

  - `/health` and the build it reports;
  - `/api/v1/meta` with the edge secret;
  - one analysis (sleep 2023 = 75.0 minutes, computed by hand);
  - the v1 error envelope;
  - the Phase 5 API contract:
    - the 403 for a request without the edge secret, and `/docs` off;
    - security headers and no `Server` header;
    - `Cache-Control: no-store` on errors;
    - the analysis headers;
    - `X-Request-ID` echoed only with edge authentication;
    - 413 and 415;
    - `/ready` and `/metrics` behind the ops token;
    - the globally unique `data_version` format.

  `--skip-protection-checks` reports the contract checks as SKIP. It exists only for images built before the API work landed. Strict mode (the default) passed every check on 2026-10-20 (wave 2), plain and with `--emulation`.
- **Run the stack yourself:**

  ```bash
  cp deploy/stack/stack.env.example .env.stack     # fill in fresh random secrets
  docker compose --env-file .env.stack -f docker-compose.prod.yml up -d --wait api
  docker compose --env-file .env.stack -f docker-compose.prod.yml --profile smoke run --rm smoke --fixture
  docker compose --env-file .env.stack -f docker-compose.prod.yml down -v
  ```

  | Service | Stands in for | Notes |
  |---|---|---|
  | `db` | Neon Free, Postgres 16 | Neon's settings for the 0.25–2 CU compute (`jit=off`, `shared_buffers=215MB`, `max_connections=839`, `max_worker_processes=17`, `max_parallel_workers=8`), `shm_size: 1g` (F33). Bootstrap creates only what Neon's console creates: `neondb_owner` (owner, `CREATEDB`/`CREATEROLE`, not a superuser) and database `atus`. Host port `127.0.0.1:5470` |
  | `restore` | the release switch's `pg_restore` | profile `dump`; set `ATUS_STACK_DUMP` to a serving dump. Restores as `neondb_owner` with `--no-owner --no-acl`, then `VACUUM (ANALYZE)` |
  | `seed` | the owner's loader and the serving build | unless a serving database is present: loads the fixture dataset with the real loader into a scratch full database, then builds the serving database from it into `atus` with the real serving builder (`atus build-serving-db`: serving shape, marker, a serving `data_version`), as `neondb_owner` in development mode like the real loader. `ATUS_SEED_FORCE=1` rebuilds it |
  | `roles` | the restore script's role step | applies `scripts/ops/roles.sql` as `neondb_owner`, exactly as production does: `atus_api` with SELECT on the served tables only, read-only transactions by default and the ADR-011 role defaults. It is the only definition of the role; `tests/deploy/test_stack_roles.py` (opt-in) proves it cannot write, truncate or create any table or schema, and reads exactly the served tables; it can create large objects, which no privilege controls, and re-applying `roles.sql` removes them ([security.md R16](security.md#accepted-risks)) |
  | `api` | the Render web service | the image with exactly Render's environment (`ATUS_ENV=production`, ...). Read-only root filesystem, all capabilities dropped. **Not published on the host** |
  | `snapshot-api` | CI's snapshot step | profile `snapshot`; the same image on the same database with generous per-client limits, published on `127.0.0.1` for `scripts/build_snapshot.py` only |
  | `edge` | Cloudflare Worker + static assets: the public origin | profile `edge`; the Worker from `edge/` under `wrangler dev` in local mode (KV and the Durable Object simulated on disk; no login, no metrics or error reports, no `cf.json` fetch, so no call to Cloudflare) on `127.0.0.1:8541`, serving `ATUS_STACK_ASSETS_DIR` (default `frontend/dist`) with `_headers` and SPA fallback, in front of `api`. The Worker accepts a plain-HTTP origin on loopback only, so a relay inside the container (`deploy/edge/origin-relay.mjs`) forwards `127.0.0.1:10000` to `api:10000`; `RENDER_ORIGIN` is that loopback address. The release vars come from `ATUS_EXPECTED_DATA_VERSION`, `ATUS_STACK_ANALYTICS_VERSION` and `ATUS_STACK_SNAPSHOT_ID` |
  | `smoke` | — | profile `smoke`; the HTTP probe, run inside the stack network with the edge secret and the API's ops token |

- **The public origin, end to end** (`--edge`; `--e2e` adds Playwright):

  ```bash
  scripts/smoke_stack.sh --edge --e2e      # needs node/npm, frontend/node_modules, Playwright browsers
  ```

  After the API smoke it does what a deploy does, in the same order: binds the API to its data (`ATUS_EXPECTED_DATA_VERSION`, as the release switch does), builds the SPA same-origin, builds the static snapshot with `scripts/build_snapshot.py` and the SPA's request generator against `snapshot-api` (with `--skip-refused`: the small fixture cannot answer every prewarmed spec, a release must), and starts the Worker with the release vars. `deploy/stack/edge_probe.py` then checks, from the host: SPA deep links (SPA fallback); `_headers` on static responses (every header of its `/*` rule, exact values); the snapshot served statically and byte-identical to the API; `/api/_edge/health`; the `/api` allowlist; the Worker's security headers on `/api` responses; 413 and 415 at the edge; a KV miss then `X-Cache: edge`; the prewake; the ops bearer; the API's origin lock (403 without the edge secret); and a governor denial (a /24's 61st new analysis of the day: 429 `rate_limited`, budget `prefix`, `Retry-After` until 00:00 UTC; each of the 61 also names another /24 in forged `X-Forwarded-For`, `X-Real-IP`, `True-Client-IP` and `Forwarded` headers, which must not change the network charged). Then `deploy.yml`'s own post-deploy gate, `scripts/ops/deploy_smoke.py`, runs with the deploy's flags (`--require-snapshot --recompute 2 --max-requests 30 --origin-lock --ops-budget`, `snapshot-api` standing in for the origin), so a regression in that script shows up here instead of in a production smoke; its §9 comparison needs the package installed in `--python`. With `--e2e` the Playwright suite runs through the edge (stack mode, `E2E_BASE_URL`; projects `E2E_PROJECTS`, default `desktop mobile webkit`), and then `deploy.yml`'s browser smoke (`playwright.deployed.config.ts` with `PUBLIC_BASE_URL`). On 2026-10-23, at c880254 (E2E suite: `frontend/e2e` tree d4aae9719461, `frontend/playwright.config.ts` blob 18a0605b8ef4), all 67 runs passed in stack mode (Chromium desktop 52, mobile 4, WebKit 11), after smoke 15/15, edge probe 13/13 and `deploy_smoke.py` 13/13 (17 requests), and the browser smoke passed; that run (on the tree then committed as c880254, its suite exactly and its code but for two CSS comments and unit-test titles; image built from that tree under 8be59ec's label, whose API c880254 does not change) followed design review round 6, which added a mobile and a desktop test and made a test's routes stop throwing after it ends (an earlier run on that tree passed 66 and lost the next test to it). Before that, on 2026-10-23, at 6b2d24a, all 65 runs had passed (Chromium desktop 51, mobile 3, WebKit 11), after smoke 15/15, edge probe 13/13 and `deploy_smoke.py` 13/13 (17 requests), and the browser smoke passed; that run (image built from 6b2d24a) followed the 2026-10-22 and 2026-10-23 design reviews, whose suite drops the tests of removed features, leaves the snapshot out of tests that check a request (a run at 7cd7663 failed 4 of 65 where the snapshot answered first) and has the browser smoke read the estimate's current caption (a run at d36876d failed it on “minutes per day”, gone since b2fbc06). Before that, on 2026-10-21, at 56b0a42, all 70 runs had passed (Chromium desktop 53, mobile 3, WebKit 14), after smoke 15/15, edge probe 13/13 and `deploy_smoke.py` 13/13 (17 requests), and the browser smoke passed; that run (image built from 56b0a42) followed the `firefox` project's launch setting, which stack mode does not run, and the loading-state test's delay of the snapshot's analysis file, after a run at ff03ba8 failed that test once in 70 (docs/frontend.md). Before that, at 46293d1, 70 of 70 had passed (Chromium desktop 53, mobile 3, WebKit 14), with the same smoke, edge probe and `deploy_smoke.py` results and the browser smoke; that run (image `atus-api:rv3`, built from 3dffe1a, whose API 46293d1 does not change) added the review-round-4 browser test of `e2e/service-states.spec.ts` (a paused page reached through Analyze takes the focus), which the earlier records predate. Earlier the same day, at 3a984fa, 68 of 68 had passed (Chromium desktop 52, mobile 3, WebKit 13), with the same smoke, edge probe and `deploy_smoke.py` results and the browser smoke; that run (image `atus-api:rv2`, built from 3a984fa) followed the review-round-2 merges, whose browser changes (the frontend's fixture that fails every test on a CSP or Permissions-Policy violation, and its test) the records before it predate. At 9188bb6, 67 of 67 had passed (Chromium desktop 51, mobile 3, WebKit 13), with the edge's `api_paused` test of `e2e/service-codes.spec.ts`; at 5beb6a4, 66 of 66 had passed (Chromium desktop 50, mobile 3, WebKit 13), after the review-round-1 merges, whose browser tests (`e2e/service-codes.spec.ts` and others) the earlier records predate; and at f208719, 52 of 52 had passed (Chromium desktop 37, mobile 2, WebKit 13); the first run, on 2026-10-20, passed 50 of 50 before `e2e/new-survey-year.spec.ts` and the static `/openapi.json` test existed. Firefox cannot launch on macOS 27. `ci.yml`'s `e2e-stack` job is configured to run this command in Linux CI on the tested image with all four projects (step 3), but no run is recorded yet.

  `wrangler dev` takes `CF-Connecting-IP` from the request when one is sent (Miniflare sets it from the socket only otherwise; Cloudflare's edge overwrites it in production). The probe and the stack-mode E2E use that to act as distinct visitors from the benchmarking range 198.18.0.0/15, so the production limits stay in place without the whole suite counting as one visitor.
- **Free-tier emulation** (`-f docker-compose.emulation.yml`): API 0.1 CPU / 512 MB, no swap (Render Free); database 0.25 vCPU / 1 GB, no swap (Neon's 0.25 CU floor). For the 2 CU ceiling set `ATUS_EMU_DB_CPUS=2 ATUS_EMU_DB_MEMORY=8g` (a Docker VM with less than 8 GB caps the RAM). `python deploy/emulation/neon_settings.py --help` prints the settings for other compute sizes from Neon's published formulas. Not emulated (F41): pageserver reads, wake from suspend, autoscaling itself, round trips between providers, Render's real CPU share and spin-up.

  The audit's emulation runs kept `shared_buffers` at 128 MB and Postgres's default `max_connections`/`max_worker_processes`. Neon's formula gives those values only for a fixed 0.25 CU compute; for ADR-011's 0.25–2 CU compute it gives 215 MB, 839 and 17, which the stack now uses. Later timed runs should state which settings they used.
- **Load tests** (brief §13): [`loadtests/`](../loadtests/README.md) runs k6 scenarios (metadata, cached, uncached, worst case, demo mix) and container cold starts against this stack under the emulation profile, with the real serving dump restored. DB profiles are 0.25 CU, 2 CU (7 GiB, below the Docker VM's 7.75 GiB) and unconstrained, plus a laptop baseline against the full database. Every bundle records each scenario's environment and whether it ran alone. Local only; never against a provider. The committed results of the 2026-10-21 runs are [`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md), and ADR-012 "Calibration (§13)" applies them (step 10).
- **Through a transaction pooler** (brief §10; production uses the direct endpoint): profile `pgbouncer` of the emulation file runs PgBouncer with Neon's pooler settings (`pool_mode=transaction`, `max_prepared_statements=1000`, `query_wait_timeout=120`).

  ```bash
  scripts/smoke_stack.sh --integration-via-pgbouncer                          # Neon's settings
  scripts/smoke_stack.sh --integration-via-pgbouncer --prepared-statements 0  # no prepared-statement support
  ```

  Both deselect the integration tests marked `direct_connection` (`pytest -m "not direct_connection"`). Those create roles or template databases, set startup options, terminate backends or hold session-level locks, which no transaction pooler supports; they cover the loader, the serving build and restore, the serving contract's role checks, the equivalence harness's altered-copy check and the stale-connection recovery, none of which runs through a pooler in production.

  Measured on 2026-10-21 (review round 4, at 3dffe1a, the merged review-round-3 tree; this round changes no integration test; functional, fixture data): with `max_prepared_statements=1000` the command **exited 0, 144 passed, 120 deselected**, in each of two runs. The 120 deselected tests, run through the same PgBouncer by hand (`pytest tests/integration -m direct_connection`), ended with 21 failed, 13 passed, 1 skipped and 85 errors; the 13 that passed are loader and restore tests (`test_reload_switch.py`, `test_serving_restore.py`), marked by module because that code only ever runs against the database directly. Review round 3 had recorded 142 passed and 113 deselected, and before the marks the same command had ended with 20 failed, 155 passed, 1 skipped and 79 errors, every one in the tests now marked (the first run, on 2026-10-20, passed all 50 integration tests of the time). With `0` the command exits 1 by design: 14 failed, 98 passed, 120 deselected and 32 errors, every one a `DuplicatePreparedStatement` (`_pg3_0` and others) or its consequence: psycopg prepares a statement after its 5th execution, and the next transaction lands on a server connection that already has one by that name; API requests answered 500. If the API ever goes through a pooler without prepared-statement support, the pool must pass `prepare_threshold=None`.
- **The image gate and the packaging lint** (step 3) run locally exactly as in CI.

### 1. Build the serving DB and its release artifact

- **What (ADR-011, ADR-013).** A serving DB of about 0.65 GB, built from the full local DB, which is only ever read.
  - Its `data_version` is `<release>:serving-<build_id>:<content_hash[:12]>`, with `build_id` = `<UTC YYYYMMDDTHHMMSSZ>-<8 random hex>`.
  - Two builds from the same full DB share the content hash, but never the version.
- **Proof, before anything is published** (brief §9). Run these on the serving DB, as its owner role:
  - `atus validate-analytics`;
  - `ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_analytics_benchmarks.py tests/data_quality/test_api_benchmarks.py`;
  - the randomized full-vs-serving diff.

  `test_full_database.py` runs on the full DB before the build, never on the serving DB.
- **Artifact.** A `pg_dump -Fc` of the serving DB plus its sha256. You attach both to an immutable GitHub Release; publishing is always your action.
- **Commands** (ADR-013 "Release procedure"): `atus build-serving-db --source <full DB, read only> --target <new empty local PostgreSQL 16 database> --artifact-dir <outside the repository>` (or `ATUS_SERVING_DATABASE_URL` for the target), then `atus compare-serving-db` for the randomized diff. The artifact is `atus-serving-<release>-<build_id>.dump` (`pg_dump -Fc --no-owner --no-acl`), `….dump.sha256` and `….manifest.json` (data version, content hash, row counts, sizes; no connection string). The restore checks `sum(pg_database_size)` against `--size-limit-bytes`, default 1,000,000,000 (whether Neon's "1 GB" means 1e9 B or 1 GiB is undocumented, so the default budgets for 1e9 B).
- **Name the release the site serves.** After publishing, run `python scripts/ops/release_manifest.py write --artifact-dir <the artifact directory> --tag <release tag>` and commit the resulting `releases/current.json`. It records the tag, the data version and every asset's name, size and SHA-256. `deploy.yml` downloads exactly those bytes and refuses to run while the file says `unpublished`; `python scripts/ops/release_manifest.py check --require-published` checks it.

### 2. Restore drill

- **What.** Restore the release dump exactly as production will, into a fresh `postgres:16`:
  - `pg_restore --no-owner --no-acl` as a non-superuser owner;
  - the roles script;
  - `VACUUM (ANALYZE)`;
  - the size check;
  - EXPLAIN of the worst cases;
  - a smoke test with a local API.

  The definition of done requires the drill to be performed and timed. The timed drill ran alone on 2026-10-21, in a fresh unconstrained `postgres:16` with the stack's Neon settings and a non-superuser owner: the restore tool took 15.6 s of wall time (`pg_restore` 8.0 s, `VACUUM (ANALYZE)` 1.3 s, the serving checks with the full content hash 5.2 s), all 22 checks passed, and `sum(pg_database_size)` was 649,903,180 B, 65% of the 1,000,000,000 B budget ([`../loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md#3-a-restore-drill-timed), section 3). On Neon the restore runs over the network into remote storage, so this time does not transfer.
- **What runs locally today.** The stack's `dump` profile does the restore half, then the probe runs against real data:

  ```bash
  echo "ATUS_STACK_DUMP=/absolute/path/to/serving.dump" >> .env.stack
  docker compose --env-file .env.stack -f docker-compose.prod.yml --profile dump up -d --wait api
  docker compose --env-file .env.stack -f docker-compose.prod.yml --profile smoke run --rm smoke
  docker compose --env-file .env.stack -f docker-compose.prod.yml --profile dump --profile smoke down -v
  ```

  The teardown names both profiles: a plain `down -v` leaves the exited `restore` container behind. These commands run the default project `atus-stack`, whose `db` publishes `127.0.0.1:${ATUS_STACK_DB_PORT}` (5470 by default); with the restore drill's database still up on that port, follow [operations.md, "Restore drill"](operations.md#restore-drill), step 6, instead.

- **The drill:** `scripts/ops/restore_serving_db.sh --artifact <dump> --verify-content-hash` (implemented by `atus restore-serving-db`) against a fresh local `postgres:16` cluster with a non-superuser owner: the owner DSN from `ATUS_OWNER_DATABASE_URL` or the `--secrets-file` (default `~/.config/atus/neon.env`); the dump's sha256 against its `.sha256` file and manifest; a refusal of any non-empty target; `pg_restore --no-owner --no-acl --single-transaction`; `scripts/ops/roles.sql` with a generated 192-bit `atus_api` password sent as a SCRAM verifier, written back as `ATUS_API_DB_PASSWORD` and `ATUS_API_DATABASE_URL` into the same file (mode 600); `VACUUM (ANALYZE)`; and checks of the data version, rows, marker, role defaults, worst-case plans and size (ADR-013).
- **The stack's roles are the deployed roles.** Its bootstrap creates only `neondb_owner` and the database; the `roles` service then applies `scripts/ops/roles.sql` after every seed or restore, exactly as the restore script does on Neon.

### 3. The image

- **Build:** `docker buildx build --platform linux/amd64 --build-arg ATUS_BUILD_SHA="$(git rev-parse HEAD)" --build-arg ATUS_VERSION=<version> -t atus-api:local .` The tag is the one the gate below checks and the local stack runs (`ATUS_STACK_IMAGE`). The gate does not compare the image's `revision` label with `HEAD`, so gate the image you just built: a stack started without `ATUS_BUILD_SHA` rebuilds `atus-api:local` as build `dev`. On an arm64 Mac, `--platform linux/amd64` runs the gate under emulation; drop it for a native local gate (CI gates the amd64 image).
- **What is in it.**
  - The base is `python:3.12-slim`, pinned by its multi-arch index digest, in a multi-stage build.
  - Runtime dependencies come from `requirements/api.lock.txt` with `--require-hashes --only-binary=:all:`. No compiler is needed, and no unpinned file is downloaded.
  - The project wheel is built with the hash-locked setuptools.
  - The runtime stage holds the base image plus `/opt/venv`, bytecode-compiled at build time. It also precompiles the base image's standard library (`python -m compileall` over `/usr/local/lib/python3.12`, commit 5844ab7): the app user cannot write `__pycache__`, so without it every cold start compiled each standard-library module it imports.
  - It runs as UID/GID 10001 and carries OCI labels (`source`, `revision` = `ATUS_BUILD_SHA`, `version`, `base.digest`).
  - Its Docker `HEALTHCHECK` calls `/health`, which never touches the database.
  - `CMD` runs `atus api --host 0.0.0.0 --port $PORT`, with `PORT=10000` by default.
- **What is not in it:** tests, data, docs, the frontend, `edge/`, `scripts/`, the pipeline-only `curl_cffi`, build tools and the migrations.
  - **Decision (F56):** the API never changes the schema. Production restores a serving dump, and `atus migrate` / `atus load` run from a source checkout, so the image ships no migrations.
  - `.dockerignore` is an allow-list.
- **libpq and TLS.** `psycopg[binary]` bundles its own libpq and OpenSSL. In this image that is libpq 18.6 (`pq.version()` = 180006) on both `linux/amd64` and `linux/arm64`, which meets the ≥ 16 that `sslrootcert=system` needs. See [TLS to the database](#tls-to-the-database).
- **Locks.** `requirements/compile.sh` (uv) regenerates four files, which keep the version ranges of `pyproject.toml` and the `.in` files:
  - `dev.lock.txt`: runtime + dev + build backend;
  - `api.lock.txt`: the image's runtime only, pinned to the dev lock's versions;
  - `build.lock.txt`;
  - `ci-tools.lock.txt`: CI-only tools (`pip-audit`), installed into their own virtualenv, never next to the app.

  `tests/deploy/test_requirements_locks.py` fails if they drift. Contributor and CI install:

  ```bash
  pip install --require-hashes --no-deps -r requirements/dev.lock.txt
  pip install --no-deps --no-build-isolation -e .
  ```
- **The gate: run it before every push, locally or in CI.**

  ```bash
  deploy/ci/lint.sh                                                   # Dockerfiles, compose files, shell syntax
  python deploy/ci/check_image.py atus-api:local
  ```

  - **`check_image.py`** needs only Python and the Docker CLI. It fails unless all of these hold (its check names in brackets):
    - the image runs as a numeric non-root UID:GID, and so does the API server as PID 1, with read-only code (`user`, `user_runtime`);
    - it holds no compiler, linker, build system, VCS, downloader, `-dev` package, Python build backend or build-stage leftover (`build_tools`);
    - its `HEALTHCHECK` requests `/health` on loopback and names nothing database-related (`healthcheck`), and passes with a database host that cannot resolve (`health_no_db`);
    - starting the API as `atus api` does imports only modules that load from a usable `.pyc`, so no cold start compiles anything (`bytecode`), and imports no pipeline-only module and not `python-dotenv` (`startup_imports`). These guard the cold-start gain of commit 5844ab7;
    - it leaves no trace in a throwaway Postgres that logs every connection and statement from the API's role, neither from its health checks (`health_db_quiet`) nor while starting (`startup_db_quiet`). A control `/api/v1/meta` request must leave a trace (`detector`), so that a quiet log means something. This catches a health check that reaches the database in a way no static check can see; the tests prove it with a deliberately broken image.
  - **Strict.** The pool is lazy (ADR-011 `min_size=0`), so the image opens no database connection at start-up and `startup_db_quiet` must PASS. The `--allow-startup-connection` option only exists for images that predate the lazy pool; no recipe uses it.
  - **`lint.sh`** runs `docker build --check` on every Dockerfile, `docker compose config` on every compose set, and `sh -n`/`bash -n` on every script. It runs hadolint and ShellCheck only if they are already on `PATH` (SKIP otherwise) and never installs anything. The GitHub-hosted runners have ShellCheck, so the CI image job always runs it; hadolint runs only where it is installed.
- **CI: `.github/workflows/image.yml`**, called by `ci.yml` (every pull request and push to `main`) and by `deploy.yml`, in this order:
  1. `deploy/ci/lint.sh`. ShellCheck runs here, because the GitHub-hosted runners have it;
  2. `docker/build-push-action` for `linux/amd64`, loaded into Docker and never pushed, with `ATUS_BUILD_SHA=$GITHUB_SHA`. Provenance and SBOM attestations are off, so every GHCR version is one deployable manifest;
  3. `python deploy/ci/check_image.py atus-api:ci` in strict mode (no `--allow-startup-connection`);
  4. `ATUS_DOCKER_TESTS=1 ATUS_TEST_IMAGE=atus-api:ci pytest tests/deploy`; the stack smoke runs as the next two steps, and the edge cycle (`test_edge_stack.py::test_edge_cycle`) as `ci.yml`'s `e2e-stack` job (below);
  5. `scripts/smoke_stack.sh --no-build` with `ATUS_STACK_IMAGE=atus-api:ci`, once plain and once with `--emulation`;
  6. Trivy, installed from its immutable GitHub release and pinned by version and SHA-256, never through `aquasecurity/trivy-action` or `setup-trivy`, whose tags were hijacked in March 2026 ([GHSA-69fq-xp46-6x23](https://github.com/advisories/GHSA-69fq-xp46-6x23)). It fails on HIGH or CRITICAL vulnerabilities that have a fix, and keeps the full SARIF report as an artifact;
  7. `docker save` of the tested image plus its ID, handed on as the workflow artifact `atus-api-image`.
- **CI: the public origin, `ci.yml` job `e2e-stack`.** It loads that tested image (same image ID) and runs `scripts/smoke_stack.sh --no-build --edge --e2e` (step 0) with `E2E_PROJECTS="desktop mobile firefox webkit"`: the API smoke, the SPA build, the static snapshot, the Worker under `wrangler dev`, every check of `deploy/stack/edge_probe.py`, `deploy.yml`'s post-deploy smoke (`deploy_smoke.py`), the whole Playwright suite in stack mode (`E2E_BASE_URL`) through the Worker, Firefox included, and `deploy.yml`'s browser smoke (`playwright.deployed.config.ts`). The job installs the hash-locked environment for `deploy_smoke.py`'s §9 comparison.
- **Push: `deploy.yml` only.** Its `push` job, the one job that pushes an image, loads that artifact, checks that the image ID is the one the build job tested, and pushes it as `ghcr.io/nathan98000/atus-api:sha-<commit>` (plus `:<tag>` for a tag run). Its output is the digest reference. After a deploy passed its smoke, the `promote` job (also `packages: write`) adds the tags `live` and `deployed-<commit>` to that same digest; it pushes no new image. `ghcr-cleanup.yml` keeps the newest 20 versions and the one tagged `live`, weekly.
- **First push and GHCR visibility:** owner step 4.5. The package starts private.

### 4. Accounts (owner)

Follow [Owner account setup](#owner-account-setup-19) in this order. Each step there says which artefact it waits for.

1. Cloudflare 1.1–1.2: the account and the subdomain. Decide the subdomain first: every share link contains it.
2. Render 2.1–2.2: the account and the region check. Create nothing yet.
3. Neon 3.1–3.4: the project in the paired region, compute 0.25–2 CU.
4. GitHub 4.2–4.4: the environments (with `production`'s variable `PUBLIC_BASE_URL`) and the repository settings.
5. Step 7, "Bootstrap": Cloudflare 1.3.1–1.3.2 and the KV namespace id committed to `main` before the run is dispatched; after the run has pushed the image, GitHub 4.5 (package public), then Render 2.3–2.6 (service by digest, environment, deploy hook) and Cloudflare 1.3.3–1.3.4 (the first Worker deploy); after that run has passed, Render 2.7 (Image URL `:live`).
6. After step 8 has verified the site: GitHub 4.1's repository variable `PUBLIC_BASE_URL`, which starts the monitors.

### 5. Load the serving DB into Neon (owner)

- **Owner step 3.5.** Run the restore script against the **direct** endpoint as `neondb_owner`, with a client of version 16 or newer. The DSN comes from the project's secrets file, `~/.config/atus/neon-<release>-<build8>.env`, passed with `--secrets-file`. The script:
  1. creates `atus_api`;
  2. applies the grants and role defaults (`statement_timeout=85s`, `lock_timeout=5s`, `idle_in_transaction_session_timeout=30s`, `jit=off`);
  3. runs `VACUUM (ANALYZE)`;
  4. checks the metered size;
  5. writes the `atus_api` DSN back into the same file.
- **Then on Render:** set `ATUS_DATABASE_URL` and `ATUS_EXPECTED_DATA_VERSION` (see [Configuration reference](#configuration-reference)).
- **First real TLS check.** This is the first connection with `sslmode=verify-full&sslrootcert=system` against Neon's real certificate: the owner's session too, whatever `sslmode` the pasted string carries (`restore.owner_url`; before review round 3 the owner's session used the pasted `sslmode=require` and verified nothing, and only the later `atus_api` checks were verified).
- **CA roots on your machine.** `system` means the operating system's CA bundle, which neither the Python driver's bundled OpenSSL nor the `postgres:16` image finds by itself. The `atus` commands (the restore, `atus api`, `verify-serving-db`, `compare-serving-db`, `build-serving-db`) set `SSL_CERT_FILE` to the system bundle when it is unset (macOS: `/etc/ssl/cert.pem`; Debian/Ubuntu: `/etc/ssl/certs/ca-certificates.crt`), and the docker fallback mounts that file read-only. On a system with none of these, `export SSL_CERT_FILE=<CA bundle>` first; without roots the restore fails closed ("certificate verify failed"), it never connects unverified. See [TLS to the database](#tls-to-the-database).
- **The command:** `scripts/ops/restore_serving_db.sh --artifact <the release's .dump> --secrets-file ~/.config/atus/neon-<release>-<build8>.env --report restore-report.json`, with `ATUS_OWNER_DATABASE_URL` in that file. Without `--secrets-file` the script reads `~/.config/atus/neon.env`, the default, which the next release would then overwrite. Keep the previous project's file until its deletion is approved: a data rollback needs its `atus_api` DSN.
- **Not verifiable before this step:** whether Neon accepts `ALTER ROLE atus_api SET ...` from `neondb_owner` (ADR-011 "Unknowns"; the local drill emulates a non-superuser `CREATEROLE` owner, where it works). The restore fails loudly if it does not, and prints PostgreSQL's reason. `pg_restore` has committed by then: after fixing the cause, finish with the same command plus `--roles-only` ([operations.md](operations.md), "Finish a restore that failed after `pg_restore`").

### 6. First Worker deploy (owner)

- **Owner step 1.3:**
  1. `cd edge && npm ci`, then `npx --no-install wrangler login` from `edge/` (the wrangler locked in `edge/package-lock.json`, never a download);
  2. `npx --no-install wrangler kv namespace create <KV_NAME> --update-config=false` from `edge/` (so wrangler adds no second binding), its id put into `edge/wrangler.jsonc` and committed to `main` before the first deploy run is dispatched (step 7, "Bootstrap");
  3. the secrets, generated into `~/.config/atus/edge.env` by `scripts/ops/gen_edge_secrets.sh`;
  4. what the deploy publishes and the locked tools, which a fresh clone lacks: `(cd frontend && npm ci && VITE_API_BASE_URL= npm run build)`, then `cd edge && npm ci`;
  5. the first `npx --no-install wrangler deploy` from `edge/`, which you run yourself, because creating a Worker needs account Admin, with the Worker's secrets from that file; later changes are piped into `wrangler secret put`. It publishes the SPA without a snapshot until the approved deploy replaces it.
- **What CI asserts on `edge/wrangler.jsonc` before every later deploy** (ADR-011):
  - no `[cache]` block;
  - `preview_urls = false` and `workers_dev = true`;
  - `run_worker_first` lists both `/api` and `/api/*`;
  - static assets come from `frontend/dist`;
  - the release vars keep their safe defaults, except the three you change by hand and commit: `EPOCH` (1 to 32 letters, digits, `.`, `_` or `-`; never empty, which would turn the edge cache off), `PREWAKE` (`on` or `off`) and `NEON_PROJECT_ID`, which may hold the project id you commit (letters, digits and hyphens, at most 64; `edge/test/node/config.test.ts`). The Worker tests pin those three (`edge/test/release.ts`), so a commit that bumps `EPOCH` or turns `PREWAKE` off changes `edge/wrangler.jsonc` alone and passes CI and the deploy's `edge-config` job.
- **Names** (`edge/wrangler.jsonc`): Worker `atus`, KV binding `RESULT_CACHE` (replace the placeholder namespace id with the one `kv namespace create` prints), Durable Object binding `GOVERNOR`, class `Governor`, migration tag `v1`.
- **Secrets:** `scripts/ops/gen_edge_secrets.sh [--render-origin https://<service>.onrender.com]` writes `~/.config/atus/edge.env` and prints the commands, including the first deploy's filtered `--secrets-file` (see [Cloudflare Worker](#cloudflare-worker)).
- **Rehearsed locally:** `scripts/smoke_stack.sh --edge --e2e` runs this Worker, with the same `wrangler.jsonc`, in front of the production-mode API (step 0).
- **What `frontend/dist` contains** (`npm run build` without `VITE_API_BASE_URL`): the SPA with a relative API base, `_headers` (CSP with `connect-src 'self'`, HSTS, `Permissions-Policy`, `index.html` and `/snap/current.json` no-cache, hashed assets and `/snap/<id>/*` immutable), and a `<meta>` CSP backstop in `index.html`. The SPA fallback is `not_found_handling = "single-page-application"` in `wrangler.jsonc`; the snapshot is added under `snap/` by step 7.

### 7. First approved deploy (CI)

`deploy.yml` runs on a `v*` tag or manual dispatch, in environment `production`, and waits for your approval as required reviewer. ADR-011 fixes its steps:

1. Call `RENDER_DEPLOY_HOOK_URL` with `imgURL=ghcr.io/nathan98000/atus-api@sha256:<hex>`, URL-encoded. Every part except the digest must match the image configured on the service.
2. Poll origin `/health` until its `build` equals `GITHUB_SHA`, then require its `expected_data_version` (Render's `ATUS_EXPECTED_DATA_VERSION`) to equal the release's `data_version`. Allow for a cold start: about a minute of Render spin-up plus about 6 s of app start.
   - At 0.1 CPU the first `/health` came at a p50 of 5.8 s (5.5–6.5 s), and `/api/v1/meta` 0.1–0.4 s later. That is the image that precompiles its standard library (commit 5844ab7), measured at 0.25 CU with n=8 ([cold-start A/B](../loadtests/results/2026-10-21-cold-start-ab.md)).
   - Before that commit it was 12.9–13.1 s ([2026-10-21 emulation](../loadtests/results/2026-10-21-emulation.md), section 4). The audit measured 7–11 s on an earlier image (F67).
3. Generate the static snapshot with that same digest, against the release dump restored into `postgres:16` with the production role settings.
4. `wrangler deploy` with the vars `DATA_VERSION`, `ANALYTICS_VERSION`, `RESPONSE_FORMAT` and `SNAPSHOT_ID`.
5. Run a smoke test of at most 40 requests.

**`deploy.yml`** runs on a `vMAJOR.MINOR.PATCH` tag or a manual run from `main`. Its jobs, each with only the permissions it needs:

| Job | Permissions | What it does |
|---|---|---|
| `preflight` | `contents: read`, `actions: read` | refuses any other ref; refuses a commit without a successful `ci.yml` run on a push to `main`, and a tag on a commit that is not on `main` (`github_ops.py deploy-gate`: this workflow runs only the Worker's config tests and the SPA build, so the Worker tests, typecheck, lint and both Playwright jobs must have passed in CI); `releases/current.json` must name a published release |
| `build` | `contents: read` | `image.yml` from scratch (no build cache) |
| `push` | `contents: read`, `packages: write` | pushes exactly the tested image; outputs `ghcr.io/nathan98000/atus-api@sha256:<hex>` |
| `snapshot` | `contents: read`, `packages: read` | builds the static snapshot from the committed release with that digest (below) |
| `edge-config` | `contents: read` | the `wrangler.jsonc` invariant tests (`npx vitest run --project node` in `edge/`) and the placeholder KV namespace id check, in a job without secrets |
| `deploy` | environment `production` (your approval); `contents: read`, `packages: read` | deploys and verifies (below) |
| `browser-smoke` | `contents: read` | after `deploy`, without secrets: one browser journey against the public URL (below) |

**The snapshot job.**

1. `scripts/ops/release_manifest.py fetch` downloads the assets named in `releases/current.json` from the GitHub release. Each must have exactly the recorded size and SHA-256, and the artifact's own manifest must agree with `current.json`.
2. It restores the dump into a `postgres:16` service with `atus restore-serving-db`, as a non-superuser `neondb_owner`. That applies `scripts/ops/roles.sql`, so `atus_api` has exactly its production privileges and role defaults; then it runs `VACUUM (ANALYZE)` and the restore checks.
3. It pulls the pushed digest and runs it read-only: `--read-only`, every capability dropped, production mode, the release's `ATUS_EXPECTED_DATA_VERSION`, and generous per-client limits for the builder. The image must report `build` = `GITHUB_SHA`.
4. It builds the same-origin SPA, then the snapshot step, the same command the local stack runs: `python scripts/build_snapshot.py --api-base <that API> --edge-secret-file <file> --requests-script frontend/scripts/snapshot-requests.mjs --api-build "$GITHUB_SHA" --data-version "$DATA_VERSION" --out frontend/dist/snap` (its stdout is `SNAPSHOT_ID`), then `--verify frontend/dist/snap`. Every analysis response must carry `X-Data-Version`, `X-Analytics-Version` and `X-Response-Format`, and every prewarmed spec must succeed on the release (no `--skip-refused`, which exists for the local stack's fixture data only).
5. It runs `npm run check:bundle` and checks the Workers static-asset limits (20,000 files, 25 MiB each), then hands `frontend/dist` to the deploy job as the artifact `site-dist`. The artifact is kept 30 days, the longest the deploy job can wait for your approval.

The snapshot is built before anything deploys, so a failed restore or snapshot deploys nothing.

**The deploy job.**

1. It checks that the environment holds its four secrets and that `PUBLIC_BASE_URL` is set. It installs only what its steps run: the API's runtime lock and the package, binary wheels only (`--only-binary=:all:`), for the smoke's §9 comparison, and the edge's lockfile for `wrangler`. The `wrangler.jsonc` invariant tests ran in `edge-config`, and the browser journey runs in `browser-smoke`: their test and browser tooling never runs next to the deploy secrets.
2. It checks the Cloudflare token before the Render hook, while nothing has changed: `wrangler deployments status` must succeed, so an expired, revoked or wrong-account token stops the job here instead of at `wrangler deploy` after Render has switched (review round 3). Its output is the Worker's rollback target. The read cannot prove that the token may deploy (the Editor role); that shows only at step 5. Then, best effort, it records the build the origin runs now and that build's GHCR digest. Those may fail without stopping the job, because a forward fix must deploy while the API is down, so a wrong `RENDER_ORIGIN` shows only at the wait (step 4), after the hook.
3. `scripts/ops/render_deploy.py trigger` POSTs `RENDER_DEPLOY_HOOK_URL` with `imgURL=<URL-encoded digest reference>`. The URL is read from the environment and never printed; anything but an `https://api.render.com/deploy/srv-...` URL is refused.
4. `scripts/ops/render_deploy.py wait` polls the origin's `/health` until `build` equals `GITHUB_SHA`. It allows 15 minutes: the image pull, Render's ~1 minute spin-up and the app start. The app start took about 6 s at 0.1 CPU in the emulation ([cold-start A/B](../loadtests/results/2026-10-21-cold-start-ab.md); about 13 s before commit 5844ab7).
   - `/health` reports the commit, not the image digest. If the origin already ran this commit before the hook (for example "Re-run all jobs", which rebuilds and pushes a new digest of the same commit), the wait cannot see the switch. The step then shows a warning, and you confirm the deploy in Render → Events.
   - Then the origin must be bound to the release this run publishes: `/health`'s `expected_data_version` (Render's `ATUS_EXPECTED_DATA_VERSION`, read from its configuration, not the database) must equal the snapshot's `DATA_VERSION`. Otherwise the step fails at once and nothing is published at the edge; the API image it deployed stays bound to its old data, and `promote` does not run, so `live` keeps naming the previous image until a later deploy passes. Once `releases/current.json` on `main` names a new release, every deploy carries it, a hotfix included, so no deploy can pass until Render is switched (data release step 6) or the manifest commit is reverted.
5. `npx --no-install wrangler deploy` (the version locked in `edge/package-lock.json`) publishes the SPA, the snapshot and the Worker, with `--var` `DATA_VERSION`, `ANALYTICS_VERSION`, `RESPONSE_FORMAT` and `SNAPSHOT_ID` read from the snapshot.
6. The post-deploy smoke, at most 40 requests in all:
   - `scripts/ops/deploy_smoke.py`, at most 30 requests: first a bounded wait until `/api/_edge/health` and `/snap/current.json` name this release (the colo answering the runner may serve the previous version for a few seconds after `wrangler deploy`; at most 4 polls each, 15 s apart, within 60 s), then the SPA shell and its headers; the SPA fallback; the snapshot pointer and manifest; `/api/_edge/health`; `/api/health`, with a cold-start budget; `/api/v1/meta` (which the Worker answers from the snapshot); two home-page starters, whose live answers also prove the API's `X-Data-Version`, served from the snapshot and recomputed live with request bytes unique to the run (JSON whitespace encoding a random nonce: the edge cache keys on the exact bytes and keeps its entries across code-only deploys, so fixed bytes were answered from KV from a release's second deploy on; an `X-Cache: edge` answer now fails the check) and diffed under the §9 criterion. A refusal by the quota governor is OWED, exit status 3: not a reason to roll back ([operations.md "Deploy"](operations.md#deploy), step 6); an unknown `/api` path; and the origin lock (the onrender.com URL answers 403 `forbidden_origin`);
   - then, in the `browser-smoke` job, one browser journey, `frontend/playwright.deployed.config.ts`: a starter renders, with no CSP errors and at most 6 `/api` requests. `promote` waits for it.
   - A failed smoke skips `browser-smoke` and `promote` (a failed browser journey skips `promote`), although Render already runs the new digest: `live` and `deployed-<commit>` keep naming the previous one until you re-run the failed jobs, tag by hand or roll the API back ([operations.md, Deploy](operations.md#deploy) step 6).
7. It writes the rollback notes (below) into the job summary, whatever the outcome.

A push to `main` deploys nothing: deploy.yml has no branch trigger, which `tests/deploy/test_workflows.py` asserts along with the permissions above.

**Rehearsed locally (2026-10-20; functional, nothing timed, no provider contacted).** The snapshot job and the deploy job's checks were run on this Mac against the real release artifact (build `20261020T224252Z-a7420a5c`):

- `release_manifest.py fetch` from a loopback server verified all three assets;
- `atus restore-serving-db` restored the dump into a fresh `postgres:16` as a non-superuser `neondb_owner`, and every restore check passed;
- the image, read-only and in production mode, served `build_snapshot.py`: 560 metadata responses and 229 analyses, every prewarmed specification answered 200, and the snapshot verified;
- `wrangler dev` (local mode) then served that `frontend/dist` with the Worker in front of the same API. `deploy_smoke.py --require-snapshot --recompute 2` passed every check in 13 requests: both starters were equivalent under §9 between snapshot and live. The browser smoke passed.

What a rehearsal cannot show is Render's deploy hook, its image pull, and the real Cloudflare deploy. On fixture data, every CI run repeats the snapshot and Worker half of this through the production compose stack (`ci.yml` job `e2e-stack`, step 3).

**Bootstrap: the very first deploy.** Nothing exists on Render yet, so the first run is split around your approval. The `deploy` job checks out the commit the run was dispatched from and refuses the placeholder KV namespace id, so the real id must be on `main` before the run starts.

1. Publish the release artifact and commit `releases/current.json` (step 1).
2. Create the KV namespace (owner steps 1.3.1–1.3.2): `cd edge && npm ci`, then, from `edge/`, `npx --no-install wrangler login` and `npx --no-install wrangler kv namespace create <KV_NAME> --update-config=false` (owner step 1.3.2 says why). Put the id it prints into `edge/wrangler.jsonc` in place of the placeholder, then commit and push that change to main. Wait until CI on that push has passed (`gh run watch`): `preflight` refuses a commit without a successful `ci.yml` run on a push to `main`.
3. **Actions → Deploy → Run workflow** on `main`, at or after that commit. `preflight`, `build`, `push` and `snapshot` run; `deploy` waits for your approval, for up to 30 days.
4. Meanwhile:
   - make the package public (owner step 4.5);
   - create the Render service from the digest in the `push` job's summary (owner steps 2.3–2.6);
   - add the GitHub secrets (owner steps 4.2–4.3), and `production`'s variable `PUBLIC_BASE_URL` if it is not set yet (owner step 4.1, first bullet; the repository variable waits for step 7);
   - run the first Worker deploy yourself (owner steps 1.3.3–1.3.4: build `frontend/dist` and run `npm ci` in `edge/` first; `RENDER_ORIGIN` must exist by then). Until step 5 the public hostname serves that build without a snapshot.
5. Approve `deploy`. It deploys the same digest through the hook, publishes the Worker with the release vars and runs the smoke. Then `promote` tags that digest `live`.
6. Point the service at `:live` (owner step 2.7). From then on nothing on Render names the bootstrap digest.
7. After step 8 has verified the site, set the repository variable `PUBLIC_BASE_URL` (owner step 4.1). The monitors start with it.

### 8. Prove the deployment

"Verified" means curl plus Playwright against the public URL. At least:

- A docs-only push to `main` deploys nothing. Check Render → Events and Cloudflare → Deployments.
- The origin lock: `curl -s -o /dev/null -w '%{http_code}' https://<service>.onrender.com/api/v1/meta` prints `403`.
- Security headers on both origins: the SPA's `_headers` and the Worker's own. Include the SPA-fallback `index.html`, where `_headers` may not apply (ADR-011 "Risks").
- `/health` (allowing for the cold start), then `/api/v1/meta` (the Worker answers it from the snapshot; `/api/v1/meta?check=1` reaches the API as a counted lookup), one cached analysis, the SPA shell and one starter analysis rendering, with no CSP violations in the console.
- At least 2 snapshot entries recomputed live and compared under §9: by the API on Render against Neon, never answered from the edge cache (an `X-Cache: edge` answer fails the check).
- The deploy job runs these checks (step 7). To repeat them by hand: `python scripts/ops/deploy_smoke.py --base-url https://atus.<sub>.workers.dev --require-snapshot --recompute 2` (exit status 3: everything passed but the quota governor refused the recompute, which is owed until the time its OWED rows name), then `PUBLIC_BASE_URL=... npx playwright test -c playwright.deployed.config.ts` in `frontend/`. The README gets the live URL only after this step passes.
- **Then start the monitors:** set the repository variable `PUBLIC_BASE_URL` (owner step 4.1). Until it exists, the edge check, deep check and quota report are skipped.

### 9. Quota-budgeted post-deploy replay

- Measure what ADR-011 lists under "Unknowns to measure after deploy".
- Budget, in total: at most 150 Worker requests, 3 Neon CU-h and 15 Render wakes. No load tests against a provider.
- Results go into ADR-011, ADR-012 and this document, labelled as measured.
- **The replay script: `scripts/replay_deployed.py`.** It is the URL-targeted, quota-budgeted full-data quality suite (brief §16).
  1. Once per release, on your machine, against `atus api` serving the **full** database (loopback only):
     `python scripts/replay_deployed.py --base-url http://127.0.0.1:8000 --record expected-<release>.json`.
     Keep the file outside the repository.
  2. Against the site, with the release's `data_version` (`releases/current.json`):
     `python scripts/replay_deployed.py --base-url https://atus.<sub>.workers.dev --expected expected-<release>.json --expect-data-version <data_version> --report replay.json`.
     The replay fails unless every answer carries that `X-Data-Version`, and when the site reports the data version the file was recorded from (the full database compared with itself).
- **What it sends.** The 24 BLS benchmarks of `atus_pipeline.analytics.benchmarks` (23 distinct specifications), `/api/v1/meta`, a full-period sleep trend, a by-sex comparison, and last the F66 broad selector (every tier-1 code pooled over every multi-year year, with replicate variance): 28 requests.
  - The broad selector is the costliest analysis on the serving DB. ADR-011 adds it to this replay so that the statement timeout is sized with it (ADR-012). Its response time is in the report.
- **What it checks.** Every benchmark against BLS's published value and the benchmark's own tolerance, and every response against the full-database recording under §9.
  - The broad selector covers every minute of the diary day, so its estimate must be exactly 1,440 minutes with a zero standard error.
- **Quota safety.**
  - Analyses are paced 10.5 s apart, to stay inside the API's per-client bucket.
  - The hard budget is 40 requests; a larger budget is refused, and so is a plan that does not fit.
  - Retries come out of the same budget.
  - A capacity or quota refusal stops the run.
  - A second run is answered from the edge cache (`X-Cache: edge`).
- **Measurements.** The report records each response's `X-Cache`, `Server-Timing` and elapsed time, so the latency matrix above can be read from it.
- **Rehearsed locally (2026-10-20, functional).**
  - `--record` against `atus api` on the full local database (read only) passed all 24 benchmarks and both by-sex values.
  - The replay against `atus api` on the restored release, as the read-only `atus_api` role, passed the same checks, and all 26 responses compared equal under §9. The largest scaled SE/CI difference was 7.45e-16; `/meta` was identical apart from its data version.
  - Each run sent 27 requests.
  - Re-run after the F66 broad selector was added (independent verification, same day, same method, against the WS-B restore drill's serving DB): 28 requests per run, 24/24 benchmarks, all 27 responses equal under §9 (largest scaled SE/CI difference 1.59e-15). The broad selector gave 1,440.000000 minutes on both databases.

### 10. Week-1 calibration

**Starting values.** The §13 emulation runs calibrated these (ADR-012 "Calibration (§13)", [`loadtests/results/2026-10-21-emulation.md`](../loadtests/results/2026-10-21-emulation.md)). Every one is the code default, so leave them all **unset on Render**:

| Setting | Value | Where it is set |
|---|---|---|
| `ATUS_API_STATEMENT_TIMEOUT_MS` | `85000` (request deadline 90 s) | code default; the `atus_api` role's backstop `statement_timeout = 85s` comes from `scripts/ops/roles.sql`, applied by the restore |
| `ATUS_API_MAX_INFLIGHT` / `ATUS_API_MAX_INFLIGHT_HEAVY` | `2` / `1` | code default |
| `ATUS_API_ADMISSION_WAIT_S` | `5` | code default |
| `ATUS_API_MAX_PARALLEL_WORKERS` | unset (the server's default) | code default |
| heavy threshold | 8 year queries | `HEAVY_YEAR_QUERIES` in the API and in `edge/src/constants.ts` |

`tests/deploy/test_packaging_files.py` fails if the template, the code, `roles.sql` and the Worker's constants stop agreeing.

**What the measurements say at Neon's 0.25 CU floor.** The F66 compare by sex took 69.2 s at p50 and at most 71.0 s, so 85 s is only 1.2 × its slowest time. A concurrent analysis slows it further: with two heavy analyses running at once, the F66 requests took 88–106 s, which the single heavy lane prevents. A request that passes 85 s answers `503 analysis_timeout`. At 2 CU the compare took about 3 s.

**After the replay (step 9):**

- **Neon compute.** Read Neon's `compute_time_seconds` per wake session. If it exceeds about 0.1 CU-h, either lower the compute's maximum to 1 CU (after a 1 CU emulation run) or fix it at 0.25 CU.
- **Timeout.** Apply `clamp(3 × p99 measured on Neon, 30 s, 85 s)` (ADR-011 "Bounded work") to the replay's worst cases, F66 included. Lower 85 s only if those requests ran while the compute was at its 0.25 CU minimum and 3 × their p99 is below 85 s. A replay that saw only a scaled-up compute says nothing about the floor. To lower it, set `ATUS_API_STATEMENT_TIMEOUT_MS` on Render: the role default and the Worker's `PROVISIONAL_BUSY_MS` stay safe upper bounds. A permanent change also updates the code default, the template, `roles.sql` and `PROVISIONAL_BUSY_MS` together, which the test above checks. It cannot go higher: 85 s is the clamp's ceiling, because ADR-011 keeps every limit at or below 100 s.
- **Parallel workers.** If the compute is fixed at 0.25 CU, set `ATUS_API_MAX_PARALLEL_WORKERS=0` on Render once its §9 proof passes. It cuts the F66 compare there from 69.2 to 38.6 s at p50. Do not set it while the compute autoscales: at 2 CU it is slower.
- **Lanes.** Change them only if production logs show light analyses shed with `overloaded`, or `analysis_timeout` under ordinary traffic.

## Configuration reference

This lists every variable and secret the deployed system uses: where it is set, where your own copy lives, its default, an example and how it changes or rotates.

- **API names are final.** They match `src/` and [`.env.production.example`](../.env.production.example); `tests/deploy/test_packaging_files.py` fails if the code reads a setting this file does not document, or if a template names one this section does not.
- **Worker names** are those of [`edge/wrangler.jsonc`](../edge/wrangler.jsonc), which is authoritative; `edge/test/node/config.test.ts` asserts its invariants. **GitHub names** are exactly those `.github/workflows/` reads; `tests/deploy/test_workflows.py` asserts which jobs may read the deploy secrets.
- **Templates:** [`.env.example`](../.env.example) for development and [`deploy/stack/stack.env.example`](../deploy/stack/stack.env.example) for the local stack.

**Where values live.** Real values exist only in the provider stores below and in your private files.

- Never in the repository: `.gitignore` ignores every `.env*` and `*.env` file except the committed templates (F31).
- Never in chat or on a command line.
- Examples below show formats only.

| Location | What it is | How a change takes effect |
|---|---|---|
| **Render env** | Render → service → Environment | "Save and deploy" redeploys the service with the new values. Render documents that it redeploys "your service's existing build" (render.com/docs/configure-environment-variables, read 2026-10-21) but not which image an image-backed service then pulls; with the Image URL at `:live` (owner step 2.7) either way it is the last digest a deploy verified. Afterwards `/health` must report the `live` build (deep-check checks it; by hand: [operations.md, Conventions](operations.md#conventions)) |
| **Image** | baked in at build time (Dockerfile) | a new image, deployed by digest; never set these on Render |
| **Worker secret** | `npx --no-install wrangler secret put NAME`, piped from a file; encrypted, and never readable again through the dashboard or wrangler, but readable by any code deployed to the Worker (so a leaked deploy token exposes every one: [Rotation](#rotation), item 5) | the command deploys a new Worker version |
| **Worker var** | `vars` in `edge/wrangler.jsonc`, or `--var` from `deploy.yml` | the next `wrangler deploy` |
| **GitHub secret / var** | Settings → Secrets and variables → Actions (the repository variable); Settings → Environments → `production` or `ops` (secrets) | the next workflow run |
| **Owner file** | `~/.config/atus/<name>.env`: `chmod 600`, outside the repository, your master copy | written by the scripts that generate the values |

### API (Render → Environment)

**Required in production.** `ATUS_ENV=production` refuses to start without `ATUS_DATABASE_URL` and `ATUS_EDGE_SECRETS`, with an edge secret or ops token shorter than 32 characters, with more than two edge secrets, or with the development database URL. `tests/deploy/test_container_image.py::test_production_mode_refuses_to_start_without_its_settings` (opt-in) proves it on the image.

| Variable | Set in (your copy) | Default (development) | Production example | Change / rotation |
|---|---|---|---|---|
| `ATUS_ENV` | Render env; also the image default | `development` | `production` | never |
| `ATUS_DATABASE_URL` | Render env, **secret** (the project's `~/.config/atus/neon-<release>-<build8>.env`, written by the restore script) | `postgresql://atus:atus@localhost:5434/atus` | `postgresql://atus_api:<password>@ep-<endpoint-id>.us-east-1.aws.neon.tech:5432/atus?sslmode=verify-full&sslrootcert=system` | at every release switch (new project); the password per [Rotation](#rotation). Never the `-pooler` host; never `sslmode=require` alone |
| `ATUS_DATABASE_TLS` | **never on Render**; set only for the local production-mode stack (`docker-compose.prod.yml`, `loadtests/`, `deploy.yml`'s snapshot job, `deploy/ci/check_image.py`) | unset: `verify` | unset | never. Production refuses an `ATUS_DATABASE_URL` without `sslmode=verify-full` (`verify-ca` included: it checks the certificate chain but not the host name, so with a public CA bundle any public certificate would pass); `local-stack-only` lifts that, and is itself refused unless every host in the URL is a compose service name without dots, `localhost` or a loopback address, `host.docker.internal`, or a Unix socket |
| `ATUS_EDGE_SECRETS` | Render env, **secret** (`~/.config/atus/edge.env`, as `EDGE_AUTH_SECRET`) | empty: no edge authentication | `<current>`, or `<current>,<next>` while rotating; each 32 random bytes, hex (64 characters, `gen_edge_secrets.sh`) | [Rotation](#rotation): two accepted values overlap, so no request fails |
| `ATUS_EXPECTED_DATA_VERSION` | Render env | empty: no check | `0325:serving-20261020T120000Z-1a2b3c4d:0123456789ab` | at every release switch, together with `ATUS_DATABASE_URL` (ADR-011 "Release switch"). A mismatch answers 503 `data_version_mismatch`. While the database serves exactly this version, the in-process snapshot is pinned: metadata routes and cache hits never read the database again (ADR-012) |
| `ATUS_OPS_TOKEN` | Render env, **secret** (`~/.config/atus/edge.env`, generated by `scripts/ops/gen_edge_secrets.sh`; not a GitHub secret) | empty: `/metrics` and `/ready` answer 404 | 32 random bytes, hex (64 characters) | [Rotation](#rotation) |
| `WEB_CONCURRENCY` | Render env; also the image default | `1` (image) | `1` | never: one uvicorn process (ADR-011) |
| `PORT` | set by Render; image default | `8000` (`atus api`), `10000` (image) | `10000` | never set by hand |

**Optional tuning.**

- **Where:** Render env, normally **unset**. The production defaults below apply.
- **Not secrets.** To change one, set it and "Save and deploy"; delete it to return to the default.
- **When:** change one only when a measurement justifies it (ADR-012).

| Variable | Default (development → production) | Example | Meaning |
|---|---|---|---|
| `ATUS_LOG_LEVEL` | `INFO` | `DEBUG` (briefly, during an incident) | log threshold |
| `ATUS_LOG_FORMAT` | `text` → `json` | `json` | log line format |
| `ATUS_API_DOCS` | `on` → `off` | `off` | the API's `/docs`, `/redoc` and `/openapi.json` routes. In production the site publishes the committed schema (`frontend/openapi.json`) as a static asset at `/openapi.json` instead, outside `/api`, so neither the Worker nor the API runs for it |
| `ATUS_API_CORS_ORIGINS` | empty | `http://localhost:5173` (development only) | allowed cross-origin callers; empty behind the same-origin Worker |
| `ATUS_API_CACHE_SIZE` | `256` | `512` | in-process result LRU entries (lost at every spin-down) |
| `ATUS_API_POOL_MAX_SIZE` | `4` | `4` | connections at most; must exceed `ATUS_API_MAX_INFLIGHT`, or the API refuses to start. The binding ceiling is the role's, not the server's (Neon allows 839 at 0.25–2 CU): `atus_api` may hold at most 10 connections (`CONNECTION LIMIT 10` in `scripts/ops/roles.sql`), and a connection past it is refused (SQLSTATE 53300), which the API answers as 503. If old and new instances overlap during a Render deploy (unverified), both pools count, so keep 2 × `ATUS_API_POOL_MAX_SIZE` ≤ 10 |
| `ATUS_API_POOL_MAX_IDLE_S` | `240` | `240` | psycopg_pool closes at most one idle connection per 240 s period, so a pool of 4 idle connections takes up to 16 min to empty and connections can outlive Neon's 300 s suspend. The first checkout after a wake finds them dead, closes every idle one and opens at most two (ADR-012 "Pool") |
| `ATUS_API_POOL_TIMEOUT_S` | `5` | `5` | pool acquire timeout → 503 `overloaded` |
| `ATUS_API_CONNECT_TIMEOUT_S` | `10` | `15` | libpq `connect_timeout` (covers a Neon wake) |
| `ATUS_API_STATEMENT_TIMEOUT_MS` | `85000` | `85000` | per-request `SET LOCAL statement_timeout` for analyses; the request deadline is this plus 5 s. Calibrated on the emulation profile (ADR-012 "Calibration (§13)"): keep 85000 while the compute's minimum is 0.25 CU, where the F66 compare takes up to 71 s. The week-1 replay may revise it (deploy guide, step 10) |
| `ATUS_API_META_STATEMENT_TIMEOUT_MS` | `10000` | `10000` | the same, for metadata and lexicon reads |
| `ATUS_API_JIT` | `off` | `off` | per-request `SET LOCAL jit` (Neon has no LLVM; F37) |
| `ATUS_API_MAX_PARALLEL_WORKERS` | unset: the server default | `0` | per-request `SET LOCAL max_parallel_workers_per_gather`. Leave unset in production: `0` is faster at 0.25 CU but slower at 2 CU (ADR-012 "Calibration (§13)"). Set `0` only if the compute is fixed at 0.25 CU, after its §9 proof |
| `ATUS_API_MAX_INFLIGHT` | `2` | `2` | uncached analyses in flight (calibrated: ADR-012 "Calibration (§13)") |
| `ATUS_API_MAX_INFLIGHT_HEAVY` | `1` | `1` | of which heavy: 8 or more year queries, `len(years)` doubled for a compare (calibrated) |
| `ATUS_API_ADMISSION_WAIT_S` | `5` | `5` | wait for a lane, then 503 `overloaded` (calibrated) |
| `ATUS_API_RATE_ANALYSIS` | `6/60` | `10/60` | per-client-IP token bucket for uncached analyses: `<tokens>/<seconds>` |
| `ATUS_API_RATE_REQUESTS` | `120/60` | `120/60` | the same, for every request except `/health` (an analysis spends from both buckets) |
| `ATUS_API_DAILY_ANALYSIS_CAP` | `600` | `600` | in-process backstop, resets 00:00 UTC; the Worker's governor is the primary cap |
| `ATUS_API_MAX_BODY_BYTES` | `65536` | `65536` | larger bodies get 413 `payload_too_large` |
| `ATUS_API_THREADS` | `16` | `16` | anyio worker-thread limit |
| `ATUS_API_SNAPSHOT_TTL_S` | `2` → `600` | `600` | refresh interval of the in-process `data_version`, years and lexicon. Not applied while `ATUS_EXPECTED_DATA_VERSION` pins the snapshot; a failed refresh keeps serving the previous snapshot (ADR-012) |
| `ATUS_API_PREPARE_THRESHOLD` | `5` | `none` | psycopg's automatic server-side prepare threshold; `none` or `off` disables prepared statements. Needed only behind a transaction pooler without prepared-statement support (production uses Neon's direct endpoint) |

### Pipeline and release tooling (your machine only)

Never set on Render. The data commands (`atus load`, `atus build-serving-db`, `atus restore-serving-db`, ...) read these from the environment or from `.env` on your machine; the secrets among them live in one file per Neon project, `~/.config/atus/neon-<release>-<build8>.env`, passed with `--secrets-file` (the restore's default is `~/.config/atus/neon.env`).

| Variable | Read by | Default | Example | Meaning |
|---|---|---|---|---|
| `ATUS_DATABASE_URL` (full DB) | `atus load`, `validate-*`, the source of `build-serving-db`; also the database `verify-serving-db` checks unless `--database-url` names one, so point it at the serving database for that command ([operations.md](operations.md#serving-db-build-and-restore)) | `postgresql://atus:atus@localhost:5434/atus` | — | the full local database; the serving build only reads it |
| `ATUS_DATA_DIR` | the pipeline | `data` | — | raw and staged BLS files |
| `ATUS_RELEASE` | the pipeline | `0325` | `0326` | the BLS multi-year release code (ADR-013 "New ATUS year") |
| `ATUS_MIGRATIONS_DIR` | `atus migrate`/`load`, `build-serving-db` | the checkout's `migrations/` | `/workspace/migrations` | where the migration files are when not running from a checkout (the image ships none, F56) |
| `ATUS_SERVING_DATABASE_URL` | `build-serving-db --target`, `compare-serving-db --serving` | unset | `postgresql://neondb_owner:...@localhost:5465/atus` | the new, empty database the serving build writes, and the serving side of the randomized diff |
| `ATUS_OWNER_DATABASE_URL` | `restore-serving-db` (`scripts/ops/restore_serving_db.sh`) | unset: read from the `--secrets-file` (default `~/.config/atus/neon.env`) | `postgresql://neondb_owner:<password>@ep-<id>.us-east-1.aws.neon.tech:5432/atus?sslmode=verify-full&sslrootcert=system` | **secret**, the owner's direct DSN (never the `-pooler` host). A bare `postgresql://` line in the secrets file counts as this key. When it is exported and the `--secrets-file` also holds one, both must name the same database (user, host, port, database), or the restore refuses before changing anything: the API role's credentials are written to that file. In `deploy.yml`'s snapshot job: a throwaway owner on the runner's `postgres:16` |
| `ATUS_API_DB_PASSWORD` | written by `restore-serving-db` into the project's secrets file | — | 192 random bits, URL-safe | **secret**, the generated `atus_api` password; reused by later restores unless `--rotate-api-password` |
| `ATUS_API_DATABASE_URL` | written by `restore-serving-db` into the project's secrets file | — | `postgresql://atus_api:<password>@ep-<id>...:5432/atus?sslmode=verify-full&sslrootcert=system` | **secret**, the DSN to copy into Render's `ATUS_DATABASE_URL` |
| `ATUS_API_ROLE_PASSWORD` | `scripts/ops/roles.sql` (psql `\getenv`) | — | a SCRAM-SHA-256 verifier | set by the restore only while psql applies roles.sql, so the plaintext never reaches the server; the local stack passes its own throwaway password |
| `ATUS_PG_CLIENT_IMAGE` | `pg_dump`/`pg_restore`/`psql` wrappers | for PostgreSQL 16, `postgres:16` pinned by digest (`serving/pgtools.py`, the digest CI uses); other majors have no default | `postgres:16@sha256:<digest>` | client image used through Docker when no local client is new enough; it receives the connection's password, so name a digest, not a tag |
| `ATUS_BIN` | `scripts/ops/restore_serving_db.sh` | `.venv/bin/atus`, then `atus` on PATH | `/opt/atus/bin/atus` | the `atus` executable the wrapper runs |
| `ATUS_EDGE_ENV_FILE` | `scripts/ops/gen_edge_secrets.sh` | `~/.config/atus/edge.env` | — | where the generated secrets go (`--file` overrides it) |
| `EDGE_AUTH_SECRET` (environment) | `scripts/build_snapshot.py` | unset: `--edge-secret-file` | — | **secret**, sent to the local snapshot API as `X-ATUS-Edge-Auth` |

### Image (build arguments and baked environment)

| Name | Default | Example | When it changes |
|---|---|---|---|
| `ATUS_BUILD_SHA` (build arg → env) | `dev` | `$(git rev-parse HEAD)` | every build. `/health` reports it as `build`; `deploy.yml` waits until it equals `GITHUB_SHA` |
| `ATUS_VERSION` (build arg → OCI label) | `unknown` | `0.1.0` (`atus_pipeline.__version__`) | every release |
| `PYTHON_BASE`, `PYTHON_DIGEST` (build args) | `python:3.12-slim`, `sha256:a6e34c59…` | — | base-image updates. Both `FROM` lines are literal (`python:3.12-slim@sha256:…`), so Dependabot's `docker` ecosystem proposes digest bumps; these two ARGs only feed the OCI labels and must be updated with them (`tests/deploy/test_dependabot.py` fails otherwise) |
| `SSL_CERT_FILE` (env) | `/etc/ssl/certs/ca-certificates.crt` | — | never: `sslrootcert=system` depends on it ([TLS](#tls-to-the-database)). Outside the image the `atus` commands set it to the system bundle when unset |
| `ATUS_ENV`, `PORT`, `WEB_CONCURRENCY` (env) | `production`, `10000`, `1` | — | never; see the API table |

### Cloudflare Worker

`edge/wrangler.jsonc` is authoritative for these names: Worker `atus`, KV binding `RESULT_CACHE`, Durable Object binding `GOVERNOR` with class `Governor` (migration tag `v1`). The local stack's `edge` service and `.env.production.example` use the same names.

| Secret | Set in (your copy) | If unset | Example | Rotation |
|---|---|---|---|---|
| `EDGE_AUTH_SECRET` | Worker secret (`~/.config/atus/edge.env`) | required | one value of the API's `ATUS_EDGE_SECRETS` | [Rotation](#rotation), with the API |
| `OPS_TOKEN` | Worker secret (`edge.env`; GitHub secret `EDGE_OPS_TOKEN`) | `/api/_ops/budget` answers 404 | 32 random bytes, hex (64 characters) | [Rotation](#rotation) |
| `IP_HMAC_KEY` | Worker secret (`edge.env`) | required | 32 random bytes, hex (64 characters) | `wrangler secret put IP_HMAC_KEY` ([Rotation](#rotation), item 4). It keys the client-prefix HMACs, names the governor's Durable Object instance and tags every KV entry, so a rotation starts a fresh governor instance and orphans every KV entry |
| `RENDER_ORIGIN` | Worker secret (`edge.env`; GitHub secret `RENDER_ORIGIN`) | required | `https://atus-api-k3x9q2.onrender.com` | only when the API's origin URL changes; update both places. Never published. It is the only setting that selects the origin: no Worker var does |
| `NEON_API_KEY` | Worker secret, optional; not kept: re-create it instead | the governor runs in its conservative mode | a Neon **project-scoped** key | revoke and re-create in Neon, then `wrangler secret put NEON_API_KEY`; a new key for each release project |
| `API_PAUSED_UNTIL`, `API_PAUSED_REASON` | Worker secrets, optional, not secret values: set and cleared only by `deploy.yml` operations `pause-api` / `resume-api` | the pause switch is off | `2026-12-01` (UTC; or `2026-12-01T06:00Z`, or `unknown`), `render_quota` (or `render_suspended`, `maintenance`) | when the API is known to be paused: [operations.md "Pausing the API"](operations.md#pausing-the-api). Secrets only so they change without a rebuild and survive deploys |

| Var | Set by | Default | Example | When it changes |
|---|---|---|---|---|
| `API_VERSION` | `edge/wrangler.jsonc` | `v1` | `v1` | only with a new API major version; part of the cache scope |
| `DATA_VERSION` | `deploy.yml` | — | equals `ATUS_EXPECTED_DATA_VERSION` | each data release; gates KV stores and the cache scope |
| `ANALYTICS_VERSION` | `deploy.yml` (from `/api/v1/meta`) | — | `0.2` | when the engine version changes |
| `RESPONSE_FORMAT` | `deploy.yml` | — | `1` | only with a new response format (the API's `RESPONSE_FORMAT`) |
| `SNAPSHOT_ID` | `deploy.yml` | — | `<api_build12>-<sha256(DATA_VERSION)[:16]>` | each deploy. The Worker answers metadata GETs from `/snap/<SNAPSHOT_ID>/get/` only while its suffix matches `DATA_VERSION`; otherwise they go to Render as governed lookups |
| `EPOCH` | `edge/wrangler.jsonc` | `1` | `2` | manually: edit it there, commit to `main` and deploy (`deploy.yml`). Bumping it orphans every KV entry, a full edge-cache flush. Not the purge after a Cloudflare compromise: its next value is public, so entries for it can be written in advance; a rotated `IP_HMAC_KEY` invalidates every entry instead ([Rotation](#rotation), item 5). No test changes with it ([what CI asserts](#6-first-worker-deploy-owner)) |
| `PREWAKE` | `edge/wrangler.jsonc` | `on` | `off` | manually, the same way, to stop `GET /api/v1/wake` from waking Render |
| `NEON_PROJECT_ID` | `edge/wrangler.jsonc`, optional | unset | `<neon-project-id>` | each release project, with `NEON_API_KEY` |

These are all the vars the Worker reads (`edge/src/`). None selects the API origin: that is the secret `RENDER_ORIGIN` alone. The Vercel fallback (ADR-011 "Fallback") is a documented manual procedure whose preconditions, its Worker settings included, are not implemented (ADR-011 amendment).

Secrets are generated into `~/.config/atus/edge.env` by `scripts/ops/gen_edge_secrets.sh` and loaded with `npx --no-install wrangler secret put <NAME>`, piped from that file (owner step 1.3); the script prints each command. A brand-new Worker's first `wrangler deploy` must already carry the required secrets (`secrets.required` in `wrangler.jsonc`, so `RENDER_ORIGIN` must be known), and `--secrets-file` uploads every line of the file it reads: give it the Worker's names only, as the script prints (`grep -E '^(EDGE_AUTH_SECRET|OPS_TOKEN|IP_HMAC_KEY|RENDER_ORIGIN)=' ~/.config/atus/edge.env` into a private temporary file, which `--secrets-file` reads and the command then removes; not a process substitution, which fails on macOS), never the whole file, which also holds the API's `ATUS_OPS_TOKEN` and any `EDGE_AUTH_SECRET_PREVIOUS` or `NEON_API_KEY`.

### GitHub (repository variable and environments)

| Name | Kind and where | Your copy | Example | Rotation |
|---|---|---|---|---|
| `PUBLIC_BASE_URL` | variable of the environment `production` (the deploy job), and the **repository** variable (the monitors), set only after step 8 | — | `https://atus.<account-subdomain>.workers.dev` | never (every share link contains it); keep both copies equal |
| `CLOUDFLARE_API_TOKEN` | secret, `production` | none (Cloudflare shows it once) | Custom token: Workers Editor on the one Worker, this account only, 90-day TTL | [Rotation](#rotation), before it expires |
| `CLOUDFLARE_ACCOUNT_ID` | secret, `production` (not sensitive) | — | 32 hex characters | never |
| `RENDER_DEPLOY_HOOK_URL` | secret, `production` | none | `https://api.render.com/deploy/srv-<id>?key=<key>` | [Rotation](#rotation) |
| `RENDER_ORIGIN` | secret, `production` | `edge.env` | as the Worker secret | with the API host |
| `EDGE_OPS_TOKEN` | secret, `ops`; optionally also `production` | `edge.env` | equals the Worker's `OPS_TOKEN` | with the Worker's token |
| `CLOUDFLARE_ANALYTICS_TOKEN` | secret, `ops`, optional (owner step 4.3) | none (Cloudflare shows it once) | Custom token: Account Analytics Read on this account only, 90-day TTL; never the deploy token | [Rotation](#rotation), item 8, before it expires: once expired, the daily quota report fails every day |
| `CLOUDFLARE_ANALYTICS_ACCOUNT` | secret, `ops`, optional, with the token (not sensitive) | — | the Account ID, as `CLOUDFLARE_ACCOUNT_ID` | never |
| `ATUS_OPS_TOKEN` | **not stored in GitHub** | `edge.env` | — | no workflow calls the origin's `/ready` or `/metrics` |
| `GHCR_IMAGE` | **not a setting** | — | — | the workflows name `ghcr.io/nathan98000/atus-api` themselves (Render requires that repository anyway) |

**Which workflow uses which name.**

- **`deploy.yml`, job `deploy` (environment `production`):** `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` (wrangler), `RENDER_DEPLOY_HOOK_URL` (the deploy), `RENDER_ORIGIN` (the build wait and the origin-lock check), `PUBLIC_BASE_URL` (the smoke; `production`'s copy) and, when set, `EDGE_OPS_TOKEN` (the smoke's budget read). No other job of any workflow can read them, except `api-switch` below; `tests/deploy/test_workflows.py` asserts it.
- **`deploy.yml`, job `api-switch` (environment `production`; operations `pause-api` and `resume-api` only):** `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` (`wrangler secret bulk`) and `PUBLIC_BASE_URL` (its check of `/api/_edge/health`). No Render secret.
- **`edge-check.yml`, `deep-check.yml`:** `PUBLIC_BASE_URL` (the repository variable) only. The edge check calls `/api/_edge/health`; the deep check calls `/api/health`, and reads the `live` tag's commit with the run's own `GITHUB_TOKEN` (`packages: read`).
- **`quota-report.yml` (environment `ops`):** `PUBLIC_BASE_URL` and `EDGE_OPS_TOKEN`, for `/api/_ops/budget`; optionally `CLOUDFLARE_ANALYTICS_TOKEN` and `CLOUDFLARE_ANALYTICS_ACCOUNT`, for the account's Worker requests today (Cloudflare's GraphQL Analytics API; without them that line is a SKIP).
- **`schedules-check.yml` and `deploy.yml`'s `preflight`:** only the run's own `GITHUB_TOKEN`, with `actions: read`.
- **`ghcr-cleanup.yml` and `deploy.yml`'s `promote`:** only the run's own `GITHUB_TOKEN`, with `packages: write`.

### Owner files (`~/.config/atus/`, `chmod 600`)

| File | Holds | Written by | Feeds |
|---|---|---|---|
| `edge.env` | `EDGE_AUTH_SECRET` (and, once it has been rotated, `EDGE_AUTH_SECRET_PREVIOUS` with the rotation's state `EDGE_AUTH_SECRET_ROTATION`, `pending` or `finished`), `OPS_TOKEN`, `IP_HMAC_KEY`, the API's `ATUS_OPS_TOKEN`, then `RENDER_ORIGIN`; optionally `NEON_API_KEY`, which you add and the script keeps | `scripts/ops/gen_edge_secrets.sh`: 256-bit hex values, re-runs keep them, `--rotate NAME` replaces one (not `EDGE_AUTH_SECRET` while its rotation is pending), `--finish-rotation` ends that rotation, `--render-origin URL` records the origin | `wrangler secret put` (piped) for the Worker's names only; `ATUS_OPS_TOKEN` goes to Render env only; `OPS_TOKEN` also to the GitHub secret `EDGE_OPS_TOKEN` (`ops`, optionally `production`) |
| `neon-<release>-<build8>.env` | one file per Neon project, named like the project: `ATUS_OWNER_DATABASE_URL`, the `neondb_owner` direct DSN (pasted from Neon → Connect, pooling off; a bare `postgresql://` line also counts), then `ATUS_API_DB_PASSWORD` and `ATUS_API_DATABASE_URL` | you (the owner DSN) and `scripts/ops/restore_serving_db.sh --secrets-file <it>` (the two `ATUS_API_*` lines) | restores, the API password rotation, Render's `ATUS_DATABASE_URL` (= `ATUS_API_DATABASE_URL`). Keep the previous project's file until that project's deletion is approved: a data rollback needs its `atus_api` DSN |

### Local stack (`deploy/stack/stack.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `ATUS_STACK_PG_SUPERUSER_PASSWORD`, `ATUS_STACK_OWNER_PASSWORD`, `ATUS_STACK_API_DB_PASSWORD` | required | passwords of `postgres` (bootstrap only), `neondb_owner` and `atus_api`; URL-safe |
| `ATUS_STACK_EDGE_SECRET`, `ATUS_STACK_OPS_TOKEN` | required | the API's `ATUS_EDGE_SECRETS` / `ATUS_OPS_TOKEN` (and the Worker's `EDGE_AUTH_SECRET`); the smoke probe gets both. At least 32 characters: the stack's API runs in production mode, which refuses shorter ones |
| `ATUS_STACK_EDGE_OPS_TOKEN`, `ATUS_STACK_IP_HMAC_KEY` | empty | the Worker's `OPS_TOKEN` / `IP_HMAC_KEY` (profile `edge`) |
| `ATUS_STACK_API_PAUSED_UNTIL`, `ATUS_STACK_API_PAUSED_REASON` | empty | the Worker's pause switch `API_PAUSED_UNTIL` / `API_PAUSED_REASON` (profile `edge`; `edge/src/pause.ts`); empty = off. Lets the local stack show the `api_paused` page |
| `ATUS_STACK_DUMP` | unset | serving dump for profile `dump` |
| `ATUS_EXPECTED_DATA_VERSION` | empty | passed to the API (and the Worker's `DATA_VERSION`); `smoke_stack.sh --edge` sets the seed's version |
| `ATUS_STACK_ASSETS_DIR` | `frontend/dist` | the edge's static assets: an SPA build plus `snap/` (set by `smoke_stack.sh --edge`) |
| `ATUS_STACK_SNAPSHOT_ID`, `ATUS_STACK_ANALYTICS_VERSION` | empty | the Worker vars `SNAPSHOT_ID` and `ANALYTICS_VERSION` (set by `smoke_stack.sh --edge`; empty turns the edge cache off) |
| `ATUS_STACK_IMAGE`, `ATUS_STACK_PULL_POLICY` | `atus-api:local`, `build` | run a registry image by digest instead (`missing` + `--no-build`) |
| `ATUS_BUILD_SHA`, `ATUS_VERSION` | `dev`, `unknown` | image build arguments |
| `ATUS_STACK_DB_PORT`, `ATUS_STACK_PGBOUNCER_PORT`, `ATUS_STACK_EDGE_PORT` | `5470`, `5471`, `8541` | loopback host ports; empty = a random free port |
| `ATUS_STACK_API_DB_HOST`, `ATUS_STACK_API_DB_HOST_PORT` | `db`, `5432` | route the API through PgBouncer (`pgbouncer`, `6432`) |
| `ATUS_STACK_DB_SHARED_BUFFERS`, `ATUS_STACK_DB_MAX_CONNECTIONS`, `ATUS_STACK_DB_MAX_WORKER_PROCESSES` | `215MB`, `839`, `17` | Neon settings for another compute size (`deploy/emulation/neon_settings.py`) |
| `ATUS_EMU_DB_CPUS`, `ATUS_EMU_DB_MEMORY` | `0.25`, `1g` | emulated database size |
| `PGBOUNCER_MAX_PREPARED_STATEMENTS`, `PGBOUNCER_DEFAULT_POOL_SIZE` | `1000`, `755` | PgBouncer (Neon's pooler values) |
| `ATUS_SEED_FORCE` | `0` | `1` reloads the fixture even when data is present |

### Tests and CI tooling

| Variable | Default | Example | Meaning |
|---|---|---|---|
| `ATUS_TEST_DATABASE_URL` | `postgresql://atus:atus@localhost:5434/atus_test` | `…/atus_test_wsc` | disposable database of the integration suite |
| `ATUS_E2E_DATABASE_URL` | `postgresql://atus:atus@localhost:5434/atus_e2e` | `…/atus_e2e_wsc` | disposable database of `scripts/e2e_backend.py`. Only a local `atus_e2e[_<suffix>]` is accepted (F60) |
| `ATUS_DATA_QUALITY` | unset | `1` | opt in to the full-DB data-quality suite |
| `ATUS_DOCKER_TESTS` | unset | `1` | opt in to the image, gate and stack tests in `tests/deploy` |
| `ATUS_TEST_IMAGE` | unset: build `atus-api:test` | `ghcr.io/nathan98000/atus-api@sha256:<digest>` | test an existing image; `test_stack_smoke.py` also runs the stack on it (`--no-build`) |
| `ATUS_DOCKER_TEST_PREFIX` | `atus-pytest` | `atus-ci-${GITHUB_RUN_ID}` | prefix of every container, network and compose project the tests create |
| `ATUS_SMOKE_ARGS` | empty | `--emulation` | extra `scripts/smoke_stack.sh` options for `test_stack_smoke.py` |
| `ATUS_SMOKE_EDGE_SECRET`, `ATUS_SMOKE_OPS_TOKEN` | set by the `smoke` service | — | the probe's credentials |
| `ATUS_PROBE_EDGE_OPS_TOKEN`, `ATUS_PROBE_DATA_VERSION` | set by `smoke_stack.sh --edge` | — | `deploy/stack/edge_probe.py`'s Worker ops bearer and expected data version |
| `E2E_BASE_URL` | unset: vite-preview mode | `http://127.0.0.1:8541` | Playwright stack mode: the suite runs through this edge and starts no server |
| `E2E_PROJECTS` | `desktop mobile webkit` | `desktop mobile firefox webkit` (CI) | the Playwright projects `smoke_stack.sh --edge --e2e` runs |
| `ATUS_EDGE_RELAY_LISTEN`, `ATUS_EDGE_RELAY_TARGET` | `127.0.0.1:10000`, set by compose to `api:10000` | — | the edge container's loopback relay to the API (`deploy/edge/origin-relay.mjs`) |
| `E2E_BACKEND_PYTHON` | `../.venv/bin/python` | `python` | interpreter Playwright uses for the E2E backend |
| `PUBLIC_BASE_URL` | unset (required) | `http://127.0.0.1:8541` | the site `frontend/playwright.deployed.config.ts` and `scripts/ops/monitors.py` check: the deployed origin, or the local stack's edge |
| `TRIVY_VERSION`, `TRIVY_SHA256` | `image.yml` | `0.75.0`, `c6e65abd…` | the pinned scanner (an immutable release); bump both together after checking the release's checksums |
| BuildKit image (`driver-opts` of `docker/setup-buildx-action`) | `image.yml` | `moby/buildkit:v0.33.1@sha256:cec9f139…` | the daemon that builds the image, pinned by digest (review round 3); Dependabot does not update it: bump the tag and digest together to a release at least two weeks old (`docker buildx imagetools inspect moby/buildkit:vX.Y.Z` prints the digest) |
| `ACTIONLINT_VERSION`, `ACTIONLINT_SHA256` | `ci.yml` | `1.7.12`, `8aca8db9…` | the pinned workflow linter; locally: `uvx --from actionlint-py actionlint` |

### Rotation

Every secret has one procedure.

1. **Edge secret.** This one is designed so that no request fails.
   1. `scripts/ops/gen_edge_secrets.sh --rotate EDGE_AUTH_SECRET` writes the new value into `edge.env` and keeps the old one as `EDGE_AUTH_SECRET_PREVIOUS`: the value the Worker sends until step 3. It records the rotation as pending, and refuses another `--rotate EDGE_AUTH_SECRET` until step 5: a second rotation would replace that value with one that was never deployed, and step 2 would then lock the Worker out (403 `forbidden_origin` on every proxied request; review round 3). If you lost track of where you are, Render's current `ATUS_EDGE_SECRETS` shows which values the API accepts.
   2. Render `ATUS_EDGE_SECRETS=<old>,<new>` (`<old>` is `EDGE_AUTH_SECRET_PREVIOUS`, `<new>` is `EDGE_AUTH_SECRET`), then "Save and deploy". Wait until Render → Events shows the deploy as live.
   3. `npx --no-install wrangler secret put EDGE_AUTH_SECRET` with `<new>`.
   4. Render `ATUS_EDGE_SECRETS=<new>`, then "Save and deploy".
   5. `scripts/ops/gen_edge_secrets.sh --finish-rotation` records that the Worker and the API use only `<new>`. `EDGE_AUTH_SECRET_PREVIOUS` stays in `edge.env` for an edge rollback to a version from before the rotation ("Rollback", Edge, step 2).
2. **`atus_api` database password.**
   1. `scripts/ops/restore_serving_db.sh --artifact <the release dump> --secrets-file <that project's file> --roles-only --rotate-api-password` re-applies `scripts/ops/roles.sql` as `neondb_owner` with a new generated 192-bit password (sent as a SCRAM verifier) and rewrites `ATUS_API_DB_PASSWORD` and `ATUS_API_DATABASE_URL` in that file.
   2. Update Render's `ATUS_DATABASE_URL`, then "Save and deploy". Do this even if step 1 ended with `Restore FAILED.` after the note "the API role's password was changed before the checks ran", or with an error after the note "the API role's password was changed before the restore stopped with an error": the new password is already in effect.

   A Postgres role has only one password. Step 1 first locks the role out (`NOLOGIN`) and ends the role's open sessions, the API's included, since a new password ends none and a session left open could set the role's password or settings again (the role may change its own); only then does `roles.sql` set the new password and `LOGIN` again, and it resets every setting of the role (the role can set its own, so a leaked password could have left some) (review round 3). It also removes the large objects the role stored, which its non-superuser owner could not unlink otherwise ([security.md R16](security.md#accepted-risks); review round 4). Its checks fail if a setting beyond roles.sql's five, or any large object, remains. If a session could not be ended, step 1 stops before `roles.sql`, with the password and the file unchanged and the role locked out: restart the compute (Neon: the branch → Postgres database → Computes → the compute's menu → Restart compute, which ends every connection), then run step 1 again. Between steps 1 and 2 the API cannot connect, so uncached analyses get 503 `database_unavailable` with `Retry-After`. Rotate when the service is idle.

   If the password may have leaked, then also bump `EPOCH` in `edge/wrangler.jsonc` and deploy once step 2 is live (a full flush of the edge cache, KV). The holder could change the role's settings, output precision (`extra_float_digits`) among them, and so the values analyses returned, without changing a row. Nothing above changes the Worker's cache scope, so every result KV stored meanwhile would otherwise keep being served (`X-Cache: edge`) for up to 30 days. Step 2's "Save and deploy" restarts the API, which empties the API's in-process cache, and the static snapshot is built in CI from the release dump, not from this database (review round 4).
3. **Ops tokens** (`ATUS_OPS_TOKEN`; the Worker's `OPS_TOKEN`).
   1. `scripts/ops/gen_edge_secrets.sh --rotate ATUS_OPS_TOKEN` (or `--rotate OPS_TOKEN`) writes the new value into `edge.env`.
   2. Set it on Render, or with `wrangler secret put OPS_TOKEN`.
   3. For `OPS_TOKEN`, update the GitHub secret `EDGE_OPS_TOKEN` (`ops`, and `production` if you set it there). `ATUS_OPS_TOKEN` has no GitHub copy.

   Until both match, only the ops checks see 404. Visitors are unaffected.
4. **`IP_HMAC_KEY`.** `npx --no-install wrangler secret put IP_HMAC_KEY` at any time, best soon after 00:00 UTC. Besides the client-prefix HMACs, the key names the governor's Durable Object instance and tags every KV entry (ADR-015), so after a rotation the Worker uses a fresh governor instance, and every KV entry is orphaned and computed again on its next request. The fresh instance starts the day's counts and compute charges at zero, so on the day of a rotation the governor can admit up to its daily budgets again; rotating early in the UTC day keeps that small. With a Neon key, ground truth returns at its next poll.
5. **Cloudflare API token** (90-day TTL).
   1. Before it expires, create a new Custom token with the same scope.
   2. Check it as in owner step 1.5: from `edge/`, `read -rs CLOUDFLARE_API_TOKEN && export CLOUDFLARE_API_TOKEN` (paste it; nothing is shown), `test -n "$CLOUDFLARE_API_TOKEN" && CLOUDFLARE_ACCOUNT_ID=<Account ID> npx --no-install wrangler deployments status` (the `test -n` stops an empty paste, which would fall back to your OAuth login and prove nothing), then `unset CLOUDFLARE_API_TOKEN`. In the dashboard's token list, it must show the same permissions as the old token: the check proves that it authenticates, not that it may deploy.
   3. Replace the GitHub secret `CLOUDFLARE_API_TOKEN`.
   4. Delete the old token in Cloudflare.
   5. **If it may have leaked**, or the Cloudflare account was compromised, assume a Worker version deployed with it ran code of its own. Such a version can read every Worker secret from `env` and send it anywhere, set any secret (secrets belong to a version, and `wrangler deploy` keeps the latest version's), write KV entries and governor state that outlive it, serve any file with any caching headers, and be rolled back to hide. It keeps running, with the Worker's KV and Durable Object bindings, until your own code replaces it. After steps 1–4, in this order (review round 4):
      1. Look for versions you did not deploy: `npx --no-install wrangler deployments list` in `edge/`.
      2. Replace them with your own code from a new commit: set `ASSET_SALT` in `frontend/src/deploy/assetNames.ts` to the output of `openssl rand -hex 8`, push it to `main` and deploy with `deploy.yml`. Not `wrangler rollback`, which leaves the attacker's version the latest one, so that wrangler then refuses to change a secret ("Rollback", Edge, step 2), and not a re-run of an earlier deploy, which reuses its file names and snapshot. If `wrangler deploy` refuses because a required secret is missing (that version may have deleted one), first `npx --no-install wrangler secret put` a throwaway value for it: that command deploys the attacker's code again, now with that value, so never put a value you keep; step 3 replaces it. The salt changes every `/assets/` file name and the new commit gives a new `SNAPSHOT_ID`, so the site asks for no bundle or snapshot file under a name a browser may hold an immutable copy of from the attacker's version. What it still names unchanged (the page, `/boot.css`, `/favicon.svg`) is in [security.md R17](security.md#accepted-risks).
      3. Straight after, rotate every Worker secret: `IP_HMAC_KEY` first (item 4), then `OPS_TOKEN` (item 3), `EDGE_AUTH_SECRET` (item 1) and, if set, `NEON_API_KEY`, which calls for a restore into a new project (item 7); and put `RENDER_ORIGIN` again from `edge.env`, since step 2's deploy kept whatever values the attacker's version set. The new `IP_HMAC_KEY` is what discards the state that version may have left: every KV entry carries an HMAC tag under a key derived from it, so no entry that version wrote verifies any more, under any scope; and it names the governor's Durable Object instance, so the Worker moves to a fresh instance whose name that code could not compute (ADR-015). Until this step the Worker may still serve such entries and use such state, so do steps 2 and 3 back to back.
      4. Treat `RENDER_ORIGIN` as known: it is not a credential, and direct requests still need the edge secret ([security.md R3](security.md#accepted-risks)).
      5. Set the pause switch (`API_PAUSED_UNTIL`, `API_PAUSED_REASON`) again with `deploy.yml` operation `pause-api` or `resume-api`: it holds no secret values, but the token can set or clear it.
      6. Never roll back to a version from before step 3 (wrangler lists `IP_HMAC_KEY` among the secrets that have changed: decline). It would bring back the old key, and with it what the attacker's code wrote. Do not rely on an `EPOCH` bump instead: its next value is public, so entries for it could be written in advance, and it leaves the governor as it is.

      Visitors who loaded the site while the attacker's version was live may keep its files (or a service worker it registered) and run them until their browser's cache or the site's data is cleared; nothing the recovered site sends is sure to reach such a browser ([security.md R17](security.md#accepted-risks)).
6. **Render deploy hook.** Render → Settings → Deploy Hook → Regenerate. The old URL stops working at once. Then replace `RENDER_DEPLOY_HOOK_URL`.
7. **Neon owner password and project key.** Reset the password in the Neon console and update that project's `neon-<release>-<build8>.env`. Revoke and re-create `NEON_API_KEY`.

   If `NEON_API_KEY` (editor rights on the project, R6) or that owner DSN may have leaked, or the Cloudflare deploy token while `NEON_API_KEY` was set (item 5), the served statistics may have been changed, and possibly without changing any row: the owner can add a row-level security policy that filters what `atus_api` reads (the owner bypasses it), or a database setting such as `search_path` with a function of the same name as one the engine calls. Restore the sha256-verified release dump into a new project ([operations.md, "Restore after a lost project"](operations.md#serving-db-build-and-restore)) and switch the API to it; that also leaves the leaked credentials behind with the old project. Then ask to delete the old project. Do not rely on `atus verify-serving-db --content-hash` against the old project instead: it re-hashes the served rows, as the role that connects, so it passes on such a database, and resetting the passwords or re-running `roles.sql` removes none of these changes (review round 3).

   Once the API runs on the new project, bump `EPOCH` in `edge/wrangler.jsonc` and deploy (a full flush of the edge cache, KV). The restore keeps the release's `data_version`, so nothing else changes the Worker's cache scope, and every result KV stored from the old project would otherwise keep being served (`X-Cache: edge`) for up to 30 days. Render's "Save and deploy" restarts the API, which empties the API's in-process cache, and the static snapshot is built in CI from the release dump, not from the old project (review round 4).
8. **Cloudflare analytics token** (optional, `ops` secret `CLOUDFLARE_ANALYTICS_TOKEN`, 90-day TTL). Before it expires, create a new Custom token with Account → Account Analytics → Read on this account only (owner step 4.3), replace the secret (`gh secret set CLOUDFLARE_ANALYTICS_TOKEN --env ops`, paste at the prompt), and delete the old token. Once it has expired, the daily quota report fails every day on its "Worker requests today" line. `CLOUDFLARE_ANALYTICS_ACCOUNT` is the Account ID and never changes.

**If a secret leaks:** rotate it, and every secret it exposes ([security.md, "Leaked tokens"](security.md#10-leaked-tokens): the Cloudflare deploy token exposes the Worker's secrets, item 5, whose recovery rotates `IP_HMAC_KEY` first; a Neon key or owner DSN calls for a restore into a new project, item 7; that restore and the `atus_api` password, item 2, also call for an `EPOCH` bump), with the procedures above straight away. Then check the provider logs for use: Render logs, Workers Logs and Neon. GitHub secret scanning and push protection stay on (owner step 4.4).

### TLS to the database

- **Connection string:** `sslmode=verify-full&sslrootcert=system`. Never `require` alone.
- **libpq version:** `sslrootcert=system` needs libpq ≥ 16, and SNI needs ≥ 14. The image's bundled libpq is 18.6.
- **Why the image sets `SSL_CERT_FILE`.** The libpq inside the `psycopg[binary]` wheel uses the wheel's own OpenSSL 3.5. That OpenSSL's compiled-in certificate directory is a build-machine path (`OPENSSLDIR: "/host/tmp/libpq.build"`), so on its own `system` finds no roots at all. The image therefore sets `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`, the Debian CA bundle.
- **How it is tested:** `tests/deploy/test_container_image.py` (opt-in, `ATUS_DOCKER_TESTS=1`) uses a throwaway CA and a TLS-only Postgres to prove all three cases:
  - the image connects with `verify-full` + `system`;
  - it rejects a certificate whose CA is not in the bundle;
  - with `SSL_CERT_FILE` unset it fails even when the CA is in the bundle.
- **Outside the image (the owner's machine).** The same wheel has the same build path on macOS (`/tmp/libpq.build/cert.pem`, missing), and the `postgres:16` image the restore falls back to has no CA bundle at all (`ca-certificates` is not installed). So the `atus` commands point `SSL_CERT_FILE` at the system bundle when it is unset (`src/atus_pipeline/database/dsn.py::ensure_system_roots`), and `serving/pgtools.py` mounts that bundle read-only into the container and sets `SSL_CERT_FILE` there; an `sslrootcert=<file>` is mounted the same way. `tests/deploy/test_owner_tls.py` (opt-in) proves, against a TLS-only Postgres, that the host driver and the docker `psql` both connect with `verify-full` + `system` once the bundle holds the CA, that the docker client still rejects a CA the bundle lacks, and that without any bundle the docker fallback refuses with advice instead of connecting.
- **Not yet verified:** the live Neon certificate. It is checked at the first restore (step 5).
- **Fallback** if `system` ever fails: `sslrootcert=<bundled CA file>` with `verify-full` (ADR-011).

## Rollback and runbooks

The general runbooks live in [`operations.md`](operations.md#runbooks): deploy, rollback, a deploy blocked by the image scan or a dependency audit, serving-DB build and restore, data release and new ATUS year, restore drill, incident first steps, token rotation, quota exhausted, and provider free-tier changes. The procedures below are the ones the workflows implement.

### Rollback

Every deploy run's summary records what ran before it and what it deployed, whatever the outcome.

- **API.** Export `RENDER_DEPLOY_HOOK_URL` from your password manager into your shell (never paste it into chat), then:
  1. `python scripts/ops/render_deploy.py trigger --image-ref ghcr.io/nathan98000/atus-api@sha256:<previous>`;
  2. `RENDER_ORIGIN=... python scripts/ops/render_deploy.py wait --expect-build <previous build>`;
  3. move the GHCR tag `live` back to that digest: within 30 days of the Deploy run that deployed it, Actions → that run → job `promote` → Re-run (GitHub re-runs a job only "up to 30 days after its initial run", docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs, read 2026-10-21); later, or while another Deploy run is in progress or waiting for approval (the re-run would wait behind it in the deploys' concurrency group (`deploy-production`), and a later deploy dispatch would cancel it), by hand with `docker buildx imagetools create --prefer-index=false`, or after rejecting that pending run ([operations.md, Rollback](operations.md#rollback) has the commands). Until then `deep-check.yml` reports the build mismatch, and a Render deploy not made through the hook would bring back the image you rolled away from. After a deploy whose smoke failed, `promote` never ran, so `live` still names the previous image and step 3 has nothing to move; roll the API back with the edge, or keep the new API and tag its digest `live` and `deployed-<commit>` ([operations.md, Deploy](operations.md#deploy) step 6).

  The previous image in the summary is the digest that commit was last deployed as (its `deployed-<commit>` tag, which only `promote` moves), so a re-run of the running commit, whose push moves `sha-<commit>`, still names the right one. GHCR keeps the newest 20 digests and the one tagged `live`. Render's dashboard can also roll back to a previous deploy.
- **Edge (SPA, snapshot, Worker and its vars):**
  1. `cd edge && npx --no-install wrangler rollback <previous version id> --message "rollback"`. Without an id, wrangler offers the version before the current one.
  2. **If wrangler says "The following secrets have changed since version … was deployed"**, confirming redeploys that version with its old secret values. After an edge-secret rotation the old `EDGE_AUTH_SECRET` is no longer in the API's `ATUS_EDGE_SECRETS`, so every proxied request would get 403 `forbidden_origin`; an old `RENDER_ORIGIN` or a revoked `NEON_API_KEY` comes back the same way. Decline, and choose a version deployed after the secret change. If you need an older one, first make the API accept the old edge secret too (Render `ATUS_EDGE_SECRETS=<current>,<EDGE_AUTH_SECRET_PREVIOUS from edge.env>`, "Save and deploy", wait until live), then confirm. You cannot fix the secrets afterwards with `wrangler secret put`: wrangler 4.148 refuses to edit a secret while a version other than the latest is deployed.
  3. Check with `python scripts/ops/monitors.py edge --base-url https://atus.<sub>.workers.dev --expect-snapshot-id <its SNAPSHOT_ID> --expect-data-version <its DATA_VERSION>`, the vars of the version you rolled back to (`npx --no-install wrangler versions view <version id> --json` lists them among its bindings; "This run" in the summary of the Deploy run that deployed it names them too), and run one analysis through the site. Without the two options the check passes on any consistent edge, the version you rolled away from included (review round 4).
  4. The pause switch is a pair of secrets too: a version from before a switch change brings back that version's switch (wrangler lists `API_PAUSED_UNTIL` among the changed secrets). The edge check reports it (`api_paused`, WARN); set it again with `deploy.yml` once the latest version is deployed.
- **Data:** the release switch in reverse (ADR-013; the commands are in [operations.md "Rollback"](operations.md#rollback)):
  1. restore the previous `ATUS_DATABASE_URL` and `ATUS_EXPECTED_DATA_VERSION` on Render;
  2. roll the edge back at once with `wrangler rollback` to the Worker version from before the release's deploy (Edge, above): it restores the Worker's `DATA_VERSION` and the old snapshot. Steps 1 and 2 need no build, scan or CI, so they work even while the image scan blocks every deploy ([operations.md, "The image scan blocks a deploy"](operations.md#the-image-scan-blocks-a-deploy));
  3. make `releases/current.json` on `main` name the previous release again (`git checkout <commit of the previous release> -- releases/current.json`, and `NEON_PROJECT_ID` in `edge/wrangler.jsonc` if the governor reads Neon usage), commit and push;
  4. wait until CI on that push has passed (`gh run watch`; preflight's CI gate refuses the commit until then), then `gh workflow run deploy.yml --ref main` and approve, so `main`, the Worker's code and the snapshot agree. Preflight runs only `main` or a `vX.Y.Z` tag; re-running an old run (GitHub allows that for 30 days) or tagging the old commit would deploy that commit's API image and Worker code too;
  5. if the governor reads Neon usage, create a new project-scoped key for the previous project in Neon and load it with `npx --no-install wrangler secret put NEON_API_KEY` once step 4 has deployed (the previous key is not kept: [Cloudflare Worker](#cloudflare-worker); step 2's version may carry its old value, but nothing else holds it), then revoke the newer project's key.
- **API host:** no failover is implemented. The Vercel fallback (ADR-011 "Fallback") is a documented manual procedure whose preconditions are not built, and no Worker var selects the API origin; the Worker reads it only from the secret `RENDER_ORIGIN`. See the runbook [Render suspends the service](operations.md#render-suspends-the-service) (owner decision).

### Monitoring and alerting ($0)

| Workflow | When | Requests | What fails it | Reaches |
|---|---|---|---|---|
| `edge-check.yml` | every 30 min (`:17`, `:47`) | 4 (1 Worker) | SPA shell or its headers, `/api/_edge/health`, the snapshot pointer or manifest, or a Worker `SNAPSHOT_ID` that differs from the pointer | Cloudflare only |
| `deep-check.yml` | every 6 h (`17 */6 * * *`) | 1–8 | `/api/health` through the Worker after a cold-start allowance of 4 min | Cloudflare and Render; never Neon |
| `quota-report.yml` | daily, 22:05 UTC | 1 (+1 to Cloudflare's API with the optional analytics token) | a quota at risk, at 70% (see below); a configured Neon key whose usage poll keeps failing; with the analytics token (owner step 4.3), the account's Worker requests today above 70% of the pro-rated 100,000 | the Worker's governor; Cloudflare's GraphQL Analytics API |
| `schedules-check.yml` | weekly (Monday 09:41 UTC) | GitHub API | a scheduled workflow not `active`; a monitor whose last scheduled run is far older than its cadence; no commit on `main` for 28 days | GitHub only |
| `ghcr-cleanup.yml` | weekly (Monday 04:23 UTC) | GitHub API | a failed deletion | GitHub only |

- **What the quota report judges.** It fails when a line is above 70% of its reference:
  - global and heavy analyses today, pro-rated by the share of the UTC day elapsed;
  - Neon's month-to-date CU-hours (Neon's own figure plus the governor's charges since its last poll), against the governor's pacing allowance, the monthly line above which it pauses analyses (`capacity_paused`). Only with `NEON_API_KEY`;
  - Neon's month-to-date egress, pro-rated by the share of Neon's consumption period. Only with `NEON_API_KEY`.

  Three lines are reported but never alert, because the governor itself holds them down and reaching them is the protection working:
  - the busiest network against the per-network cap;
  - the governor's CU-hours today against its daily ceiling. Reaching it refuses analyses (`daily_capacity_reached`, budget `cu_day`). In conservative mode a typical day of the traffic model does (ADR-015, "Conservative mode is tight"), so this line would otherwise fail every day;
  - its rolling 31-day charges. They charge 2 CU for every second and the whole idle tail, an upper bound far above what Neon measures on normal traffic: compared with the 100 CU-h quota they crossed 70% from about day 23 (ground truth) or day 26 (conservative mode) of a typical month (review round 2, quota-report-alerts-on-normal-traffic).

  Without `NEON_API_KEY` no Neon CU-hour line can alert. The report shows a WARN instead: Neon's real usage, including what the governor does not count (ADR-015, "Model error"), is visible only in the Neon console. With the key, a poll that keeps failing (conservative mode with a poll error) fails the report.
- **Not monitored: Render outbound bandwidth** (5 GB a month; the service pauses until the 1st when it runs out). Reading it needs a Render API key, which grants every workspace (owner step 2.6 says never to create one), so no workflow warns before it runs out, and no notification prompts you to look either: a weekly look at the workspace's usage is an item of the [owner checklist](#owner-checklist) below, which you open yourself. Governed traffic stays far below it (projection in [Free-tier budget](#render-free-web-service-hobby-workspace)), so a rise means direct-origin hits or another ungoverned path. Without the analytics token, the Worker's daily requests are unmonitored too, and the same checklist covers them.
- **Email.** A failed scheduled run emails its owner when notifications are set as in owner step 4.4. A run that passes sends nothing.
- **Skipped monitors.** The monitors stay skipped until the repository variable `PUBLIC_BASE_URL` exists, which you set only after step 8 has verified the site (owner step 4.1).
- **Inactivity.** GitHub disables scheduled workflows after 60 days without repository activity, and then nothing alerts, not even the schedule check. GitHub does not say whether Dependabot's pushes to its update branches count as activity, so the check measures the newest commit on `main`, not the repository's `pushed_at` (which those branch pushes keep fresh). Its 28-day threshold leaves four more weekly runs before the cutoff, so one dropped run does not cost the warning. If it fails for inactivity, push a commit to `main`; if a workflow shows as disabled, re-enable it under Actions → *workflow* → "Enable workflow".
- **Manual runs.** Every monitor can be run by hand (Actions → *workflow* → Run workflow). The same scripts run locally:
  - `python scripts/ops/monitors.py edge|deep --base-url ...`;
  - `EDGE_OPS_TOKEN=... python scripts/ops/monitors.py quota --base-url ...`.

#### Owner checklist

Nothing sends you this list. `schedules-check.yml` writes it into the summary of its weekly run, but that run normally passes, and with owner step 4.4's "Only notify for failed workflows" a passing run sends no email. Open **Actions → Schedules check → the latest run** every Monday (a recurring entry in your own calendar is the only prompt), or work from this copy, which `tests/deploy/test_docs_accuracy_r2.py` keeps equal to the workflow's.

Weekly (no monitor watches these):

- Render outbound bandwidth this month (Render → workspace usage): 5 GB a month, and the service pauses until the 1st when it runs out. Above about 3.5 GB, find the cause in the Render logs (direct hits: `edge_authenticated: false`, 403).
- If the quota report shows no "Worker requests today" line (no analytics token): the account's Worker requests on the Cloudflare dashboard, 100,000 a day.

Monthly:

- The Cloudflare API tokens expire 90 days after creation: rotate them before then.
- Quota reports: any warning this month? Neon CU-hours and egress on the Neon console.
- Render: no suspension or card notice; the service is still on the Free plan.
- GitHub: Actions notifications still set to email on failed workflows.

Once a month, also look at the Actions tab itself: every monitor, the schedule check included, should have a recent run. That monthly look is the backstop for the schedule check: if GitHub has disabled it, it cannot fail, and nothing else notices that the monitors have stopped (re-enable them: [operations.md](operations.md#scheduled-workflows-were-disabled)).
