import dataclasses
from fastapi.testclient import TestClient
from atus_pipeline.config import Settings
from atus_pipeline.api.app import create_app
s = Settings(database_url="postgresql://x@localhost:1/x", data_dir="/tmp", release="2024", log_level="INFO")
s = dataclasses.replace(s, api_cors_origins=("https://fe.example",))
app = create_app(s, open_pool=False)
c = TestClient(app, raise_server_exceptions=False)
for m,p,kw in [("GET","/health",{}),("POST","/api/v1/analysis/estimate",{"json":{}})]:
    r = c.request(m,p,headers={"Origin":"https://fe.example"},**kw)
    print(m,p,r.status_code,{k:v for k,v in r.headers.items() if k.startswith(("access-control","x-"))})
