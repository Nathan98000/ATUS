"""7.5: run every endpoint (hits, misses, errors) with default_transaction_read_only=on
set on the scratch database; any write attempt would raise ReadOnlySqlTransaction (-> 500)."""
import sys
from pathlib import Path
import psycopg
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings
URL = "postgresql://atus:atus@localhost:5434/atus_audit_readapi"
with psycopg.connect(URL) as c:
    print("default_transaction_read_only =", c.execute("SHOW default_transaction_read_only").fetchone()[0])
app = create_app(Settings(database_url=URL, data_dir=Path("/tmp/none"), release="0325", log_level="WARNING"))
E = {"activity": {"include": ["010101"]}, "years": [2023]}
calls = [("get", "/ready", None), ("get", "/api/v1/meta", None), ("get", "/api/v1/activities", None),
         ("get", "/api/v1/activities?search=sle&level=3", None), ("get", "/api/v1/activities/0101", None),
         ("get", "/api/v1/activities/010101", None), ("get", "/api/v1/activities/999999", None),
         ("get", "/api/v1/activities/presets", None), ("get", "/api/v1/population/metadata", None)]
for v in ("replicate", "none"):
    calls += [("post", "/api/v1/analysis/estimate", {**E, "variance": v}),
              ("post", "/api/v1/analysis/estimate", {**E, "variance": v}),
              ("post", "/api/v1/analysis/trend", {**E, "years": [2019, 2020, 2023], "variance": v}),
              ("post", "/api/v1/analysis/estimate", {**E, "years": [2019, 2020], "weights": "pandemic", "variance": v}),
              ("post", "/api/v1/analysis/compare", {**E, "variance": v, "group_a": {"sex": "male", "education_level": "bachelor_or_higher"}, "group_b": {"sex": "female", "region": 3}})]
calls += [("post", "/api/v1/analysis/estimate", {**E, "years": [2020]}), ("post", "/api/v1/analysis/estimate", {**E, "years": []})]
with TestClient(app, raise_server_exceptions=False) as cl:
    for m, p, b in calls:
        r = cl.post(p, json=b) if m == "post" else cl.get(p)
        print(r.status_code, r.headers.get("x-cache", "-"), m.upper(), p, (r.json().get("error", {}) or {}).get("code") if r.status_code >= 400 else "")
