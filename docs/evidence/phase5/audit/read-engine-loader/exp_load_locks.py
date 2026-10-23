"""7.1 code-side confirmation on the fixture scratch DB: which locks `atus load` (load_all)
holds, what concurrent engine/API-style reads experience, and the REPEATABLE READ +
TRUNCATE MVCC caveat. The load is slowed artificially (sleep per activity row) so the
TRUNCATE transaction stays open ~8 s; the full-DB load holds it ~2.5 min."""
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
T0 = time.monotonic()
SPEC = AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY,
                    activity=ActivitySelector.preset("sleep"), years=(2023,))
events: list[str] = []
lock = threading.Lock()


def log(msg: str) -> None:
    with lock:
        events.append(f"t={time.monotonic() - T0:6.2f}s {msg}")


orig_stream = loader._stream_rows
orig_lexicon = loader._load_lexicon
truncated = threading.Event()


def slow_stream(path):
    for row in orig_stream(path):
        if path.name.startswith("atusact"):
            time.sleep(0.35)
        yield row


def lexicon_hook(conn, settings):
    log("loader: TRUNCATE executed (inside data transaction)")
    truncated.set()
    return orig_lexicon(conn, settings)


loader._stream_rows = slow_stream
loader._load_lexicon = lexicon_hook


def run_loader():
    tmp = Path(tempfile.mkdtemp(prefix="fx-lock-", dir=WORK))
    settings = analytics_settings(tmp, URL)
    with psycopg.connect(URL, application_name="audit-loader") as conn:
        log("loader: start load_all")
        loader.load_all(conn, settings)
    log("loader: load_all returned (data committed + status 'succeeded')")


def baseline():
    with psycopg.connect(URL) as conn:
        r = AnalysisEngine(conn).estimate(SPEC)
        return read_data_version(conn), r.estimate.value, r.estimate.standard_error


def reader_rc_engine(name: str):
    t = time.monotonic()
    with psycopg.connect(URL, application_name=name) as conn:
        v1 = read_data_version(conn)
        log(f"{name}: data_version read in {time.monotonic() - t:.3f}s -> {v1}")
        try:
            r = AnalysisEngine(conn).estimate(SPEC)
            v2 = read_data_version(conn)
            log(f"{name}: estimate returned after {time.monotonic() - t:.2f}s value="
                f"{r.estimate.value} se={r.estimate.standard_error:.6f} version before/after={v1}/{v2}")
        except Exception as exc:  # noqa: BLE001
            log(f"{name}: {type(exc).__name__} after {time.monotonic() - t:.2f}s: {exc}")


def reader_meta(name: str):
    t = time.monotonic()
    with psycopg.connect(URL, application_name=name) as conn:
        n = conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0]
        log(f"{name}: /meta-style count(*) respondents={n} after {time.monotonic() - t:.2f}s")


def reader_lexicon(name: str):
    t = time.monotonic()
    with psycopg.connect(URL, application_name=name) as conn:
        n = conn.execute("SELECT count(*) FROM atus.activity_codes").fetchone()[0]
        log(f"{name}: /activities-style lexicon read rows={n} after {time.monotonic() - t:.2f}s")


def reader_rr_engine(name: str):
    t = time.monotonic()
    with psycopg.connect(URL, application_name=name) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        v1 = read_data_version(conn)  # snapshot taken here (before the load commits)
        log(f"{name}: REPEATABLE READ snapshot taken, data_version={v1}")
        try:
            r = AnalysisEngine(conn).estimate(SPEC)
            log(f"{name}: estimate value={r.estimate.value} after {time.monotonic() - t:.2f}s")
        except Exception as exc:  # noqa: BLE001
            log(f"{name}: {type(exc).__name__} after {time.monotonic() - t:.2f}s: {exc}")


def reader_lock_timeout(name: str):
    t = time.monotonic()
    with psycopg.connect(URL, application_name=name) as conn:
        conn.execute("SET lock_timeout = '2s'")
        try:
            AnalysisEngine(conn).estimate(SPEC)
            log(f"{name}: estimate ok after {time.monotonic() - t:.2f}s")
        except Exception as exc:  # noqa: BLE001
            log(f"{name}: {type(exc).__name__} after {time.monotonic() - t:.2f}s")


def lock_snapshot():
    with psycopg.connect(URL, autocommit=True) as conn:
        rows = conn.execute("""
            SELECT a.application_name, l.mode, l.granted, c.relname
            FROM pg_locks l JOIN pg_stat_activity a USING (pid)
            LEFT JOIN pg_class c ON c.oid = l.relation
            WHERE l.locktype = 'relation' AND a.datname = current_database()
              AND a.application_name LIKE 'audit-%%'
              AND (c.relname IS NULL OR c.relname NOT LIKE 'pg_%%')
            ORDER BY 1, 4""").fetchall()
    held = sorted({r[3] for r in rows if r[0] == "audit-loader" and r[1] == "AccessExclusiveLock"
                   and r[2] and r[3] and not r[3].startswith(("idx_", "activit", "respondents_pkey"))
                   or (r[0] == "audit-loader" and r[1] == "AccessExclusiveLock" and r[3] in (
                       "activities", "activity_codes", "activity_tier1", "activity_tier2",
                       "activity_companions"))})
    waiting = [(r[0], r[1], r[3]) for r in rows if not r[2]]
    log(f"pg_locks: loader AccessExclusive on tables {held}")
    log(f"pg_locks: waiting (not granted) = {waiting}")


print("baseline before reload:", baseline())

# ---------- Scenario 1: readers arriving while the load transaction is open ----------
lt = threading.Thread(target=run_loader)
lt.start()
truncated.wait()
time.sleep(1.0)
threads = [threading.Thread(target=f, args=(n,)) for f, n in (
    (reader_rc_engine, "audit-rc-engine"), (reader_meta, "audit-meta"),
    (reader_lexicon, "audit-lexicon"), (reader_rr_engine, "audit-rr-engine"),
    (reader_lock_timeout, "audit-locktimeout"))]
for th in threads:
    th.start()
time.sleep(1.0)
lock_snapshot()
lt.join()
for th in threads:
    th.join()
print("\n".join(events))
print("baseline after reload:", baseline())

# ---------- Scenario 2: lock queue behind a long-running reader ----------
events.clear()
T0 = time.monotonic()
loader._stream_rows = orig_stream  # fast load this time
long_reader = psycopg.connect(URL, application_name="audit-long-reader")
long_reader.execute("SELECT count(*) FROM atus.respondents").fetchone()  # ACCESS SHARE held
log("long reader: holds ACCESS SHARE on respondents in an open transaction (simulated 4 s analysis)")
truncated.clear()
lt = threading.Thread(target=run_loader)
lt.start()
time.sleep(0.5)
late = threading.Thread(target=reader_lexicon, args=("audit-late-lexicon",))
late.start()
time.sleep(0.5)
lock_snapshot()
time.sleep(3.0)
long_reader.rollback()
log("long reader: committed/rolled back (releases ACCESS SHARE)")
long_reader.close()
lt.join()
late.join()
print("\n--- scenario 2 ---")
print("\n".join(events))
