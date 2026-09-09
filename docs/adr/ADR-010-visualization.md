# ADR-010: Hand-rolled SVG charts instead of a charting library

**Status:** accepted (Phase 4)

## Context

The product needs exactly two visualizations: a yearly trend line with
confidence bands and explicitly unavailable years, and a two-group
dot-and-interval comparison. Three behaviors are *correctness requirements*,
not styling preferences:

1. an unavailable year (2020 under the multi-year weights) must be a
   labeled gap — no connecting line, no interpolation, never zero;
2. confidence intervals must be first-class (bands, caps, tooltips);
3. every data point must be reachable by keyboard with a screen-reader
   announcement, and each chart needs a data-table alternative.

## Decision

Build the two charts as plain React SVG components (`TrendChart`,
`ComparisonChart`, ~480 lines total including interaction), with a tiny
scale/ticks helper (`chartScale.ts`, no statistics — presentation math
only).

Rejected: Recharts/visx/etc. A library would make the three requirements
*harder*: gap rendering means fighting `connectNulls` defaults and custom
segment logic anyway; CI bands and interval caps are custom layers; keyboard
interaction and live-region announcements are not provided; and the
dependency adds ~100 kB+ gzip for two chart types. With owned SVG, the gap
behavior is directly testable (unit tests assert one polyline per available
segment and no path crossing the gap; E2E asserts against the real API).

Design choices: the y-axis does not force zero (year-to-year variation is
the subject; the axis is fully labeled), unavailable years get a shaded band
labeled "<year> — no estimate" plus the API's reason in text below the
chart and in the table, and hover/keyboard share one tooltip and one
aria-live description.

## Consequences

- The 2020-gap contract is enforced by tests at three levels (pure function,
  component render, live E2E).
- Bundle stays small (whole app: 110.8 kB gzip, one chunk — no chart-library
  code splitting needed).
- New chart types are real work rather than a config object; acceptable —
  Phase 4 needs two, and the scale helpers are reusable.
