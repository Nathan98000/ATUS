"""Year pushdown (F39): the activity CTE is restricted to the requested
years' respondents and materialized, and those are the only changes to the
Phase 2-4 SQL. The
equivalence of the two queries is proven against a database in
tests/integration/test_year_pushdown.py.

The "Phase 2-4 SQL" every such check renders is ``push_down_years=False`` of
the current template; it is held byte for byte to what the Phase 1-4 code
(387fc2d) rendered, frozen in ``phase4_queries.json`` (``phase4_sql.py``)."""

from __future__ import annotations

from atus_pipeline.analytics.activities import ResolvedActivity
from atus_pipeline.analytics.population import build_population_sql
from atus_pipeline.analytics.queries import build_query
from atus_pipeline.analytics.spec import ActivitySelector, PopulationFilter
from atus_pipeline.analytics.weights import MULTIYEAR, PANDEMIC
from tests.unit.analytics.phase4_sql import SHAPES, fixture_entries, render

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


def test_the_non_pushdown_rendering_is_the_frozen_phase_4_query():
    """Review r2 (phase24-sql-baseline-not-pinned): the old query of every
    "Phase 2-4 vs current" check came from the current template, so an edit
    to its shared part (say ``sum(base.w) AS sum_wp``) changed both
    renderings alike and passed them all. The old rendering is now held to
    the text 387fc2d itself rendered, for shapes covering every branch."""
    entries = fixture_entries()
    assert [entry["name"] for entry in entries] == [name for name, _ in SHAPES]
    for entry in entries:
        rendered = render(entry["spec"], push_down_years=False)
        assert rendered["sql"] == entry["sql"], entry["name"]
        assert rendered["params"] == entry["params"], entry["name"]
        pushed = render(entry["spec"])["sql"]
        assert pushed.replace(PUSHDOWN, "").replace("AS MATERIALIZED (", "AS (") == entry["sql"]


def test_the_frozen_shapes_reach_every_branch_of_the_query():
    texts = [entry["sql"] for entry in fixture_entries()]
    for fragment in ("JOIN atus.household_members hm", "JOIN atus.cps_persons c",
                     "JOIN atus.replicate_weights rw", "JOIN atus.pandemic_replicate_weights rw",
                     "hm.age >= ", "hm.sex = ", "r.labor_force_status", "r.household_children > 0",
                     "r.household_children = 0", "c.region", "c.state_fips", "c.education",
                     "r.diary_day_of_week", "r.diary_date >= ", "r.diary_date <= "):
        assert any(fragment in text for text in texts), fragment
    assert any("JOIN" not in text.split("FROM atus.respondents r")[1].split("LEFT JOIN")[0]
               for text in texts)                                   # and none of them
