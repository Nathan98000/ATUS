"""Heavier pooled specs on one DB: planner total cost and execution time with
default JIT vs jit=off. usage: python jit_heavy.py <url> <label>"""
import re, sys, psycopg
from atus_pipeline.analytics import ActivitySelector, PopulationFilter
from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.analytics.population import build_population_sql
from atus_pipeline.analytics.queries import build_query
from atus_pipeline.analytics.weights import MULTIYEAR
url, label = sys.argv[1], sys.argv[2]
P22 = [y for y in range(2003, 2026) if y != 2020]
cases = {
  "leisure (BLS A-1) pooled 22y": (ActivitySelector.preset("leisure_and_sports_bls_table"), PopulationFilter()),
  "all 18 tier-1 codes pooled 22y": (None, PopulationFilter()),
  "sleep pooled 22y, women 25-54 bachelor+ (roster+CPS)": (ActivitySelector.preset("sleep"),
        PopulationFilter(sex="female", age_min=25, age_max=54, education_level="bachelor_or_higher")),
}
with psycopg.connect(url, options="-c default_transaction_read_only=on", autocommit=True) as conn:
    tier1 = tuple(r[0] for r in conn.execute("select code from atus.activity_tier1 order by 1"))
    for name, (sel, popf) in cases.items():
        sel = sel or ActivitySelector(tier1, label="all")
        act = ActivityResolver(conn).resolve(sel)
        pop = build_population_sql(popf)
        sql = build_query(activity=act, population=pop, scheme=MULTIYEAR, with_replicates=True)
        rendered = psycopg.ClientCursor(conn).mogrify(sql, {"years": P22, **act.sql_params, **pop.params})
        cost = re.search(r"cost=[\d.]+\.\.([\d.]+)", conn.execute("EXPLAIN " + rendered).fetchone()[0]).group(1)
        for jit in ("on", "off"):
            conn.execute(f"SET jit = {jit}")
            plan = "\n".join(r[0] for r in conn.execute("EXPLAIN (ANALYZE) " + rendered))
            ex = float(re.search(r"Execution Time: ([\d.]+)", plan).group(1))
            jt = re.search(r"Timing: .*Total ([\d.]+) ms", plan)
            opt = re.search(r"Options: (.*)", plan)
            print(f"{label} | {name:55s} | est. cost {float(cost):>10.0f} | jit={jit:3s} exec {ex:7.0f} ms | "
                  f"JIT total {jt.group(1) if jt else '-'} ms [{opt.group(1) if opt else '-'}]", flush=True)
        conn.execute("RESET jit")
