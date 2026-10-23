"""Sample pg_stat_activity + waiting pg_locks for the scratch DB every 0.5 s (JSONL).
usage: sampler.py <db> <out.jsonl> <duration_s>"""
import json
import sys
import time

import psycopg

db, out, duration = sys.argv[1], sys.argv[2], float(sys.argv[3])
URL = f"postgresql://atus:atus@localhost:5434/{db}?application_name=audit_sampler"
SQL = """
SELECT a.pid, a.application_name, a.state, a.wait_event_type, a.wait_event,
       extract(epoch FROM a.xact_start)::float8 AS xact_start,
       left(regexp_replace(a.query, '\\s+', ' ', 'g'), 70) AS query,
       pg_blocking_pids(a.pid) AS blocked_by,
       (SELECT array_agg(c.relname || ':' || l.mode ORDER BY c.relname)
          FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
         WHERE l.pid = a.pid AND NOT l.granted) AS waiting_for,
       (SELECT array_agg(c.relname ORDER BY c.relname)
          FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
         WHERE l.pid = a.pid AND l.granted AND l.mode = 'AccessExclusiveLock'
           AND c.relkind = 'r') AS ae_held
FROM pg_stat_activity a
WHERE a.datname = current_database() AND a.pid <> pg_backend_pid()
ORDER BY a.pid
"""
stop = time.time() + duration
with psycopg.connect(URL, autocommit=True) as c, open(out, "w") as fh:
    while time.time() < stop:
        t = time.time()
        rows = c.execute(SQL).fetchall()
        cols = ["pid", "app", "state", "wtype", "wevent", "xact_start", "query",
                "blocked_by", "waiting_for", "ae_held"]
        fh.write(json.dumps({"t": t, "backends": [dict(zip(cols, r)) for r in rows]}) + "\n")
        fh.flush()
        time.sleep(max(0, 0.5 - (time.time() - t)))
