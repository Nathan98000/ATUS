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
- Tests: 93 unit/component/integration tests (Vitest + Testing Library +
  MSW with captured real payloads, including a stale-response race test) and
  20 Playwright E2E tests (18 desktop + 2 mobile) against the real API on
  the hand-computed fixture database — full user journeys, share-link
  reproduction in a fresh browser context, the 2020 gap, error handling,
  axe accessibility checks, keyboard-only operation. CI runs
  typecheck/lint/format/unit/build and the E2E suite.
- Docs: [frontend.md](frontend.md), ADRs 007–010; measured performance:
  110.9 kB gzip JS, home visually complete ~170 ms locally with exactly one
  API call.

Deliberately not built: exports (CSV/PNG/PDF), saved analyses/accounts,
persistent history, dark mode, SSR/SEO work.

## Phase 5 — Hardening & Deployment

Deployment packaging for the API + static frontend, traffic controls (rate
limiting) for public exposure, monitoring, performance work driven by real
query patterns, release automation for new ATUS years, and frontend niceties
deferred from Phase 4 (exports, short share links).
