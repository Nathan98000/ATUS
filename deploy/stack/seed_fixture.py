"""Load the hand-computed fixture dataset into the production-shaped stack DB.

Runs in the `seed` service of docker-compose.prod.yml, inside the API image
(which has the package and psycopg but deliberately no migrations, tests or
data), with the repository's ``migrations/``, ``tests/``, ``data/reference/``
and ``deploy/`` bind-mounted read-only under /workspace. Connects as the owner
role (``neondb_owner``), never as a superuser:

1. applies the real migrations;
2. loads the analytics fixture dataset (tests/integration/analytics_dataset.py,
   the same pencil-and-paper data the integration and E2E suites assert on)
   through the real Phase 1 loader;
3. grants the API role read access (deploy/postgres/grant_api_role.sql) and
   runs VACUUM (ANALYZE), as the production restore does.

A database that already holds data is left alone unless ATUS_SEED_FORCE=1, so
restarting the stack keeps its data_version. In the ``dump`` profile the
restore service fills the database first and this step only re-applies the
grants.

    ATUS_SEED_DATABASE_URL=postgresql://neondb_owner:...@db:5432/atus \\
        python /workspace/deploy/stack/seed_fixture.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import psycopg

WORKSPACE = Path(os.environ.get("ATUS_SEED_WORKSPACE", "/workspace"))
sys.path.insert(0, str(WORKSPACE))  # the tests package is not installed

from atus_pipeline.database.migrate import apply_migrations  # noqa: E402
from atus_pipeline.database.versions import read_data_version  # noqa: E402
from atus_pipeline.loading.loader import load_all  # noqa: E402
from tests.integration.analytics_dataset import analytics_settings  # noqa: E402

GRANTS = WORKSPACE / "deploy" / "postgres" / "grant_api_role.sql"


def is_loaded(conn: psycopg.Connection) -> bool:
    """True when the atus schema exists and holds at least one respondent."""
    exists = conn.execute("SELECT to_regclass('atus.respondents') IS NOT NULL").fetchone()[0]
    if not exists:
        return False
    return conn.execute("SELECT EXISTS (SELECT 1 FROM atus.respondents)").fetchone()[0]


def main() -> int:
    database_url = os.environ["ATUS_SEED_DATABASE_URL"]
    force = os.environ.get("ATUS_SEED_FORCE") == "1"

    with psycopg.connect(database_url) as conn:
        loaded = is_loaded(conn)
        conn.rollback()
        if loaded and not force:
            print("seed: data already present; skipping the fixture load", flush=True)
        else:
            apply_migrations(conn, WORKSPACE / "migrations")
            with tempfile.TemporaryDirectory(prefix="atus-seed-") as tmp:
                settings = analytics_settings(Path(tmp), database_url)
                load_all(conn, settings)
            print("seed: loaded the analytics fixture dataset", flush=True)

        with conn.transaction():
            conn.execute(GRANTS.read_text())
        version = read_data_version(conn)
        conn.commit()

    with psycopg.connect(database_url, autocommit=True) as conn:
        tables = conn.execute(
            "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
            "WHERE schemaname = 'atus' ORDER BY 1"
        ).fetchall()
        for (table,) in tables:
            conn.execute(f"VACUUM (ANALYZE) {table}")

    print(f"seed: ready; data_version={version}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
