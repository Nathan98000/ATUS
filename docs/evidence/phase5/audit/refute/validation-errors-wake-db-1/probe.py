import psycopg
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
log=[]
orig=psycopg.Connection.execute
def rec(self,q,*a,**k):
    log.append(str(q)[:60]); return orig(self,q,*a,**k)
psycopg.Connection.execute=rec
cases={
 "unknown_field":{"activity":{"preset":"sleep"},"years":[2025],"bogus":1},
 "year_2002":{"activity":{"preset":"sleep"},"years":[2002]},
 "51_years":{"activity":{"preset":"sleep"},"years":list(range(2003,2054))},
 "missing_body_field":{"years":[2025]},
}
with TestClient(create_app()) as c:
    import time; time.sleep(1)
    for n,b in cases.items():
        log.clear(); r=c.post("/api/v1/analysis/estimate",json=b)
        print(n,r.status_code,r.json().get("error",r.json()) if r.status_code!=200 else "", "SQL:",[x for x in log if 'ingestion_runs' in x or True])
    log.clear(); r=c.post("/api/v1/analysis/estimate",content=b"{bad",headers={"content-type":"application/json"})
    print("malformed",r.status_code,"SQL:",log)
    c.app.state.pool.close()
    log.clear(); r=c.post("/api/v1/analysis/estimate",json=cases["unknown_field"])
    print("pool_closed unknown_field",r.status_code,r.text[:200])
