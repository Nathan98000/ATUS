"""Poll pg_stat_activity once per second for backends of the audit API
(application_name = atus_audit_disc, including their parallel workers).

Read-only: the poller's own session is default_transaction_read_only and only
reads pg_stat_activity. Usage: poll_activity.py <out_file> <max_seconds>
       [<stop_after_idle_polls>]
Stops after max_seconds, or after N consecutive polls with zero *active*
audit backends (if given and at least one active backend was seen).
"""

import sys
import time
from datetime import datetime

import subprocess

import psycopg

APP = "atus_audit_disc"
out_path = sys.argv[1]
max_seconds = float(sys.argv[2])
stop_after_idle = int(sys.argv[3]) if len(sys.argv) > 3 else 0

SQL = """
select a.pid, a.leader_pid, a.backend_type, a.state,
       to_char(a.query_start, 'HH24:MI:SS.MS') as query_start,
       round(extract(epoch from now() - a.query_start)::numeric, 1) as running_s,
       a.wait_event_type, a.wait_event,
       left(regexp_replace(a.query, '\\s+', ' ', 'g'), 60) as q
from pg_stat_activity a
where a.datname = 'atus' and a.application_name = %s
order by coalesce(a.leader_pid, a.pid), a.pid
"""
OTHERS = """
select count(*) filter (where backend_type = 'client backend' and state = 'active'),
       count(*) filter (where backend_type = 'parallel worker'),
       string_agg(distinct coalesce(nullif(application_name, ''), '<none>') || ':' || datname, ',')
from pg_stat_activity
where application_name not in (%s, 'atus_audit_disc_poller')
  and backend_type in ('client backend', 'parallel worker') and state = 'active'
"""


def shm_used() -> str:
    try:
        out = subprocess.run(
            ["docker", "exec", "atus-postgres", "df", "-B1", "--output=used,size", "/dev/shm"],
            capture_output=True, text=True, timeout=5,
        ).stdout.split()
        return f"{int(out[2]) / 2**20:.1f}MiB/{int(out[3]) / 2**20:.0f}MiB"
    except Exception as exc:  # noqa: BLE001
        return f"n/a({exc.__class__.__name__})"

conn = psycopg.connect(
    "postgresql://atus:atus@localhost:5434/atus",
    application_name="atus_audit_disc_poller",
    autocommit=True,
)
conn.execute("SET default_transaction_read_only = on")
start = time.monotonic()
seen_active = False
idle_polls = 0
with open(out_path, "a", buffering=1) as f:
    while time.monotonic() - start < max_seconds:
        t0 = time.monotonic()
        rows = conn.execute(SQL, (APP,)).fetchall()
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        active_leaders = [r for r in rows if r[3] == "active" and r[2] == "client backend"]
        workers = [r for r in rows if r[2] == "parallel worker"]
        idle = [r for r in rows if r[3] != "active" and r[2] == "client backend"]
        o_active, o_workers, o_apps = conn.execute(OTHERS, (APP,)).fetchone()
        f.write(
            f"--- {ts} client_backends_active={len(active_leaders)} "
            f"parallel_workers={len(workers)} client_backends_idle={len(idle)} "
            f"| OTHER sessions on server: active={o_active} workers={o_workers} apps={o_apps} "
            f"| /dev/shm used={shm_used()}\n"
        )
        for r in rows:
            f.write("    " + " | ".join("" if v is None else str(v) for v in r) + "\n")
        if active_leaders:
            seen_active = True
            idle_polls = 0
        else:
            idle_polls += 1
        if stop_after_idle and seen_active and idle_polls >= stop_after_idle:
            f.write(f"--- {ts} stopping: {idle_polls} consecutive polls with no active audit backend\n")
            break
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t0)))
conn.close()
