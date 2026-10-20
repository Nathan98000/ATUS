"""Realistic variant (no artificial gap inside any request): an analysis is in flight
(holding AccessShare on activities etc. for ~3 s, as a long pooled-replicate query would)
when `atus load` starts; one more ordinary request arrives meanwhile."""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, "$REPO")
import psycopg  # noqa: E402

import atus_pipeline.loading.loader as loader  # noqa: E402
from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure  # noqa: E402
from atus_pipeline.api.dependencies import read_data_version  # noqa: E402
from tests.integration.analytics_dataset import analytics_settings  # noqa: E402

URL = "postgresql://atus:atus@localhost:5434/atus_audit_rel_engine"
WORK = Path(sys.argv[1])
SPEC = AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY,
                    activity=ActivitySelector.preset("sleep"), years=(2023,))
out: dict[str, str] = {}
t0 = time.monotonic()


def stamp():
    return f"t={time.monotonic() - t0:.2f}s"


def run_loader():
    tmp = Path(tempfile.mkdtemp(prefix="fx-dl2-", dir=WORK))
    settings = analytics_settings(tmp, URL)
    try:
        with psycopg.connect(URL, application_name="audit-dl2-loader") as conn:
            loader.load_all(conn, settings)
        out["loader"] = f"succeeded {stamp()}"
    except Exception as exc:  # noqa: BLE001
        out["loader"] = f"FAILED {type(exc).__name__} {stamp()}"


def new_request():
    with psycopg.connect(URL, application_name="audit-dl2-new") as conn:
        try:
            read_data_version(conn)
            r = AnalysisEngine(conn).estimate(SPEC)   # unmodified engine path
            out["new request"] = f"ok value={r.estimate.value} {stamp()}"
        except Exception as exc:  # noqa: BLE001
            out["new request"] = f"FAILED {type(exc).__name__} {stamp()}"


with psycopg.connect(URL, application_name="audit-dl2-long") as long_conn:
    read_data_version(long_conn)
    AnalysisEngine(long_conn).estimate(SPEC)   # now holds AccessShare on all engine tables
    lt = threading.Thread(target=run_loader)
    lt.start()                                  # TRUNCATE queues behind the long request
    time.sleep(0.5)
    nt = threading.Thread(target=new_request)
    nt.start()                                  # ordinary request arrives during the wait
    time.sleep(2.5)
    long_conn.commit()                          # long analysis finishes at t~3 s
    out["long request"] = f"finished {stamp()}"
lt.join()
nt.join()
for k, v in out.items():
    print(f"{k:13s}: {v}")
with psycopg.connect(URL) as c:
    print("latest ingestion run:", c.execute(
        "SELECT id, status, left(notes, 60) FROM atus.ingestion_runs ORDER BY id DESC LIMIT 1"
    ).fetchone())
