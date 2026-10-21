# Deployment

This is the **first version** of the deployment document. It was written with the hosting decision ([ADR-011](adr/ADR-011-hosting.md)) on 2026-10-20.

**Status: not deployed.**

- Nothing here has been verified against a live URL. The README will show a live URL only after it has been checked with curl and Playwright.
- Numbers come in two kinds:
  - **measured**: from the Phase 5 audit ([`operations.md`](operations.md), [`evidence/phase5/01-audit-timed.md`](evidence/phase5/01-audit-timed.md));
  - **PROJECTION**: arithmetic on the traffic model below, with its assumptions stated.
- Free-tier terms were read on the official pages listed in each table, all accessed 2026-10-20. ADR-011 has the full citation list with quotes.
- Sections marked **PLACEHOLDER** are filled in during implementation.
- **Environment-variable and secret names in this document are provisional.** Implementation finalizes them in the configuration reference.

## Architecture

```mermaid
flowchart LR
  U["Browser"] -->|"HTTPS"| EDGE{"Cloudflare edge: path?"}

  subgraph CF["Cloudflare Workers Free: WORKER.ACCOUNT-SUBDOMAIN.workers.dev"]
    EDGE -->|"/, /explore, /analysis/*, /about, /assets/*, /snap/*"| S["Static assets: SPA, snapshot of metadata and prewarmed results, _headers. Free, not metered"]
    EDGE -->|"/api, /api/*"| W["Worker: allowlist, 64 KiB cap, edge auth, security headers"]
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
| Fallback API (**not provisioned**) | Vercel Hobby (see ADR-011 "Fallback") | `https://<project>.vercel.app` | Protected by Vercel Deployment Protection |

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
| Monitoring | edge check every 30 minutes (48 per day; 1 Worker request each, no Render or Neon wake); deep check every 6 hours (4 per day; 1 Worker request + a Render `/health` wake, no DB); daily quota report (1 Worker request) |

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
| Worker requests (account-wide) | 100,000/day, reset 00:00 UTC ([limits](https://developers.cloudflare.com/workers/platform/limits/)) | 113/day typical (60 from visits + 53 from monitoring); 653 on the spike day; ~4,040/month | ≥ 99% even on the spike day | `run_worker_first` requests get 429 ([billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/)) or an Error 1027 page with an undocumented status, until 00:00 UTC. Never billed | Static SPA and snapshot keep working (home, starters, presets, metadata, ~130 prewarmed analyses). Novel and KV-cached analyses fail. The SPA treats a non-JSON 429/4xx/5xx from `/api` as "daily request allowance reached; resets at 00:00 UTC", shown in local time |
| Static asset requests | free and unlimited; 20,000 files and 25 MiB per file per version ([billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/), [limits](https://developers.cloudflare.com/workers/platform/limits/)) | ~12,000 requests/month; ~750 files per version | unlimited requests; ~96% of the file cap free | n/a | n/a. This tier is the degradation floor for every other quota |
| Worker CPU per request | 10 ms; network, KV and DO waits not counted ([limits](https://developers.cloudflare.com/workers/platform/limits/)) | expected well under 1 ms (SHA-256 of ≤ 64 KiB, header work, no JSON parsing). **Unmeasured** | expected large; measure in Workers Logs | Error 1102 (HTTP 503) for that request; occasional overruns tolerated | one failed request, which the SPA retries |
| KV reads | 100,000/day ([KV pricing](https://developers.cloudflare.com/kv/platform/pricing/)) | 45/day typical, 450 on the spike day (1 per analysis POST) | ≥ 99% | reads fail until 00:00 UTC | treated as a miss: more traffic to the origin, still under the governor. Reads can never run out before Worker requests do |
| KV writes | 1,000/day; 1 write/s per key ([KV pricing](https://developers.cloudflare.com/kv/platform/pricing/)) | 30/day typical, 300 on the spike day (1 per uncached 200 response) | 70% on the spike day. The governor's cap of 600 uncached analyses/day keeps writes ≤ 600 (≥ 40%) | writes fail until 00:00 UTC; the Worker ignores the error | none visible: new results are not edge-cached for the rest of the day |
| KV storage | 1 GB per account | 1.3–10 MB (≤ 1,200 live entries × 1.1–8.4 KB, measured response sizes; 30-day TTL) | > 99%. Hard bound: 600 writes/day × 30 days × 16 KiB value cap ≈ 0.29 GB (71% free) | writes fail | as for KV writes |
| Durable Object requests | 100,000/day, SQLite backend only ([DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)) | ~95/day typical, ~760 on the spike day (2 per uncached analysis + alarm polls + ops reads) | ≥ 99% | operations fail until 00:00 UTC | fail-closed: uncached analyses get 503 `capacity_unavailable`; static and KV keep working |
| Durable Object duration | 13,000 GB-s/day | far below the bound | hard bound 10,800 GB-s/day even if the object never idles (128 MB × 86,400 s), ≥ 17% headroom | operations fail | as above |
| Durable Object rows written / storage | 100,000 rows/day; 5 GB | ~150 rows/day typical, ~1,350 on the spike day; < 1 MB stored | ≥ 98% | writes fail | as above |
| Workers Logs | 200,000 events/day, 3-day retention, until 2026-11-30. From 2026-12-01: 0.5 GB/day, 7 days ([Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [observability](https://developers.cloudflare.com/observability/pricing/)) | ~420 events/day typical, ~2,800 on the spike day | ≥ 98% | after Dec 1, ingestion stops until 00:00 UTC; before then, undocumented | none for users; the edge logs for that day are lost |
| Workers Builds (build minutes) | 3,000 min/month | **0**: never connected; wrangler deploys from GitHub Actions | n/a | n/a | n/a |

### Render (Free web service, Hobby workspace)

| Quota | Limit | Projected use | Headroom | On exhaustion | Degradation |
|---|---|---|---|---|---|
| Free instance hours | 750 per workspace per calendar month ([free](https://render.com/docs/free)) | ~220 h: 14 wake sessions/day × ~25 min (1 min spin-up + activity + 15 min idle) ≈ 5.8 h, plus 4 deep checks × ~16 min ≈ 1.1 h, plus ~14 h on the spike day | ≥ 70%. One service can use at most 744 h, so it cannot run out. **Rule: never add a second Free web service** | all Free web services suspended until next month | cannot happen with one service |
| Outbound bandwidth (includes API→DB traffic the service initiates; inbound not billed) | 5 GB per Hobby workspace per month ([outbound bandwidth](https://render.com/docs/outbound-bandwidth)) | ~0.05 GB: 1,200 uncached × ≤ 40 KB (SQL text to Neon, estimated at 15–34 KB from the query shape, plus a gzipped response ≤ 3 KB) + ~2 MB of health and wake responses | ~99%. Hard bound from governor caps: 600/day × 40 KB × 31 ≈ 0.74 GB (85% free) | with no card on file, all services suspended until next month ([free](https://render.com/docs/free)). **With a card, overage would be billed at $0.15/GB, so a card is never added** | the Worker maps platform errors to `upstream_unavailable`; the SPA shows "analysis service paused until the 1st"; static snapshot and KV keep working |
| Discretionary suspension for "uncommonly high" service-initiated traffic | no published threshold; restore only via a paid plan ([free](https://render.com/docs/free)) | ~1.6 MB/day of API→DB traffic (PROJECTION); ≤ ~24 MB/day at governor caps | unknown | indefinite suspension | as above, but indefinite. Runbook: owner decision → fallback API host via a Worker var (ADR-011). Never recreate the service on Render; never pay |
| Pipeline minutes | 500/month (Hobby) ([free](https://render.com/docs/free)) | **0 builds**: image-backed. Whether image pulls count is undocumented | ~100% | new builds and deploys disabled for the rest of the month; the running service keeps serving | none for users; deploys wait |
| Logs and metrics | 7 days on Hobby; latency percentiles need Pro ([logging](https://render.com/docs/logging), [metrics](https://render.com/docs/service-metrics)) | structured app logs plus a periodic metrics event (ADR-014) | n/a | n/a | n/a |

### Neon (Free)

| Quota | Limit | Projected use | Headroom | On exhaustion | Degradation |
|---|---|---|---|---|---|
| Compute | 100 CU-hours per project per month ([plans](https://neon.com/docs/introduction/plans)) | Scenario A ~17, B ~45–80, C ~120 CU-h (see "Neon CU-hour scenarios") | A 83%, B 20–55%, **C none** | compute suspended until the next billing period; connections drop. Never billed ([FAQ](https://neon.com/faqs/free-plan-limits-and-quotas)) | prevented by the governor: uncached analyses get 503 `capacity_paused` with `Retry-After`/`resumes_at` once the paced allowance is used (hard stop at 85 CU-h). If Neon suspends anyway, the API returns `database_paused` and the SPA shows a "new analyses paused until <date>" page. Static and KV keep working |
| Public network transfer (egress, outbound only) | 5 GB per project per month ([network transfer](https://neon.com/docs/introduction/network-transfer)) | ~0.15 GB: 1,200 analyses × ~80 KB average (float8 text ≈ 10 KB per year-row; 70% single-year ~15 KB, 30% multi-year up to ~230 KB) + ~45 MB of per-process metadata bootstraps. Estimated, **unmeasured** | ~97%. Governor: heavy analyses ≤ 300/day, hard stop at 4.5 GB | compute suspended until the next billing period | as for CU-hours |
| Storage (`logical_size` across branches) | 1 GB per project, 20 GB per account ([plans](https://neon.com/docs/introduction/plans), [usage](https://neon.com/docs/introduction/usage-calculations)) | **measured** 0.65 GB (cluster sum 650,458,708 B, local prototype). +~26 MB per new ATUS year (PROJECTION: 598 MiB / 23 years) | ≥ 35% if the cap is 1e9 B (unit undocumented) | storage-increasing writes fail; reads continue; no deletion, no billing | none for serving (the API is read-only); only owner tasks that write (`validate-analytics`) fail. Each release goes into a new project |
| Projects / branches | 100 projects, 20 GB account total; 10 branches per project | 1 project (2 during a release switch, ≈ 1.3 GB); 1 branch | large | creation fails | none for serving |
| Scale to zero | fixed at 5 min, cannot be disabled | n/a | n/a | n/a | the first query after idle waits for a wake: a few hundred ms to a few seconds, longer after a branch is archived (unmeasured) |

### GitHub (Free, public repository)

| Quota | Limit | Projected use | Headroom | On exhaustion | Degradation |
|---|---|---|---|---|---|
| Actions minutes (standard runners) | free and unmetered for public repos; 6 h per job, 20 concurrent jobs ([Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)) | CI per PR; deploys (+10–15 min each for snapshot generation, unmeasured); ~1,640 scheduled runs/month (1,488 edge checks + 124 deep checks + 31 quota reports) | unmetered | n/a. Without a card, metered items are blocked, never billed | none |
| Scheduled workflows | disabled after 60 days without repo activity; runs may be delayed or dropped ([disable rule](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows), [events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)) | 3 schedules (`:17`/`:47` edge, `17 */6 * * *` deep, daily report) | n/a | monitors stop silently | none for users. Outages go unalerted until the monthly runbook check re-enables the schedules |
| GHCR storage and bandwidth | "currently free" (≥ 1 month's notice of change) ([Packages billing](https://docs.github.com/en/billing/concepts/product-billing/github-packages)) | last 20 digests kept (image size to be measured) | n/a | policy change announced | none; fallback is another public registry plus an `imgURL` change |
| Release assets | < 2 GiB per file; no total size or bandwidth limit ([about releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)) | one 336.5 MB dump + sha256 per data release (measured prototype size) | 83% under the per-file cap | n/a | n/a |

### Neon CU-hour scenarios

All values are **PROJECTION**. They exist because the autoscaler's behaviour during the 5-minute idle tail is undocumented.

Neon's algorithm page says the autoscaler reads the 1-minute load average every 5 s and targets ≤ 90% CPU. It does not say how fast the compute scales back down.

Common assumptions:

- 480 Neon wake sessions a month (16/day × 30), each with ~1.9 uncached analyses about 60 s apart;
- busy time at 2 CU of ~1 s per analysis (measured 0.38 s single-year, 2.7–3.4 s worst);
- spike day: compute awake for about 12 h;
- ~1 CU-h reserve for restore and smoke tests.

| Scenario | Idle-tail behaviour | Per session | Month |
|---|---|---|---|
| A | drops to 0.25 CU as soon as a query ends | 75 CU-s tail + 15 CU-s think time + ~4 CU-s busy ≈ 0.026 CU-h | ≈ 12.5 + 3.2 (spike) + 1 ≈ **17 CU-h** |
| B | stays raised while the 1-minute load average decays (~2–3 min) | + ~210–400 CU-s ≈ 0.08–0.14 CU-h | ≈ **45–80 CU-h** |
| C | stays at 2 CU through the whole tail | ≈ 2 × (300 + 60) CU-s ≈ 0.2 CU-h | ≈ 96 + 24 (spike) ≈ **120 CU-h**, over quota; the governor stops at 85, so uncached analyses pause late in the month |
| Fixed 0.25 CU (fallback lever) | constant | ≈ 0.25 × (300 + 60 + ~23 s busy) ≈ 0.027 CU-h | ≈ **17 CU-h**, but worst cases take 47–52 s (measured at the 0.25 CU emulation) |

These figures line up with proposal P1's independent scenario analysis (A 17, B 45–55, C 105–110).

**Week-1 calibration.** Read `compute_time_seconds` per wake session from `GET /projects/{id}`. If it exceeds about 0.1 CU-h, lower the compute's maximum to 1 CU (after a 1 CU emulation run) or fix it at 0.25 CU. To be finalized in ADR-012.

### Governor starting parameters

To be finalized in ADR-012.

| Budget | Starting value |
|---|---|
| Neon CU-h month-to-date (ground truth, polled from the Neon API only while traffic is active) | admit while < min(85, 10 + 75 × elapsed share of the consumption period) |
| Neon CU-h per UTC day | ≤ 10. Without ground truth: ≤ (100 × 0.9)/31 ≈ 2.9, with every wake session charged at max CU for its whole 300 s tail |
| Neon egress | stop at 4.5 GB month-to-date; heavy analyses ≤ 300/day |
| Global uncached analyses | ≤ 600 per UTC day (also bounds KV writes and Render outbound) |
| Per IP prefix (/24 or /48, HMAC with a daily key) | ≤ 60 uncached analyses per UTC day |
| API in-flight (in-process, global on one Render instance) | ≤ 2 uncached, ≤ 1 heavy; wait ≤ 5 s, then 503 `overloaded` |
| API per-IP token buckets | uncached: burst ~10, refill 1 per 30 s; other `/api`: ~120/min |

## Scraping-burst scenarios

All values are **PROJECTION** from the limits above. Nothing here was load-tested against a provider, and nothing will be: §13 load tests run locally under emulation.

| Scenario | What happens | Blast radius |
|---|---|---|
| **S1. One client sends novel specs at 5 req/s for 2 h** (36,000 requests) | Worker: 36% of the day's 100k. Each request misses KV, and the governor admits ~60 (per-prefix daily cap). The rest get a 429 from the Worker with `Retry-After` to 00:00 UTC, without touching Render. Neon: ≤ 60 analyses, ≤ ~1 CU-h even under scenario C. KV writes ≤ 60 | Other visitors unaffected; Worker headroom for the day shrinks by 36% |
| **S2. Distributed scraper** (≥ 200 /24 prefixes) sending novel specs at 3 req/s, sustained | The global cap (600 uncached/day) and the daily CU ceiling are reached within hours. After that, real visitors' novel analyses get 503 `daily_capacity_reached` until 00:00 UTC. The Worker's 100k is exhausted after ~9.3 h, and `/api` then returns 429/1027 until 00:00 UTC | One UTC day per attack day for `/api`. The static SPA and snapshot keep working throughout. If it repeats daily for a month: Neon stays ≤ 85 CU-h (paced), Render outbound ≤ 0.74 GB. **No month-long pause**, but novel analyses are mostly unavailable while the attack lasts |
| **S3. Replay of cached specs** (KV hits) at 5 req/s | Only Worker requests and KV reads are spent. Both run out after ~5.6 h, with the same 100k limit. Render and Neon untouched | `/api` down until 00:00 UTC; static floor intact |
| **S4. Direct hits on the onrender.com origin**, skipping the Worker | Each gets a minimal 403 with 0 DB work. It still wakes Render (instance hours, which cannot run out) and costs a few hundred bytes of Render outbound. ~10 req/s sustained for a month ≈ 26.8M × ~300 B ≈ 8 GB, which would exceed 5 GB | Accepted risk (ADR-011): services paused until next month. Mitigated by an unpublished name with a random suffix, a minimal 403 body and Render's own Cloudflare DDoS layer |

**What this protection does not cover:** a determined distributed attacker can always spend the account-wide 100k Worker requests per day. On a workers.dev hostname at $0, nothing can rate-limit before the Worker is invoked.

## Failure modes and degradation

| Event | How it shows up | What visitors see | Recovery |
|---|---|---|---|
| Render spun down (normal, after 15 idle minutes) | first `/api` call waits about a minute, or gets Render's hibernate 503, which the Worker maps to `service_waking` | home, starters, presets and metadata are instant (static). A novel analysis shows "Waking up the analysis service — free hosting sleeps when idle; this can take up to a minute" after 3 s (polite live region, no fake progress bar). The SPA retries for up to 150 s, honouring `Retry-After` | automatic |
| Neon suspended (normal, after 5 idle minutes) | first query waits a few hundred ms to a few s | absorbed in the analysis wait | automatic |
| Neon branch archived after quiet days | slow first query (unmeasured) | same waiting message | automatic |
| Neon compute update restart | dropped connections | none: `check_connection` replaces them (test) | automatic |
| Worker 100k/day exhausted | non-JSON 429 or 1027 from `/api` | full-page notice for analysis features: "daily request allowance reached; resets at <local time of 00:00 UTC>". Static content and prewarmed results still work | 00:00 UTC |
| KV reads or writes exhausted | KV errors in the Worker | none (miss, or not cached) | 00:00 UTC |
| Durable Object error or quota exhausted | governor call fails | novel analyses get 503 `capacity_unavailable`; everything else works | 00:00 UTC, or a fix |
| Governor daily cap reached | 503 `daily_capacity_reached` | "Today's free analysis capacity is used up; resets at <time>". Snapshot and cached results work | 00:00 UTC |
| Governor monthly pacing or hard stop | 503 `capacity_paused` with `resumes_at` | "New analyses paused until <date> (free database allowance)". Snapshot and cached results work | allowance resumes; or the owner applies the calibration lever |
| Neon CU-h or egress exhausted anyway | connection failures, which the API maps to 503 `database_paused` | the same paused page | the next Neon billing period |
| Neon storage over the cap | owner writes fail | none | free space, or restore into a new project |
| Render bandwidth exhausted | platform errors, which the Worker maps to `upstream_unavailable` | "Analysis service paused until the 1st" (snapshot and KV work) | next month; find the cause in the logs |
| Render discretionary suspension | same symptoms; Render email or dashboard notice | same, but indefinite | **owner decision**: fallback API host (ADR-011) by changing a Worker var. Never pay; never recreate on Render |
| Render restart (can happen any time) | in-process caches and token buckets lost | none (edge tiers and the governor persist) | automatic |
| Card prompt at any provider | sign-up or deploy screen | — | **stop and report** (constraint 1); owner decision |
| GHCR image unavailable | Render "Image Pull Failed" on a restart or deploy | Render serves nothing; snapshot and KV still work | republish the image or switch registry; redeploy by digest |
| GitHub schedules auto-disabled | no monitor runs | none (no alerts) | monthly runbook: re-enable the workflows |
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
   1. `npx wrangler@4 login`. Wrangler must be 4.20 or newer.
   2. `npx wrangler kv namespace create <KV_NAME>`. The namespace id is not secret; it goes into `wrangler.jsonc`.
   3. Generate the secrets: `scripts/ops/gen_edge_secrets.sh --render-origin https://<service>.onrender.com` writes `~/.config/atus/edge.env` (mode 600) and prints, without any value, the commands below. The Render origin exists only after step 2.3; until then run the script without `--render-origin` and re-run it later.
   4. Run the first `npx wrangler deploy` yourself. Creating a Worker needs account-level Admin. This creates the Worker, its static assets and the Durable Object migration. `wrangler.jsonc` marks `EDGE_AUTH_SECRET`, `OPS_TOKEN`, `IP_HMAC_KEY` and `RENDER_ORIGIN` as required, so this first deploy passes them with `--secrets-file`, filtered to the Worker's names exactly as the script prints it (the file also holds the API's `ATUS_OPS_TOKEN`, which never goes to the Worker). The printed command copies those names into a private temporary file that it removes afterwards: a process substitution (`--secrets-file <(grep ...)`) fails on macOS, because wrangler reads the file in a child process that cannot open `/dev/fd/N`. Later changes use `npx wrangler secret put <NAME>`, piped from the file:
      - `EDGE_AUTH_SECRET`
      - `OPS_TOKEN`
      - `IP_HMAC_KEY`
      - `RENDER_ORIGIN` (after step 2.3)
      - optionally `NEON_API_KEY` (step 3.6)
4. **Worker → Settings → Builds:** leave it **disconnected**. Never use "Connect to Git". The config sets `preview_urls = false`, so no extra Version URLs exist.
5. **Account API Tokens → Create Token → Custom token** for CI:
   - Workers role **Editor**, scoped to the single Worker `<worker>`.
   - Add **Account Settings: Read** only if a `wrangler deploy --dry-run` with the token fails without it.
   - Account resources: only this account. TTL 90 days.
   - If the UI forces a template, use **Edit Cloudflare Workers** restricted to this account. It is broader: it includes KV, R2 and routes.
   - Save the token as GitHub environment secret `CLOUDFLARE_API_TOKEN`, and the Account ID (Workers overview sidebar) as `CLOUDFLARE_ACCOUNT_ID`.

### 2. Render (Free web service)

1. Sign up at `dashboard.render.com`. Use the **Hobby workspace created automatically** at sign-up. **Never create another workspace**: that flow asks for a payment method. If any card prompt appears, stop.
2. **Region check, before creating anything on Neon.** Open New → Web Service, choose **Free** as the instance type, and note which regions are offered. **Cancel without creating.** Tell Claude whether **Virginia** is offered. If it is, Neon goes to AWS US East 1. If not: Ohio pairs with AWS US East 2, Oregon with AWS US West 2.
3. *(After the first CI image push and the GHCR package is made Public, step 4.4.)* **New → Web Service → Existing Image:**
   - Image URL: `ghcr.io/nathan98000/atus-api@sha256:<digest printed by CI>` (public; no credentials).
   - Name: `atus-api-<6 random chars>`. Never publish this hostname. Store the resulting URL as the Worker secret `RENDER_ORIGIN` and the GitHub secret `RENDER_ORIGIN`.
   - Region: as agreed in step 2. It cannot be changed later.
   - Instance type: **Free**. Health check path: **`/health`**.
4. **Environment** (provisional names):
   - `ATUS_ENV=production`
   - `ATUS_DATABASE_URL`: the `atus_api` direct DSN with `sslmode=verify-full&sslrootcert=system`, from the file the restore script writes (step 3.5)
   - `ATUS_EDGE_SECRETS`: the same value as `EDGE_AUTH_SECRET`
   - `ATUS_EXPECTED_DATA_VERSION`
   - `ATUS_OPS_TOKEN`: the `ATUS_OPS_TOKEN` line of `~/.config/atus/edge.env` (step 1.3)
   - `WEB_CONCURRENCY=1`
5. **Settings → Deploy Hook:** copy the URL into the GitHub environment secret `RENDER_DEPLOY_HOOK_URL`. Regenerate it if it ever leaks. Confirm that **no Git repository is connected**; image-backed services never auto-deploy.
6. Do **not** create a Render API key: it grants access to every workspace you belong to. Enable email notifications for failed deploys and service events. Never add a second Free web service to this workspace.

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
   1. **Connect**, with connection pooling **off** (the direct host, without `-pooler`). Copy the `neondb_owner` connection string into `~/.config/atus/neon.env`. Keep that file outside the repo, `chmod 600`.
   2. Run the restore script yourself. It runs `pg_restore --no-owner --no-acl` with a client of version 16 or newer, creates `atus_api` with a generated password of at least 60 bits, applies grants and role settings, runs `VACUUM (ANALYZE)` and checks the metered size. It writes the `atus_api` DSN back into the same file. Nothing is printed or pasted.
6. *(Optional; strengthens the quota governor.)* **Organization settings → API keys → Create → Project-scoped key** for this project only, if the Free plan offers it.
   - Load it with `npx wrangler secret put NEON_API_KEY`. The project id goes in as the Worker var `NEON_PROJECT_ID`.
   - Never use a personal or organization-wide key here.
   - The key must be re-created for each release project.

### 4. GitHub (repository `Nathan98000/ATUS`, stays public)

1. **Settings → Environments → New environment `production`:**
   - Required reviewers: yourself, with **Prevent self-review OFF** (otherwise nobody can approve).
   - Deployment branches and tags: `main` and the tag pattern `v*`.
   - Secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `RENDER_DEPLOY_HOOK_URL`, `RENDER_ORIGIN`, `ATUS_OPS_TOKEN` (the API's), `EDGE_OPS_TOKEN` (the Worker's `OPS_TOKEN`). `scripts/ops/gen_edge_secrets.sh` prints the `gh secret set` commands for the last two, each reading its value from `edge.env`.
   - Variables: `PUBLIC_BASE_URL=https://<worker>.<account-subdomain>.workers.dev`, `GHCR_IMAGE=ghcr.io/nathan98000/atus-api`.
2. **New environment `ops`**, used by the scheduled monitors and the quota report:
   - No reviewers. Branch: `main` only.
   - Secret: `EDGE_OPS_TOKEN` (the daily quota report reads `/api/_ops/budget`). Variable: `PUBLIC_BASE_URL`.
3. **Repository settings:**
   - **Actions → General → Workflow permissions:** "Read repository contents". Jobs that push images request `packages: write` explicitly.
   - **General → Releases:** enable **release immutability**.
   - **Advanced Security:** enable CodeQL default setup, Dependabot alerts and Dependabot security updates. Secret scanning and push protection are on by default for public repos; confirm they are.
   - **Your profile → Settings → Notifications → System → Actions:** "Email", with "Only notify for failed workflows". Without this, monitor failures send no email.
4. *(After the first CI image push.)* **Packages → `atus-api` → Package settings:**
   - **Change visibility → Public.** This cannot be undone; the image contains no secrets or data.
   - Connect the package to the repository.

### 5. Verification

1. Push a docs-only commit to `main`. Confirm that **nothing deploys**: check Render → Events and Cloudflare → Worker → Deployments.
2. Tell Claude that the secrets above are set, by name only.
3. Approve the first run of the `deploy` workflow in the `production` environment.
4. After that deploy, check the origin lock: `curl -s -o /dev/null -w '%{http_code}' https://<service>.onrender.com/api/v1/meta` must print `403`.

## From-scratch deploy guide

**Status.** ADR-011 fixes the order below. Steps 0 to 3 and 6 are written and were run locally (functional checks only); step 0 now covers the public origin too (the Worker, the SPA and the static snapshot). What is still missing is marked **TODO(owner)**, where the owner is the work that fills it:

- **WS-A**: the API (protection, pool, observability);
- **WS-B**: the serving DB and data releases (ADR-013);
- **WS-D1**: the frontend build;
- **WS-D2**: the edge Worker and the snapshot (ADR-015);
- **wave 2**: the CI workflows, runbooks and README.

Nothing here has been run against a provider.

| Step | Who acts | Needs | Produces |
|---|---|---|---|
| 0. Rehearse locally | anyone | Docker | a passing stack smoke, image gate and lint |
| 1. Serving DB and release artifact | owner's machine | the full local DB (read only) | a checksummed serving dump, its `data_version`, the §9 equivalence proof |
| 2. Restore drill | owner's machine | step 1 | a restored, validated copy and its restore notes |
| 3. The image | CI (and locally, for rehearsal) | the repository | `ghcr.io/nathan98000/atus-api@sha256:<digest>`, gated and scanned |
| 4. Accounts | owner | — | Cloudflare, Render, Neon and GitHub set up as in §19 |
| 5. Load Neon | owner | steps 1 and 4 | the serving DB on Neon; the `atus_api` DSN in `~/.config/atus/neon.env` |
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
  | `roles` | the restore script's role step | applies `scripts/ops/roles.sql` as `neondb_owner`, exactly as production does: `atus_api` with SELECT on the served tables only, read-only transactions by default and the ADR-011 role defaults. It is the only definition of the role; `tests/deploy/test_stack_roles.py` (opt-in) proves it cannot write, truncate or create anything, and reads exactly the served tables |
  | `api` | the Render web service | the image with exactly Render's environment (`ATUS_ENV=production`, ...). Read-only root filesystem, all capabilities dropped. **Not published on the host** |
  | `snapshot-api` | CI's snapshot step | profile `snapshot`; the same image on the same database with generous per-client limits, published on `127.0.0.1` for `scripts/build_snapshot.py` only |
  | `edge` | Cloudflare Worker + static assets: the public origin | profile `edge`; the Worker from `edge/` under `wrangler dev` in local mode (KV and the Durable Object simulated on disk; no login, no metrics or error reports, no `cf.json` fetch, so no call to Cloudflare) on `127.0.0.1:8541`, serving `ATUS_STACK_ASSETS_DIR` (default `frontend/dist`) with `_headers` and SPA fallback, in front of `api`. The Worker accepts a plain-HTTP origin on loopback only, so a relay inside the container (`deploy/edge/origin-relay.mjs`) forwards `127.0.0.1:10000` to `api:10000`; `RENDER_ORIGIN` is that loopback address. The release vars come from `ATUS_EXPECTED_DATA_VERSION`, `ATUS_STACK_ANALYTICS_VERSION` and `ATUS_STACK_SNAPSHOT_ID` |
  | `smoke` | — | profile `smoke`; the HTTP probe, run inside the stack network with the edge secret and the API's ops token |

- **The public origin, end to end** (`--edge`; `--e2e` adds Playwright):

  ```bash
  scripts/smoke_stack.sh --edge --e2e      # needs node/npm, frontend/node_modules, Playwright browsers
  ```

  After the API smoke it does what a deploy does, in the same order: binds the API to its data (`ATUS_EXPECTED_DATA_VERSION`, as the release switch does), builds the SPA same-origin, builds the static snapshot with `scripts/build_snapshot.py` and the SPA's request generator against `snapshot-api` (with `--skip-refused`: the small fixture cannot answer every prewarmed spec, a release must), and starts the Worker with the release vars. `deploy/stack/edge_probe.py` then checks, from the host: SPA deep links (SPA fallback); `_headers` on static responses; the snapshot served statically and byte-identical to the API; `/api/_edge/health`; the `/api` allowlist; the Worker's security headers on `/api` responses; 413 and 415 at the edge; a KV miss then `X-Cache: edge`; the prewake; the ops bearer; the API's origin lock (403 without the edge secret); and a governor denial (a /24's 61st new analysis of the day: 429 `rate_limited`, budget `prefix`, `Retry-After` until 00:00 UTC). With `--e2e` the Playwright suite runs through the edge (stack mode, `E2E_BASE_URL`; projects `E2E_PROJECTS`, default `desktop mobile webkit`). On 2026-10-20 (wave 2): smoke 15/15, edge probe 13/13, Playwright 50/50 (Chromium desktop 35, mobile 2, WebKit 13). Firefox cannot launch on macOS 27 and runs in Linux CI.

  `wrangler dev` takes `CF-Connecting-IP` from the request when one is sent (Miniflare sets it from the socket only otherwise; Cloudflare's edge overwrites it in production). The probe and the stack-mode E2E use that to act as distinct visitors from the benchmarking range 198.18.0.0/15, so the production limits stay in place without the whole suite counting as one visitor.
- **Free-tier emulation** (`-f docker-compose.emulation.yml`): API 0.1 CPU / 512 MB, no swap (Render Free); database 0.25 vCPU / 1 GB, no swap (Neon's 0.25 CU floor). For the 2 CU ceiling set `ATUS_EMU_DB_CPUS=2 ATUS_EMU_DB_MEMORY=8g` (a Docker VM with less than 8 GB caps the RAM). `python deploy/emulation/neon_settings.py --help` prints the settings for other compute sizes from Neon's published formulas. Not emulated (F41): pageserver reads, wake from suspend, autoscaling itself, round trips between providers, Render's real CPU share and spin-up.

  The audit's emulation runs kept `shared_buffers` at 128 MB and Postgres's default `max_connections`/`max_worker_processes`. Neon's formula gives those values only for a fixed 0.25 CU compute; for ADR-011's 0.25–2 CU compute it gives 215 MB, 839 and 17, which the stack now uses. Later timed runs should state which settings they used.
- **Through a transaction pooler** (brief §10; production uses the direct endpoint): profile `pgbouncer` of the emulation file runs PgBouncer with Neon's pooler settings (`pool_mode=transaction`, `max_prepared_statements=1000`, `query_wait_timeout=120`).

  ```bash
  scripts/smoke_stack.sh --integration-via-pgbouncer                          # Neon's settings
  scripts/smoke_stack.sh --integration-via-pgbouncer --prepared-statements 0  # no prepared-statement support
  ```

  Measured on 2026-10-20 (functional, fixture data): 50/50 integration tests pass with `max_prepared_statements=1000`. With `0`, 3 fail with `DuplicatePreparedStatement` (`_pg3_0`): psycopg prepares a statement after its 5th execution, and the next transaction lands on a server connection that already has one by that name. One of them is an API request that returned 500. If the API ever goes through a pooler without prepared-statement support, the pool must pass `prepare_threshold=None`.
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

### 2. Restore drill

- **What.** Restore the release dump exactly as production will, into a fresh `postgres:16`:
  - `pg_restore --no-owner --no-acl` as a non-superuser owner;
  - the roles script;
  - `VACUUM (ANALYZE)`;
  - the size check;
  - EXPLAIN of the worst cases;
  - a smoke test with a local API.

  The definition of done requires the drill to be performed and timed. The timing belongs to the serialized measurement phase.
- **What runs locally today.** The stack's `dump` profile does the restore half, then the probe runs against real data:

  ```bash
  echo "ATUS_STACK_DUMP=/absolute/path/to/serving.dump" >> .env.stack
  docker compose --env-file .env.stack -f docker-compose.prod.yml --profile dump up -d --wait api
  docker compose --env-file .env.stack -f docker-compose.prod.yml --profile smoke run --rm smoke
  ```

- **The drill:** `scripts/ops/restore_serving_db.sh --artifact <dump> --verify-content-hash` (implemented by `atus restore-serving-db`) against a fresh local `postgres:16` cluster with a non-superuser owner: the owner DSN from `ATUS_OWNER_DATABASE_URL` or `~/.config/atus/neon.env`; the dump's sha256 against its `.sha256` file and manifest; a refusal of any non-empty target; `pg_restore --no-owner --no-acl --single-transaction`; `scripts/ops/roles.sql` with a generated 192-bit `atus_api` password sent as a SCRAM verifier, written back as `ATUS_API_DB_PASSWORD` and `ATUS_API_DATABASE_URL` into the same file (mode 600); `VACUUM (ANALYZE)`; and checks of the data version, rows, marker, role defaults, worst-case plans and size (ADR-013).
- **The stack's roles are the deployed roles.** Its bootstrap creates only `neondb_owner` and the database; the `roles` service then applies `scripts/ops/roles.sql` after every seed or restore, exactly as the restore script does on Neon.

### 3. The image

- **Build:** `docker buildx build --platform linux/amd64 --build-arg ATUS_BUILD_SHA="$(git rev-parse HEAD)" --build-arg ATUS_VERSION=<version> -t atus-api .`
- **What is in it.**
  - The base is `python:3.12-slim`, pinned by its multi-arch index digest, in a multi-stage build.
  - Runtime dependencies come from `requirements/api.lock.txt` with `--require-hashes --only-binary=:all:`. No compiler is needed, and no unpinned file is downloaded.
  - The project wheel is built with the hash-locked setuptools.
  - The runtime stage holds only the base image plus `/opt/venv`, bytecode-compiled at build time.
  - It runs as UID/GID 10001 and carries OCI labels (`source`, `revision` = `ATUS_BUILD_SHA`, `version`, `base.digest`).
  - Its Docker `HEALTHCHECK` calls `/health`, which never touches the database.
  - `CMD` runs `atus api --host 0.0.0.0 --port $PORT`, with `PORT=10000` by default.
- **What is not in it:** tests, data, docs, the frontend, `edge/`, `scripts/`, the pipeline-only `curl_cffi`, build tools and the migrations.
  - **Decision (F56):** the API never changes the schema. Production restores a serving dump, and `atus migrate` / `atus load` run from a source checkout, so the image ships no migrations.
  - `.dockerignore` is an allow-list.
- **libpq and TLS.** `psycopg[binary]` bundles its own libpq and OpenSSL. In this image that is libpq 18.6 (`pq.version()` = 180006) on both `linux/amd64` and `linux/arm64`, which meets the ≥ 16 that `sslrootcert=system` needs. See [TLS to the database](#tls-to-the-database).
- **Locks.** `requirements/compile.sh` (uv) regenerates three files from `pyproject.toml`, which keeps its version ranges:
  - `dev.lock.txt`: runtime + dev + build backend;
  - `api.lock.txt`: the image's runtime only, pinned to the dev lock's versions;
  - `build.lock.txt`.

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

  - **`check_image.py`** needs only Python and the Docker CLI. It fails unless all of these hold:
    - the image runs as a numeric non-root UID:GID, and so does the API server as PID 1, with read-only code;
    - it holds no compiler, linker, build system, VCS, downloader, `-dev` package, Python build backend or build-stage leftover;
    - its `HEALTHCHECK` passes with a database host that cannot resolve;
    - it leaves no trace in a throwaway Postgres that logs every connection and statement from the API's role. A control `/api/v1/meta` request must leave a trace, so that a quiet log means something. This catches a health check that reaches the database in a way no static check can see; the tests prove it with a deliberately broken image.
  - **Strict.** The pool is lazy (ADR-011 `min_size=0`), so the image opens no database connection at start-up and `startup_db_quiet` must PASS. The `--allow-startup-connection` option only exists for images that predate the lazy pool; no recipe uses it.
  - **`lint.sh`** runs `docker build --check` on every Dockerfile, `docker compose config` on every compose set, and `sh -n`/`bash -n` on every script. It runs hadolint and shellcheck only if they are already installed. Neither was installed where this was written, so neither has run on these files. The script never installs anything.
- **TODO(wave 2): the CI image job,** in this order:
  1. `deploy/ci/lint.sh`;
  2. `docker buildx build --platform linux/amd64` with `ATUS_BUILD_SHA=$GITHUB_SHA`;
  3. `python deploy/ci/check_image.py <image>` (strict);
  4. `ATUS_DOCKER_TESTS=1 ATUS_TEST_IMAGE=<image> pytest tests/deploy`;
  5. `scripts/smoke_stack.sh --no-build` with `ATUS_STACK_IMAGE=<image>`, once plain and once with `--emulation`;
  6. a Trivy scan, pinned to a commit;
  7. push to `ghcr.io/nathan98000/atus-api` by digest, with `packages: write` on that job only. GHCR keeps the last 20 digests for rollback.
- **First push and GHCR visibility:** owner step 4.4. The package starts private.

### 4. Accounts (owner)

Follow [Owner account setup](#owner-account-setup-19) in this order. Each step there says which artefact it waits for.

1. Cloudflare 1.1–1.2: the account and the subdomain. Decide the subdomain first: every share link contains it.
2. Render 2.1–2.2: the account and the region check. Create nothing yet.
3. Neon 3.1–3.4: the project in the paired region, compute 0.25–2 CU.
4. GitHub 4.1–4.3: environments and repository settings.
5. After the first CI image push (step 3): GitHub 4.4 (package public), then Render 2.3–2.6 (service by digest, environment, deploy hook).

### 5. Load the serving DB into Neon (owner)

- **Owner step 3.5.** Run the restore script against the **direct** endpoint as `neondb_owner`, with a client of version 16 or newer. The DSN comes from `~/.config/atus/neon.env`. The script:
  1. creates `atus_api`;
  2. applies the grants and role defaults (`statement_timeout=85s`, `lock_timeout=5s`, `idle_in_transaction_session_timeout=30s`, `jit=off`);
  3. runs `VACUUM (ANALYZE)`;
  4. checks the metered size;
  5. writes the `atus_api` DSN back into the same file.
- **Then on Render:** set `ATUS_DATABASE_URL` and `ATUS_EXPECTED_DATA_VERSION` (see [Configuration reference](#configuration-reference)).
- **First real TLS check.** This is the first connection with `sslmode=verify-full&sslrootcert=system` against Neon's real certificate.
- **The command:** `scripts/ops/restore_serving_db.sh --artifact <the release's .dump> --report restore-report.json`, with `ATUS_OWNER_DATABASE_URL` in `~/.config/atus/neon.env`.
- **Not verifiable before this step:** whether Neon accepts `ALTER ROLE atus_api SET ...` from `neondb_owner` (ADR-011 "Unknowns"; the local drill emulates a non-superuser `CREATEROLE` owner, where it works). The restore's role checks fail loudly if it does not.

### 6. First Worker deploy (owner)

- **Owner step 1.3:**
  1. `npx wrangler@4 login`;
  2. `npx wrangler kv namespace create <KV_NAME>`;
  3. the first `npx wrangler deploy`, which you run yourself, because creating a Worker needs account Admin;
  4. the Worker secrets, piped from `~/.config/atus/edge.env`.
- **What CI asserts on `edge/wrangler.jsonc` before every later deploy** (ADR-011):
  - no `[cache]` block;
  - `preview_urls = false` and `workers_dev = true`;
  - `run_worker_first` lists both `/api` and `/api/*`;
  - static assets come from `frontend/dist`.
- **Names** (`edge/wrangler.jsonc`): Worker `atus`, KV binding `RESULT_CACHE` (replace the placeholder namespace id with the one `kv namespace create` prints), Durable Object binding `GOVERNOR`, class `Governor`, migration tag `v1`.
- **Secrets:** `scripts/ops/gen_edge_secrets.sh [--render-origin https://<service>.onrender.com]` writes `~/.config/atus/edge.env` and prints the commands, including the first deploy's filtered `--secrets-file` (see [Cloudflare Worker](#cloudflare-worker)).
- **Rehearsed locally:** `scripts/smoke_stack.sh --edge --e2e` runs this Worker, with the same `wrangler.jsonc`, in front of the production-mode API (step 0).
- **What `frontend/dist` contains** (`npm run build` without `VITE_API_BASE_URL`): the SPA with a relative API base, `_headers` (CSP with `connect-src 'self'`, HSTS, `Permissions-Policy`, `index.html` and `/snap/current.json` no-cache, hashed assets and `/snap/<id>/*` immutable), and a `<meta>` CSP backstop in `index.html`. The SPA fallback is `not_found_handling = "single-page-application"` in `wrangler.jsonc`; the snapshot is added under `snap/` by step 7.

### 7. First approved deploy (CI)

`deploy.yml` runs on a `v*` tag or manual dispatch, in environment `production`, and waits for your approval as required reviewer. ADR-011 fixes its steps:

1. Call `RENDER_DEPLOY_HOOK_URL` with `imgURL=ghcr.io/nathan98000/atus-api@sha256:<hex>`, URL-encoded. Every part except the digest must match the image configured on the service.
2. Poll origin `/health` until its `build` equals `GITHUB_SHA`. Allow for a cold start: about a minute of Render spin-up plus 7–11 s of app start (measured, F67).
3. Generate the static snapshot with that same digest, against the release dump restored into `postgres:16` with the production role settings.
4. `wrangler deploy` with the vars `DATA_VERSION`, `ANALYTICS_VERSION`, `RESPONSE_FORMAT` and `SNAPSHOT_ID`.
5. Run a smoke test of at most 40 requests.

- **TODO(wave 2):** `deploy.yml` and its smoke list.
- **The snapshot step** (the same command the local stack runs): `python scripts/build_snapshot.py --api-base <local API on the restored release dump> --edge-secret-file <file> --requests-script frontend/scripts/snapshot-requests.mjs --api-build "$GITHUB_SHA" --data-version "$DATA_VERSION" --out frontend/dist/snap`; its stdout is `SNAPSHOT_ID`. Every analysis response must carry `X-Data-Version`, `X-Analytics-Version` and `X-Response-Format`, and every prewarmed spec must succeed on the release (no `--skip-refused`).

### 8. Prove the deployment

"Verified" means curl plus Playwright against the public URL. At least:

- A docs-only push to `main` deploys nothing. Check Render → Events and Cloudflare → Deployments.
- The origin lock: `curl -s -o /dev/null -w '%{http_code}' https://<service>.onrender.com/api/v1/meta` prints `403`.
- Security headers on both origins: the SPA's `_headers` and the Worker's own. Include the SPA-fallback `index.html`, where `_headers` may not apply (ADR-011 "Risks").
- `/health`, then `/api/v1/meta` (allowing for the cold start), one cached analysis, the SPA shell and one starter analysis rendering, with no CSP violations in the console.
- At least 2 snapshot entries recomputed live and compared under §9.
- **TODO(wave 2):** the URL-targeted, quota-budgeted check script (brief §16). The README gets the live URL only after this step passes.

### 9. Quota-budgeted post-deploy replay

- Measure what ADR-011 lists under "Unknowns to measure after deploy".
- Budget, in total: at most 150 Worker requests, 3 Neon CU-h and 15 Render wakes. No load tests against a provider.
- Results go into ADR-011, ADR-012 and this document, labelled as measured.
- **TODO(wave 2):** the replay script.

### 10. Week-1 calibration

- **Neon compute.** Read Neon's `compute_time_seconds` per wake session. If it exceeds about 0.1 CU-h, either lower the compute's maximum to 1 CU (after a 1 CU emulation run) or fix it at 0.25 CU.
- **Timeout.** Set the statement timeout to `clamp(3 × p99 measured on Neon, 30 s, 85 s)` (ADR-011 "Bounded work").
- **TODO(WS-A, ADR-012):** the final values and where each one is set.

## Configuration reference

This lists every variable and secret the deployed system uses: where it is set, where your own copy lives, its default, an example and how it changes or rotates.

- **API names are final.** They match `src/` and [`.env.production.example`](../.env.production.example); `tests/deploy/test_packaging_files.py` fails if the code reads a setting this file does not document, or if a template names one this section does not.
- **Worker names** are those of [`edge/wrangler.jsonc`](../edge/wrangler.jsonc), which is authoritative; `edge/test/node/config.test.ts` asserts its invariants. **GitHub names** follow ADR-011.
- **Templates:** [`.env.example`](../.env.example) for development and [`deploy/stack/stack.env.example`](../deploy/stack/stack.env.example) for the local stack.

**Where values live.** Real values exist only in the provider stores below and in your private files.

- Never in the repository: `.gitignore` ignores every `.env*` and `*.env` file except the committed templates (F31).
- Never in chat or on a command line.
- Examples below show formats only.

| Location | What it is | How a change takes effect |
|---|---|---|
| **Render env** | Render → service → Environment | "Save and deploy" restarts the instance with the new values |
| **Image** | baked in at build time (Dockerfile) | a new image, deployed by digest; never set these on Render |
| **Worker secret** | `npx wrangler secret put NAME`, piped from a file; encrypted and never readable again | the command deploys a new Worker version |
| **Worker var** | `vars` in `edge/wrangler.jsonc`, or `--var` from `deploy.yml` | the next `wrangler deploy` |
| **GitHub secret / var** | Settings → Environments → `production` or `ops` | the next workflow run |
| **Owner file** | `~/.config/atus/<name>.env`: `chmod 600`, outside the repository, your master copy | written by the scripts that generate the values |

### API (Render → Environment)

**Required in production.** `ATUS_ENV=production` refuses to start without `ATUS_DATABASE_URL` and `ATUS_EDGE_SECRETS`, with an edge secret or ops token shorter than 32 characters, with more than two edge secrets, or with the development database URL. `tests/deploy/test_container_image.py::test_production_mode_refuses_to_start_without_its_settings` (opt-in) proves it on the image.

| Variable | Set in (your copy) | Default (development) | Production example | Change / rotation |
|---|---|---|---|---|
| `ATUS_ENV` | Render env; also the image default | `development` | `production` | never |
| `ATUS_DATABASE_URL` | Render env, **secret** (`~/.config/atus/neon.env`, written by the restore script) | `postgresql://atus:atus@localhost:5434/atus` | `postgresql://atus_api:<password>@ep-<endpoint-id>.us-east-1.aws.neon.tech:5432/atus?sslmode=verify-full&sslrootcert=system` | at every release switch (new project); the password per [Rotation](#rotation). Never the `-pooler` host; never `sslmode=require` alone |
| `ATUS_EDGE_SECRETS` | Render env, **secret** (`~/.config/atus/edge.env`, as `EDGE_AUTH_SECRET`) | empty: no edge authentication | `<current>`, or `<current>,<next>` while rotating; each 32 random bytes, URL-safe (43 characters) | [Rotation](#rotation): two accepted values overlap, so no request fails |
| `ATUS_EXPECTED_DATA_VERSION` | Render env | empty: no check | `0325:serving-20261020T120000Z-1a2b3c4d:0123456789ab` | at every release switch, together with `ATUS_DATABASE_URL` (ADR-011 "Release switch"). A mismatch answers 503 `data_version_mismatch`. While the database serves exactly this version, the in-process snapshot is pinned: metadata routes and cache hits never read the database again (ADR-012) |
| `ATUS_OPS_TOKEN` | Render env, **secret** (`~/.config/atus/edge.env`, generated by `scripts/ops/gen_edge_secrets.sh`; GitHub secret `ATUS_OPS_TOKEN`) | empty: `/metrics` and `/ready` answer 404 | 32 random bytes, hex (64 characters) | [Rotation](#rotation) |
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
| `ATUS_API_DOCS` | `on` → `off` | `off` | `/docs`, `/redoc`, `/openapi.json` (published as a static asset instead) |
| `ATUS_API_CORS_ORIGINS` | empty | `http://localhost:5173` (development only) | allowed cross-origin callers; empty behind the same-origin Worker |
| `ATUS_API_CACHE_SIZE` | `256` | `512` | in-process result LRU entries (lost at every spin-down) |
| `ATUS_API_POOL_MAX_SIZE` | `4` | `4` | connections at most (Neon allows 839 at 0.25–2 CU) |
| `ATUS_API_POOL_MAX_IDLE_S` | `240` | `240` | close idle connections before Neon suspends at 300 s |
| `ATUS_API_POOL_TIMEOUT_S` | `5` | `5` | pool acquire timeout → 503 `overloaded` |
| `ATUS_API_CONNECT_TIMEOUT_S` | `10` | `15` | libpq `connect_timeout` (covers a Neon wake) |
| `ATUS_API_STATEMENT_TIMEOUT_MS` | `85000` | `60000` | per-request `SET LOCAL statement_timeout` for analyses; final value from the week-1 calibration |
| `ATUS_API_META_STATEMENT_TIMEOUT_MS` | `10000` | `10000` | the same, for metadata and lexicon reads |
| `ATUS_API_JIT` | `off` | `off` | per-request `SET LOCAL jit` (Neon has no LLVM; F37) |
| `ATUS_API_MAX_PARALLEL_WORKERS` | unset: the server default | `0` | per-request `SET LOCAL max_parallel_workers_per_gather`; set only after the §9-gated load tests |
| `ATUS_API_MAX_INFLIGHT` | `2` | `2` | uncached analyses in flight |
| `ATUS_API_MAX_INFLIGHT_HEAVY` | `1` | `1` | of which multi-year |
| `ATUS_API_ADMISSION_WAIT_S` | `5` | `5` | wait for a lane, then 503 `overloaded` |
| `ATUS_API_RATE_ANALYSIS` | `6/60` | `10/60` | per-client-IP token bucket for uncached analyses: `<tokens>/<seconds>` |
| `ATUS_API_RATE_REQUESTS` | `120/60` | `120/60` | the same, for every other route |
| `ATUS_API_DAILY_ANALYSIS_CAP` | `600` | `600` | in-process backstop, resets 00:00 UTC; the Worker's governor is the primary cap |
| `ATUS_API_MAX_BODY_BYTES` | `65536` | `65536` | larger bodies get 413 `payload_too_large` |
| `ATUS_API_THREADS` | `16` | `16` | anyio worker-thread limit |
| `ATUS_API_SNAPSHOT_TTL_S` | `2` → `600` | `600` | refresh interval of the in-process `data_version`, years and lexicon. Not applied while `ATUS_EXPECTED_DATA_VERSION` pins the snapshot; a failed refresh keeps serving the previous snapshot (ADR-012) |
| `ATUS_API_PREPARE_THRESHOLD` | `5` | `none` | psycopg's automatic server-side prepare threshold; `none` or `off` disables prepared statements. Needed only behind a transaction pooler without prepared-statement support (production uses Neon's direct endpoint) |

### Pipeline and release tooling (your machine only)

Never set on Render. The data commands (`atus load`, `atus build-serving-db`, `atus restore-serving-db`, ...) read these from the environment or from `.env` on your machine; the secrets among them live in `~/.config/atus/neon.env`.

| Variable | Read by | Default | Example | Meaning |
|---|---|---|---|---|
| `ATUS_DATABASE_URL` (full DB) | `atus load`, `validate-*`, the source of `build-serving-db` | `postgresql://atus:atus@localhost:5434/atus` | — | the full local database; the serving build only reads it |
| `ATUS_DATA_DIR` | the pipeline | `data` | — | raw and staged BLS files |
| `ATUS_RELEASE` | the pipeline | `0325` | `0326` | the BLS multi-year release code (ADR-013 "New ATUS year") |
| `ATUS_MIGRATIONS_DIR` | `atus migrate`/`load`, `build-serving-db` | the checkout's `migrations/` | `/workspace/migrations` | where the migration files are when not running from a checkout (the image ships none, F56) |
| `ATUS_SERVING_DATABASE_URL` | `build-serving-db --target`, `compare-serving-db` | unset | `postgresql://neondb_owner:...@localhost:5465/atus` | the new, empty database the serving build writes |
| `ATUS_OWNER_DATABASE_URL` | `restore-serving-db` (`scripts/ops/restore_serving_db.sh`) | unset: read from `neon.env` | `postgresql://neondb_owner:<password>@ep-<id>.us-east-1.aws.neon.tech:5432/atus?sslmode=verify-full&sslrootcert=system` | **secret**, the owner's direct DSN (never the `-pooler` host). A bare `postgresql://` line in `neon.env` counts as this key |
| `ATUS_API_DB_PASSWORD` | written by `restore-serving-db` into `neon.env` | — | 192 random bits, URL-safe | **secret**, the generated `atus_api` password; reused by later restores unless `--rotate-api-password` |
| `ATUS_API_DATABASE_URL` | written by `restore-serving-db` into `neon.env` | — | `postgresql://atus_api:<password>@ep-<id>...:5432/atus?sslmode=verify-full&sslrootcert=system` | **secret**, the DSN to copy into Render's `ATUS_DATABASE_URL` |
| `ATUS_API_ROLE_PASSWORD` | `scripts/ops/roles.sql` (psql `\getenv`) | — | a SCRAM-SHA-256 verifier | set by the restore only while psql applies roles.sql, so the plaintext never reaches the server; the local stack passes its own throwaway password |
| `ATUS_PG_CLIENT_IMAGE` | `pg_dump`/`pg_restore`/`psql` wrappers | `postgres:<server major>` | `postgres:16` | client image used through Docker when no local client is new enough |
| `ATUS_BIN` | `scripts/ops/restore_serving_db.sh` | `.venv/bin/atus`, then `atus` on PATH | `/opt/atus/bin/atus` | the `atus` executable the wrapper runs |
| `ATUS_EDGE_ENV_FILE` | `scripts/ops/gen_edge_secrets.sh` | `~/.config/atus/edge.env` | — | where the generated secrets go (`--file` overrides it) |
| `EDGE_AUTH_SECRET` (environment) | `scripts/build_snapshot.py` | unset: `--edge-secret-file` | — | **secret**, sent to the local snapshot API as `X-ATUS-Edge-Auth` |

### Image (build arguments and baked environment)

| Name | Default | Example | When it changes |
|---|---|---|---|
| `ATUS_BUILD_SHA` (build arg → env) | `dev` | `$(git rev-parse HEAD)` | every build. `/health` reports it as `build`; `deploy.yml` waits until it equals `GITHUB_SHA` |
| `ATUS_VERSION` (build arg → OCI label) | `unknown` | `0.1.0` (`atus_pipeline.__version__`) | every release |
| `PYTHON_BASE`, `PYTHON_DIGEST` (build args) | `python:3.12-slim`, `sha256:a6e34c59…` | — | base-image updates; bump both together. **TODO(wave 2):** Dependabot's `docker` ecosystem proposes them |
| `SSL_CERT_FILE` (env) | `/etc/ssl/certs/ca-certificates.crt` | — | never: `sslrootcert=system` depends on it ([TLS](#tls-to-the-database)) |
| `ATUS_ENV`, `PORT`, `WEB_CONCURRENCY` (env) | `production`, `10000`, `1` | — | never; see the API table |

### Cloudflare Worker

`edge/wrangler.jsonc` is authoritative for these names: Worker `atus`, KV binding `RESULT_CACHE`, Durable Object binding `GOVERNOR` with class `Governor` (migration tag `v1`). The local stack's `edge` service and `.env.production.example` use the same names.

| Secret | Set in (your copy) | If unset | Example | Rotation |
|---|---|---|---|---|
| `EDGE_AUTH_SECRET` | Worker secret (`~/.config/atus/edge.env`) | required | one value of the API's `ATUS_EDGE_SECRETS` | [Rotation](#rotation), with the API |
| `OPS_TOKEN` | Worker secret (`edge.env`; GitHub secret `EDGE_OPS_TOKEN`) | `/api/_ops/budget` answers 404 | 32 random bytes, URL-safe | [Rotation](#rotation) |
| `IP_HMAC_KEY` | Worker secret (`edge.env`) | required | 32 random bytes, URL-safe | `wrangler secret put IP_HMAC_KEY`. The governor's per-prefix counters restart, which they also do every UTC day |
| `RENDER_ORIGIN` | Worker secret (`edge.env`; GitHub secret `RENDER_ORIGIN`) | required | `https://atus-api-k3x9q2.onrender.com` | only when the API host changes (fallback switch); update both places. Never published |
| `NEON_API_KEY` | Worker secret, optional; not kept: re-create it instead | the governor runs in its conservative mode | a Neon **project-scoped** key | revoke and re-create in Neon, then `wrangler secret put NEON_API_KEY`; a new key for each release project |

| Var | Set by | Default | Example | When it changes |
|---|---|---|---|---|
| `DATA_VERSION` | `deploy.yml` | — | equals `ATUS_EXPECTED_DATA_VERSION` | each data release; gates KV stores and the cache scope |
| `ANALYTICS_VERSION` | `deploy.yml` (from `/api/v1/meta`) | — | `0.2` | when the engine version changes |
| `RESPONSE_FORMAT` | `deploy.yml` | — | `1` | only with a new response format (the API's `RESPONSE_FORMAT`) |
| `SNAPSHOT_ID` | `deploy.yml` | — | `<api_build12>-<sha256(DATA_VERSION)[:16]>` | each deploy |
| `EPOCH` | `edge/wrangler.jsonc` | `1` | `2` | manually. Bumping it orphans every KV entry, a full edge-cache flush |
| `PREWAKE` | `edge/wrangler.jsonc` | `on` | `off` | manually, to stop `GET /api/v1/wake` from waking Render |
| `NEON_PROJECT_ID` | `edge/wrangler.jsonc`, optional | unset | `<neon-project-id>` | each release project, with `NEON_API_KEY` |
| `API_ORIGIN`, `GATE_INFLIGHT`, the Vercel origin mode | fallback only (ADR-011 "Fallback") | unset | `https://<project>.vercel.app`, `on` | only on an owner decision to fail over |

Secrets are generated into `~/.config/atus/edge.env` by `scripts/ops/gen_edge_secrets.sh` and loaded with `npx wrangler secret put <NAME>`, piped from that file (owner step 1.3); the script prints each command. A brand-new Worker's first `wrangler deploy` must already carry the required secrets (`secrets.required` in `wrangler.jsonc`, so `RENDER_ORIGIN` must be known), and `--secrets-file` uploads every line of the file it reads: give it the Worker's names only, as the script prints (`grep -E '^(EDGE_AUTH_SECRET|OPS_TOKEN|IP_HMAC_KEY|RENDER_ORIGIN)=' ~/.config/atus/edge.env` into a private temporary file, which `--secrets-file` reads and the command then removes; not a process substitution, which fails on macOS), never the whole file, which also holds the API's `ATUS_OPS_TOKEN` and any `EDGE_AUTH_SECRET_PREVIOUS` or `NEON_API_KEY`.

### GitHub environments

| Name | Kind and environment | Your copy | Example | Rotation |
|---|---|---|---|---|
| `CLOUDFLARE_API_TOKEN` | secret, `production` | none (Cloudflare shows it once) | Custom token: Workers Editor on the one Worker, this account only, 90-day TTL | [Rotation](#rotation), before it expires |
| `CLOUDFLARE_ACCOUNT_ID` | secret, `production` (not sensitive) | — | 32 hex characters | never |
| `RENDER_DEPLOY_HOOK_URL` | secret, `production` | none | `https://api.render.com/deploy/srv-<id>?key=<key>` | [Rotation](#rotation) |
| `RENDER_ORIGIN` | secret, `production` | `edge.env` | as the Worker secret | with the API host |
| `ATUS_OPS_TOKEN` | secret, `production` | `edge.env` | equals the API's `ATUS_OPS_TOKEN` | with the API's token |
| `EDGE_OPS_TOKEN` | secret, `production` and `ops` | `edge.env` | equals the Worker's `OPS_TOKEN` | with the Worker's token |
| `PUBLIC_BASE_URL` | variable, `production` and `ops` | — | `https://atus.<account-subdomain>.workers.dev` | never (every share link contains it) |
| `GHCR_IMAGE` | variable, `production` | — | `ghcr.io/nathan98000/atus-api` | never |

**Which job uses which token.**

- **`deploy.yml`** uses `ATUS_OPS_TOKEN` for origin `/ready` in its post-deploy smoke, and `EDGE_OPS_TOKEN` to read `/api/_ops/budget`.
- **The `ops` monitors:**
  - the edge check calls `/api/_edge/health` and needs no token;
  - the deep check calls `/api/health` and needs no token;
  - the daily quota report reads `/api/_ops/budget` with `EDGE_OPS_TOKEN`.
- `ops` needs `ATUS_OPS_TOKEN` only if a monitor ever reads the origin's `/metrics`; none does, so owner steps 4.1 and 4.2 list `EDGE_OPS_TOKEN` for `ops` and both tokens for `production`.

### Owner files (`~/.config/atus/`, `chmod 600`)

| File | Holds | Written by | Feeds |
|---|---|---|---|
| `edge.env` | `EDGE_AUTH_SECRET` (and `EDGE_AUTH_SECRET_PREVIOUS` while rotating), `OPS_TOKEN`, `IP_HMAC_KEY`, the API's `ATUS_OPS_TOKEN`, then `RENDER_ORIGIN`; optionally `NEON_API_KEY`, which you add and the script keeps | `scripts/ops/gen_edge_secrets.sh`: 256-bit hex values, re-runs keep them, `--rotate NAME` replaces one, `--render-origin URL` records the origin | `wrangler secret put` (piped) for the Worker's names only; `ATUS_OPS_TOKEN` goes to Render env and the GitHub secret `ATUS_OPS_TOKEN`; `OPS_TOKEN` also to the GitHub secret `EDGE_OPS_TOKEN` |
| `neon.env` | `ATUS_OWNER_DATABASE_URL`: the `neondb_owner` direct DSN (pasted from Neon → Connect, pooling off; a bare `postgresql://` line also counts), then `ATUS_API_DB_PASSWORD` and `ATUS_API_DATABASE_URL` | you (the owner DSN) and `scripts/ops/restore_serving_db.sh` (the two `ATUS_API_*` lines) | restores, the API password rotation, Render's `ATUS_DATABASE_URL` (= `ATUS_API_DATABASE_URL`) |

### Local stack (`deploy/stack/stack.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `ATUS_STACK_PG_SUPERUSER_PASSWORD`, `ATUS_STACK_OWNER_PASSWORD`, `ATUS_STACK_API_DB_PASSWORD` | required | passwords of `postgres` (bootstrap only), `neondb_owner` and `atus_api`; URL-safe |
| `ATUS_STACK_EDGE_SECRET`, `ATUS_STACK_OPS_TOKEN` | required | the API's `ATUS_EDGE_SECRETS` / `ATUS_OPS_TOKEN` (and the Worker's `EDGE_AUTH_SECRET`); the smoke probe gets both. At least 32 characters: the stack's API runs in production mode, which refuses shorter ones |
| `ATUS_STACK_EDGE_OPS_TOKEN`, `ATUS_STACK_IP_HMAC_KEY` | empty | the Worker's `OPS_TOKEN` / `IP_HMAC_KEY` (profile `edge`) |
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
| `E2E_PROJECTS` | `desktop mobile webkit` | `mobile` | the Playwright projects `smoke_stack.sh --edge --e2e` runs |
| `ATUS_EDGE_RELAY_LISTEN`, `ATUS_EDGE_RELAY_TARGET` | `127.0.0.1:10000`, set by compose to `api:10000` | — | the edge container's loopback relay to the API (`deploy/edge/origin-relay.mjs`) |
| `E2E_BACKEND_PYTHON` | `../.venv/bin/python` | `python` | interpreter Playwright uses for the E2E backend |

### Rotation

Every secret has one procedure.

1. **Edge secret.** This one is designed so that no request fails.
   1. `scripts/ops/gen_edge_secrets.sh --rotate EDGE_AUTH_SECRET` writes the new value into `edge.env` and keeps the old one as `EDGE_AUTH_SECRET_PREVIOUS`.
   2. Render `ATUS_EDGE_SECRETS=<old>,<new>`, then "Save and deploy". Wait until Render → Events shows the deploy as live.
   3. `npx wrangler secret put EDGE_AUTH_SECRET` with `<new>`.
   4. Render `ATUS_EDGE_SECRETS=<new>`, then "Save and deploy".
2. **`atus_api` database password.**
   1. `scripts/ops/restore_serving_db.sh --artifact <the release dump> --roles-only --rotate-api-password` re-applies `scripts/ops/roles.sql` as `neondb_owner` with a new generated 192-bit password (sent as a SCRAM verifier) and rewrites `ATUS_API_DB_PASSWORD` and `ATUS_API_DATABASE_URL` in `neon.env`.
   2. Update Render's `ATUS_DATABASE_URL`, then "Save and deploy".

   A Postgres role has only one password. Between steps 1 and 2, new connections fail, so uncached analyses may get 503 `database_unavailable` with `Retry-After`; open connections keep working. Rotate when the service is idle.
3. **Ops tokens** (`ATUS_OPS_TOKEN`; the Worker's `OPS_TOKEN`).
   1. `scripts/ops/gen_edge_secrets.sh --rotate ATUS_OPS_TOKEN` (or `--rotate OPS_TOKEN`) writes the new value into `edge.env`.
   2. Set it on Render, or with `wrangler secret put OPS_TOKEN`.
   3. Update the matching GitHub secret.

   Until both match, only the ops checks see 404. Visitors are unaffected.
4. **`IP_HMAC_KEY`.** `npx wrangler secret put IP_HMAC_KEY` at any time.
5. **Cloudflare API token** (90-day TTL).
   1. Before it expires, create a new Custom token with the same scope.
   2. Replace the GitHub secret `CLOUDFLARE_API_TOKEN`.
   3. Delete the old token in Cloudflare.
6. **Render deploy hook.** Render → Settings → Deploy Hook → Regenerate. The old URL stops working at once. Then replace `RENDER_DEPLOY_HOOK_URL`.
7. **Neon owner password and project key.** Reset the password in the Neon console and update `neon.env`. Revoke and re-create `NEON_API_KEY`.

**If a secret leaks:** rotate it with the procedure above straight away. Then check the provider logs for use: Render logs, Workers Logs and Neon. GitHub secret scanning and push protection stay on (owner step 4.3).

### TLS to the database

- **Connection string:** `sslmode=verify-full&sslrootcert=system`. Never `require` alone.
- **libpq version:** `sslrootcert=system` needs libpq ≥ 16, and SNI needs ≥ 14. The image's bundled libpq is 18.6.
- **Why the image sets `SSL_CERT_FILE`.** The libpq inside the `psycopg[binary]` wheel uses the wheel's own OpenSSL 3.5. That OpenSSL's compiled-in certificate directory is a build-machine path (`OPENSSLDIR: "/host/tmp/libpq.build"`), so on its own `system` finds no roots at all. The image therefore sets `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`, the Debian CA bundle.
- **How it is tested:** `tests/deploy/test_container_image.py` (opt-in, `ATUS_DOCKER_TESTS=1`) uses a throwaway CA and a TLS-only Postgres to prove all three cases:
  - the image connects with `verify-full` + `system`;
  - it rejects a certificate whose CA is not in the bundle;
  - with `SSL_CERT_FILE` unset it fails even when the CA is in the bundle.
- **Not yet verified:** the live Neon certificate. It is checked at the first restore (step 5).
- **Fallback** if `system` ever fails: `sslrootcert=<bundled CA file>` with `verify-full` (ADR-011).

## Rollback and runbooks

**PLACEHOLDER.** The runbooks live in [`operations.md`](operations.md#runbooks) and are written during implementation: deploy, rollback, serving-DB build and restore, data release and new ATUS year, restore drill, incident first steps, token rotation, quota exhausted, and provider free-tier changes.

Summary of the rollback paths decided in ADR-011:

- **API:** call the Render deploy hook with the previous GHCR digest.
- **Edge (SPA, snapshot, Worker):** `wrangler rollback`.
- **Data:** the reverse of the release switch. The previous Neon project is kept until you approve deleting it.
- **API host:** fail over to the Vercel fallback by changing a Worker var (owner decision).
