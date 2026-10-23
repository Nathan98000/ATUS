import contextlib, json
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
from atus_pipeline.api.dependencies import get_engine
log=[]
class Cur:
    def __init__(s,sql): s.sql=sql
    def fetchone(s): return ("2024",7)
class Conn:
    def execute(s,sql,*a,**k): log.append(sql.split()[0:4]); return Cur(sql)
class Pool:
    @contextlib.contextmanager
    def connection(s,timeout=None):
        log.append("CHECKOUT"); yield Conn()
class FakeEngine:
    def estimate(s,spec): raise AssertionError("compute called")
app=create_app(open_pool=False)
app.dependency_overrides[get_engine]=lambda: FakeEngine()
c=TestClient(app)
app.state.pool=Pool()
class FC:
    def get(s,k): return {'fake':'hit'}
    def put(s,k,v): pass
app.state.cache=FC()
def run(name,fn):
    log.clear()
    try:
        r=fn(); print(name, r.status_code, r.headers.get("X-Cache"), log)
    except Exception as e: print(name, "EXC(fake hit payload failed response validation after handler returned)", type(e).__name__, log)
body={"activity":{"preset":"sleep"},"years":[2023]}
run("estimate cache-hit", lambda: c.post("/api/v1/analysis/estimate", json=body))
run("estimate schema-invalid extra", lambda: c.post("/api/v1/analysis/estimate", json={"zzz":1}))
run("estimate malformed json", lambda: c.post("/api/v1/analysis/estimate", content=b"{", headers={"content-type":"application/json"}))
run("activities level=9", lambda: c.get("/api/v1/activities?level=9"))
