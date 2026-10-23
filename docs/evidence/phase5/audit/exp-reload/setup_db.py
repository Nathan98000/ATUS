"""Build the fixture scratch DB atus_audit_reload exactly like scripts/e2e_backend.py
(migrations + real loader with analytics_settings), and prepare two persistent
staged-data dirs:
  data_v1/  - the unmodified hand-computed fixture (loaded now -> run1)
  data_v2/  - same, but E1's sleep episode 100 -> 160 min (TV 1340 -> 1280)
              so a reload produces distinguishable numbers.
Does NOT touch database "atus".
"""
from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

import psycopg

REPO = Path("$REPO")
sys.path.insert(0, str(REPO))

from atus_pipeline.database.migrate import apply_migrations  # noqa: E402
from atus_pipeline.loading.loader import load_all  # noqa: E402
from tests.integration.analytics_dataset import analytics_settings, E1  # noqa: E402

OUT = Path(__file__).resolve().parent
DBNAME = sys.argv[1] if len(sys.argv) > 1 else "atus_audit_reload"
URL = f"postgresql://atus:atus@localhost:5434/{DBNAME}"
assert DBNAME.startswith("atus_audit_reload"), DBNAME

with psycopg.connect("postgresql://atus:atus@localhost:5434/postgres", autocommit=True) as c:
    if not c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DBNAME,)).fetchone():
        c.execute(f'CREATE DATABASE "{DBNAME}"')

with psycopg.connect(URL) as conn:
    with conn.transaction():
        conn.execute("DROP SCHEMA IF EXISTS atus CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    apply_migrations(conn, REPO / "migrations")

for d in ("v1", "v2"):
    p = OUT / f"tmp_{d}"
    if p.exists():
        shutil.rmtree(p)
    p.mkdir()

s1 = analytics_settings(OUT / "tmp_v1", URL)
s2 = analytics_settings(OUT / "tmp_v2", URL)

# modify v2: E1 sleep 100 -> 160, TV 1340 -> 1280
act = s2.staging_dir / "atusact_0325.dat"
rows = list(csv.DictReader(act.open(newline="")))
cols = list(rows[0].keys())
changed = 0
for r in rows:
    if r["TUCASEID"] == E1 and r["TUACTIVITY_N"] == "1":
        r["TUSTOPTIME"] = "06:40:00"; r["TUACTDUR24"] = "160"; r["TUACTDUR"] = "160"; r["TUCUMDUR24"] = "160"
        changed += 1
    elif r["TUCASEID"] == E1 and r["TUACTIVITY_N"] == "2":
        r["TUSTARTTIM"] = "06:40:00"; r["TUACTDUR24"] = "1280"; r["TUACTDUR"] = "1280"
        changed += 1
with act.open("w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader(); w.writerows(rows)
print("v2 rows changed:", changed)

with psycopg.connect(URL) as conn:
    res = load_all(conn, s1)
print("loaded v1:", [(r.table, r.loaded_rows) for r in res])
print("data_v1:", s1.data_dir, "data_v2:", s2.data_dir)
