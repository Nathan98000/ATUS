# Roadmap

A planning framework, not a contract — boundaries can move as the project
learns. The fixed principle: **design for extensibility, implement only what
the current phase needs.**

## Phase 1 — Data Foundation ✅ (this repository)

Delivered:

- Reproducible acquisition of the official BLS 2003–2025 multi-year microdata
  with a provenance manifest.
- Raw → staging → canonical separation; immutable raw files.
- PostgreSQL schema modeling the survey's real structure: respondents,
  household roster, activity episodes, companions, CPS
  demographics/geography, final + replicate + pandemic weights, three-level
  activity lexicon.
- Checksummed plain-SQL migrations.
- Atomic, rerunnable ETL (single-transaction rebuild, ~13.3M rows).
- Validation: 19 file-level checks, ~38 database checks (official-count
  regressions, diary arithmetic, weight rules, referential structure,
  independent cross-check against the BLS Activity Summary file).
- Test suites: unit (transformers, registry, lexicon, migrations),
  integration (disposable DB, end-to-end fixture load, failure atomicity),
  opt-in data-quality (full database).
- Documentation: architecture, database, lineage, methodology, sources.

Explicitly *not* built: analytics, API, UI, auth, caching, deployment
infrastructure.

## Phase 2 — Statistical / Analytical Layer ✅ (this repository)

Delivered (package `atus_pipeline.analytics`, `analytics_version 0.1`):

- Official estimators (User's Guide ch. 7.4): weighted mean minutes/day,
  daily participation rate, mean minutes per participant, participants per
  day — over serializable analysis specs (population filters, activity
  selectors with lexicon validation, years, diary-date windows).
- Replicate-weight variance (ch. 7.5 formula), confidence intervals, and
  covariance-correct group-difference SEs via per-replicate differences.
- Weight-scheme rules encoded once: TUFNWGTP (refuses 2020 explicitly) and
  TU20FWGT (2019/2020 windows, person-day denominators 312/313).
- Trends with explicit 2020 unavailability; comparisons; pooled multi-year
  estimates with correct day denominators.
- 24-benchmark validation against official BLS numbers
  (`atus validate-analytics`): both User's Guide worked examples (mean exact,
  SE exact), Appendix J subgroup persons/day, 17 published Table A-1 2025
  values, and 3 published 2020 pandemic-period values.
- CLI (`atus analyze estimate|trend|compare|run`) with JSON output; docs
  ([analytics.md](analytics.md), [examples.md](examples.md), ADRs 001–006);
  performance baseline (`scripts/benchmark_analytics.py`).

Deliberately deferred: episode-context analyses (location/time-of-day/who),
distributional statistics (medians/percentiles need their own design), module
files and module weights, materialized analytical tables (benchmarked as not
yet needed).

## Phase 3 — Backend / API ✅ (this repository)

Delivered (package `atus_pipeline.api`, contract version `v1`):

- FastAPI application (`atus api`) whose request bodies are the canonical
  Phase 2 analysis specs and whose responses are the engine result objects'
  `to_dict()` — no statistical logic in HTTP code, CLI and API share the one
  engine.
- Endpoints: analysis estimate/trend/compare; activity lexicon
  (list/search/detail/presets); population-filter metadata served from the
  engine's own dimension registry; `/meta` with API/analytics/data versions
  and capability discovery; `/health` + `/ready`.
- Structured error contract (one envelope, stable codes): 400 malformed JSON,
  422 validation/domain/unsupported (2020 guidance preserved verbatim),
  404 unknown activity resource, 503 database unavailable, sanitized 500s.
- Versioned deterministic result cache (canonical spec + analytics version +
  data release/ingestion run): pooled 22-year variance 11.0 s → 4 ms; errors
  never cached; graceful degradation without the cache.
