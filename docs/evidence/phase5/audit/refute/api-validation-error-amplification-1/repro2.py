import json, gzip
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
app = create_app()
with TestClient(app, raise_server_exceptions=False) as c:
    def post(name, body):
        raw = json.dumps(body, separators=(',',':')).encode()
        r = c.post("/api/v1/analysis/estimate", content=raw, headers={"content-type":"application/json","accept-encoding":"gzip"})
        print(name, "status", r.status_code, "req", len(raw), "resp", len(r.content), "ratio %.2f" % (len(r.content)/len(raw)), "enc", r.headers.get("content-encoding"), "gzip_would_be", len(gzip.compress(r.content)), r.text[:150].replace("\n"," "))
    base = {"activity":{"include":["0101"]},"years":[2023]}
    b = dict(base); [b.__setitem__(format(i,'x'),0) for i in range(100000)]; post("extrakeys100k", b)
    b = dict(base); [b.__setitem__(format(i,'x'),0) for i in range(10000)]; post("extrakeys10k", b)
    post("preset1m", {"activity":{"preset":"P"*1000000},"years":[2023]})
    b = dict(base); b["K"*100000]=0; post("longkey100k", b)
    r = c.get("/api/v1/activities"); print("get_activities", r.status_code, len(r.content))
