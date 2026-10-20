from fastapi.testclient import TestClient
from atus_pipeline.api.app import create_app
from atus_pipeline.config import load_settings
import os
app = create_app(open_pool=False)
c = TestClient(app)
paths=[]
pass
post="/api/v1/analysis/estimate"
def show(name,r): print(name, r.status_code, r.headers.get('content-type'), r.text[:300])
show('invalid_utf8', c.post(post, content=b'{"a":"\xff\xfe"}', headers={'content-type':'application/json'}))
show('nested', c.post(post, content=b'['*10001+b']'*10001, headers={'content-type':'application/json'}))
show('bigint', c.post(post, content=b'{"age_min":'+b'9'*5000+b'}', headers={'content-type':'application/json'}))
show('badjson', c.post(post, content=b'{bad', headers={'content-type':'application/json'}))
show('unknown', c.get('/nope'))
show('put_health', c.put('/health'))
show('preflight', c.options(post, headers={'Origin':'https://evil.example','Access-Control-Request-Method':'POST'}))