- Tests: 53 DB-less contract/error/cache/CORS tests, 24 full-stack tests
  against the hand-computed fixture database, and 9 BLS benchmarks +
  published-value comparisons replayed through HTTP; performance baseline
  script. An adversarial review fleet (86 agents: 7 code reviewers, 3 live
  black-box probes, 2-vote verification) confirmed 25 findings, all fixed —
  including two analytics-semantics corrections that bumped the engine to
  version 0.2 (see docs/analytics.md version history).
- Docs: docs/api.md, OpenAPI with real verified examples,
  runnable request files in docs/examples/api/.

Deliberately deferred: authentication, rate limiting, async jobs, persistent
saved analyses, production deployment (Phase 5).

## Phase 4 — Interactive Application ✅ (this repository)

Delivered (**ATUS Explorer**, `frontend/` — React 19 + TypeScript strict +
Vite, client-side only):

- Analysis builder for estimates, trends, and two-group comparisons, driven
  end-to-end by the API's discovery endpoints: years/measures/weight schemes
  from `/meta`, population controls generated from `/population/metadata`,
  activity search/presets/hierarchy from `/activities*` (the 556-entry
  lexicon is fetched once and searched client-side — zero requests per
  keystroke). Nothing analytical is hard-coded; no statistics are computed
  in the frontend.
- Uncertainty-first result views: estimate ± SE with confidence interval,
  unweighted sample vs weighted population always distinguished, API
  warnings always visible, a methodology panel built from response metadata,
  and exact-value tables beside every formatted number.
- Hand-rolled SVG charts (ADR-010): trend lines with confidence bands and
  **explicit labeled gaps for unavailable years** (2020 is never connected,
  interpolated, or zeroed — tested at unit, component, and E2E level), and a
  dot-and-interval comparison chart; both keyboard-accessible with data-table
  alternatives.
- Shareable stateless analysis URLs (ADR-009): base64url-encoded canonical
  specs, post-run canonicalization to the API's own `spec`, deep links,
  refresh, and "adjust" back into the builder.
- API types generated from the backend's OpenAPI schema
  (`scripts/export_openapi.py` + `npm run generate:api-types`); one central
  typed client mapping the error envelope to distinct user presentations.
- Tests: 119 unit/component/integration tests (Vitest + Testing Library +
  MSW with captured real payloads, including a stale-response race test) and
  20 Playwright E2E tests (18 desktop + 2 mobile) against the real API on
  the hand-computed fixture database — full user journeys, share-link
  reproduction in a fresh browser context, the 2020 gap, error handling,
  axe accessibility checks, keyboard-only operation. CI runs
  typecheck/lint/format/unit/build and the E2E suite. An adversarial review
  fleet (96 agents: 8 code-dimension reviewers, 3 live browser probes
  driving the running app, 2-vote verification) confirmed 37 findings — all
  fixed, including three statistical-presentation defects (proportion
  differences shown in % instead of percentage points; an interpretation
  sentence claiming "all days of the week" under day-type filters; trend
  lines drawn across non-contiguous requested years).
- Docs: [frontend.md](frontend.md), ADRs 007–010; measured performance:
  110.8 kB gzip JS, home visually complete ~170 ms locally with exactly one
  API call.

Deliberately not built: exports (CSV/PNG/PDF; CSV and JSON exports came in
Phase 5), saved analyses/accounts, persistent history, dark mode, SSR/SEO
work.

## Phase 5 — Production Hardening & $0 Deployment

**Status: implemented; deployment pending owner steps (see
[deployment.md](deployment.md)).** Phase 5 is **not** complete. It is marked
complete only when every item of the definition of done (brief §21) holds;
the items still open are listed under "Remaining" below.

Phase 5 changes how the system runs, not what it computes:
`ANALYTICS_VERSION` stays `0.2`. How the parts fit together is in
[architecture.md](architecture.md).

### Implemented and verified locally

- **Audit** ([operations.md](operations.md)): the baseline at commit
  `387fc2d`, 71 findings and 14 recorded facts, each finding checked by two
  independent refuters; the timed free-tier measurements of the pre-Phase-5
  code are in
  [evidence/phase5/01-audit-timed.md](evidence/phase5/01-audit-timed.md).
