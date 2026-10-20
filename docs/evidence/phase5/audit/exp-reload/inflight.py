"""Emulate ONE in-flight worst-case (~11-12 s on the full DB) uncached analysis:
a transaction that reads the same relations, in the same order, as an uncached
POST /analysis/estimate (dependencies.py:53 ingestion_runs; engine.py:310
respondents; activities.py:42-44 lexicon; queries.py:123-149 activities +
respondents + replicate_weights), then keeps its ACCESS SHARE locks for N s
(as the real analysis does while its big statement runs), then commits.
usage: inflight.py <db> <seconds> <events.jsonl>"""
import json
import sys
import time

import psycopg

db, secs, evp = sys.argv[1], float(sys.argv[2]), sys.argv[3]
URL = f"postgresql://atus:atus@localhost:5434/{db}?application_name=inflight_emul"
ev = open(evp, "a")


def event(name, **kw):
    ev.write(json.dumps({"t": time.time(), "event": name, **kw}) + "\n"); ev.flush()


with psycopg.connect(URL) as c:
    event("inflight_start", pid=c.info.backend_pid)
    c.execute("SELECT release, id FROM atus.ingestion_runs WHERE status='succeeded' ORDER BY id DESC LIMIT 1").fetchall()
    c.execute("SELECT DISTINCT data_year FROM atus.respondents ORDER BY data_year").fetchall()
    c.execute("SELECT code FROM atus.activity_tier1").fetchall()
    c.execute("SELECT code FROM atus.activity_tier2").fetchall()
    c.execute("SELECT code, name FROM atus.activity_codes").fetchall()
    c.execute("SELECT count(*) FROM atus.activities a JOIN atus.respondents r USING (tucaseid) "
              "JOIN atus.replicate_weights rw USING (tucaseid), pg_sleep(%s)", (secs,)).fetchall()
    event("inflight_statement_done")
event("inflight_committed")
