import psycopg, threading, time, sys
base="postgresql://atus:atus@localhost:5434/"
with psycopg.connect(base+"postgres", autocommit=True) as c:
    c.execute("DROP DATABASE IF EXISTS atus_refute_rdl"); c.execute("CREATE DATABASE atus_refute_rdl")
url=base+"atus_refute_rdl"
with psycopg.connect(url, autocommit=True) as c:
    c.execute("CREATE TABLE companions(x int); CREATE TABLE activities(x int); CREATE TABLE repw(x int); CREATE TABLE respondents(x int); INSERT INTO activities VALUES(1); INSERT INTO respondents VALUES(1)")
out={}
def inflight():
    with psycopg.connect(url) as c:  # non-autocommit like the pool
        c.execute("SELECT * FROM respondents"); c.execute("SELECT * FROM activities")
        time.sleep(4); c.commit(); out['inflight']='committed'
def loader():
    with psycopg.connect(url) as c:
        try:
            with c.transaction(): c.execute("TRUNCATE companions, activities, repw, respondents")
            out['loader']='ok'
        except Exception as e: out['loader']=repr(e)[:300]
def request():
    with psycopg.connect(url) as c:
        try:
            c.execute("SELECT DISTINCT x FROM respondents")   # _get_loaded_years
            c.execute("SELECT count(*) FROM activities a JOIN respondents r USING(x)")  # big query
            c.commit(); out['request']='ok'
        except Exception as e: out['request']=repr(e)[:300]
t1=threading.Thread(target=inflight); t1.start(); time.sleep(1)
t2=threading.Thread(target=loader); t2.start(); time.sleep(1)
t3=threading.Thread(target=request); t3.start()
for t in (t1,t2,t3): t.join()
print(out)
with psycopg.connect(base+"postgres", autocommit=True) as c: c.execute("DROP DATABASE atus_refute_rdl")
