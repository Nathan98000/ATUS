"""Integration-test fixtures: a disposable PostgreSQL test database.

Tests connect to ``ATUS_TEST_DATABASE_URL`` (default: the docker-compose
instance, database ``atus_test``). The database is created if missing and its
schema is rebuilt from the real migrations for each test session. If no
PostgreSQL is reachable, integration tests are skipped rather than failed.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from atus_pipeline.database.migrate import apply_migrations

DEFAULT_TEST_URL = "postgresql://atus:atus@localhost:5434/atus_test"
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _test_url() -> str:
    return os.environ.get("ATUS_TEST_DATABASE_URL", DEFAULT_TEST_URL)


def _admin_url(test_url: str) -> str:
    """Same server, but the maintenance database (to be able to CREATE DATABASE)."""
    parts = urlsplit(test_url)
    return urlunsplit(parts._replace(path="/postgres"))


def _ensure_test_database(test_url: str) -> None:
    dbname = urlsplit(test_url).path.lstrip("/")
    with psycopg.connect(_admin_url(test_url), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{dbname}"')


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = _test_url()
    try:
        _ensure_test_database(url)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL not available for integration tests: {exc}")
    return url


@pytest.fixture(scope="session")
def migrated_db(test_db_url: str) -> str:
    """Test database with a freshly applied schema (once per session)."""
    with psycopg.connect(test_db_url) as conn:
        with conn.transaction():
            conn.execute("DROP SCHEMA IF EXISTS atus CASCADE")
            conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
        apply_migrations(conn, MIGRATIONS_DIR)
    return test_db_url


@pytest.fixture
def db(migrated_db: str):
    with psycopg.connect(migrated_db) as conn:
        yield conn
        conn.rollback()
