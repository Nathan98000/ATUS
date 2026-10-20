import time, random
from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
with TestClient(create_app()) as c:
  if True:
    Y=list(range(2003,2026)); P=[y for y in Y if y!=2020]
    for i in range(3):
        for name,path,body in [("pooled22","/api/v1/analysis/estimate",{"activity":{"preset":"leisure_and_sports_bls_table"},"years":P}),
                               ("trend23_12","/api/v1/analysis/trend",{"activity":{"include":["12"]},"years":Y})]:
            body=dict(body, confidence_level=round(0.9+random.random()*0.05,4))  # unique spec => cache miss
            t=time.perf_counter(); r=c.post(path,json=body); dt=time.perf_counter()-t
            print(name,i,r.status_code,r.headers.get("x-cache"),round(dt,2),flush=True)
