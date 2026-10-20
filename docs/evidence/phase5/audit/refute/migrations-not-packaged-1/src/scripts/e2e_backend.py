"""Deterministic API backend for the frontend's Playwright E2E tests.

Builds the hand-computed analytics fixture dataset (the same one the Phase 2/3
integration tests use — see tests/integration/analytics_dataset.py), loads it
into a disposable database through the real Phase 1 loader, and serves the
real Phase 3 API with CORS for the frontend under test. Every number the E2E
tests assert is therefore verifiable by pencil and paper, with no dependence
on the full BLS download.

    python scripts/e2e_backend.py --port 8137 --origin http://localhost:4173

Environment: ATUS_E2E_DATABASE_URL overrides the disposable database
(default: the docker-compose instance, database ``atus_e2e``).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import uvicorn

from atus_pipeline.api.app import create_app
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # the tests package is not installed

from tests.integration.analytics_dataset import analytics_settings  # noqa: E402

DEFAULT_URL = "postgresql://atus:atus@localhost:5434/atus_e2e"


def ensure_database(url: str) -> None:
    parts = urlsplit(url)
    dbname = parts.path.lstrip("/")
    admin_url = urlunsplit(parts._replace(path="/postgres"))
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{dbname}"')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8137)
    parser.add_argument(
        "--origin",
        action="append",
        default=None,
        help="Allowed CORS origin (repeatable). Default: the Playwright preview origin.",
    )
    args = parser.parse_args()
    origins = tuple(args.origin or ["http://localhost:4173", "http://127.0.0.1:4173"])

    database_url = os.environ.get("ATUS_E2E_DATABASE_URL", DEFAULT_URL)
    ensure_database(database_url)

    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute("DROP SCHEMA IF EXISTS atus CASCADE")
            conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
        apply_migrations(conn, REPO_ROOT / "migrations")

    with tempfile.TemporaryDirectory(prefix="atus-e2e-") as tmp:
        settings = analytics_settings(Path(tmp), database_url)
        settings = type(settings)(
            database_url=settings.database_url,
            data_dir=settings.data_dir,
            release=settings.release,
            log_level="WARNING",
            api_cors_origins=origins,
        )
        with psycopg.connect(database_url) as conn:
            load_all(conn, settings)
        print(f"e2e backend ready on :{args.port} (db {database_url})", flush=True)
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
