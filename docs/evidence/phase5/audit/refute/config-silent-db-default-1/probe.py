import os, time
os.environ.pop("ATUS_DATABASE_URL", None)
from atus_pipeline import config
s = config.load_settings()
print("default url used:", s.database_url)
# emulate production where localhost:5434 is unreachable: use closed port 8437
from dataclasses import replace
s2 = replace(s, database_url="postgresql://atus:atus@localhost:8437/atus")
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
app = create_app(s2)
with TestClient(app) as c:
    print("startup ok (no fail-fast)")
    t=time.time(); r=c.get("/health"); print("/health", r.status_code, round(time.time()-t,2))
    t=time.time(); r=c.get("/ready"); print("/ready", r.status_code, round(time.time()-t,2), r.text[:200])
