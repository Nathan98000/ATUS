"""Worst-case pooled 22-year replicate query: execution time with the server's
default JIT settings vs session `SET jit = off` (read-only session).
usage: python jit_probe.py <url> <label> <runs>"""
import re, sys, time, psycopg
from atus_pipeline.analytics import ActivitySelector, PopulationFilter
from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.analytics.population import build_population_sql
from atus_pipeline.analytics.queries import build_query
from atus_pipeline.analytics.weights import MULTIYEAR
url, label, runs = sys.argv[1], sys.argv[2], int(sys.argv[3])
P22 = [y for y in range(2003, 2026) if y != 2020]
with psycopg.connect(url, options="-c default_transaction_read_only=on", autocommit=True) as conn:
    print(label, {k: conn.execute(f"show {k}").fetchone()[0] for k in
                  ("jit", "jit_above_cost", "jit_inline_above_cost", "jit_optimize_above_cost",
                   "max_parallel_workers_per_gather", "work_mem")})
    act = ActivityResolver(conn).resolve(ActivitySelector.preset("sleep"))
    pop = build_population_sql(PopulationFilter())
    sql = build_query(activity=act, population=pop, scheme=MULTIYEAR, with_replicates=True)
    rendered = psycopg.ClientCursor(conn).mogrify(sql, {"years": P22, **act.sql_params, **pop.params})
    for jit in ("on", "off"):
        conn.execute(f"SET jit = {jit}")
        for i in range(runs):
            plan = "\n".join(r[0] for r in conn.execute("EXPLAIN (ANALYZE) " + rendered))
            ex = re.search(r"Execution Time: ([\d.]+)", plan).group(1)
            jt = re.search(r"Timing: .*Total ([\d.]+) ms", plan)
            opt = re.search(r"Options: (.*)", plan)
            t = time.perf_counter(); rows = conn.execute(rendered).fetchall(); wall = time.perf_counter() - t
            print(f"{label} jit={jit} run{i+1}: EXPLAIN ANALYZE execution {float(ex):.0f} ms; "
                  f"JIT total {jt.group(1) if jt else 'n/a'} ms; JIT options [{opt.group(1) if opt else 'none'}]; "
                  f"plain query wall {wall*1000:.0f} ms ({len(rows)} rows)", flush=True)
