import psycopg, time
from psycopg_pool import ConnectionPool
admin = psycopg.connect("postgresql://atus:atus@localhost:5434/postgres", autocommit=True)
admin.execute("CREATE DATABASE atus_audit_refpool")
try:
    url="postgresql://atus:atus@localhost:5434/atus_audit_refpool"
    for chk in (None, ConnectionPool.check_connection):
        pool = ConnectionPool(url, min_size=1, max_size=10, open=False, name="t", check=chk)
        pool.open(wait=True)
        with pool.connection() as c: c.execute("select 1")
        admin.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname='atus_audit_refpool' and pid<>pg_backend_pid()")
        time.sleep(0.5)
        for i in range(2):
            try:
                with pool.connection(timeout=10) as c: print("check=",chk is not None,"req",i,c.execute("select 1").fetchone())
            except Exception as e: print("check=",chk is not None,"req",i,type(e).__name__, str(e)[:80])
        print(pool.get_stats()); pool.close()
finally:
    admin.execute("DROP DATABASE atus_audit_refpool WITH (FORCE)")
