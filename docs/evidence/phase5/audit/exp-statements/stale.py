"""(c) What do the first requests get after the database closes the pool's
connections while the API stays warm?

mode terminate : pg_terminate_backend() every API backend (application_name=atus-api)
mode restart   : docker restart atus-audit-stmt (emulates a suspend/resume)
mode pause N   : docker pause for N s while requests are in flight, then unpause
After the event, sends a fixed sequence of requests and records status/body/latency."""
import json, sys, time, subprocess, datetime, urllib.request, urllib.error, threading
import psycopg
API = "http://127.0.0.1:8204"
DSN_MON = "postgresql://atus:atus@127.0.0.1:5442/atus?application_name=monitor"
mode = sys.argv[1]; out_path = sys.argv[2]; arg = float(sys.argv[3]) if len(sys.argv) > 3 else 0
LOG = open(out_path, "a")
def p(*a):
    line = " ".join(str(x) for x in a)
    print(line, file=LOG, flush=True); print(line, flush=True)
def http(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            st, h, b = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        st, h, b = e.code, dict(e.headers), e.read()
    except Exception as e:
        st, h, b = "EXC", {}, repr(e).encode()
    ms = (time.perf_counter() - t0) * 1000
    try:
        j = json.loads(b); code = j["error"]["code"] if "error" in j else "ok"
    except Exception:
        code = b[:120]
    return st, code, round(ms, 1), h.get("x-cache") or h.get("X-Cache"), b[:300]
def api_backends(conn):
    return [r[0] for r in conn.execute("SELECT pid FROM pg_stat_activity WHERE application_name='atus-api' ORDER BY pid")]
def wait_db(limit=60):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            c = psycopg.connect(DSN_MON, autocommit=True, connect_timeout=2); return c, time.time() - t0
        except Exception:
            time.sleep(0.25)
    raise SystemExit("db not back")
EST_HIT = {"activity": {"include": ["010101"], "label": "Sleeping"}, "years": [2023]}  # cached by earlier runs
def est_miss():
    return {"activity": {"include": ["010101"]}, "years": [2023], "confidence_level": round(0.51 + (time.time_ns() % 400000) / 1000000, 6)}
SEQ = [("GET", "/api/v1/meta", None), ("POST", "/api/v1/analysis/estimate", "miss"),
       ("POST", "/api/v1/analysis/estimate", EST_HIT), ("GET", "/ready", None), ("GET", "/health", None)]
p("=" * 70); p("mode", mode, arg, "at", datetime.datetime.now().isoformat(timespec="seconds"))
mon = psycopg.connect(DSN_MON, autocommit=True)
# warm: make sure the API is serving and note its pool backends
p("warm /meta:", http("GET", "/api/v1/meta")[:3], "warm estimate (cached):", http("POST", "/api/v1/analysis/estimate", EST_HIT)[:4])
before = api_backends(mon)
p("API backends before event:", len(before), before)
if mode == "terminate":
    r = mon.execute("SELECT pid, pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='atus-api'").fetchall()
    p("terminated:", r)
    time.sleep(0.2)
elif mode == "restart":
    mon.close()
    t0 = time.time(); subprocess.run(["docker", "restart", "atus-audit-stmt"], check=True, capture_output=True)
    mon, waited = wait_db()
    p(f"docker restart done in {time.time()-t0:.1f}s (db accepting after extra {waited:.1f}s)")
elif mode == "pause":
    results = []
    def inflight(i):
        results.append((i, "GET /api/v1/meta during pause") + http("GET", "/api/v1/meta")[:3])
    subprocess.run(["docker", "pause", "atus-audit-stmt"], check=True)
    tp = time.time(); p("paused at", datetime.datetime.now().isoformat(timespec="milliseconds"))
    ths = [threading.Thread(target=inflight, args=(i,)) for i in range(2)]
    for t in ths: t.start(); time.sleep(0.2)
    time.sleep(max(0, arg - (time.time() - tp)))
    subprocess.run(["docker", "unpause", "atus-audit-stmt"], check=True)
    p(f"unpaused after {time.time()-tp:.1f}s")
    for t in ths: t.join()
    for r in results: p("  in-flight:", r)
    mon.close(); mon = psycopg.connect(DSN_MON, autocommit=True)
p("API backends right after event:", api_backends(mon))
if mode == "terminate" and arg > 0:
    t0 = time.time()
    while time.time() - t0 < arg:
        time.sleep(10)
        p(f"  idle t+{int(time.time()-t0)}s: API backends = {api_backends(mon)}")
seq = SEQ + [("GET", "/api/v1/meta", None)] * 12
for i, (m, path, body) in enumerate(seq, 1):
    b = est_miss() if body == "miss" else body
    st, code, ms, xc, raw = http(m, path, b)
    p(f"  #{i:02d} {m} {path}{' (miss)' if body=='miss' else ' (cached)' if body is EST_HIT else ''} -> {st} {code} {ms}ms x-cache={xc}")
    if st != 200 and i <= 3:
        p("       body:", raw.decode(errors="replace"))
p("API backends at end:", api_backends(mon))
