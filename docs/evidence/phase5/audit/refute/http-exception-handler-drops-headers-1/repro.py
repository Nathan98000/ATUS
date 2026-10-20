from fastapi.testclient import TestClient
from atus_pipeline.api import app as m
import inspect
app = m.create_app() if hasattr(m,'create_app') else m.app
c = TestClient(app)
for meth, path in [("PUT","/health"),("DELETE","/meta"),("POST","/health")]:
    r = c.request(meth, path)
    print(meth, path, r.status_code, {k:v for k,v in r.headers.items()}, r.text)
