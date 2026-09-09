"""Population filters -> SQL fragments (structure, not database behavior)."""

from datetime import date

from atus_pipeline.analytics.population import build_population_sql
from atus_pipeline.analytics.spec import PopulationFilter


def test_unrestricted_filter_builds_nothing():
    sql = build_population_sql(PopulationFilter())
    assert sql.conditions == []
    assert not sql.needs_roster and not sql.needs_cps


def test_age_and_sex_need_the_roster_join_only():
    sql = build_population_sql(PopulationFilter(age_min=25, age_max=54, sex="female"))
    assert sql.needs_roster and not sql.needs_cps
    assert sql.params == {"age_min": 25, "age_max": 54, "sex": 2}
    assert any("hm.age >=" in c for c in sql.conditions)


def test_cps_dimensions_need_the_cps_join():
    sql = build_population_sql(
        PopulationFilter(region=3, education_level="bachelor_or_higher")
    )
    assert sql.needs_cps and not sql.needs_roster
    assert sql.params["region"] == 3
    assert sql.params["education_lo"] == 43 and sql.params["education_hi"] == 46


def test_employment_maps_to_telfs_codes():
    sql = build_population_sql(PopulationFilter(employment_status="employed"))
    assert sql.params["employment_codes"] == [1, 2]
    sql = build_population_sql(PopulationFilter(employment_status="unemployed"))
    assert sql.params["employment_codes"] == [3, 4]


def test_day_type_maps_atus_day_codes():
    # TUDIARYDAY: 1=Sunday, 7=Saturday
    sql = build_population_sql(PopulationFilter(day_type="weekend"))
    assert sql.params["day_codes"] == [1, 7]


def test_children_filter_uses_counts_not_nulls():
    has = build_population_sql(PopulationFilter(has_household_children=True))
    none = build_population_sql(PopulationFilter(has_household_children=False))
    assert any("> 0" in c for c in has.conditions)
    assert any("= 0" in c for c in none.conditions)


def test_diary_window_parameters():
    sql = build_population_sql(
        PopulationFilter(diary_date_min=date(2020, 5, 10), diary_date_max=date(2020, 12, 31))
    )
    assert sql.params["diary_date_min"] == date(2020, 5, 10)
    assert sql.params["diary_date_max"] == date(2020, 12, 31)


def test_topcoded_age_bound_warns():
    sql = build_population_sql(PopulationFilter(age_min=80))
    assert any("topcoded" in w for w in sql.warnings)


def test_analytics_version_is_consistent():
    from atus_pipeline.analytics import ANALYTICS_VERSION
    from atus_pipeline.analytics.engine import _ANALYTICS_VERSION

    assert ANALYTICS_VERSION == _ANALYTICS_VERSION
