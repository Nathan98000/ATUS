"""Experiment (NOT a repo change): pooled-22 leisure SQL at 0.25 CU with the default
parallel plan vs SET LOCAL max_parallel_workers_per_gather = 0 (read-only, jit=off).
Interleaved, 2 runs each; compares results numerically."""
import sys, time, psycopg
sys.path.insert(0, "$REPO/src")
from atus_pipeline.analytics import ActivitySelector, PopulationFilter
from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.analytics.population import build_population_sql
from atus_pipeline.analytics.queries import build_query
from atus_pipeline.analytics.weights import MULTIYEAR
P22 = [y for y in range(2003, 2026) if y != 2020]
with psycopg.connect("postgresql://atus:atus@localhost:5441/atus",
                     options="-c default_transaction_read_only=on -c jit=off") as conn:
    act = ActivityResolver(conn).resolve(ActivitySelector.preset("leisure_and_sports_bls_table"))
    pop = build_population_sql(PopulationFilter())
    sql = build_query(activity=act, population=pop, scheme=MULTIYEAR, with_replicates=True)
    params = {"years": P22, **act.sql_params, **pop.params}
    rows = {}
    for name in ("parallel_default", "parallel_off", "parallel_default", "parallel_off"):
        if name == "parallel_off":
            conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        t = time.perf_counter()
        r = conn.execute(sql, params).fetchall()
        print(f"{name}: {time.perf_counter()-t:.2f}s", flush=True)
        rows.setdefault(name, r)
        conn.rollback()
    def flat(rs):
        out = []
        for row in rs:
            for v in row:
                out.extend(float(x) for x in v) if isinstance(v, (list, tuple)) else out.append(float(v))
        return out
    a, b = rows["parallel_default"], rows["parallel_off"]
    print("ints+numeric point sums identical:", [r[:6] for r in a] == [r[:6] for r in b],
          "| max rel diff:", max(abs(x - y) / max(abs(x), 1e-300) for x, y in zip(flat(a), flat(b))))
