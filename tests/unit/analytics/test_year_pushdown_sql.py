"""Year pushdown (F39): the activity CTE is restricted to the requested
years' respondents and materialized, and those are the only changes to the
Phase 2-4 SQL. The
equivalence of the two queries is proven against a database in
tests/integration/test_year_pushdown.py."""

from __future__ import annotations

from atus_pipeline.analytics.activities import ResolvedActivity
from atus_pipeline.analytics.population import build_population_sql
from atus_pipeline.analytics.queries import build_query
from atus_pipeline.analytics.spec import ActivitySelector, PopulationFilter
from atus_pipeline.analytics.weights import MULTIYEAR, PANDEMIC

ACTIVITY = ResolvedActivity(
    selector=ActivitySelector(("010101",)), label="Sleeping", leaf_codes=("010101",),
    sql_condition="a.activity_code = ANY(%(activity_codes)s)",
    sql_params={"activity_codes": ["010101"]},
)
PUSHDOWN = (
    "      AND a.tucaseid IN (SELECT yr.tucaseid FROM atus.respondents yr"
    " WHERE yr.data_year = ANY(%(years)s))\n"
)


def _render(push_down: bool, **kwargs) -> str:
    return build_query(
        activity=ACTIVITY,
        population=build_population_sql(kwargs.pop("population", PopulationFilter())),
        scheme=kwargs.pop("scheme", MULTIYEAR),
        with_replicates=kwargs.pop("with_replicates", True),
        push_down_years=push_down,
    )


def test_the_activity_cte_is_restricted_to_the_requested_years():
    sql = _render(True)
    cte = sql[sql.index("WITH activity_minutes AS"):sql.index("base AS")]
    assert PUSHDOWN in cte
    assert cte.index(PUSHDOWN) < cte.index("GROUP BY a.tucaseid")


def test_the_pushdown_is_the_only_difference_from_the_phase_4_query():
    for kwargs in (
        {},
        {"with_replicates": False},
        {"scheme": PANDEMIC},
        {"population": PopulationFilter(sex="female", region=3, day_type="weekday")},
    ):
        new, old = _render(True, **dict(kwargs)), _render(False, **dict(kwargs))
        assert PUSHDOWN not in old
        assert "MATERIALIZED" not in old
        assert new.replace(PUSHDOWN, "").replace("AS MATERIALIZED (", "AS (") == old


def test_the_pushed_down_cte_is_materialized():
    """Correlated respondent filters can make the planner expect a dozen rows
    and re-run an inlined aggregate per respondent (the §9 equivalence run);
    materializing computes it once."""
    assert "WITH activity_minutes AS MATERIALIZED (" in _render(True)
    assert "WITH activity_minutes AS (" in _render(False)


def test_the_pushdown_reuses_the_years_parameter_only():
    sql = _render(True)
    assert sql.count("%(years)s") == 2
    assert "%(" not in PUSHDOWN.replace("%(years)s", "")
