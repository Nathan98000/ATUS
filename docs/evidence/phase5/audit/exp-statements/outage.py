"""(c) DB container restart / pause while the API stays warm.
restart: poll GET /api/v1/meta every 0.5 s (sequentially) from 1 s before a
`docker restart` until 5 s after the DB accepts connections again, then run a
short request sequence. pause N C: `docker pause` for N s with C concurrent
GET /api/v1/meta in flight, then unpause and run the sequence."""
import json, sys, time, subprocess, datetime, threading, urllib.request, urllib.error
import psycopg
API = "http://127.0.0.1:8204"
DSN = "postgresql://atus:atus@127.0.0.1:5442/atus?application_name=monitor"
mode = sys.argv[1]; out = open(sys.argv[2], "a")
T0 = time.time()
def p(*a):
    line = f"[t={time.time()-T0:7.2f}s] " + " ".join(str(x) for x in a)
    print(line, file=out, flush=True); print(line, flush=True)
def http(method, path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            st, b, h = r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        st, b, h = e.code, e.read(), dict(e.headers)
    except Exception as e:
        st, b, h = "EXC", repr(e).encode(), {}
    try:
        j = json.loads(b); code = j["error"]["code"] if "error" in j else "ok"
    except Exception:
        code = b[:100]
    return st, code, round((time.perf_counter() - t0) * 1000, 1), (h.get("x-cache") or h.get("X-Cache"))
def backends():
    try:
        with psycopg.connect(DSN, autocommit=True, connect_timeout=2) as c:
            return [r[0] for r in c.execute("SELECT pid FROM pg_stat_activity WHERE application_name='atus-api' ORDER BY pid")]
    except Exception as e:
        return f"monitor connect failed: {type(e).__name__}"
EST = {"activity": {"include": ["010101"], "label": "Sleeping"}, "years": [2023]}
def sequence():
    for m, path, body in [("GET", "/api/v1/meta", None), ("POST", "/api/v1/analysis/estimate", EST),
                          ("POST", "/api/v1/analysis/estimate", {"activity": {"include": ["010101"]}, "years": [2023], "confidence_level": round(0.51 + (time.time_ns() % 400000) / 1e6, 6)}),
                          ("GET", "/ready", None)] + [("GET", "/api/v1/meta", None)] * 8:
        p("   seq", m, path, "->", http(m, path, body))
p("=" * 60, mode, sys.argv[3:] , datetime.datetime.now().isoformat(timespec="seconds"))
p("warm:", http("GET", "/api/v1/meta"), http("POST", "/api/v1/analysis/estimate", EST))
p("API backends before:", backends())
if mode == "restart":
    stop = threading.Event(); rows = []
    def poll():
        while not stop.is_set():
            t = time.time() - T0
            rows.append((round(t, 2),) + http("GET", "/api/v1/meta"))
            time.sleep(0.5)
    th = threading.Thread(target=poll); th.start(); time.sleep(1)
    tr = time.time(); p("docker restart begin")
    subprocess.run(["docker", "restart", "atus-audit-stmt"], check=True, capture_output=True)
    p(f"docker restart returned after {time.time()-tr:.2f}s")
    while True:
        try:
            psycopg.connect(DSN, connect_timeout=1).close(); break
        except Exception:
            time.sleep(0.2)
    p(f"DB accepting connections {time.time()-tr:.2f}s after restart began")
    time.sleep(5); stop.set(); th.join()
    for r in rows: p("   poll t=%.2f status=%s code=%s %sms" % (r[0], r[1], r[2], r[3]))
    p("API backends after restart:", backends())
elif mode == "pause":
    secs, conc = float(sys.argv[3]), int(sys.argv[4])
    res = []
    def inflight(i):
        t = time.time() - T0
        res.append((i, round(t, 2)) + http("GET", "/api/v1/meta"))
    subprocess.run(["docker", "pause", "atus-audit-stmt"], check=True); tp = time.time(); p("paused")
    ths = [threading.Thread(target=inflight, args=(i,)) for i in range(conc)]
    for t in ths: t.start()
    time.sleep(max(0, secs - (time.time() - tp)))
    subprocess.run(["docker", "unpause", "atus-audit-stmt"], check=True); p(f"unpaused after {time.time()-tp:.2f}s")
    for t in ths: t.join()
    for r in sorted(res): p("   in-flight #%d sent t=%.2f -> status=%s code=%s %sms" % r[:5])
    p("API backends after unpause:", backends())
sequence()
p("API backends at end:", backends())
