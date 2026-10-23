"""Experiment (NOT a repo change): does restricting the activity_minutes CTE to the
requested years' respondents speed up a single-year estimate at 0.25 CU? Compares
the as-shipped SQL (queries.build_query) with a variant that adds
  AND a.tucaseid IN (SELECT tucaseid FROM atus.respondents WHERE data_year = ANY(%(years)s))
to the CTE, and checks the result rows are identical. Read-only session, jit=off."""
import sys, time, psycopg
sys.path.insert(0, "$REPO/src")
from atus_pipeline.analytics import ActivitySelector, PopulationFilter
from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.analytics.population import build_population_sql
from atus_pipeline.analytics.queries import build_query
from atus_pipeline.analytics.weights import MULTIYEAR
with psycopg.connect("postgresql://atus:atus@localhost:5441/atus",
                     options="-c default_transaction_read_only=on -c jit=off") as conn:
    act = ActivityResolver(conn).resolve(ActivitySelector(("010101",)))
    pop = build_population_sql(PopulationFilter())
    sql = build_query(activity=act, population=pop, scheme=MULTIYEAR, with_replicates=True)
    marker = f"WHERE {act.sql_condition}\n    GROUP BY a.tucaseid"
    assert marker in sql, "CTE text not found"
    variant = sql.replace(marker, f"WHERE {act.sql_condition}\n      AND a.tucaseid IN (SELECT tucaseid FROM atus.respondents WHERE data_year = ANY(%(years)s))\n    GROUP BY a.tucaseid")
    params = {"years": [2025], **act.sql_params, **pop.params}
    rows = {}
    for name, q in (("as_shipped", sql), ("year_pushdown", variant), ("as_shipped", sql), ("year_pushdown", variant)):
        t = time.perf_counter()
        r = conn.execute(q, params).fetchall()
        dt = time.perf_counter() - t
        rows.setdefault(name, r)
        print(f"{name}: {dt:.2f}s rows={len(r)}")
        conn.rollback()
    a, b = rows["as_shipped"], rows["year_pushdown"]
    print("identical result rows:", a == b)
    # numeric comparison
    def flat(row):
        out = []
        for v in row:
            if isinstance(v, (list, tuple)):
                out.extend(float(x) for x in v)
            else:
                out.append(float(v))
        return out
    fa, fb = flat(a[0]), flat(b[0])
    exact_int = a[0][:3] == b[0][:3]
    exact_numeric_sums = a[0][3:6] == b[0][3:6]
    rel = max(abs(x - y) / max(abs(x), 1e-300) for x, y in zip(fa, fb))
    print("year,n,n_part identical:", exact_int, "| numeric point sums identical:", exact_numeric_sums,
          "| max relative diff over all 486 values:", rel)
