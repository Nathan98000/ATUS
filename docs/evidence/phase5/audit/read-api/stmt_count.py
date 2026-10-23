"""Server-side statement counts per request type (log_statement=all on the scratch
DB only), plus the dead-connection behaviour after pg_terminate_backend.
API PIDs are captured client-side via a pool `configure` hook that only reads
conn.info.backend_pid (no SQL)."""
import json, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
import psycopg
from psycopg_pool import ConnectionPool
from fastapi.testclient import TestClient

REPO = Path("$REPO")
sys.path.insert(0, str(REPO))
import atus_pipeline.api.app as appmod
from atus_pipeline.config import Settings

URL = "postgresql://atus:atus@localhost:5434/atus_audit_readapi"
PIDS = set()

class TracedPool(ConnectionPool):
    def __init__(self, *a, **kw):
        assert "configure" not in kw and "check" not in kw, kw  # confirm app passes neither
        kw["configure"] = lambda c: PIDS.add(c.info.backend_pid)
        print("pool kwargs from app:", {k: v for k, v in kw.items() if k not in ("conninfo", "configure")})
        super().__init__(*a, **kw)

appmod.ConnectionPool = TracedPool
settings = Settings(database_url=URL, data_dir=Path("/tmp/none"), release="0325", log_level="WARNING")
app = appmod.create_app(settings)
marker = psycopg.connect(URL, autocommit=True)
def mark(name):
    marker.execute("SELECT %s::text", (f"AUDITMARK {name}",))