- **Hosting decision** ([ADR-011](adr/ADR-011-hosting.md)): a judge panel
  scored five proposals. Chosen: Cloudflare Workers Free (SPA, static
  snapshot, and an `/api` Worker with a KV result cache and a Durable Object
  quota governor), Render Free (the API image, by digest) and Neon Free (the
  serving database). The runner-up, Vercel Hobby as the API host, is the
  documented fallback: a manual procedure whose preconditions are not
  implemented (ADR-011, amendments). The free-tier budget, failure modes,
  owner steps and configuration reference are in [deployment.md](deployment.md).
- **Reload without downtime and unique data versions**
  ([ADR-013](adr/ADR-013-data-releases-and-serving-db.md)): `atus load`
  builds the new data in a shadow schema and switches it in with one short
  transaction; `data_version` is globally unique and committed with the data
  (migration `0003`); release-specific values derive from the source
  registry, and a test runs a synthetic next release (2003–2026) through
  validation, the load, the engine and the API.
- **Serving database** (ADR-013): `atus build-serving-db` derives a ~0.6 GB
  database whose content hash repeats across builds, and a checksummed
  `pg_dump` release artifact with a manifest; `atus restore-serving-db`
  applies least-privilege roles (`scripts/ops/roles.sql`); full-database
  commands refuse a serving database. A restore drill into a fresh
  PostgreSQL 16 cluster as a non-superuser owner passed
  `atus validate-analytics` (24/24) and the opt-in benchmark tests. The
  timed drill (2026-10-21, alone on the machine) restored the release
  artifact with the production restore tool in 15.6 s of wall time, passed
  all 22 checks and measured `sum(pg_database_size)` at 649,903,180 B, 65%
  of the 1,000,000,000 B budget
  ([record, section 3](../loadtests/results/2026-10-21-emulation.md#3-a-restore-drill-timed)).
- **Statistical equivalence** (brief §9): criterion in ADR-013 (Decision 10)
  and `atus_pipeline.serving.equivalence`. `atus compare-serving-db` found
  all 161 specifications equivalent on the restore drill (ADR-013). The
  three-way HTTP replay passed 544 of 544 specifications and 84 of 84 GET
  requests after the one plan regression it found was fixed (commit
  `9afd1b4`; [evidence](evidence/phase5/equivalence/final-9afd1b4/README.md)).
- **Load and quota protection in the API**
  ([ADR-012](adr/ADR-012-load-and-quota-protection.md)): body cap, edge
  authentication, per-client token buckets, a daily cap, admission lanes,
  single-flight, per-transaction timeouts and a request deadline,
  stale-connection handling, and specific error codes with `Retry-After`.
  Cache hits, metadata and 4xx answers decided before the statistics query
  run no SQL while the in-process data snapshot is fresh. The limits were
  calibrated on the free-tier emulation and kept at their code defaults
  (ADR-012, "Calibration (§13)"); the post-deploy replay on Neon must still
  confirm them.
- **Load tests** (brief §13; [`loadtests/`](../loadtests/README.md)): k6
  scenarios, cold starts and a timed restore drill, run locally on
  2026-10-21 against the production-mode stack with the real serving release,
  the API at 0.1 CPU / 512 MB and the database at Neon's 0.25 CU floor and
  2 CU ceiling
  ([record](../loadtests/results/2026-10-21-emulation.md)). Uncached
  single-year estimates with replicate SEs took 0.99 s at p50 at 0.25 CU and
  79 ms at 2 CU. Pooled 22-year and 23-year trend worst cases took
  21.4–21.8 s and 2.03–2.12 s, and the F66 broad-selector compare by sex
  69–71 s at 0.25 CU. Throughput at 0.25 CU peaks at one analysis in
  flight; at 2 CU it plateaus at about 24 analyses per second from 2
  clients. The API's peak
  RSS was 76.1 MiB of 512 MB. Every protection default was kept, including
  the 85 s statement timeout, which at the floor is only 1.2 × the F66
  compare's p99 ([ADR-012, "Calibration (§13)"](adr/ADR-012-load-and-quota-protection.md#calibration-13)).
- **Edge** ([ADR-015](adr/ADR-015-edge-cache-snapshot-and-governor.md)): the
  Worker in `edge/`, the KV result cache, the static snapshot of all
  metadata and about 130 prewarmed analyses, and the quota governor.
- **Observability** ([ADR-014](adr/ADR-014-observability.md)): JSON access
  logs with truncated client addresses, `/metrics`, token-gated `/ready`, and
  monitor workflows that never touch the database.
- **Frontend** ([frontend.md](frontend.md)): API version check, cold-start
  notice and a 150 s retry budget, quota and paused states, CSV and JSON
  exports, a boot fallback, a same-origin production build under a strict
  CSP, and Playwright projects for Chromium, mobile, Firefox and WebKit.
- **Packaging**: a multi-stage, non-root API image installed from
  hash-locked requirements; the production-mode compose stack, the free-tier
  emulation profile and a PgBouncer profile; `scripts/smoke_stack.sh`. On
  2026-10-20 the stack smoke (plain and emulated), the edge probe and the
  Playwright suite through the local edge passed ([deployment.md](deployment.md),
  step 0). Firefox, which cannot launch natively on the development machine,
  passed on 2026-10-21 in vite-preview mode with its browser in the official
  Playwright Linux image (`scripts/e2e_in_container.sh`,
  [frontend.md](frontend.md#testing)); it has not run in stack mode yet.
- **CI/CD** (`.github/workflows/`): CI for backend, supply chain, frontend,
  edge, the image (gate, deployment tests, stack smoke, Trivy) and both E2E
  modes; a deploy workflow that deploys the tested image by digest behind the
  `production` environment's required reviewer; monitor workflows;
  Dependabot.
- **Short share links**: deferred by decision
  ([ADR-016](adr/ADR-016-short-share-links.md)).

### Remaining for the definition of done

Each item must be done or verified before Phase 5 is marked complete and
`v1.0.0` is tagged.

The load tests and the timed restore drill are done (above). The
documentation now carries their numbers, ADR-008 is amended for the Phase 5
data version, and [deployment.md](deployment.md) matches the ADR-011
amendments (the Vercel fallback is a documented manual procedure, not
implemented, and no Worker var selects the API origin).

1. **CI on GitHub**: the Phase 5 workflows have not yet run on GitHub-hosted
   runners; their steps were rehearsed locally. That first run is also the
   first native-Linux and first stack-mode Firefox E2E result.
2. **Owner account steps** ([deployment.md](deployment.md#owner-account-setup-19)):
   Cloudflare, Render, Neon and GitHub on free plans; publish the serving-DB
   release artifact (`releases/current.json` says `unpublished`); restore it
   into Neon; run the first Worker deploy; approve the first deploy run.
3. **Prove the deployment** (deployment.md, step 8): curl and Playwright
   against the public URL, including the security headers and CSP on the
   real origins, the origin lock, and live recomputation of snapshot entries
   under §9. Only then does the README show a live URL.
4. **Post-deploy measurement** (deployment.md, steps 9 and 10): the
   quota-budgeted replay (`scripts/replay_deployed.py`: the BLS benchmarks
   and §9 spot checks against the deployed URL), the unknowns listed in
   ADR-011, and the week-1 calibration of the Neon compute and timeouts. The
   replay must confirm the emulation's calibration on Neon, F66 included
   (ADR-012, "What stays open").
5. **Monitors live**: the scheduled workflows stay skipped until the
   repository variable `PUBLIC_BASE_URL` is set, after the first deploy has
   been verified (deployment.md, owner step 4.1 and step 8).
