# ADR-006: Where computation happens (SQL vs Python)

**Status:** accepted (Phase 2) · **Date:** 2026-09-09

## Decision

PostgreSQL does selection, joins, and **all weighted summation** (including
the 160 replicate-weight sums, as ~483 aggregate expressions per year);
Python turns the returned sufficient statistics into estimates, variances,
and result objects. No dataframe layer; respondent-level data never leaves
the database.

Precision split: full-sample sums aggregate in `numeric` (exact — the User's
Guide worked example reproduces to the last digit), replicate sums in
`float8` (relative error ~1e-12 against the ~3 significant digits an SE
carries), keeping the wide replicate aggregation fast.

## Rationale

Moving 259k × 161 weight values per analysis into Python would cost hundreds
of MB of transfer to compute 161 ratios; the database computes those sums in
one indexed scan. Conversely, the estimator/variance algebra, error handling,
and result assembly are clearer, unit-testable, and reusable in Python.
Dynamic SQL is limited to fixed internal vocabularies (column names generated
from the schema's own replicate naming, validated activity-code lists);
every user-influenced value is a bound parameter.

## Consequences

The heaviest query (pooled all-years with variance) reads the full replicate
table once (~11 s baseline). That cost is documented, acceptable for Phase 2,
and localized in one query builder if later phases need materialization.
