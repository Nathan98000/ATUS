"""Exp D: the gap between the loader's data commit (T2, loader.py:203-212) and its
separate 'succeeded' UPDATE (T3, loader.py:223-228), which is what bumps data_version
(dependencies.py:50-60).

Emulation: a separate session holds FOR NO KEY UPDATE on the new 'running'
ingestion_runs row (compatible with the FOR KEY SHARE that T2's source_files FK
checks take, but blocks T3's UPDATE). After T2 has committed and T3 is blocked,
we observe traffic for WINDOW s, then terminate the loader's backend
(pg_terminate_backend = emulated connection loss / DB restart between T2 and T3),
release the row lock, and observe for AFTER s more.
"""
import json
import os
import subprocess
import sys
import time

import psycopg

S = os.path.dirname(os.path.abspath(__file__))
PY = "$REPO/.venv/bin/python"
DB = "atus_audit_reload"
URL = f"postgresql://atus:atus@localhost:5434/{DB}?application_name=expD_ctl"
OUT = os.path.join(S, "expD"); os.makedirs(OUT, exist_ok=True)
EV = os.path.join(OUT, "events.jsonl")
WINDOW, AFTER = 15, 25
evf = open(EV, "w")


def event(name, **kw):
    evf.write(json.dumps({"t": time.time(), "event": name, **kw}) + "\n"); evf.flush()
    print(name, kw, flush=True)


subprocess.run(["curl", "-s", "-o", "/dev/null", "-X", "POST", "http://127.0.0.1:8201/api/v1/analysis/estimate",
                "-H", "content-type: application/json", "-d", '{"activity":{"include":["010101"]},"years":[2023]}'])
event("exp_start")
env = dict(os.environ, PROBES="health_probe:0.25,est_cached_probe:1,threads_probe:1")
traffic = subprocess.Popen([PY, "traffic.py", os.path.join(OUT, "requests.csv"), str(5 + 5 + WINDOW + AFTER + 10), "12",
                            "meta,est_cached,est_uncached"], cwd=S, env=env,
                           stdout=open(os.path.join(OUT, "traffic.out"), "w"), stderr=subprocess.STDOUT)
time.sleep(5)
ctl = psycopg.connect(URL, autocommit=True)
before = ctl.execute("SELECT coalesce(max(id),0) FROM atus.ingestion_runs").fetchone()[0]
loader = subprocess.Popen([PY, "run_loader.py", DB, os.path.join(S, "tmp_v2", "data"), "5", EV], cwd=S,
                          stdout=open(os.path.join(OUT, "loader.out"), "w"), stderr=subprocess.STDOUT)
# wait for the loader's T1 'running' row, then lock it
lock = psycopg.connect(URL.replace("expD_ctl", "expD_rowlock"))
while True:
    row = ctl.execute("SELECT id FROM atus.ingestion_runs WHERE id > %s AND status='running'", (before,)).fetchone()
    if row:
        break
    time.sleep(0.01)
run_id = row[0]
lock.execute("SELECT id FROM atus.ingestion_runs WHERE id = %s FOR NO KEY UPDATE", (run_id,))
event("rowlock_taken", run_id=run_id)
# wait until T2 committed and T3 is blocked on our row lock
while True:
    r = ctl.execute("SELECT pid, wait_event_type, left(query,60) FROM pg_stat_activity "
                    "WHERE datname=current_database() AND application_name='atus_loader'").fetchone()
    if r and r[1] == "Lock" and r[2].lstrip().upper().startswith("UPDATE ATUS.INGESTION_RUNS"):
        loader_pid = r[0]
        break
    time.sleep(0.05)
event("T2_committed_T3_blocked", loader_backend=loader_pid,
      committed_sleep_minutes=ctl.execute(
          "SELECT sum(duration_minutes) FROM atus.activities WHERE tucaseid=20230101000001 AND activity_code='010101'").fetchone()[0],
      data_version_row=ctl.execute("SELECT release, id FROM atus.ingestion_runs WHERE status='succeeded' ORDER BY id DESC LIMIT 1").fetchone())
time.sleep(WINDOW)
ctl.execute("SELECT pg_terminate_backend(%s)", (loader_pid,))
event("loader_backend_terminated")
lock.rollback(); lock.close()
event("rowlock_released")
loader.wait()
event("loader_exit", code=loader.returncode)
runs = ctl.execute("SELECT id, status, finished_at IS NOT NULL FROM atus.ingestion_runs ORDER BY id").fetchall()
event("runs_after", runs=runs,
      committed_sleep_minutes=ctl.execute(
          "SELECT sum(duration_minutes) FROM atus.activities WHERE tucaseid=20230101000001 AND activity_code='010101'").fetchone()[0])
traffic.wait()
event("traffic_done")
