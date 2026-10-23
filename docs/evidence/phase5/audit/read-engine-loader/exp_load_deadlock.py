"""Can a concurrent engine request deadlock with `atus load`'s TRUNCATE?

Engine statement order per request: ingestion_runs -> respondents (DISTINCT data_year,
AccessShare kept until commit) -> lexicon tables -> stats query (activities, respondents,
roster/cps, replicate weights). TRUNCATE locks tables in _CANONICAL_TABLES order:
activity_companions, activities, cps_persons, household_members, replicate_weights,
pandemic_replicate_weights, respondents, lexicon.
If TRUNCATE arrives after the request read respondents but before its stats query, the
loader holds activities and waits for respondents while the request waits for activities.
The gap between the two engine steps is widened artificially (`gap` seconds) to hit the window.
"""
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


def trial(gap: float) -> None:
    out: dict[str, str] = {}
    t0 = time.monotonic()

    def run_loader():
        tmp = Path(tempfile.mkdtemp(prefix="fx-dl-", dir=WORK))
        settings = analytics_settings(tmp, URL)
        try:
            with psycopg.connect(URL, application_name="audit-dl-loader") as conn:
                loader.load_all(conn, settings)
            out["loader"] = f"succeeded at t={time.monotonic() - t0:.2f}s"
        except Exception as exc:  # noqa: BLE001
            out["loader"] = f"FAILED {type(exc).__name__} at t={time.monotonic() - t0:.2f}s: {str(exc)[:160]}"

    with psycopg.connect(URL, application_name="audit-dl-request") as conn:
        read_data_version(conn)
        engine = AnalysisEngine(conn)
        engine._get_loaded_years()          # step 1 of _prepare: AccessShare on respondents
        lt = threading.Thread(target=run_loader)
        lt.start()
        time.sleep(gap)                      # TRUNCATE now queued on respondents
        try:
            r = engine.estimate(SPEC)        # resolver + stats query -> needs activities
            out["request"] = f"ok value={r.estimate.value} at t={time.monotonic() - t0:.2f}s"
        except Exception as exc:  # noqa: BLE001
            out["request"] = f"FAILED {type(exc).__name__} at t={time.monotonic() - t0:.2f}s: {str(exc)[:160]}"
            conn.rollback()
    lt.join()
    with psycopg.connect(URL) as c:
        runs = c.execute("SELECT id, status, left(notes, 90) FROM atus.ingestion_runs "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        n = c.execute("SELECT count(*) FROM atus.respondents").fetchone()[0]
    print(f"gap={gap}s -> request: {out['request']}\n           loader : {out['loader']}\n"
          f"           latest run: {runs}; respondents now {n}")


with psycopg.connect(URL) as c:
    print("deadlock_timeout =", c.execute("SHOW deadlock_timeout").fetchone()[0])
for gap in (0.3, 1.6):
    trial(gap)
