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

## Phase 3 — Backend / API

Read-only HTTP API over the analytical layer (likely FastAPI): estimate
endpoints with subgroup/year/activity parameters, activity-hierarchy
browsing, caching of expensive estimates.

## Phase 4 — Interactive Application

Web UI for exploring time use: activity drill-down through the lexicon
hierarchy, demographic comparisons, trends, uncertainty display.

## Phase 5 — Hardening & Deployment

CI-run integration against a seeded database, performance work driven by real
query patterns, deployment packaging, monitoring, release automation for new
ATUS years.
