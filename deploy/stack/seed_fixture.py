"""Build the production-shaped stack database from the hand-computed fixture.

Runs in the `seed` service of docker-compose.prod.yml, inside the API image
(which has the package and psycopg but deliberately no migrations, tests or
data), with the repository's ``migrations/``, ``tests/``, ``data/reference/``
and ``deploy/`` bind-mounted read-only under /workspace. Connects as the owner
role (``neondb_owner``), never as a superuser, and produces exactly what
production serves (ADR-013):

1. loads the analytics fixture dataset (tests/integration/analytics_dataset.py,
   the same pencil-and-paper data the integration and E2E suites assert on)
   through the real Phase 1 loader into a scratch *full* database,
   ``atus_fixture_full`` (the owner has CREATEDB, as Neon's console owner does);
2. builds the serving database from it into ``atus`` with the real serving
   builder (``atus build-serving-db``): the serving shape, the serving marker,
   a globally unique serving ``data_version``, ``VACUUM (ANALYZE)``;
3. drops the scratch database.

The API role is not created here: the ``roles`` service applies
scripts/ops/roles.sql next, exactly as the production restore does.

A database that already serves (an earlier run, or a dump the ``restore``
service restored in the ``dump`` profile) is left alone unless
ATUS_SEED_FORCE=1, which drops ``atus`` and builds it again, so restarting the
stack keeps its data_version. A database that holds data without the serving
marker (a volume from an older stack layout) is rebuilt.

    ATUS_SEED_DATABASE_URL=postgresql://neondb_owner:...@db:5432/atus \\
        python /workspace/deploy/stack/seed_fixture.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

WORKSPACE = Path(os.environ.get("ATUS_SEED_WORKSPACE", "/workspace"))
sys.path.insert(0, str(WORKSPACE))  # the tests package is not installed

from atus_pipeline.database.migrate import apply_migrations  # noqa: E402
from atus_pipeline.database.versions import read_data_version  # noqa: E402
from atus_pipeline.loading.loader import load_all  # noqa: E402
from atus_pipeline.serving.builder import build_serving_database  # noqa: E402
from atus_pipeline.serving.guard import serving_marker  # noqa: E402
from tests.integration.analytics_dataset import analytics_settings  # noqa: E402

MIGRATIONS = WORKSPACE / "migrations"
SCRATCH_DATABASE = "atus_fixture_full"


def with_database(url: str, name: str) -> str:
    """``url`` pointing at database ``name`` instead (same server and role)."""
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


def state(url: str) -> str:
    """``serving``, ``empty`` (nothing loaded) or ``other`` (data, no marker)."""
    with psycopg.connect(url, autocommit=True) as conn:
        if serving_marker(conn):
            return "serving"
        exists = conn.execute("SELECT to_regclass('atus.respondents') IS NOT NULL").fetchone()[0]
        if not exists:
            return "empty"
        loaded = conn.execute("SELECT EXISTS (SELECT 1 FROM atus.respondents)").fetchone()[0]
        return "other" if loaded else "empty"


def recreate(maintenance_url: str, name: str, *, create: bool = True) -> None:
    """Drop database ``name`` (as its owner) and, unless told not to, create it empty."""
    with psycopg.connect(maintenance_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(name)))
        if create:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def load_fixture(full_url: str) -> None:
    with psycopg.connect(full_url) as conn:
        apply_migrations(conn, MIGRATIONS)
        # The loader commits in its own transactions and refuses a connection
        # with one still open (apply_migrations leaves its read open).
        conn.commit()
        with tempfile.TemporaryDirectory(prefix="atus-seed-") as tmp:
            load_all(conn, analytics_settings(Path(tmp), full_url))


def main() -> int:
    database_url = os.environ["ATUS_SEED_DATABASE_URL"]
    force = os.environ.get("ATUS_SEED_FORCE") == "1"
    name = urlsplit(database_url).path.lstrip("/")
    maintenance_url = with_database(database_url, "postgres")
    full_url = with_database(database_url, SCRATCH_DATABASE)

    current = state(database_url)
    if current == "serving" and not force:
        print("seed: a serving database is present; skipping the build", flush=True)
    else:
        if current != "empty":
            print(f"seed: dropping {name} ({current}) to build it again", flush=True)
            recreate(maintenance_url, name)
        recreate(maintenance_url, SCRATCH_DATABASE)
        try:
            load_fixture(full_url)
            print("seed: loaded the analytics fixture into a scratch full database",
                  flush=True)
            report = build_serving_database(
                full_url, database_url, require_validated=False, migrations_dir=MIGRATIONS
            )
        finally:
            recreate(maintenance_url, SCRATCH_DATABASE, create=False)
        print(f"seed: built the serving database (content hash {report.content_hash[:12]})",
              flush=True)

    with psycopg.connect(database_url, autocommit=True) as conn:
        version = read_data_version(conn)
    print(f"seed: ready; data_version={version}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
