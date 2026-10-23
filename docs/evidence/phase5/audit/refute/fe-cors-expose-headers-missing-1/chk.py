from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient
from starlette.routing import Route
# replicate app.py:84-90 config exactly
app=Starlette(routes=[Route('/x',lambda r: JSONResponse({},headers={'X-Analysis-Key':'k','X-Cache':'miss'}),methods=['POST'])])
app.add_middleware(CORSMiddleware,allow_origins=['https://fe.example'],allow_credentials=False,allow_methods=['GET','POST'],allow_headers=['Content-Type'])
c=TestClient(app)
r=c.post('/x',headers={'Origin':'https://fe.example'})
print(dict(r.headers))
print('expose header:', r.headers.get('access-control-expose-headers'))
