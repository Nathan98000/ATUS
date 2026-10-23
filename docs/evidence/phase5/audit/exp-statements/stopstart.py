"""DB down for ~8 s (docker stop; docker start) with sequential GET /api/v1/meta
polling: what do requests get while the DB is down and right after it returns?"""
import json, time, subprocess, threading, urllib.request, urllib.error, sys
API = "http://127.0.0.1:8204"; T0 = time.time(); out = open(sys.argv[1], "a")
def p(*a):
    line = f"[t={time.time()-T0:6.2f}s] " + " ".join(map(str, a)); print(line, file=out, flush=True); print(line, flush=True)
def meta():
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(API + "/api/v1/meta", timeout=60) as r: st, code = r.status, "ok"
    except urllib.error.HTTPError as e:
        st, code = e.code, json.loads(e.read())["error"]["code"]
    return st, code, round((time.perf_counter() - t) * 1000)
p("warm", meta())
def db():
    time.sleep(1); p("docker stop"); subprocess.run(["docker", "stop", "atus-audit-stmt"], capture_output=True); p("stopped")
    time.sleep(8); p("docker start"); subprocess.run(["docker", "start", "atus-audit-stmt"], capture_output=True); p("started")
th = threading.Thread(target=db); th.start()
while time.time() - T0 < 40:
    t = time.time() - T0; r = meta(); p(f"meta sent t={t:.2f} ->", r); time.sleep(0.5)
th.join()
