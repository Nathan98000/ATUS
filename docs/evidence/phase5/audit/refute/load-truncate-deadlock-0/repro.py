import sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, "$REPO")
import psycopg
from atus_pipeline.database.migrate import apply_migrations
import atus_pipeline.loading.loader as loader
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure
from atus_pipeline.api.dependencies import read_data_version
from tests.integration.analytics_dataset import analytics_settings
ADMIN="postgresql://atus:atus@localhost:5434/postgres"
URL="postgresql://atus:atus@localhost:5434/atus_audit_refute_dl"
WORK=Path(sys.argv[1])
with psycopg.connect(ADMIN, autocommit=True) as c:
    c.execute('DROP DATABASE IF EXISTS atus_audit_refute_dl'); c.execute('CREATE DATABASE atus_audit_refute_dl')
with psycopg.connect(URL) as c:
    apply_migrations(c, Path("$REPO/migrations"))
    loader.load_all(c, analytics_settings(Path(tempfile.mkdtemp(dir=WORK)), URL))
SPEC=AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY, activity=ActivitySelector.preset("sleep"), years=(2023,))
for trial in range(3):
    out={}; t0=time.monotonic(); st=lambda: f"t={time.monotonic()-t0:.2f}"
    def run_loader():
        try:
            with psycopg.connect(URL) as conn: loader.load_all(conn, analytics_settings(Path(tempfile.mkdtemp(dir=WORK)), URL))
            out["loader"]="ok "+st()
        except Exception as e: out["loader"]=f"FAILED {type(e).__name__} {st()}"
    def new_req():
        with psycopg.connect(URL) as conn:
            try:
                read_data_version(conn); r=AnalysisEngine(conn).estimate(SPEC); out["new"]=f"ok {r.estimate.value} {st()}"
            except Exception as e: out["new"]=f"FAILED {type(e).__name__} {st()}"
    with psycopg.connect(URL) as lc:
        read_data_version(lc); AnalysisEngine(lc).estimate(SPEC)
        lt=threading.Thread(target=run_loader); lt.start(); time.sleep(0.5)
        nt=threading.Thread(target=new_req); nt.start()
        lc.execute("SELECT pg_sleep(2.5)")  # long-running statement, not idle
        lc.commit(); out["long"]="done "+st()
    lt.join(); nt.join(); print(trial, out, flush=True)
with psycopg.connect(URL) as c:
    print(c.execute("SELECT id,status,left(notes,50) FROM atus.ingestion_runs ORDER BY id").fetchall())
with psycopg.connect(ADMIN, autocommit=True) as c: c.execute('DROP DATABASE atus_audit_refute_dl')
