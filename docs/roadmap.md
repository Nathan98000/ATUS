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

## Phase 2 — Statistical / Analytical Layer

The natural next steps, in rough order of leverage:

1. **Estimation functions** implementing User's Guide ch. 7: weighted means,
   totals, participation rates ("percent doing X on a given day"), average
   minutes per day / per participant — with the weight-selection rules from
   [methodology.md](methodology.md) encoded once, centrally.
2. **Variance estimation** via the stored 160 replicate weights (successive
   difference replication), so every estimate can carry a standard error.
3. **Analytical views / materialized summaries** (e.g. a per-respondent
   activity summary equivalent to `atussum`, respondent + CPS demographic
   join), derived from — never replacing — the canonical tables.
4. **Subgroup and trend machinery**: demographic cuts, year-over-year series,
   2020 partial-year handling baked in.
5. Optional scope growth: eldercare roster, module files (each with its own
   weights).

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
