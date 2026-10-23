"""E3a: does an analysis keep running after the client disconnects?"""
import json, subprocess, time
import psycopg
SOCK = "api.sock"
BODY = {"activity": {"include": ["010101"], "label": "sleep=5;id=disconnect-%d" % int(time.time())}, "years": [2023]}
admin = psycopg.connect("postgresql://atus:atus@localhost:5434/postgres", autocommit=True)
def active():
    return admin.execute(
        "SELECT pid, state, now()-query_start AS running_for, left(query,40) FROM pg_stat_activity "
        "WHERE datname='atus_audit_readapi' AND query LIKE 'SELECT pg_sleep%%'").fetchall()
t0 = time.monotonic()
p = subprocess.Popen(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "--max-time", "1",
                      "--unix-socket", SOCK, "-H", "content-type: application/json", "-d", json.dumps(BODY),
                      "http://x/api/v1/analysis/estimate"], stdout=subprocess.PIPE, text=True)
out, _ = p.communicate()
print(f"t={time.monotonic()-t0:.2f}s curl finished (client gave up): code/time = {out!r} rc={p.returncode}")
for _ in range(3):
    time.sleep(1.2)
    print(f"t={time.monotonic()-t0:.2f}s server-side pg_sleep sessions:", active())
time.sleep(2.5)
print(f"t={time.monotonic()-t0:.2f}s after expected completion:", active())
r = subprocess.run(["curl", "-s", "-D", "-", "-o", "/dev/null", "--unix-socket", SOCK, "-H", "content-type: application/json",
                    "-d", json.dumps(BODY), "http://x/api/v1/analysis/estimate"], capture_output=True, text=True)
print("same spec re-sent; response headers:\n" + r.stdout)
