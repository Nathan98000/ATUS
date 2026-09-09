"""Data-quality tests against the fully loaded ATUS database.

These tests need the real database (all 2003-25 data loaded) and staged source
files, so they are opt-in:

    ATUS_DATA_QUALITY=1 pytest tests/data_quality

They run the same validation suite as ``atus validate-db`` plus a
demonstration that the schema supports weighted estimation (ATUS User's Guide,
chapter 7).
"""

from __future__ import annotations

import os

import psycopg
import pytest

from atus_pipeline.config import load_settings
from atus_pipeline.validation.db_checks import run_db_checks

pytestmark = pytest.mark.data_quality

if not os.environ.get("ATUS_DATA_QUALITY"):
    pytest.skip(
        "set ATUS_DATA_QUALITY=1 to run checks against the fully loaded database",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def settings():
    return load_settings()


@pytest.fixture(scope="module")
def conn(settings):
    try:
        connection = psycopg.connect(settings.database_url)
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unavailable: {exc}")
    yield connection
    connection.close()


def test_validation_suite_has_no_errors(conn, settings):
    results = run_db_checks(conn, settings)
    failures = [r for r in results if not r.passed and r.severity == "error"]
    details = "\n".join(f"{r.name}: {r.observed} ({r.detail})" for r in failures)
    assert not failures, f"validation errors:\n{details}"


def test_weighted_sleep_estimate_is_plausible(conn):
    """Weighted average daily sleep, computed per the ATUS estimation formula
    (User's Guide ch. 7): sum(w * minutes) / sum(w) over a non-2020 year.

    This is a plausibility band, not a regression against a published value:
    BLS publishes roughly 8.5-9.5 hours/day of sleep across recent years, so
    anything far outside 420-660 minutes means weights or durations are broken.
    """
    minutes = conn.execute(
        """
        SELECT sum(r.final_weight * s.sleep_minutes) / sum(r.final_weight)
        FROM atus.respondents r
        JOIN (
            SELECT tucaseid, sum(duration_minutes) AS sleep_minutes
            FROM atus.activities
            WHERE tier2_code = '0101'
            GROUP BY tucaseid
        ) s USING (tucaseid)
        WHERE r.data_year = 2024
        """
    ).fetchone()[0]
    assert 420 <= float(minutes) <= 660, f"weighted mean sleep {minutes} min/day"


def test_2020_partial_year_has_two_windows(conn):
    """2020 diary dates must form exactly two windows around the collection gap."""
    pre, post = conn.execute(
        """
        SELECT
            count(*) FILTER (WHERE diary_date < '2020-03-18'),
            count(*) FILTER (WHERE diary_date > '2020-05-09')
        FROM atus.respondents WHERE data_year = 2020
        """
    ).fetchone()
    total = conn.execute(
        "SELECT count(*) FROM atus.respondents WHERE data_year = 2020"
    ).fetchone()[0]
    assert pre > 0 and post > 0
    assert pre + post == total
