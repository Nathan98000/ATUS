import os
os.environ["ATUS_API_CORS_ORIGINS"]="https://allowed.example"
os.environ.setdefault("ATUS_DATABASE_URL","postgresql://atus:atus@localhost:5434/atus")
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
app=create_app()
with TestClient(app) as c:
    r=c.get("/health",headers={"Origin":"https://allowed.example"})
    print(r.status_code, dict(r.headers))
    r=c.post("/api/v1/analysis/estimate",headers={"Origin":"https://allowed.example"},json={"activity":{"include":["0101"]},"years":[2023]})
    print(r.status_code, dict(r.headers))
