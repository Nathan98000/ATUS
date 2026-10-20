import os, json, psycopg
os.environ["ATUS_DATABASE_URL"]="postgresql://atus:atus@localhost:5434/atus"
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings
log=[]
orig=psycopg.Connection.execute
def rec(self,q,*a,**k):
    log.append(str(q)[:80]); return orig(self,q,*a,**k)
psycopg.Connection.execute=rec
cases={
 "unknown_field":{"activity":{"preset":"sleep"},"years":[2025],"bogus":1},
 "year_2002":{"activity":{"preset":"sleep"},"years":[2002]},
 "51_years":{"activity":{"preset":"sleep"},"years":list(range(2003,2054))},
}
app=create_app()
with TestClient(app) as c:
    c.get("/api/v1/health") if False else None
    import time; time.sleep(1)
    for n,b in cases.items():
        log.clear(); r=c.post("/api/v1/analysis/estimate",json=b)
        print(n,r.status_code,r.json().get("error",r.json()) if r.status_code!=200 else "", "SQL:",log)
    log.clear(); r=c.post("/api/v1/analysis/estimate",content=b"{bad",headers={"content-type":"application/json"})
    print("malformed",r.status_code,"SQL:",log)
# pool closed case
app2=create_app(open_pool=False)
with TestClient(app2) as c:
    import atus_pipeline.api.dependencies as d; d._CONNECTION_TIMEOUT_SECONDS=1.0
    log.clear(); r=c.post("/api/v1/analysis/estimate",json=cases["unknown_field"])
    print("nopool unknown_field",r.status_code,r.text[:200],"SQL:",log)
