"""Read-only equivalence check of activity pre-aggregation on the FULL database.

The engine's only read of atus.activities is the activity_minutes CTE
(queries.py:123-128). We re-run fetch_year_statistics with `atus.activities a`
replaced by an inline pre-aggregated relation
  (SELECT tucaseid, activity_code, sum(duration_minutes)::<T> AS duration_minutes
   FROM atus.activities GROUP BY 1, 2) a
for T in smallint / integer / bigint, and compare every returned statistic exactly.
No DDL/DML: session runs with default_transaction_read_only = on.
"""
from __future__ import annotations

import sys
import time
from datetime import date

sys.path.insert(0, "$REPO")
import psycopg  # noqa: E402

from atus_pipeline.analytics import queries  # noqa: E402
from atus_pipeline.analytics.activities import ActivityResolver  # noqa: E402
from atus_pipeline.analytics.population import build_population_sql  # noqa: E402
from atus_pipeline.analytics.spec import ActivitySelector, PopulationFilter  # noqa: E402
from atus_pipeline.analytics.weights import MULTIYEAR, PANDEMIC  # noqa: E402

URL = "postgresql://atus:atus@localhost:5434/atus"
orig_build = queries.build_query
CASES = [
    # (label, selector, years, population, scheme, with_replicates)
    ("tv 2007 reps", ActivitySelector.preset("watching_tv"), (2007,), PopulationFilter(), MULTIYEAR, True),
    ("household A-1 2025", ActivitySelector.preset("household_activities_bls_table"), (2025,), PopulationFilter(), MULTIYEAR, False),
    ("leisure A-1 2003-05 women 25-54", ActivitySelector.preset("leisure_and_sports_bls_table"), (2003, 2004, 2005), PopulationFilter(sex="female", age_min=25, age_max=54), MULTIYEAR, False),
    ("work 2015 employed weekday", ActivitySelector.preset("work_main_and_other_jobs"), (2015,), PopulationFilter(employment_status="employed", day_type="weekday"), MULTIYEAR, False),
    ("rare leaf 2010-2012 region 4 edu", ActivitySelector(("130101",)), (2010, 2011, 2012), PopulationFilter(region=4, education_level="bachelor_or_higher"), MULTIYEAR, False),
    ("tier1 + exclude, CA 2019", ActivitySelector(("02",), ("0201",)), (2019,), PopulationFilter(state_fips="06"), MULTIYEAR, False),
    ("pandemic 2020 tv window reps", ActivitySelector.preset("watching_tv"), (2020,), PopulationFilter(diary_date_min=date(2020, 5, 10)), PANDEMIC, True),
    ("pandemic 2019+2020 sleep kids", ActivitySelector.preset("sleep"), (2019, 2020), PopulationFilter(has_household_children=True), PANDEMIC, False),
    ("eating 2021-2023 reps age80+", ActivitySelector.preset("eating_and_drinking"), (2021, 2022, 2023), PopulationFilter(age_min=80), MULTIYEAR, True),
]


def patched_build(type_name):
    def build(**kw):
        sql = orig_build(**kw)
        sub = (f"(SELECT tucaseid, activity_code, sum(duration_minutes)::{type_name} "
               "AS duration_minutes FROM atus.activities GROUP BY 1, 2) a")
        assert sql.count("FROM atus.activities a") == 1
        return sql.replace("FROM atus.activities a", "FROM " + sub)
    return build


def run(conn, resolver, build):
    queries.build_query = build
    out = {}
    for label, sel, years, pop, scheme, reps in CASES:
        out[label] = queries.fetch_year_statistics(
            conn, years=years, activity=resolver.resolve(sel),
            population=build_population_sql(pop), scheme=scheme, with_replicates=reps)
        conn.rollback()
    queries.build_query = orig_build
    return out


def rel(a, b):
    return 0.0 if a == b else abs(a - b) / max(abs(a), abs(b))


with psycopg.connect(URL) as conn:
    conn.execute("SET default_transaction_read_only = on")
    conn.commit()
    resolver = ActivityResolver(conn)
    conn.rollback()
    t = time.monotonic(); base = run(conn, resolver, orig_build); print(f"baseline {time.monotonic()-t:.1f}s")
    for type_name in ("smallint", "integer", "bigint"):
        t = time.monotonic()
        alt = run(conn, resolver, patched_build(type_name))
        worst_rep = 0.0
        exact_fields = True
        for label in base:
            assert base[label].keys() == alt[label].keys(), label
            for year, b in base[label].items():
                a = alt[label][year]
                for f in ("n_respondents", "n_participants", "sum_w", "sum_wx", "sum_wp"):
                    if getattr(a, f) != getattr(b, f):
                        exact_fields = False
                        print(f"  MISMATCH {type_name} {label} {year} {f}: {getattr(b, f)} vs {getattr(a, f)}")
                if b.rep_sum_w is not None:
                    for f in ("rep_sum_w", "rep_sum_wx", "rep_sum_wp"):
                        for x, y in zip(getattr(b, f), getattr(a, f), strict=True):
                            worst_rep = max(worst_rep, rel(x, y))
        print(f"pre-agg as {type_name}: {time.monotonic()-t:.1f}s; n/sum_w/sum_wx/sum_wp exactly equal: "
              f"{exact_fields}; worst replicate-sum relative diff: {worst_rep:.3e}")
    print("cases:", len(CASES), "year-rows:", sum(len(v) for v in base.values()))
