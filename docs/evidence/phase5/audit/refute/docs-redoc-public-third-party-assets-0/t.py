from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
import inspect
app = create_app(open_pool=False) if 'open_pool' in inspect.signature(create_app).parameters else create_app()
c = TestClient(app)
for p in ["/docs","/redoc","/docs/oauth2-redirect","/openapi.json"]:
    r = c.get(p, headers={"Accept-Encoding":"gzip"})
    print(p, r.status_code, len(r.content), dict(r.headers))
    if p!="/openapi.json":
        print([l.strip() for l in r.text.splitlines() if "http" in l])
import re; b=c.get("/openapi.json").text
print("leak:", re.findall(r"localhost|postgres|ATUS_|5434", b))