EST = {"activity": {"include": ["010101"]}, "years": [2023]}
SCEN = [
    ("GET /health", "get", "/health", None),
    ("GET /ready", "get", "/ready", None),
    ("GET /api/v1/meta", "get", "/api/v1/meta", None),
    ("GET /api/v1/population/metadata", "get", "/api/v1/population/metadata", None),
    ("GET /api/v1/activities/presets", "get", "/api/v1/activities/presets", None),
    ("GET /api/v1/activities (full)", "get", "/api/v1/activities", None),
    ("GET /api/v1/activities?search=sleep", "get", "/api/v1/activities?search=sleep", None),
    ("GET /api/v1/activities/010101 (detail)", "get", "/api/v1/activities/010101", None),
    ("GET /api/v1/activities/abc (malformed->404)", "get", "/api/v1/activities/abc", None),
    ("GET /api/v1/activities/999999 (unknown->404)", "get", "/api/v1/activities/999999", None),
    ("GET /api/v1/activities?level=9 (422 query)", "get", "/api/v1/activities?level=9", None),
    ("POST estimate replicate (miss)", "post", "/api/v1/analysis/estimate", EST),
    ("POST estimate replicate (hit)", "post", "/api/v1/analysis/estimate", EST),
    ("POST estimate variance=none (miss)", "post", "/api/v1/analysis/estimate", {**EST, "variance": "none"}),
    ("POST trend 2019,2020,2023 (miss)", "post", "/api/v1/analysis/trend", {**EST, "years": [2019, 2020, 2023]}),
    ("POST trend (hit)", "post", "/api/v1/analysis/trend", {**EST, "years": [2019, 2020, 2023]}),
    ("POST compare (miss)", "post", "/api/v1/analysis/compare", {**EST, "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}),
    ("POST compare (hit)", "post", "/api/v1/analysis/compare", {**EST, "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}),
    ("POST estimate cache-bust via confidence_level (miss)", "post", "/api/v1/analysis/estimate", {**EST, "confidence_level": 0.9500001}),
    ("POST estimate cache-bust via label (miss)", "post", "/api/v1/analysis/estimate", {"activity": {"include": ["010101"], "label": "x1"}, "years": [2023]}),
    ("POST estimate malformed JSON (400)", "raw", "/api/v1/analysis/estimate", b"{not json"),
    ("POST estimate schema-invalid years=[] (422 validation_error)", "post", "/api/v1/analysis/estimate", {**EST, "years": []}),
    ("POST estimate extra field (422 validation_error)", "post", "/api/v1/analysis/estimate", {**EST, "bogus": 1}),
    ("POST estimate duplicate years (422 invalid_spec)", "post", "/api/v1/analysis/estimate", {**EST, "years": [2023, 2023]}),
    ("POST estimate unknown activity (422)", "post", "/api/v1/analysis/estimate", {"activity": {"include": ["999999"]}, "years": [2023]}),
    ("POST estimate 2020 multiyear (422 unsupported)", "post", "/api/v1/analysis/estimate", {**EST, "years": [2020]}),
    ("POST estimate year not loaded 2010 (422)", "post", "/api/v1/analysis/estimate", {**EST, "years": [2010]}),
    ("GET /nope (404)", "get", "/nope", None),
    ("GET estimate (405)", "get", "/api/v1/analysis/estimate", None),
    ("GET /openapi.json", "get", "/openapi.json", None),
    ("GET /docs", "get", "/docs", None),
    ("GET /redoc", "get", "/redoc", None),
]
results = []
start = datetime.now(timezone.utc).isoformat()
with TestClient(app, raise_server_exceptions=False) as client:
    time.sleep(1.0)  # let the background pool connect (min_size=1)
    mark("STARTUP_DONE")
    for name, kind, path, body in SCEN:
        mark(name)
        if kind == "get":
            r = client.get(path)
        elif kind == "post":
            r = client.post(path, json=body)
        else:
            r = client.post(path, content=body, headers={"content-type": "application/json"})
        results.append((name, r.status_code, r.headers.get("x-cache"), r.text[:300] if r.status_code >= 400 else ""))
    # dead-connection experiment
    mark("TERMINATE")
    pids_before = sorted(PIDS)
    admin = psycopg.connect("postgresql://atus:atus@localhost:5434/postgres", autocommit=True)
    killed = admin.execute(
        "SELECT pid, pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='atus_audit_readapi' AND pid = ANY(%s)",
        (pids_before,)).fetchall()
    time.sleep(0.5)
    dead = []
    for i, (path, body) in enumerate([("/api/v1/meta", None), ("/api/v1/meta", None),
                                      ("/api/v1/analysis/estimate", {**EST, "variance": "none", "confidence_level": 0.91})]):
        mark(f"AFTER_TERMINATE_{i} {path}")
        r = client.post(path, json=body) if body else client.get(path)
        dead.append((i, path, r.status_code, r.text[:200]))
    mark("TERMINATE2")
    killed2 = admin.execute(
        "SELECT pid, pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='atus_audit_readapi' AND pid = ANY(%s)",
        (sorted(PIDS),)).fetchall()
    time.sleep(0.5)
    mark("AFTER_TERMINATE2 analysis-first")
    r = client.post("/api/v1/analysis/estimate", json={**EST, "variance": "none", "confidence_level": 0.92})
    dead.append(("analysis-first-after-kill", "/api/v1/analysis/estimate", r.status_code, r.text[:200]))
    mark("END")
    stats = app.state.pool.get_stats()
time.sleep(1.5)
logs = subprocess.run(["docker", "logs", "--since", start, "atus-postgres"], capture_output=True, text=True)
raw = logs.stdout + logs.stderr
Path(__file__).with_name("logs").joinpath("stmt_count_pg.log").write_text(raw)
pat = re.compile(r"^\S+ \S+ \S+ \[(\d+)\] LOG:  (?:statement|execute [^:]*): (.*)$")
current, per = None, {}
order = []
for line in sorted(raw.splitlines(), key=lambda l: l[:23]) if False else raw.splitlines():
    m = pat.match(line)
    if not m:
        continue
    pid, stmt = int(m.group(1)), m.group(2)
    if "AUDITMARK" in stmt:
        current = stmt.split("AUDITMARK ", 1)[1].rstrip("'")  # marker text
        current = re.sub(r"'::text$", "", current)
        per.setdefault(current, []); order.append(current)
        continue
    if pid in PIDS and current is not None:
        per[current].append(stmt[:90])
out = {"pool_stats": stats, "api_pids": sorted(PIDS), "killed": killed, "killed2": killed2,
       "results": results, "dead_conn": dead, "statements": {k: per[k] for k in order}}
Path(__file__).with_name("logs").joinpath("stmt_count.json").write_text(json.dumps(out, indent=1, default=str))
for name, status, xc, err in results:
    stmts = per.get(name, [])
    print(f"{status} cache={xc} n={len(stmts):2d}  {name}  :: {[s[:40] for s in stmts]}")
    if err: print("     body:", err[:200])
for d in dead: print("DEAD", d)
for k in order:
    if k.startswith("AFTER") or k.startswith("TERM") or k in ("STARTUP_DONE", "END"):
        print(k, per[k])
print("pool stats", stats)
