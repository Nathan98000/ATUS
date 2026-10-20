"""API side through a real psycopg_pool ConnectionPool (same config as app.py) and the real
dependency functions; loader side = the real load_all() (full TRUNCATE+reload of the fixture)."""
import sys, threading, time, tempfile
from pathlib import Path
REPO=Path("$REPO"); sys.path.insert(0,str(REPO))
import psycopg
from psycopg_pool import ConnectionPool
from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.analytics.spec import AnalysisSpec, ActivitySelector, Measure, PopulationFilter
from atus_pipeline.api.dependencies import read_data_version
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings
URL="postgresql://atus:atus@localhost:5434/atus_audit_refdl"
pool=ConnectionPool(conninfo=URL,min_size=1,max_size=10,open=True,name="atus-api"); pool.wait()
with psycopg.connect(URL) as c:
    years=[r[0] for r in c.execute("select distinct data_year from atus.respondents order by 1")]
print("years", years)
spec=AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY, activity=ActivitySelector(include=("010101",)),
    years=(years[0],), population=PopulationFilter(), weights="multiyear" if False else "multiyear", variance="replicate", confidence_level=0.95)
t0=time.monotonic(); res={}
with pool.connection() as a:
    print("pool conn autocommit:", a.autocommit)
    read_data_version(a); eng=AnalysisEngine(a); prep=eng._prepare(spec)
    print(f"t={time.monotonic()-t0:.2f} after _prepare, txn status={a.info.transaction_status.name}")
    def loader():
        try:
            with psycopg.connect(URL) as b:
                load_all(b, analytics_settings(Path(tempfile.mkdtemp(dir=sys.argv[1])), URL))
            res["loader"]="load_all succeeded"
        except Exception as e: res["loader"]=f"{type(e).__module__}.{type(e).__name__}: {e}".splitlines()[0]
    th=threading.Thread(target=loader); th.start(); time.sleep(0.5)
    with psycopg.connect(URL, autocommit=True) as m:
        for r in m.execute("select pid, wait_event_type, left(query,60) from pg_stat_activity where datname='atus_audit_refdl' and wait_event_type='Lock'"): print("waiting:", r)
    try: res["api"]=f"OK {list(eng._fetch(spec, prep))}"
    except Exception as e: res["api"]=f"{type(e).__name__}: {e}".splitlines()[0]
    print(f"t={time.monotonic()-t0:.2f} api: {res['api']}")
th.join(); print(f"t={time.monotonic()-t0:.2f} loader: {res['loader']}")
with psycopg.connect(URL) as c:
    print("last runs:", c.execute("select id,status,left(notes,80) from atus.ingestion_runs order by id desc limit 2").fetchall())
pool.close()
