"""Candidate fix probe: pool with check=ConnectionPool.check_connection.
(1) what the check sends to the server per checkout; (2) whether a
terminated connection is replaced transparently on the next checkout."""
import time, psycopg
from psycopg_pool import ConnectionPool
DSN = "postgresql://atus:atus@127.0.0.1:5442/atus?application_name=check-probe"
pool = ConnectionPool(DSN, min_size=1, max_size=10, open=False, check=ConnectionPool.check_connection)
pool.open(wait=True)
mon = psycopg.connect("postgresql://atus:atus@127.0.0.1:5442/atus?application_name=probe-monitor", autocommit=True)
with pool.connection() as c: print("1st checkout ->", c.execute("SELECT 'probe-q1'").fetchone())
print("terminate:", mon.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='check-probe'").fetchall())
time.sleep(0.3)
t = time.perf_counter()
with pool.connection() as c: print("2nd checkout after terminate ->", c.execute("SELECT 'probe-q2'").fetchone(), f"{(time.perf_counter()-t)*1000:.1f}ms")
print("stats", {k: pool.get_stats().get(k) for k in ("connections_num", "connections_lost", "requests_num")})
pool.close()
