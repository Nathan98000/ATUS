import threading, time, urllib.request, uvicorn
from atus_pipeline.api.app import create_app
from atus_pipeline.config import load_settings
import os
os.environ.setdefault("ATUS_DATABASE_URL","postgresql://nobody:x@127.0.0.1:1/none")
app=create_app(open_pool=False)
cfg=uvicorn.Config(app,host="127.0.0.1",port=8437,log_level="warning")
srv=uvicorn.Server(cfg); t=threading.Thread(target=srv.run,daemon=True); t.start(); time.sleep(2)
for p in ["/docs","/openapi.json","/api/v1/nope","/health"]:
    try: r=urllib.request.urlopen("http://127.0.0.1:8437"+p); h=r.headers; st=r.status
    except urllib.error.HTTPError as e: h=e.headers; st=e.code
    print(p,st,dict(h))
srv.should_exit=True; time.sleep(1)
