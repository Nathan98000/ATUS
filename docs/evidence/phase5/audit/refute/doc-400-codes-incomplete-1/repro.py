from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from atus_pipeline.api.errors import register_exception_handlers
app=FastAPI(); register_exception_handlers(app)
class M(BaseModel):
    age_min:int|None=None
@app.post("/x")
def x(m:M): return {}
c=TestClient(app)
for name,body in [("invalid_utf8",b'{"age_min": "\xff\xfe"}'),("nested",b'['*10000+b']'*10000),("bigint",b'{"age_min": '+b'9'*5000+b'}'),("trunc",b'{"age_min":')]:
    r=c.post("/x",content=body,headers={"content-type":"application/json"}); print(name,r.status_code,r.text[:200])
print("404",c.get("/nope").text); print("405",c.put("/x").text)
