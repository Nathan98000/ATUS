"""Pool configured exactly like api/app.py:62-70 (min_size=1, max_size=10,
open=False, open(wait=False); all other parameters default). Grow it to 10
connections, release them, then watch it idle: server-side backend count and
pool stats every 30 s. Shows max_idle shrink timing and absence of idle traffic."""
import sys, time, datetime, threading
import psycopg
from psycopg_pool import ConnectionPool
OUT = sys.argv[1]; DURATION = float(sys.argv[2])
log = open(OUT, "w")
def p(*a):
    print(datetime.datetime.now().time().isoformat(timespec="seconds"), *a, file=log, flush=True)
pool = ConnectionPool(conninfo="postgresql://atus:atus@127.0.0.1:5442/atus?application_name=pool-probe",
                      min_size=1, max_size=10, open=False, name="pool-probe")
p("defaults: max_idle", pool.max_idle, "max_lifetime", pool.max_lifetime, "reconnect_timeout", pool.reconnect_timeout,
  "timeout", pool.timeout, "num_workers", pool.num_workers, "check", pool._check, "reset", pool._reset, "configure", pool._configure)
pool.open(wait=False)
time.sleep(1)
mon = psycopg.connect("postgresql://atus:atus@127.0.0.1:5442/atus?application_name=probe-monitor", autocommit=True)
def backends():
    return mon.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name='pool-probe'").fetchone()[0]
barrier = threading.Barrier(10)
def hold():
    with pool.connection(timeout=10) as c:
        c.execute("SELECT 1")
        barrier.wait()
        time.sleep(0.5)
ts = [threading.Thread(target=hold) for _ in range(10)]
[t.start() for t in ts]; [t.join() for t in ts]
p("after burst: backends", backends(), "stats", {k: v for k, v in pool.get_stats().items()})
t0 = time.time()
while time.time() - t0 < DURATION:
    time.sleep(30)
    st = pool.get_stats()
    p(f"t+{int(time.time()-t0)}s backends={backends()} pool_size={st.get('pool_size')} pool_available={st.get('pool_available')}")
pool.close()
p("closed")
