"""Deterministic variant: first benchmark hits lock_timeout (another session holds
ACCESS EXCLUSIVE on atus.activities for 0.4 s); later benchmarks run after the lock is
released, so any failure of theirs comes from the aborted shared transaction."""
import sys, threading, time
sys.path.insert(0, "$REPO")
import psycopg
from atus_pipeline.analytics import benchmarks
from atus_pipeline.analytics.spec import ActivitySelector, AnalysisSpec, Measure

URL = "postgresql://atus:atus@localhost:5434/atus_audit_rel_engine"
sleep = ActivitySelector.preset("sleep")
spec = AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY, activity=sleep, years=(2023,))

def slow_extract(result):
    return result.estimate.value

def build():
    first = benchmarks.Benchmark("first (lock_timeout)", "x", spec, benchmarks._value, 150.0, 1e9)
    def wait_then_value(result):  # not reached for the first one
        return result.estimate.value
    rest = [benchmarks.Benchmark(f"later #{i}", "x", spec, wait_then_value, 150.0, 1e9) for i in range(3)]
    return (first, *rest)

benchmarks.build_benchmarks = build
held = threading.Event()
def locker():
    with psycopg.connect(URL) as c:
        c.execute("LOCK TABLE atus.activities IN ACCESS EXCLUSIVE MODE")
        held.set()
        time.sleep(0.4)
        c.rollback()
t = threading.Thread(target=locker); t.start(); held.wait()
conn = psycopg.connect(URL)
conn.execute("SET lock_timeout = '100ms'")
conn.commit()
orig_estimate = benchmarks.AnalysisEngine.estimate
calls = {"n": 0}
def estimate(self, s):
    calls["n"] += 1
    if calls["n"] == 2:
        time.sleep(0.6)  # make sure the locker has released before later benchmarks
    return orig_estimate(self, s)
benchmarks.AnalysisEngine.estimate = estimate
try:
    res = benchmarks.run_benchmarks(conn)
    for r in res:
        print(r.passed, r.name, "|", r.observed[:120])
except Exception as exc:
    print("run_benchmarks raised (results never printed):", type(exc).__name__, str(exc)[:200])
finally:
    conn.close()
t.join()
