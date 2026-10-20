"""Build scratch databases for the exposure/error probe (mirrors scripts/e2e_backend.py).
atus_audit_probe: fixture dataset loaded through the real loader.
atus_audit_empty: empty database (no schema)."""
import sys, tempfile
from pathlib import Path
import psycopg
REPO = Path("$REPO")
sys.path.insert(0, str(REPO))
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings

ADMIN = "postgresql://atus:atus@localhost:5434/postgres"
def create(db):
    assert db.startswith("atus_audit_")
    with psycopg.connect(ADMIN, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db,)).fetchone():
            c.execute(f'CREATE DATABASE "{db}"')
create("atus_audit_probe"); create("atus_audit_empty")
url = "postgresql://atus:atus@localhost:5434/atus_audit_probe"
with psycopg.connect(url) as conn:
    with conn.transaction():
        conn.execute("DROP SCHEMA IF EXISTS atus CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    apply_migrations(conn, REPO / "migrations")
with tempfile.TemporaryDirectory(prefix="atus-audit-") as tmp:
    settings = analytics_settings(Path(tmp), url)
    with psycopg.connect(url) as conn:
        load_all(conn, settings)
with psycopg.connect(url) as conn:
    print("respondents:", conn.execute("select count(*) from atus.respondents").fetchone()[0])
    print("runs:", conn.execute("select id, release, status from atus.ingestion_runs").fetchall())
