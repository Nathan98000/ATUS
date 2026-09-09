# ADR-002: Weighted estimator design (sufficient statistics at the grain boundary)

**Status:** accepted (Phase 2) · **Date:** 2026-09-09

## Decision

Every supported estimator is expressed as a ratio (or day-scaling) of three
weighted sums per period — Σw, Σw·x, Σw·p — computed at the **respondent**
grain. Episode minutes are aggregated to one value per respondent *inside* the
query, before the respondent-level weight is joined exactly once. Python never
sees respondent rows; it sees sufficient statistics.

## Rationale

- **Grain safety by construction.** The classic ATUS error — joining
  respondent weights to episode rows and averaging, which counts a
  30-episode respondent 30 times — is impossible in this shape: the join key
  for weights only exists after episodes have collapsed to respondent totals.
  A fixture respondent with ten episodes pins this in integration tests.
- **One query shape serves every measure** (mean, rate, per-participant,
  persons/day) and pooling is just summing the statistics across years, which
  is exactly how ratio estimators over person-day weights combine.
- **Efficiency**: only ~483 aggregate values per year leave the database, so
  variance for the full population costs one scan, not a 40M-value transfer.

## Consequences

Statistics that are not functions of these sums (medians, percentiles) are
deliberately out of scope until they can be specified rigorously
(distributional statistics over person-day weights need their own design);
the engine's scope is the User's Guide ch. 7.4 estimator family.
