import asyncio, os
os.environ.pop("FORWARDED_ALLOW_IPS", None)
from uvicorn.config import Config
seen={}
async def app(scope, receive, send): seen['c']=scope.get('client'); seen['s']=scope.get('scheme')
cfg=Config(app=app); cfg.load()
print("proxy_headers", cfg.proxy_headers, "allow", cfg.forwarded_allow_ips)
async def run(peer, hdrs):
    sc={'type':'http','client':(peer,5555),'scheme':'http','headers':[(k.encode(),v.encode()) for k,v in hdrs]}
    await cfg.loaded_app(sc,None,None); return dict(seen)
for peer,h in [("127.0.0.1",[("x-forwarded-for","203.0.113.66")]),("127.0.0.1",[("x-forwarded-for","198.51.100.1, 203.0.113.77")]),("127.0.0.1",[("x-forwarded-for","<script>, $(id), ../../")]),("127.0.0.1",[("x-forwarded-proto","https")]),("127.0.0.1",[("forwarded","for=192.0.2.43;proto=https")]),("10.0.0.5",[("x-forwarded-for","203.0.113.66"),("x-forwarded-proto","https")])]:
    print(peer,h,asyncio.run(run(peer,h)))
