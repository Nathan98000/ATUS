import json
from pathlib import Path
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings
import inspect, atus_pipeline.api.app as m
print("create_app sig:", inspect.signature(m.create_app))
s = Settings(database_url="postgresql://nouser@127.0.0.1:1/none", data_dir=Path("/tmp/none"), release="0325", log_level="INFO")
app = create_app(s, open_pool=False) if "open_pool" in str(inspect.signature(m.create_app)) else create_app(s)
with TestClient(app) as c:
    for mb in (1, 20):
        body = json.dumps({"activity": {"include": ["010101"], "label": "x"*(mb*1024*1024)}, "years": [2023]}).encode()
        r = c.post("/api/v1/analysis/estimate", content=body, headers={"content-type":"application/json"})
        print(f"{mb} MB body ({len(body)} bytes): status={r.status_code} resp_len={len(r.content)} {r.text[:200]}")
