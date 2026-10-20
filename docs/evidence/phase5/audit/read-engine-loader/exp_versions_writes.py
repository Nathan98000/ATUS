"""Experiments for audit questions 7.4 (data_version reuse) and 7.5 (write paths).

Uses ONLY the scratch database atus_audit_rel_engine on localhost:5434; the full
database 'atus' is touched read-only (one SELECT under default_transaction_read_only).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path("$REPO")
sys.path.insert(0, str(REPO))
import psycopg  # noqa: E402

from atus_pipeline.api.dependencies import read_data_version  # noqa: E402
from atus_pipeline.database.migrate import apply_migrations  # noqa: E402
from atus_pipeline.loading.loader import load_all  # noqa: E402
from tests.integration.analytics_dataset import analytics_settings  # noqa: E402

ADMIN = "postgresql://atus:atus@localhost:5434/postgres"
DB = "atus_audit_rel_engine"
URL = f"postgresql://atus:atus@localhost:5434/{DB}"
FULL = "postgresql://atus:atus@localhost:5434/atus"
WORK = Path(sys.argv[1])
VENV = REPO / ".venv" / "bin"


def recreate_db():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{DB}"')
        c.execute(f'CREATE DATABASE "{DB}"')


def fresh_schema_and_load(tag: str):
    with psycopg.connect(URL) as conn:
        with conn.transaction():
            conn.execute("DROP SCHEMA IF EXISTS atus CASCADE")
            conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
        apply_migrations(conn, REPO / "migrations")
    load_once(tag)


def load_once(tag: str):
    tmp = Path(tempfile.mkdtemp(prefix=f"fx-{tag}-", dir=WORK))
    settings = analytics_settings(tmp, URL)
    with psycopg.connect(URL) as conn:
        load_all(conn, settings)


def version(url: str, read_only: bool = False) -> str:
    with psycopg.connect(url) as conn:
        if read_only:
            conn.execute("SET default_transaction_read_only = on")
            conn.commit()
        v = read_data_version(conn)
        n = conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0]
        runs = conn.execute("SELECT id, status FROM atus.ingestion_runs ORDER BY id").fetchall()
        conn.rollback()
    return f"data_version={v} respondents={n} ingestion_runs={runs}"


def vr_count() -> int:
    with psycopg.connect(URL) as conn:
        return conn.execute("SELECT count(*) FROM atus.validation_results").fetchone()[0]


print("=== 7.4: data_version reuse ===")
recreate_db()
fresh_schema_and_load("a")
print("scratch fixture DB after first build :", version(URL))
print("full DB (read-only)                 :", version(FULL, read_only=True))
load_once("b")
print("scratch after a 2nd `load`           :", version(URL))
fresh_schema_and_load("c")
print("scratch after rebuild (drop+migrate+load):", version(URL))

print("\n=== 7.5: write paths ===")
env = dict(os.environ, ATUS_DATABASE_URL=URL, ATUS_LOG_LEVEL="WARNING")
before = vr_count()
p = subprocess.run([str(VENV / "atus"), "validate-analytics"], env=env, capture_output=True,
                   text=True, cwd=WORK)
after = vr_count()
print(f"atus validate-analytics (normal session): exit={p.returncode} "
      f"validation_results {before} -> {after}")
print("  last stdout line:", p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "")

env_ro = dict(env, PGOPTIONS="-c default_transaction_read_only=on")
before = vr_count()
p = subprocess.run([str(VENV / "atus"), "validate-analytics"], env=env_ro, capture_output=True,
                   text=True, cwd=WORK)
after = vr_count()
print(f"atus validate-analytics (read-only session): exit={p.returncode} "
      f"validation_results {before} -> {after}")
err_tail = [ln for ln in p.stderr.strip().splitlines() if ln.strip()][-3:]
print("  stderr tail:", *err_tail, sep="\n    ")
print("  stdout lines:", len(p.stdout.strip().splitlines()))

# data-quality test path: psycopg.connect() without context manager, then close()
from atus_pipeline.analytics.benchmarks import run_benchmarks  # noqa: E402
before = vr_count()
c = psycopg.connect(URL)
res = run_benchmarks(c)
status_inside = c.info.transaction_status.name
c.close()
after = vr_count()
print(f"test_analytics_benchmarks-style call: {len(res)} results, tx status before close="
      f"{status_inside}, validation_results {before} -> {after} (INSERTs rolled back on close)")

c = psycopg.connect(URL, options="-c default_transaction_read_only=on")
try:
    run_benchmarks(c)
    print("run_benchmarks on read-only session: no error (unexpected)")
except Exception as exc:  # noqa: BLE001
    print(f"run_benchmarks on read-only session: {type(exc).__name__}: {exc}")
finally:
    c.close()

print("\n=== 7.5: API under default_transaction_read_only=on ===")
from fastapi.testclient import TestClient  # noqa: E402

from atus_pipeline.api.app import create_app  # noqa: E402
from atus_pipeline.config import Settings  # noqa: E402

ro_url = URL + "?options=-c%20default_transaction_read_only%3Don"
settings = Settings(database_url=ro_url, data_dir=WORK, release="0325", log_level="WARNING")
app = create_app(settings)
calls = [
    ("GET", "/health", None), ("GET", "/ready", None), ("GET", "/api/v1/meta", None),
    ("GET", "/api/v1/activities", None), ("GET", "/api/v1/activities?search=sleep", None),
    ("GET", "/api/v1/activities/01", None), ("GET", "/api/v1/activities/presets", None),
    ("GET", "/api/v1/population/metadata", None),
    ("POST", "/api/v1/analysis/estimate",
     {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2023]}),
    ("POST", "/api/v1/analysis/estimate",
     {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2023]}),
    ("POST", "/api/v1/analysis/trend",
     {"measure": "participation_rate", "activity": {"preset": "sleep"}, "years": [2019, 2023]}),
    ("POST", "/api/v1/analysis/compare",
     {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2023],
      "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}),
    ("POST", "/api/v1/analysis/estimate",
     {"measure": "average_minutes_per_day", "activity": {"preset": "sleep"}, "years": [2020]}),
]
with TestClient(app) as client:
    for method, path, body in calls:
        r = client.request(method, path, json=body)
        code = r.json().get("error", {}).get("code") if r.status_code >= 400 else ""
        print(f"  {method} {path} -> {r.status_code} X-Cache={r.headers.get('X-Cache')} {code}")
