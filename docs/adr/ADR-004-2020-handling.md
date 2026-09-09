# ADR-004: 2020 statistical handling

**Status:** accepted (Phase 2) · **Date:** 2026-09-09

## Decision

Two explicit weight schemes, no automatic switching:

- `multiyear` (default): TUFNWGTP + its replicates. **Hard-refuses** any
  request whose years include 2020 (`UnsupportedAnalysisError` explains why
  and what to do). In a trend, 2020 is returned as an explicit unavailable
  point with the reason.
- `pandemic` (opt-in): TU20FWGT + the 2019–20 pandemic replicate weights.
  Valid only for years ⊆ {2019, 2020}. Person-day denominators are 312 (2019)
  and 313 (2020) — the weights are calibrated to the collection-comparable
  windows (Jan 1–Mar 17, May 10–Dec 31), verified empirically: 2019 diaries
  inside the excluded window carry zero TU20FWGT, and Σw/312 equals the
  multiyear population. Diary-date windows within those bounds are supported,
  which is how BLS itself published 2020 (May 10–Dec 31); the benchmark suite
  reproduces those published values.

## Rationale

BLS: 2020 has no TUFNWGTP (schema-enforced NULL in Phase 1), TU20FWGT
represents only the collected windows, and "annual estimates with the 2020
data are not possible." Auto-substituting the pandemic weight would silently
change the estimand (a 313-day partial period vs a year); silently dropping
2020 from a pooled request would misdescribe the period. Both violate the
no-silent-fallback rule, so the user must say what they mean, and the errors
teach them how.

## Consequences

A 2003–2025 pooled estimate is impossible by design (it is also impossible
methodologically); the supported longitudinal form is the trend with an
explicit 2020 gap, or separate multiyear + pandemic estimates. Pooling 2019+
2020 under the pandemic scheme is supported and warned as window-limited.
