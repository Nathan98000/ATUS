"""Official-BLS benchmark suite for the analytical engine (opt-in).

Runs the same benchmarks as `atus validate-analytics` against the fully
loaded database:

    ATUS_DATA_QUALITY=1 pytest tests/data_quality
"""

from __future__ import annotations

import os

import psycopg
import pytest

from atus_pipeline.config import load_settings

pytestmark = pytest.mark.data_quality

if not os.environ.get("ATUS_DATA_QUALITY"):
    pytest.skip(
        "set ATUS_DATA_QUALITY=1 to run analytics benchmarks against the full database",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def conn():
    settings = load_settings()
    try:
        connection = psycopg.connect(settings.database_url)
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")
    yield connection
    connection.close()


def test_engine_reproduces_official_bls_estimates(conn):
    from atus_pipeline.analytics.benchmarks import run_benchmarks

    results = run_benchmarks(conn)
    failures = [r for r in results if not r.passed]
    details = "\n".join(f"{r.name}: {r.observed}" for r in failures)
    assert not failures, f"benchmark failures:\n{details}"
    assert len(results) >= 20  # the suite is meant to stay substantial


def test_full_trend_runs_and_flags_2020(conn):
    """A 2003-2025 trend on the real data: every year estimable except 2020,
    which must be explicitly unavailable under the multi-year weight."""
    from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure

    engine = AnalysisEngine(conn)
    result = engine.trend(
        AnalysisSpec(
            measure=Measure.AVERAGE_MINUTES_PER_DAY,
            activity=ActivitySelector.preset("sleep"),
            years=tuple(range(2003, 2026)),
        )
    )
    by_year = {p.year: p for p in result.points}
    assert len(by_year) == 23
    assert by_year[2020].estimate is None and "TUFNWGTP" in by_year[2020].unavailable_reason
    for year, point in by_year.items():
        if year == 2020:
            continue
        assert point.estimate is not None, year
        # plausibility: national average sleep is in the 8-10 h/day range
        assert 480 <= point.estimate.value <= 600, (year, point.estimate.value)
        assert point.estimate.standard_error < 5.0  # minutes; full-population SE
