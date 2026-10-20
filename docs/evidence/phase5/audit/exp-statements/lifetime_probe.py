"""Does max_lifetime cause background reconnects on an idle pool? Same pool
shape as the app but max_lifetime=20 s (to observe within a minute)."""
import time, datetime, psycopg
from psycopg_pool import ConnectionPool
def ts(): return datetime.datetime.now().time().isoformat(timespec="seconds")
mon = psycopg.connect("postgresql://atus:atus@127.0.0.1:5442/atus?application_name=probe-monitor", autocommit=True)
def pids(): return [r[0] for r in mon.execute("SELECT pid FROM pg_stat_activity WHERE application_name='lifetime-probe' ORDER BY pid")]
pool = ConnectionPool("postgresql://atus:atus@127.0.0.1:5442/atus?application_name=lifetime-probe", min_size=1, max_size=10, open=False, max_lifetime=20)
pool.open(wait=True)
print(ts(), "opened, backends", pids())
with pool.connection() as c: c.execute("SELECT 1")
for i in range(5):
    time.sleep(10); print(ts(), f"idle t+{(i+1)*10}s backends", pids(), "stats", {k: pool.get_stats().get(k) for k in ("connections_num", "pool_size")})
with pool.connection() as c: print(ts(), "request on backend pid", c.execute("SELECT pg_backend_pid()").fetchone()[0])
time.sleep(1); print(ts(), "after return of expired conn: backends", pids(), "connections_num", pool.get_stats().get("connections_num"))
pool.close()
