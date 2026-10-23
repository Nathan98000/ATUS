"""Load the hand-computed fixture dataset into the scratch DB exactly as
scripts/e2e_backend.py does (migrations + real loader). Scratch only."""
import sys, tempfile
from pathlib import Path
import psycopg
REPO = Path("$REPO")
sys.path.insert(0, str(REPO))
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings

URL = "postgresql://atus:atus@127.0.0.1:5442/atus?application_name=loader"
assert ":5442/" in URL
with psycopg.connect(URL) as conn:
    with conn.transaction():
        conn.execute("DROP SCHEMA IF EXISTS atus CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    apply_migrations(conn, REPO / "migrations")
with tempfile.TemporaryDirectory(prefix="atus-audit-") as tmp:
    settings = analytics_settings(Path(tmp), URL)
    with psycopg.connect(URL) as conn:
        load_all(conn, settings)
print("loaded")
