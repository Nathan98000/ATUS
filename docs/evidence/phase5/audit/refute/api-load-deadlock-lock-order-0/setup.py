import sys, tempfile
from pathlib import Path
REPO=Path("$REPO"); sys.path.insert(0,str(REPO))
import psycopg
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings
URL="postgresql://atus:atus@localhost:5434/atus_audit_refdl"
with psycopg.connect("postgresql://atus:atus@localhost:5434/postgres", autocommit=True) as c:
    c.execute('DROP DATABASE IF EXISTS atus_audit_refdl'); c.execute('CREATE DATABASE atus_audit_refdl')
with psycopg.connect(URL) as conn: apply_migrations(conn, REPO/"migrations")
tmp=tempfile.mkdtemp(dir=sys.argv[1])
s=analytics_settings(Path(tmp), URL)
with psycopg.connect(URL) as conn: load_all(conn, s)
print("loaded")
