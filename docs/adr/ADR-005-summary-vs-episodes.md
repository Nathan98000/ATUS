# ADR-005: Activity Summary file vs episode aggregation

**Status:** accepted (Phase 2) · **Date:** 2026-09-09

## Decision

The engine's canonical pathway for per-respondent activity totals is
aggregating Activity-file episodes (`SUM(duration_minutes)` per respondent
per selected code set), not the BLS Activity Summary file — which Phase 1
deliberately stages but does not load.

## Rationale

The User's Guide presents the two routes as equivalent ("Both methods yield
the same result", ch. 7.4) and recommends the summary file only as the
*easier* one for flat-file users. In a relational canonical model the
episode aggregation is a one-line GROUP BY, and keeping a single source of
truth avoids a duplicated representation that could drift. The equivalence is
not assumed but continuously demonstrated: Phase 1's validation compares
recomputed totals against the real summary file (0 of 2,000 sampled cases
disagree), and the benchmark suite reproduces the guide's summary-file worked
example (2007 TV: sums exact to the digit) through the episode pathway.

Episodes also keep the door open for analyses the summary file cannot
support at all — location, time-of-day, companionship — without a second
pathway appearing later.

## Consequences

Composite categories cost an indexed scan over episodes rather than a column
sum; measured cost is sub-second per year (see the performance baseline). If
Phase 5 profiling ever justifies a materialized per-respondent summary, it
would be a derived structure regenerated from episodes — never a second
source of truth.
