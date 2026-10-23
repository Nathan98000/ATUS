import asyncio
from atus_pipeline.api.app import create_app
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
app = create_app(open_pool=False)
wrapped = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
async def call(a, headers, client=("127.0.0.1", 5555)):
    scope={"type":"http","asgi":{"version":"3.0"},"http_version":"1.1","method":"GET","scheme":"http","path":"/api/v1/meta/","raw_path":b"/api/v1/meta/","query_string":b"","root_path":"","headers":[(k.lower().encode(),v.encode()) for k,v in headers],"client":client,"server":("127.0.0.1",8000)}
    out=[]
    async def receive(): return {"type":"http.request","body":b"","more_body":False}
    async def send(m): out.append(m)
    await a(scope,receive,send)
    s=out[0]; print(headers, client, "->", s["status"], [h for h in s["headers"] if h[0]==b"location"])
asyncio.run(call(app,[("Host","evil.example")]))
asyncio.run(call(wrapped,[("Host","evil.example"),("X-Forwarded-Proto","https")]))
asyncio.run(call(wrapped,[("Host","evil.example"),("X-Forwarded-Proto","https")],client=("10.0.0.5",1)))
