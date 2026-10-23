"""Create scratch DB atus_audit_readapi and load the hand-computed fixture dataset
through the real migrations + loader (mirrors scripts/e2e_backend.py)."""
import sys
from pathlib import Path
import psycopg

REPO = Path("$REPO")
sys.path.insert(0, str(REPO))
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings

SCRATCH = Path(__file__).resolve().parent
URL = "postgresql://atus:atus@localhost:5434/atus_audit_readapi"
ADMIN = "postgresql://atus:atus@localhost:5434/postgres"

with psycopg.connect(ADMIN, autocommit=True) as c:
    if not c.execute("SELECT 1 FROM pg_database WHERE datname='atus_audit_readapi'").fetchone():
        c.execute('CREATE DATABASE "atus_audit_readapi"')
with psycopg.connect(URL) as conn:
    with conn.transaction():
        conn.execute("DROP SCHEMA IF EXISTS atus CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    apply_migrations(conn, REPO / "migrations")
settings = analytics_settings(SCRATCH / "staged", URL)
with psycopg.connect(URL) as conn:
    res = load_all(conn, settings)
print("loaded", [(r.table, r.loaded_rows) for r in res])
with psycopg.connect(URL) as conn:
    print(conn.execute("SELECT id, release, status FROM atus.ingestion_runs ORDER BY id").fetchall())
