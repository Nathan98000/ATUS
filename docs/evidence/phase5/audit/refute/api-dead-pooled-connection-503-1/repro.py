import psycopg, time
from psycopg_pool import ConnectionPool
admin="postgresql://atus:atus@localhost:5434/postgres"
with psycopg.connect(admin, autocommit=True) as c:
    c.execute("DROP DATABASE IF EXISTS atus_refute_dead"); c.execute("CREATE DATABASE atus_refute_dead")
url="postgresql://atus:atus@localhost:5434/atus_refute_dead"
for label, kw in [("no_check", {}), ("with_check", {"check": ConnectionPool.check_connection})]:
    pool=ConnectionPool(conninfo=url,min_size=1,max_size=10,open=False,name=label,**kw); pool.open(wait=True)
    with pool.connection() as c: c.execute("select 1")
    with psycopg.connect(admin, autocommit=True) as a:
        n=a.execute("select count(pg_terminate_backend(pid)) from pg_stat_activity where datname='atus_refute_dead'").fetchone()
    time.sleep(0.5)
    for i in range(3):
        try:
            with pool.connection() as c: c.execute("select 1"); print(label,i,"OK")
        except Exception as e: print(label,i,type(e).__name__, str(e)[:80])
    print(label,"terminated",n,pool.get_stats()); pool.close()
with psycopg.connect(admin, autocommit=True) as c: c.execute("DROP DATABASE atus_refute_dead")
