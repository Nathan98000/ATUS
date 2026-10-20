from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
import inspect
app = create_app() if callable(create_app) else create_app
c = TestClient(app, raise_server_exceptions=False)
for p in ["/health","/ready","/api/v1/meta","/docs","/openapi.json"]:
    r = c.head(p); print("HEAD", p, r.status_code, r.headers.get("allow"))
print("GET /health", c.get("/health").status_code)
for r in app.routes:
    print(type(r).__name__, getattr(r,"path",None), sorted(getattr(r,"methods",None) or []))
