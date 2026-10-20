import psycopg, collections
from atus_pipeline.analytics.benchmarks import run_benchmarks
conn = psycopg.connect("postgresql://atus:atus@localhost:5434/atus_audit_bmkref0")
try:
    r = run_benchmarks(conn)
    print("returned", len(r))
except Exception as e:
    print("run_benchmarks raised:", type(e).__name__, e)
