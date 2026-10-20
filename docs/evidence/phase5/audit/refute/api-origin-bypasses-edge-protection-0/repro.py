from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
from atus_pipeline.config import load_settings
import dataclasses
s=load_settings()
try: s=dataclasses.replace(s, api_cors_origins=("https://good.example",))
except Exception as e: print("replace failed",e)
app=create_app(s, open_pool=False)
mw=[m.cls.__name__ for m in app.user_middleware]; print("middleware:",mw)
with TestClient(app, base_url="http://evil.example") as c:
    r=c.get("/openapi.json", headers={"Origin":"https://evil.example","Host":"evil.example"})
    print(r.status_code, {k:v for k,v in r.headers.items() if k.lower().startswith("access")})
    for i in range(50): r=c.get("/openapi.json")
    print("50 rapid requests last status", r.status_code)
