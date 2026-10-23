import os
os.environ.setdefault("ATUS_DATABASE_URL","postgresql://atus:atus@localhost:5434/atus")
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
with TestClient(create_app()) as c:
    for p in ["/api/v1/activities","/openapi.json","/docs"]:
        r=c.get(p,headers={"Accept-Encoding":"gzip, br"})
        print(p,r.status_code,dict(r.headers),len(r.content))
