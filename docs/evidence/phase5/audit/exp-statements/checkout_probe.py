"""In-process (TestClient) check of which request types check out a pool
connection even when they send no statement: pool.get_stats()['requests_num']
delta per request. Same app factory, scratch DB on 5442."""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings
s = Settings(database_url="postgresql://atus:atus@127.0.0.1:5442/atus?application_name=inproc",
             data_dir=Path("/nonexistent"), release="0325", log_level="WARNING")
app = create_app(s)
cases = [("GET", "/health", None, None), ("GET", "/api/v1/activities/presets", None, None),
         ("GET", "/api/v1/population/metadata", None, None), ("GET", "/api/v1/activities/abc", None, None),
         ("GET", "/api/v1/activities?level=9", None, None), ("GET", "/api/v1/nope", None, None),
         ("POST", "/api/v1/analysis/estimate", None, '{"activity": '),
         ("POST", "/api/v1/analysis/estimate", {"activity": {"include": ["010101"]}, "years": "x"}, None),
         ("GET", "/openapi.json", None, None), ("GET", "/api/v1/meta", None, None)]
with TestClient(app, raise_server_exceptions=False) as c:
    pool = app.state.pool
    pool.wait(10)
    for m, path, body, raw in cases:
        before = pool.get_stats().get("requests_num", 0)
        if raw is not None:
            r = c.request(m, path, content=raw, headers={"Content-Type": "application/json"})
        else:
            r = c.request(m, path, json=body)
        after = pool.get_stats().get("requests_num", 0)
        print(f"{m} {path} {'(raw)' if raw else ''} -> {r.status_code}  pool checkouts={after-before}")
