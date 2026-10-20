"""`atus validate-analytics` robustness (audit F22).

One failing benchmark must not abort the others, results must be printed
even when recording them fails, and under a read-only role the command must
say clearly that nothing was recorded. The fixture database cannot reproduce
BLS's published values (most benchmarks fail on it, as expected); these
tests are about the mechanics, not the numbers.
"""

from __future__ import annotations

from itertools import count
from urllib.parse import quote

import psycopg
import pytest
from click.testing import CliRunner

from atus_pipeline.analytics import benchmarks
from atus_pipeline.analytics.benchmarks import Benchmark, run_benchmarks
from atus_pipeline.analytics.engine import AnalysisEngine
from atus_pipeline.analytics.spec import ActivitySelector, AnalysisSpec, Measure
from atus_pipeline.cli import cli
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings
from tests.integration.conftest import create_scratch_database, drop_scratch_database

pytestmark = pytest.mark.integration

READ_ONLY = "?options=" + quote("-c default_transaction_read_only=on")


@pytest.fixture(scope="module")
def db_url(test_db_url, tmp_path_factory):
    url = create_scratch_database(test_db_url, "benchmarks")
    settings = analytics_settings(tmp_path_factory.mktemp("benchmarks"), url)
    with psycopg.connect(url) as conn:
        load_all(conn, settings)
    yield url
    drop_scratch_database(url)


def recorded(url: str) -> int:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT count(*) FROM atus.validation_results").fetchone()[0]


def invoke(url: str, *args: str):
    return CliRunner().invoke(
        cli, ["validate-analytics", *args],
        env={"ATUS_DATABASE_URL": url, "ATUS_LOG_LEVEL": "WARNING"},
    )


def test_a_database_error_fails_one_benchmark_not_the_rest(db_url, monkeypatch):
    calls = count()
    real_estimate = AnalysisEngine.estimate

    def estimate(self, spec):
        if next(calls) == 0:
            self._conn.execute("SELECT 1/0")  # a DB error inside the first benchmark
        return real_estimate(self, spec)

    monkeypatch.setattr(AnalysisEngine, "estimate", estimate)
    with psycopg.connect(db_url) as conn:
        results = run_benchmarks(conn, persist=False)
        assert conn.execute("SELECT 1").fetchone() == (1,)  # the session is still usable

    assert len(results) == len(benchmarks.build_benchmarks())
    assert results[0].observed.startswith("DivisionByZero")
    assert not any("InFailedSqlTransaction" in r.observed for r in results)
    # benchmarks the fixture can answer (2020 pandemic window) still computed
    pandemic = [r for r in results if r.name.startswith("analytics: NR-2020")]
    assert pandemic and all(r.observed[0].isdigit() for r in pandemic), pandemic


def test_results_are_recorded_by_default(db_url):
    before = recorded(db_url)
    with psycopg.connect(db_url) as conn:
        results = run_benchmarks(conn)
    assert recorded(db_url) == before + len(results)


def test_read_only_role_prints_every_result_and_reports_the_recording_failure(db_url):
    before = recorded(db_url)
    result = invoke(db_url + READ_ONLY)
    assert result.exit_code == 1
    for benchmark in benchmarks.build_benchmarks():
        assert benchmark.name in result.output
    assert "passed," in result.output                       # the summary line
    assert "Results were NOT recorded" in result.stderr
    assert "ReadOnlySqlTransaction" in result.stderr
    assert "--no-persist" in result.stderr
    assert "Traceback" not in result.output
    assert recorded(db_url) == before


def test_no_persist_only_prints(db_url):
    before = recorded(db_url)
    result = invoke(db_url + READ_ONLY, "--no-persist")
    assert result.exit_code == 1               # the fixture cannot match BLS values
    assert "NOT recorded" not in result.output
    assert "passed," in result.output
    assert recorded(db_url) == before


def test_passing_suite_records_and_exits_zero(db_url, monkeypatch):
    sleep_2023 = Benchmark(
        name="fixture: sleep 2023, mean minutes/day",
        source="hand computation (tests/integration/analytics_dataset.py)",
        spec=AnalysisSpec(
            Measure.AVERAGE_MINUTES_PER_DAY, ActivitySelector(("010101",)), (2023,),
            variance="none",
        ),
        extract=lambda result: result.estimate.value,
        expected=75.0,
        tolerance=1e-9,
    )
    monkeypatch.setattr(benchmarks, "build_benchmarks", lambda: (sleep_2023,))
    before = recorded(db_url)
    result = invoke(db_url)
    assert result.exit_code == 0, result.output
    assert "1 passed, 0 failed" in result.output
    assert "Recorded 1 results in atus.validation_results." in result.output
    assert recorded(db_url) == before + 1
