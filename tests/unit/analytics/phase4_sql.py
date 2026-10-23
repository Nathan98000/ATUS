"""The Phase 2-4 sufficient-statistics SQL, frozen (review r2,
phase24-sql-baseline-not-pinned).

``phase4_queries.json`` holds the SQL text and parameters that the Phase 1-4
code itself (commit 387fc2d, the baseline of the §9 HTTP replay) renders for
a set of request shapes covering every branch of the query: both weight
schemes, with and without replicates, the roster and CPS joins, every
population dimension, include/exclude/prefix selectors and diary dates.
``tests/unit/analytics/test_year_pushdown_sql.py`` renders the same shapes
with the current code (``push_down_years=False``) and requires the same
bytes, so every "Phase 2-4 SQL vs current SQL" check, which renders the old
query from the current template, compares against the real Phase 2-4 query.

The file is never regenerated. To check that it is what 387fc2d renders::

    git archive 387fc2d src | tar -x -C /tmp/p4
    python -I tests/unit/analytics/phase4_sql.py /tmp/p4/src > /tmp/p4.json
    diff /tmp/p4.json tests/unit/analytics/phase4_queries.json

Run as a script, this module imports ``atus_pipeline`` from the directory
given, and prints the fixture for its shapes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE = Path(__file__).with_name("phase4_queries.json")

# A slice of the harmonized lexicon: two tier-1 trees, enough leaves for
# prefix, include and exclude selections. The 160-column replicate select is
# rendered for three shapes (each scheme, and every join), not all of them.
LEXICON = {
    "tier1": ["01", "12"],
    "tier2": ["0101", "0102", "1203"],
    "codes": {
        "010101": "Sleeping", "010102": "Sleeplessness", "010201": "Washing, dressing",
        "120301": "Relaxing, thinking", "120303": "Television and movies (not religious)",
        "120304": "Television (religious)",
    },
}

SHAPES = [
    ("leaf, one year", {"measure": "average_minutes_per_day",
                        "activity": {"include": ["010101"]}, "years": [2023]}),
    ("no replicates", {"measure": "average_minutes_per_day",
                       "activity": {"include": ["010101"]}, "years": [2023],
                       "variance": "none"}),
    ("tier-1 minus tier-2, pooled", {"measure": "participation_rate",
                                     "activity": {"include": ["01"], "exclude": ["0101"],
                                                  "label": "Personal care but sleep"},
                                     "years": [2019, 2021, 2023], "variance": "none"}),
    ("several prefixes", {"measure": "average_minutes_per_participant",
                          "activity": {"include": ["0101", "120303", "120304"]},
                          "years": [2010, 2015], "variance": "none"}),
    ("pandemic weights", {"measure": "average_minutes_per_day",
                          "activity": {"include": ["1203"]}, "years": [2019, 2020],
                          "weights": "pandemic"}),
    ("pandemic, diary window, no replicates", {
        "measure": "participants_per_day", "activity": {"include": ["12"]},
        "years": [2020], "weights": "pandemic", "variance": "none",
        "population": {"diary_date_min": "2020-05-10", "diary_date_max": "2020-12-31"}}),
    ("roster: age and sex", {"measure": "average_minutes_per_day",
                             "activity": {"include": ["010101"]}, "years": [2024],
                             "variance": "none",
                             "population": {"age_min": 25, "age_max": 54,
                                            "sex": "female"}}),
    ("roster: topcoded age", {"measure": "average_minutes_per_day",
                              "activity": {"include": ["010101"]}, "years": [2024],
                              "variance": "none", "population": {"age_min": 80}}),
    ("respondent columns", {"measure": "participation_rate",
                            "activity": {"include": ["1203"]}, "years": [2022],
                            "variance": "none",
                            "population": {"employment_status": "employed",
                                           "has_household_children": True,
                                           "day_type": "weekend"}}),
    ("no household children", {"measure": "participation_rate",
                               "activity": {"include": ["1203"]}, "years": [2022],
                               "variance": "none",
                               "population": {"has_household_children": False,
                                              "employment_status": "not_in_labor_force",
                                              "day_type": "weekday"}}),
    ("CPS: region, state, education", {"measure": "average_minutes_per_day",
                                       "activity": {"include": ["120303"]},
                                       "years": [2018, 2019], "variance": "none",
                                       "population": {"region": 3, "state_fips": "48",
                                                      "education_level": "bachelor_or_higher"}}),
    ("every dimension", {"measure": "average_minutes_per_participant",
                         "activity": {"include": ["01", "12"], "exclude": ["010102"]},
                         "years": [2016, 2017, 2018],
                         "population": {"age_min": 15, "age_max": 64, "sex": "male",
                                        "employment_status": "unemployed",
                                        "has_household_children": True, "region": 1,
                                        "state_fips": "36",
                                        "education_level": "less_than_high_school",
                                        "day_type": "weekday",
                                        "diary_date_min": "2016-02-01",
                                        "diary_date_max": "2018-11-30"}}),
]


class LexiconConnection:
    """Answers the resolver's lexicon queries (and the release read of the
    current resolver) from :data:`LEXICON`; nothing else."""

    class _Rows(list):
        def fetchall(self):
            return list(self)

        def fetchone(self):
            return self[0] if self else None

    def execute(self, sql, params=None):
        if "activity_tier1" in sql:
            return self._Rows((code,) for code in LEXICON["tier1"])
        if "activity_tier2" in sql:
            return self._Rows((code,) for code in LEXICON["tier2"])
        if "activity_codes" in sql:
            return self._Rows(LEXICON["codes"].items())
        if "ingestion_runs" in sql:
            return self._Rows([({"release": "0325"},)])
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


def render(spec_payload: dict, **build_query_kwargs) -> dict:
    """The statistics query and parameters the engine sends for one shape,
    rendered by whichever ``atus_pipeline`` is importable (as the engine does:
    ``fetch_year_statistics``'s parameters)."""
    from atus_pipeline.analytics.activities import ActivityResolver
    from atus_pipeline.analytics.population import build_population_sql
    from atus_pipeline.analytics.queries import build_query
    from atus_pipeline.analytics.spec import AnalysisSpec
    from atus_pipeline.analytics.weights import get_scheme

    spec = AnalysisSpec.from_dict(spec_payload)
    activity = ActivityResolver(LexiconConnection()).resolve(spec.activity)
    population = build_population_sql(spec.population)
    sql = build_query(activity=activity, population=population,
                      scheme=get_scheme(spec.weights),
                      with_replicates=spec.variance == "replicate", **build_query_kwargs)
    params = {"years": list(spec.years), **activity.sql_params, **population.params}
    return {"sql": sql, "params": json.loads(json.dumps(params, default=str))}


def fixture_entries() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["queries"]


def _generate(src: Path) -> dict:
    sys.path.insert(0, str(src))
    import atus_pipeline
    from atus_pipeline.analytics.spec import AnalysisSpec

    assert Path(atus_pipeline.__file__).resolve().is_relative_to(src.resolve())
    queries = []
    for name, payload in SHAPES:
        spec = AnalysisSpec.from_dict(payload).to_dict()
        queries.append({"name": name, "spec": spec, **render(spec)})
    return {
        "source": "rendered by atus_pipeline at commit 387fc2d (Phases 1-4); never edit",
        "queries": queries,
    }


if __name__ == "__main__":
    print(json.dumps(_generate(Path(sys.argv[1])), indent=1, sort_keys=True))
