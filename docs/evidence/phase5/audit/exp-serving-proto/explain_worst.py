"""EXPLAIN (ANALYZE, BUFFERS) of the worst-case pooled 22-year replicate query
(sleep, all years except 2020, multiyear weights, 160 replicates), exactly as
queries.build_query renders it. usage: python explain_worst.py <url> <label> <runs>"""
import sys, psycopg
from atus_pipeline.analytics import ActivitySelector, PopulationFilter
from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.analytics.population import build_population_sql
from atus_pipeline.analytics.queries import build_query
from atus_pipeline.analytics.weights import MULTIYEAR
url, label, runs = sys.argv[1], sys.argv[2], int(sys.argv[3])
P22 = [y for y in range(2003, 2026) if y != 2020]
with psycopg.connect(url, options="-c default_transaction_read_only=on") as conn:
    act = ActivityResolver(conn).resolve(ActivitySelector.preset("sleep"))
    pop = build_population_sql(PopulationFilter())
    sql = build_query(activity=act, population=pop, scheme=MULTIYEAR, with_replicates=True)
    params = {"years": P22, **act.sql_params, **pop.params}
    rendered = psycopg.ClientCursor(conn).mogrify(sql, params)
    for i in range(runs):
        plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, SETTINGS) " + rendered).fetchall()
        print(f"===== {label} run {i+1} =====")
        lines = [r[0] for r in plan]
        for l in lines:
            if len(l) > 400:
                l = l[:400] + " ...[truncated: replicate aggregate list]"
            print(l)
        conn.rollback()
