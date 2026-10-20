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

The script drops schema ``atus`` in that database, so it refuses any target
that is not plainly disposable (audit F60): the database must be named
``atus_e2e`` or ``atus_e2e_<suffix>`` and the host must be local (loopback, a
Unix socket, or a single-label container name such as ``db``). It never prints
the password.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import psycopg
import uvicorn

from atus_pipeline.api.app import create_app
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))  # the tests package is not installed

from tests.integration.analytics_dataset import analytics_settings  # noqa: E402

DEFAULT_URL = "postgresql://atus:atus@localhost:5434/atus_e2e"

DISPOSABLE_DATABASE = re.compile(r"^atus_e2e(_[a-z0-9_]+)?$")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SINGLE_LABEL_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", re.IGNORECASE)


def redact_url(url: str) -> str:
    """The URL with its password (if any) replaced by ``***``: safe to print."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user = userinfo.split(":", 1)[0]
    return urlunsplit(parts._replace(netloc=f"{user}:***@{hostport}"))


def _is_local_host(host: str) -> bool:
    return host in LOOPBACK_HOSTS or host.startswith("/") or bool(SINGLE_LABEL_HOST.match(host))


def check_disposable(url: str) -> None:
    """Exit unless ``url`` names a local database called atus_e2e[_<suffix>].

    libpq lets query parameters override the host and database of a URL, so
    those are checked too. Key=value connection strings are refused outright:
    a URL is the only form whose target can be read unambiguously here.
    """
    parts = urlsplit(url)
    problems = []
    if parts.scheme not in ("postgresql", "postgres"):
        problems.append("use a postgresql:// URL")
    query = parse_qs(parts.query)
    dbname = query.get("dbname", [parts.path.lstrip("/")])[-1]
    if not DISPOSABLE_DATABASE.match(dbname):
        problems.append(f"database {dbname!r} is not named atus_e2e or atus_e2e_<suffix>")
    hosts = [parts.hostname or "localhost"]
    for key in ("host", "hostaddr"):
        hosts.extend(h for value in query.get(key, []) for h in value.split(","))
    remote = [h for h in hosts if not _is_local_host(h)]
    if remote:
        problems.append(f"host {', '.join(remote)} is not local")
    if "service" in query:
        problems.append("service= files are not allowed (their target cannot be checked)")
    if problems:
        raise SystemExit(
            "e2e_backend: refusing to drop schema atus in "
            f"{redact_url(url)}: {'; '.join(problems)}"
        )


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
    check_disposable(database_url)
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
        print(f"e2e backend ready on :{args.port} (db {redact_url(database_url)})", flush=True)
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
