"""Does one DB error inside run_benchmarks poison the rest (shared implicit transaction)?"""
import sys
from pathlib import Path
sys.path.insert(0, "$REPO")
import psycopg
from atus_pipeline.analytics import benchmarks
from atus_pipeline.analytics.spec import ActivitySelector, AnalysisSpec, Measure

URL = "postgresql://atus:atus@localhost:5434/atus_audit_rel_engine"
orig = benchmarks.build_benchmarks

def patched():
    # Fixture-valid specs (2023 exists in the fixture); the first is made to fail via a
    # statement_timeout so we can see what happens to the following ones.
    sleep = ActivitySelector.preset("sleep")
    spec = AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY, activity=sleep, years=(2023,))
    first = benchmarks.Benchmark("first (forced timeout)", "x", spec, benchmarks._value, 150.0, 1e9)
    rest = [benchmarks.Benchmark(f"later #{i}", "x", spec, benchmarks._value, 150.0, 1e9) for i in range(3)]
    return (first, *rest)

benchmarks.build_benchmarks = patched
conn = psycopg.connect(URL)
# Force the next statement(s) to be cancelled: pg_sleep-free approach -> tiny timeout
conn.execute("SET statement_timeout = '1ms'")  # session-level, only for this experiment
try:
    res = benchmarks.run_benchmarks(conn)
    for r in res:
        print(r.passed, r.name, "|", r.observed[:110])
except Exception as exc:
    print("run_benchmarks raised:", type(exc).__name__, str(exc)[:200])
finally:
    conn.close()

# Control: without timeout the same 4 specs pass
benchmarks.build_benchmarks = patched
conn = psycopg.connect(URL)
res = benchmarks.run_benchmarks(conn)
print("control:", [(r.passed, r.observed[:40]) for r in res])
conn.close()
