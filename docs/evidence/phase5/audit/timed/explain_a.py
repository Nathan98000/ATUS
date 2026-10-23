"""EXPLAIN (ANALYZE, BUFFERS) of the single-year estimate and pooled-22 SQL exactly as
queries.build_query renders them, on the prototype serving DB under the applied profile,
session jit=off (read-only). usage: python explain_a.py <label> <est_runs> <pooled_runs>"""
import sys, time, psycopg
sys.path.insert(0, "$REPO/src")
from atus_pipeline.analytics import ActivitySelector, PopulationFilter
from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.analytics.population import build_population_sql
from atus_pipeline.analytics.queries import build_query
from atus_pipeline.analytics.weights import MULTIYEAR
label, ne, npool = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
P22 = [y for y in range(2003, 2026) if y != 2020]
with psycopg.connect("postgresql://atus:atus@localhost:5441/atus",
                     options="-c default_transaction_read_only=on -c jit=off") as conn:
    res = ActivityResolver(conn)
    pop = build_population_sql(PopulationFilter())
    for name, sel, years, n in (("est_1y_010101_2025", ActivitySelector(("010101",)), [2025], ne),
                                ("pooled22_leisure_bls", ActivitySelector.preset("leisure_and_sports_bls_table"), P22, npool)):
        act = res.resolve(sel)
        sql = build_query(activity=act, population=pop, scheme=MULTIYEAR, with_replicates=True)
        rendered = psycopg.ClientCursor(conn).mogrify(sql, {"years": years, **act.sql_params, **pop.params})
        for i in range(n):
            t = time.perf_counter()
            plan = [r[0] for r in conn.execute("EXPLAIN (ANALYZE, BUFFERS) " + rendered).fetchall()]
            print(f"===== {label} {name} run {i+1}: client {time.perf_counter()-t:.2f}s")
            for l in plan:
                print(l[:300])
            conn.rollback()
