import sys
sys.path.insert(0, "$REPO/src")
import psycopg
from atus_pipeline.analytics import benchmarks
from atus_pipeline.database.connection import connect
URL="postgresql://atus:atus@localhost:5434/atus_refute_bac"
n={"i":0}
def estimate(self, spec):
    n["i"]+=1
    if n["i"]==1:
        self._conn.execute("SET LOCAL statement_timeout='1ms'")
        self._conn.execute("SELECT pg_sleep(0.2)")
    self._conn.execute("SELECT 1")
    raise RuntimeError("reached-db-ok")  # marker: DB statement succeeded
benchmarks.AnalysisEngine.estimate = estimate
print("benchmarks:", len(benchmarks.build_benchmarks()))
# 1) per-benchmark outcomes with persist disabled
orig=benchmarks._persist
benchmarks._persist=lambda c,r: None
with connect(URL) as conn:
    res=benchmarks.run_benchmarks(conn)
from collections import Counter
print(Counter(r.observed.split(":")[0] for r in res))
print(res[0].observed[:100]); print(res[1].observed[:100])
# 2) with real persist
benchmarks._persist=orig; n["i"]=0
try:
    with connect(URL) as conn:
        benchmarks.run_benchmarks(conn)
    print("no raise")
except Exception as e:
    print("run_benchmarks raised:", type(e).__name__, str(e)[:100])
