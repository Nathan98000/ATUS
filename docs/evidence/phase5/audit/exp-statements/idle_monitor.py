"""(a) Grow the API pool with a concurrent burst, then leave the API idle and
sample pg_stat_activity for the API's backends every 30 s. Afterwards, the
server log is scanned for anything the API sent during the idle window."""
import json, sys, time, datetime, urllib.request, concurrent.futures as cf
import psycopg
sys.path.insert(0, "docs/evidence/phase5/audit/exp-statements")
from pglog import read_entries, is_statement, short
OUT = sys.argv[1]; DURATION = float(sys.argv[2])
API = "http://127.0.0.1:8204"
def post(i):
    body = {"activity": {"include": ["010101"]}, "years": [2019, 2023], "confidence_level": round(0.70 + i * 0.01, 2)}
    req = urllib.request.Request(API + "/api/v1/analysis/compare" if False else API + "/api/v1/analysis/trend",
                                 data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status
log = open(OUT, "w")
def p(*a):
    print(*a, file=log, flush=True); print(*a, flush=True)
mon = psycopg.connect("postgresql://atus:atus@127.0.0.1:5442/atus?application_name=monitor", autocommit=True)
def snap():
    return mon.execute("SELECT pid, state, backend_start::time(0), state_change::time(0), left(query, 60) FROM pg_stat_activity WHERE application_name='atus-api' ORDER BY pid").fetchall()
p("before burst", datetime.datetime.now().time(), snap())
with cf.ThreadPoolExecutor(30) as ex:
    statuses = list(ex.map(post, range(30)))
p("burst statuses", statuses)
burst_end = datetime.datetime.utcnow()
p("burst end (UTC)", burst_end.isoformat())
time.sleep(1)
t0 = time.time()
while time.time() - t0 < DURATION:
    s = snap()
    p(datetime.datetime.now().time().isoformat(timespec="seconds"), f"api_backends={len(s)}", s)
    time.sleep(30)
idle_start = (burst_end + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
entries = [e for e in read_entries() if e["ts"] >= idle_start and e["app"] == "atus-api"]
p("API-backend log entries during idle window (after burst):", len(entries))
for e in entries:
    p("   ", e["ts"], e["pid"], e["level"], e["msg"][:200])
