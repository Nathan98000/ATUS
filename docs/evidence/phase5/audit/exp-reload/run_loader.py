"""Run the REAL atus_pipeline.loading.loader.load_all (same call as `atus load`,
cli.py:115-120: psycopg.connect + load_all) against a fixture staging dir, with ONE
injected delay to emulate a multi-minute COPY of the full 3.4 GB staged data:
after the real TRUNCATE of all 10 canonical tables and the real lexicon COPY,
`SELECT pg_sleep(HOLD)` runs inside the same load transaction, so every lock the
real TRUNCATE took (ACCESS EXCLUSIVE x10) is held for HOLD seconds. Everything
else (run-row INSERT txn, TRUNCATE list/order, COPYs, source_files rows, commit,
separate 'succeeded' UPDATE txn) is the unmodified loader code.

usage: run_loader.py <db> <data_dir> <hold_seconds> <events.jsonl>
"""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from pathlib import Path

import psycopg

from atus_pipeline.config import Settings
from atus_pipeline.loading import loader

db, data_dir, hold, events_path = sys.argv[1], Path(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
assert db.startswith("atus_audit_reload"), db
URL = f"postgresql://atus:atus@localhost:5434/{db}?application_name=atus_loader"
ev = open(events_path, "a")


def event(name, **kw):
    rec = {"t": time.time(), "event": name, **kw}
    ev.write(json.dumps(rec) + "\n"); ev.flush()


class _H(logging.Handler):
    def emit(self, record):
        event("log", msg=record.getMessage(), level=record.levelname)


logging.basicConfig(level=logging.INFO)
logging.getLogger("atus_pipeline").addHandler(_H())

_orig_lexicon = loader._load_lexicon


def _slow_lexicon(conn, settings):
    out = _orig_lexicon(conn, settings)
    event("hold_start", hold=hold, backend_pid=conn.info.backend_pid)
    conn.execute("SELECT pg_sleep(%s)", (hold,))
    event("hold_end")
    return out


loader._load_lexicon = _slow_lexicon

settings = Settings(database_url=URL, data_dir=data_dir, release="0325", log_level="INFO")
event("loader_start", data_dir=str(data_dir))
try:
    with psycopg.connect(URL) as conn:
        event("connected", backend_pid=conn.info.backend_pid)
        res = loader.load_all(conn, settings)
    event("loader_done", ok=True, tables=[(r.table, r.loaded_rows) for r in res])
except Exception as exc:  # record, then exit non-zero like the CLI would
    event("loader_done", ok=False, error=f"{type(exc).__name__}: {exc}",
          tb=traceback.format_exc()[-1500:])
    sys.exit(1)
