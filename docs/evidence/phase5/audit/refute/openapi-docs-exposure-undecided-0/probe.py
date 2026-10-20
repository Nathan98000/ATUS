from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings, load_settings
import inspect
app=create_app(load_settings(), open_pool=True)
with TestClient(app) as c:
    for p in ["/docs","/redoc","/docs/oauth2-redirect","/openapi.json","/ready"]:
        r=c.get(p); print(p,r.status_code); 
        if p in("/docs","/redoc"): 
            import re; print(re.findall(r'https://[^"\' ]+', r.text))
        if p=="/ready": print(r.text[:800])
