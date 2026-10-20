"""Silent connection loss (no FATAL message to the client): `docker kill`
(SIGKILL) the DB container, start it again, wait until it accepts
connections, then send requests to the still-warm API."""
import json, time, subprocess, sys, urllib.request, urllib.error
import psycopg
API = "http://127.0.0.1:8204"; out = open(sys.argv[1], "a"); T0 = time.time()
def p(*a):
    line = f"[t={time.time()-T0:6.2f}s] " + " ".join(map(str, a)); print(line, file=out, flush=True); print(line, flush=True)
def req(m, path, body=None):
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(API + path, data=data, method=m, headers={"Content-Type": "application/json"} if data else {})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=60) as x: st, b = x.status, x.read()
    except urllib.error.HTTPError as e: st, b = e.code, e.read()
    j = json.loads(b); return st, (j["error"]["code"] if "error" in j else "ok"), round((time.perf_counter() - t) * 1000, 1)
EST = {"activity": {"include": ["010101"], "label": "Sleeping"}, "years": [2023]}
p("warm", req("GET", "/api/v1/meta"), req("POST", "/api/v1/analysis/estimate", EST))
p("docker kill"); subprocess.run(["docker", "kill", "atus-audit-stmt"], capture_output=True)
p("docker start"); subprocess.run(["docker", "start", "atus-audit-stmt"], capture_output=True)
while True:
    try: psycopg.connect("postgresql://atus:atus@127.0.0.1:5442/atus?application_name=monitor", connect_timeout=1).close(); break
    except Exception: time.sleep(0.2)
p("DB accepting connections; sending requests")
for m, path, body in [("GET", "/api/v1/meta", None), ("POST", "/api/v1/analysis/estimate", EST)] + [("GET", "/api/v1/meta", None)] * 8:
    p("  ", m, path, "->", req(m, path, body))
