from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from atus_pipeline.api.errors import register_exception_handlers
app=FastAPI(); register_exception_handlers(app)
@app.get("/h")
def h(): return {}
@app.get("/r")
def r(): raise HTTPException(429, "slow", headers={"Retry-After":"5"})
c=TestClient(app)
x=c.put("/h"); print(x.status_code, dict(x.headers), x.text)
x=c.get("/r"); print(x.status_code, dict(x.headers), x.text)
app2=FastAPI()
@app2.get("/h")
def h2(): return {}
x=TestClient(app2).put("/h"); print("default:",x.status_code, dict(x.headers))
