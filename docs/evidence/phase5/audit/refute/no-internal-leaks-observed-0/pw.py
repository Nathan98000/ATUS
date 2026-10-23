import os, logging, sys
os.environ["ATUS_DATABASE_URL"]=sys.argv[1]
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
with TestClient(create_app()) as c:
    for p in ["/ready","/api/v1/meta"]:
        r=c.get(p); print("RESP",p,r.status_code,r.text)
