import os
os.environ.setdefault("ATUS_DATABASE_URL","postgresql://atus:atus@localhost:5434/atus")
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
app=create_app()
with TestClient(app) as c:
    for p in ["/health","/ready","/api/v1/meta","/docs","/openapi.json"]:
        g=c.get(p); h=c.head(p)
        print(p,"GET",g.status_code,"HEAD",h.status_code,h.headers.get("allow"))
