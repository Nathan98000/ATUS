import json
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
app = create_app(open_pool=True)
c = TestClient(app, raise_server_exceptions=False).__enter__()
body = {"measure":"average_minutes_per_day","activity":{"include":["0101"]},"years":[2023]}
for i in range(100000): body[format(i,"x")] = 0
data = json.dumps(body).encode()
r = c.post("/api/v1/analysis/estimate", content=data, headers={"Content-Type":"application/json","Accept-Encoding":"gzip"})
print("extra", len(data), r.status_code, len(r.content), r.headers.get("content-encoding"), r.text[:300])
body={"measure":"average_minutes_per_day","activity":{"include":["0101"]},"years":[2023], "k"*100000:0}
data=json.dumps(body).encode()
r = c.post("/api/v1/analysis/estimate", content=data, headers={"Content-Type":"application/json"})
print("longkey", len(data), r.status_code, len(r.content))
body={"measure":"average_minutes_per_day","activity":{"preset":"P"*1000000},"years":[2023]}
data=json.dumps(body).encode()
r = c.post("/api/v1/analysis/estimate", content=data, headers={"Content-Type":"application/json"})
print("preset", len(data), r.status_code, len(r.content), r.text[:150])
