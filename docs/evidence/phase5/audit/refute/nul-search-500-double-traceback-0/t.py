import logging, os
logging.basicConfig(level=logging.INFO)
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
app = create_app()
with TestClient(app, raise_server_exceptions=False) as c:
    for q in ["%00%00", "ab%00", "ab"]:
        r = c.get("/api/v1/activities?search="+q)
        print("RESULT", q, r.status_code, r.text[:150])
