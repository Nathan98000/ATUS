# capture per-benchmark observed text by intercepting _persist
import psycopg, collections
import atus_pipeline.analytics.benchmarks as b
captured=[]
orig=b._persist
def p(conn, results):
    captured.extend(results); return orig(conn, results)
b._persist=p
conn = psycopg.connect("postgresql://atus:atus@localhost:5434/atus_audit_bmkref0")
try: b.run_benchmarks(conn)
except Exception as e: print("raised:", type(e).__name__, str(e)[:120])
c=collections.Counter(r.observed.split(":")[0] for r in captured)
print(len(captured), c)
print(captured[0].observed[:150])
