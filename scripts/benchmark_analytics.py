#!/usr/bin/env python3
"""Performance baseline for representative analytical queries.

Not an optimization tool — a record of what typical analyses cost on the full
database (2003-2025 for release 0325), so later phases can detect regressions
and optimize from evidence. The years come from the loaded data, so the cases
follow a new release automatically. Run from the repo root with the database
loaded:

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

sleep = ActivitySelector.preset("sleep")
leisure = ActivitySelector.preset("leisure_and_sports_bls_table")


def cases(years: tuple[int, ...]) -> list[tuple[str, str, AnalysisSpec]]:
    """The benchmark cases for the loaded survey years."""
    latest = max(years)
    pooled = tuple(y for y in years if y != 2020)  # 2020 has no multi-year weight
    return [
        ("one activity, one year, point only", "estimate",
         AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (latest,), variance="none")),
        ("one activity, one year, with replicate SE", "estimate",
         AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (latest,))),
        ("composite category (leisure), one year, with SE", "estimate",
         AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, leisure, (latest,))),
        ("demographic subgroup (women 25-54, employed), with SE", "estimate",
         AnalysisSpec(
             Measure.AVERAGE_MINUTES_PER_DAY, leisure, (latest,),
             PopulationFilter(sex="female", age_min=25, age_max=54,
                              employment_status="employed"),
         )),
        (f"pooled {len(pooled)} years (all except 2020), with SE", "estimate",
         AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, pooled)),
        (f"{len(years)}-year trend with per-year SEs", "trend",
         AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, years)),
    ]


def main() -> None:
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        years = tuple(
            row[0] for row in
            conn.execute("SELECT DISTINCT data_year FROM atus.respondents ORDER BY 1")
        )
        engine = AnalysisEngine(conn)
        # warm the connection / catalog caches once
        engine.estimate(
            AnalysisSpec(Measure.PARTICIPATION_RATE, sleep, (years[-2],), variance="none")
        )
        print(f"{'benchmark':<55} {'seconds':>8}")
        for name, mode, spec in cases(years):
            start = time.perf_counter()
            if mode == "estimate":
                engine.estimate(spec)
            else:
                engine.trend(spec)
            elapsed = time.perf_counter() - start
            print(f"{name:<55} {elapsed:>8.2f}")


if __name__ == "__main__":
    main()
