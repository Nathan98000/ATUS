import sys, time, threading, tempfile, contextlib
from pathlib import Path
import psycopg
REPO = Path("$REPO"); sys.path.insert(0, str(REPO))
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading import loader
from atus_pipeline.api.app import create_app
from tests.integration.analytics_dataset import analytics_settings
from fastapi.testclient import TestClient
DB = "atus_refute_sstx"
URL = f"postgresql://atus:atus@localhost:5434/{DB}"
with psycopg.connect("postgresql://atus:atus@localhost:5434/postgres", autocommit=True) as c:
    c.execute(f'DROP DATABASE IF EXISTS "{DB}"'); c.execute(f'CREATE DATABASE "{DB}"')
with psycopg.connect(URL) as conn:
    apply_migrations(conn, REPO / "migrations")
tmp = tempfile.mkdtemp(dir=".")
settings = analytics_settings(Path(tmp), URL)
with psycopg.connect(URL) as conn:
    loader.load_all(conn, settings)
print("run1 loaded")

class W:
    def __init__(s, c): s._c = c; s.n = 0
    def __getattr__(s, k): return getattr(s._c, k)
    @contextlib.contextmanager
    def transaction(s, *a, **k):
        s.n += 1
        if s.n == 3:   # the final 'succeeded' transaction: widen the window
            print(f"{time.monotonic()-T0:.2f}s data committed; pausing 3s before succeeded UPDATE", flush=True)
            time.sleep(3)
        with s._c.transaction(*a, **k) as t:
            yield t
            if s.n == 2:
                print(f"{time.monotonic()-T0:.2f}s data tx about to commit (sleep 2s first)", flush=True)
                time.sleep(2)

def reload():
    with psycopg.connect(URL) as conn:
        loader.load_all(W(conn), settings)
    print(f"{time.monotonic()-T0:.2f}s run2 succeeded committed", flush=True)

app = create_app(settings)
body = {"activity": {"preset": "sleep"}, "years": [2023]}
with TestClient(app) as client:
    r = client.post("/api/v1/analysis/estimate", json=body)
    print("pre-reload", r.status_code, r.headers.get("x-cache"), r.text[:200])
    app.state.cache._entries.clear() if hasattr(app.state.cache, "_entries") else None
    T0 = time.monotonic()
    th = threading.Thread(target=reload); th.start()
    time.sleep(1.0)
    print(f"{time.monotonic()-T0:.2f}s request sent (load in progress, tables truncated)", flush=True)
    r = client.post("/api/v1/analysis/estimate", json={**body, "variance": "replicate"} if False else body | {"years": None} if False else body)
    print(f"{time.monotonic()-T0:.2f}s request done", r.status_code, "X-Cache", r.headers.get("x-cache"), "key", r.headers.get("x-analysis-key"))
    with psycopg.connect(URL) as c:
        print("  during window: latest succeeded run =", c.execute("select max(id) from atus.ingestion_runs where status='succeeded'").fetchone(),
              "; source_file run_ids visible =", c.execute("select distinct ingestion_run_id from atus.source_files").fetchall())
    th.join()
    r2 = client.post("/api/v1/analysis/estimate", json=body)
    print("after flip", r2.headers.get("x-cache"), r2.headers.get("x-analysis-key"))
    print("cache keys:", list(getattr(app.state.cache, "_entries", {}).keys()) if hasattr(app.state.cache,"_entries") else dir(app.state.cache))
with psycopg.connect("postgresql://atus:atus@localhost:5434/postgres", autocommit=True) as c:
    c.execute(f'DROP DATABASE "{DB}"')
print("dropped")
