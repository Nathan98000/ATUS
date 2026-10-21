"""Query-plan regression on the full database (opt-in).

The Phase 5 year pushdown let PostgreSQL choose a nested loop that re-ran the
activity aggregate once per respondent for specifications whose correlated
respondent filters (year, pandemic weight > 0, diary dates) it misestimates
by two to three orders of magnitude; two of them never finished on the full
database (docs/evidence/phase5/equivalence/README.md). The pushed-down CTE is
now materialized. These specifications, the hazard set found by the
equivalence run's EXPLAIN survey, must scan ``atus.activities`` only inside
the materialized CTE, finish within the production statement budget, and
return the Phase 2-4 query's sufficient statistics (its rendering without the
pushdown, held to the text 387fc2d rendered by
tests/unit/analytics/test_year_pushdown_sql.py).

    ATUS_DATA_QUALITY=1 pytest tests/data_quality/test_query_plans.py
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.data_quality

if not os.environ.get("ATUS_DATA_QUALITY"):
    pytest.skip(
        "set ATUS_DATA_QUALITY=1 to check query plans against the full database",
        allow_module_level=True,
    )

import psycopg  # noqa: E402

from atus_pipeline.analytics.activities import ActivityResolver  # noqa: E402
from atus_pipeline.api.schemas.analysis import EstimateRequest  # noqa: E402
from atus_pipeline.config import load_settings  # noqa: E402
from atus_pipeline.serving.equivalence import (  # noqa: E402
    compare_statistics,
    rendered_queries,
)

# Production statement budget (ATUS_API_STATEMENT_TIMEOUT_MS default).
STATEMENT_TIMEOUT_MS = 85_000

def _pandemic(measure: str, activity: dict, year: int, *, confidence: float | None = None,
              **population: str) -> dict:
    body = {"measure": measure, "activity": activity, "years": [year], "weights": "pandemic",
            "population": population}
    if confidence is not None:
        body["confidence_level"] = confidence
    return body


HAZARD_SPECS = {
    "population-071": _pandemic(
        "participants_per_day", {"preset": "eating_and_drinking"}, 2019,
        diary_date_min="2019-05-10", diary_date_max="2019-12-31"),
    "measures-weights-043": _pandemic(
        "participants_per_day", {"preset": "watching_tv"}, 2020, confidence=0.8,
        diary_date_min="2020-05-10", diary_date_max="2020-12-31"),
    "measures-weights-059": _pandemic(
        "average_minutes_per_day", {"preset": "work_main_and_other_jobs"}, 2020,
        diary_date_min="2020-03-01", diary_date_max="2020-06-30"),
    "population-074": _pandemic(
        "participants_per_day", {"include": ["1301"]}, 2020,
        diary_date_min="2020-03-01", diary_date_max="2020-06-30"),
    "measures-weights-042": _pandemic(
        "average_minutes_per_participant", {"include": ["1203"]}, 2020, confidence=0.99,
        diary_date_min="2020-01-01", diary_date_max="2020-03-17"),
    "errors-weird-024": _pandemic(
        "participants_per_day", {"preset": "housework"}, 2019,
        day_type="weekend", diary_date_min="2019-03-01", diary_date_max="2019-06-30"),
    "measures-weights-057": _pandemic(
        "participants_per_day", {"include": ["08"]}, 2020,
        diary_date_min="2020-05-10", state_fips="06"),
}


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(load_settings().database_url, autocommit=True) as connection:
        connection.execute("SET default_transaction_read_only = on")
        connection.execute("SET jit = off")  # as the API runs
        connection.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        yield connection


@pytest.fixture(scope="module")
def rendered(conn):
    resolver = ActivityResolver(conn)
    years = tuple(r[0] for r in conn.execute("SELECT DISTINCT data_year FROM atus.respondents"))
    out = {}
    for name, body in HAZARD_SPECS.items():
        spec = EstimateRequest(**body).to_spec()
        (legacy, current, params), = rendered_queries(conn, resolver, spec, years_loaded=years)
        out[name] = (legacy, current, params)
    return out


def _activity_scans_outside_the_cte(node: dict, inside: bool = False) -> int:
    inside = inside or node.get("Subplan Name") == "CTE activity_minutes"
    own = int(node.get("Relation Name") == "activities" and not inside)
    return own + sum(_activity_scans_outside_the_cte(child, inside)
                     for child in node.get("Plans", ()))


@pytest.mark.parametrize("name", sorted(HAZARD_SPECS))
def test_activities_are_aggregated_once_in_the_materialized_cte(conn, rendered, name):
    _, current, params = rendered[name]
    plan = conn.execute(f"EXPLAIN (FORMAT JSON) {current}", params).fetchone()[0][0]["Plan"]
    assert '"CTE activity_minutes"' in str(plan).replace("'", '"')
    assert _activity_scans_outside_the_cte(plan) == 0


@pytest.mark.parametrize("name", sorted(HAZARD_SPECS))
def test_hazard_specs_answer_within_budget_and_match_the_phase4_query(conn, rendered, name):
    legacy, current, params = rendered[name]
    new = conn.execute(current, params).fetchall()   # QueryCanceled past the budget
    old = conn.execute(legacy, params).fetchall()
    result = compare_statistics(old, new)
    assert result.equivalent, result.mismatches[:3]
