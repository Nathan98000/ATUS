#!/usr/bin/env python3
"""Performance baseline for representative analytical queries.

Not an optimization tool — a record of what typical analyses cost on the full
2003-25 database, so later phases can detect regressions and optimize from
evidence. Run from the repo root with the database loaded:

    python scripts/benchmark_analytics.py
"""

from __future__ import annotations

import time

import psycopg

from atus_pipeline.analytics import (
    ActivitySelector,
    AnalysisEngine,
    AnalysisSpec,
    Measure,
    PopulationFilter,
)
from atus_pipeline.config import load_settings

CASES: list[tuple[str, str, AnalysisSpec]] = []


def case(name: str, mode: str, spec: AnalysisSpec) -> None:
    CASES.append((name, mode, spec))


sleep = ActivitySelector.preset("sleep")
leisure = ActivitySelector.preset("leisure_and_sports_bls_table")

case("one activity, one year, point only", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2025,), variance="none"))
case("one activity, one year, with replicate SE", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2025,)))
case("composite category (leisure), one year, with SE", "estimate",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, leisure, (2025,)))
case("demographic subgroup (women 25-54, employed), with SE", "estimate",
     AnalysisSpec(
         Measure.AVERAGE_MINUTES_PER_DAY, leisure, (2025,),
         PopulationFilter(sex="female", age_min=25, age_max=54,
                          employment_status="employed"),
     ))
case("pooled 22 years (all except 2020), with SE", "estimate",
     AnalysisSpec(
         Measure.AVERAGE_MINUTES_PER_DAY, sleep,
         tuple(y for y in range(2003, 2026) if y != 2020),
     ))
case("22-year trend with per-year SEs", "trend",
     AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, tuple(range(2003, 2026))))


def main() -> None:
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        engine = AnalysisEngine(conn)
        # warm the connection / catalog caches once
        engine.estimate(
            AnalysisSpec(Measure.PARTICIPATION_RATE, sleep, (2024,), variance="none")
        )
        print(f"{'benchmark':<55} {'seconds':>8}")
        for name, mode, spec in CASES:
            start = time.perf_counter()
            if mode == "estimate":
                engine.estimate(spec)
            else:
                engine.trend(spec)
            elapsed = time.perf_counter() - start
            print(f"{name:<55} {elapsed:>8.2f}")


if __name__ == "__main__":
    main()
