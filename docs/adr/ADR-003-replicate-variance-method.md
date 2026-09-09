# ADR-003: Replicate-weight variance method

**Status:** accepted (Phase 2) · **Date:** 2026-09-09

## Decision

Standard errors use exclusively the official ATUS replicate method (User's
Guide ch. 7.5): recompute the estimator under each of the 160 replicate
weights and apply Var = (4/160)·Σ(Ŷᵢ−Ŷ₀)². No Taylor linearization, no
design-effect shortcuts, no unweighted SEs — and no silent fallback when
replicates cannot support a request (zero replicate denominators raise
`InsufficientDataError`).

Comparison SEs use per-replicate differences (Ŷᵃᵢ−Ŷᵇᵢ), which carries the
covariance between overlapping-sample subgroup estimates through the same
official machinery. Confidence intervals are the normal approximation with a
documented 95% default; rate CIs are truncated to [0,1] with a warning.

## Rationale

The replicate weights *are* the BLS-published variance method for this
survey; anything else would be a textbook approximation of unknown accuracy
for this design. The implementation is anchored to the guide's own worked
example (2007 TV watching, SE = 0.0293 hours — reproduced exactly in the
benchmark suite) and to a hand-computed toy dataset in unit/integration
tests, so the formula cannot drift unnoticed.

## Consequences

Variance requires the replicate join (~160 extra aggregates); `variance="none"`
exists as an explicit, visibly-labeled fast path. Significance testing is not
reported at all — Phase 2 provides estimates, SEs, CIs, and covariance-correct
difference SEs, and leaves inference framing to consumers.
