# ADR-001: Analytical layer architecture

**Status:** accepted (Phase 2) · **Date:** 2026-09-09

## Decision

The analytical layer is a package (`atus_pipeline.analytics`) with one module
per concern, composed by an engine:

    AnalysisSpec (data) → ActivityResolver → PopulationFilter SQL
        → sufficient-statistics query → estimators → replicate variance
        → result objects (+ metadata, warnings)

`AnalysisEngine.estimate/trend/compare` are the only entry points; the CLI is
a thin parser/formatter over them, and specs/results are serializable
dataclasses.

## Rationale

- Phase 3's API must call the engine without rewriting anything, so the
  interface is deterministic, serializable, and HTTP-free from day one.
- Statistical review requires that selection (population), measurement
  (activity → per-respondent value), and estimation (weights, variance) be
  auditable separately; each lives in its own module with its own tests.
- "Analysis as data" (specs with `to_dict`/`from_dict`) makes analyses
  reproducible, storable, and diffable — the foundation for API request
  bodies later.

## Consequences

Adding a measure touches exactly: `Measure` enum, `estimators.py`, docs, and
tests. Adding a filter dimension touches `PopulationFilter`, `population.py`,
docs, and tests. Nothing statistical can hide in the CLI.
