"""Refuter repro via the real HTTP route (TestClient + real pool) on a scratch fixture DB."""
import sys, tempfile, threading, time, logging
from pathlib import Path
sys.path.insert(0, "$REPO")
import psycopg
from fastapi.testclient import TestClient
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from atus_pipeline.api.app import create_app
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure
from tests.integration.analytics_dataset import analytics_settings

REPO = Path("$REPO")
URL = "postgresql://atus:atus@localhost:5434/atus_refute_dl"
WORK = Path(sys.argv[1])
with psycopg.connect("postgresql://atus:atus@localhost:5434/postgres", autocommit=True) as c:
    if not c.execute("SELECT 1 FROM pg_database WHERE datname='atus_refute_dl'").fetchone():
        c.execute('CREATE DATABASE atus_refute_dl')
with psycopg.connect(URL) as conn:
    with conn.transaction():
        conn.execute("DROP SCHEMA IF EXISTS atus CASCADE"); conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    apply_migrations(conn, REPO / "migrations")
settings = analytics_settings(Path(tempfile.mkdtemp(dir=WORK)), URL)
with psycopg.connect(URL) as conn:
    load_all(conn, settings)
SPEC = AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY, activity=ActivitySelector.preset("sleep"), years=(2023,))

def loader_run(out, t0):
    s = analytics_settings(Path(tempfile.mkdtemp(dir=WORK)), URL)
    try:
        with psycopg.connect(URL) as conn: load_all(conn, s)
        out["loader"] = f"succeeded t={time.monotonic()-t0:.2f}"
    except Exception as e:
        out["loader"] = f"FAILED {type(e).__name__} t={time.monotonic()-t0:.2f}"

print("== Scenario B: no artificial holder; 8 client threads hammering distinct specs while 5 reloads run")
app = create_app(settings)
import logging as L
h=L.FileHandler(sys.argv[2]); L.getLogger().addHandler(h); L.getLogger().setLevel(L.ERROR)
stop = False; counts = {}
lock = threading.Lock()
with TestClient(app) as client:
    def hammer(i):
        n = 0
        while not stop:
            n += 1
            measures = ["average_minutes_per_day"]
            r = client.post("/api/v1/analysis/estimate", json={"activity": {"preset": "sleep"}, "years": [2023], "variance": "none" if n % 2 else "replicate"} )
            key = (r.status_code, r.json().get("error", {}).get("code") if r.status_code != 200 else "")
            with lock: counts[key] = counts.get(key, 0) + 1
    ths = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for t in ths: t.start()
    res = []
    for k in range(5):
        out = {}; loader_run(out, time.monotonic()); res.append(out["loader"]); time.sleep(0.3)
    stop = True
    for t in ths: t.join()
print("loader results:", res)
print("http status counts:", counts)
