import json
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
import inspect; print(inspect.signature(create_app))
app = create_app()
c = TestClient(app, raise_server_exceptions=False)
body = {"measure":"average_hours_per_day","activity":{"include":["0101"]},"years":[2023]}
for i in range(100000): body[format(i,'x')] = 0
raw = json.dumps(body, separators=(',',':')).encode()
r = c.post("/api/v1/analysis/estimate", content=raw, headers={"content-type":"application/json","accept-encoding":"gzip"})
print("extrakeys", r.status_code, len(raw), len(r.content), r.headers.get("content-encoding"), r.text[:300])
