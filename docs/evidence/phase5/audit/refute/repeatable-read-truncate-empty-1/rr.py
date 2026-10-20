import psycopg, threading, time
base="postgresql://atus:atus@localhost:5434/"
with psycopg.connect(base+"atus",autocommit=True) as c:
    c.execute("DROP DATABASE IF EXISTS atus_refute_rrtrunc"); c.execute("CREATE DATABASE atus_refute_rrtrunc")
u=base+"atus_refute_rrtrunc"
with psycopg.connect(u,autocommit=True) as c:
    c.execute("create table t(x int); insert into t select generate_series(1,6); create table meta(v text); insert into meta values('run1')")
res={}
def loader():
    with psycopg.connect(u) as c:
        c.execute("truncate t"); c.execute("insert into t select generate_series(1,6)"); c.execute("update meta set v='run2'")
        time.sleep(2); c.commit()
def reader(iso,name):
    with psycopg.connect(u) as c:
        c.isolation_level=iso
        v=c.execute("select v from meta").fetchone()[0]
        n=c.execute("select count(*) from t").fetchone()[0]
        v2=c.execute("select v from meta").fetchone()[0]
        res[name]=(v,n,v2)
L=threading.Thread(target=loader); L.start(); time.sleep(0.5)
ts=[threading.Thread(target=reader,args=(i,n)) for i,n in [(psycopg.IsolationLevel.REPEATABLE_READ,"RR"),(psycopg.IsolationLevel.READ_COMMITTED,"RC")]]
[t.start() for t in ts]; [t.join() for t in ts]; L.join()
print(res)
with psycopg.connect(base+"atus",autocommit=True) as c: c.execute("DROP DATABASE atus_refute_rrtrunc")
