"""Minimal, transparent SQL migration runner.

Migrations are plain ``.sql`` files in ``migrations/``, named
``NNNN_description.sql`` and applied in filename order. Each migration runs in
its own transaction and is recorded in ``public.schema_migrations`` together
with a SHA-256 of its content; re-running is a no-op, and editing an
already-applied migration fails loudly (write a new migration instead).

A full framework (Alembic) was deliberately not used: Phase 1 has a small,
SQL-first schema, and a ~100-line runner keeps the entire mechanism readable.
The tracking table makes switching to a heavier tool later straightforward.

The same migration files also build the loader's *shadow* schema (see
``loading/switch.py``): :func:`build_schema_copy` replays them with every
``atus.`` reference retargeted to another schema, so the copy is structurally
identical to the live schema by construction.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

LIVE_SCHEMA = "atus"
MIGRATIONS_DIR_ENV = "ATUS_MIGRATIONS_DIR"

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_QUALIFIED_REF = re.compile(r"\batus\.")
_CREATE_SCHEMA = re.compile(r"\bCREATE SCHEMA IF NOT EXISTS atus\b", re.IGNORECASE)
_BARE_SCHEMA_REF = re.compile(r"\batus\b")
_SQL_COMMENT = re.compile(r"--[^\n]*")
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str      # e.g. "0001"
    name: str         # file name
    path: Path
    sql: str
    checksum: str


def discover_migrations(migrations_dir: Path) -> list[Migration]:
    migrations = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise MigrationError(
                f"Migration file {path.name!r} does not match NNNN_description.sql"
            )
        sql = path.read_text()
        migrations.append(
            Migration(
                version=match.group(1),
                name=path.name,
                path=path,
                sql=sql,
                checksum=hashlib.sha256(sql.encode()).hexdigest(),
            )
        )
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"Duplicate migration versions in {migrations_dir}: {versions}")
    return migrations


def apply_migrations(conn: psycopg.Connection, migrations_dir: Path) -> list[str]:
    """Apply pending migrations; returns the names of those applied."""
    with conn.transaction():
        conn.execute(_TRACKING_TABLE_DDL)

    applied_rows = conn.execute(
        "SELECT version, checksum FROM public.schema_migrations"
    ).fetchall()
    applied = {version: checksum for version, checksum in applied_rows}

    newly_applied: list[str] = []
    for migration in discover_migrations(migrations_dir):
        if migration.version in applied:
            if applied[migration.version] != migration.checksum:
                raise MigrationError(
                    f"{migration.name} was modified after being applied "
                    f"(checksum mismatch). Create a new migration instead."
                )
            log.debug("%s: already applied", migration.name)
            continue
        log.info("Applying migration %s", migration.name)
        with conn.transaction():
            conn.execute(migration.sql)
            conn.execute(
                "INSERT INTO public.schema_migrations (version, checksum) VALUES (%s, %s)",
                (migration.version, migration.checksum),
            )
        newly_applied.append(migration.name)
    if not newly_applied:
        log.info("Schema is up to date (%d migrations applied)", len(applied))
    return newly_applied


def default_migrations_dir() -> Path:
    """Where the migration files are: ``$ATUS_MIGRATIONS_DIR`` if set, else
    ``migrations/`` of the source checkout this package runs from.

    An installed package without a checkout (for example the API image) has
    no migrations; data commands then need ``ATUS_MIGRATIONS_DIR``. The API
    itself never needs them.
    """
    override = os.environ.get(MIGRATIONS_DIR_ENV)
    if override:
        path = Path(override)
        if not path.is_dir():
            raise MigrationError(f"{MIGRATIONS_DIR_ENV}={override} is not a directory")
        return path
    candidate = Path(__file__).resolve().parents[3] / "migrations"
    if candidate.is_dir():
        return candidate
    raise MigrationError(
        f"migrations directory not found at {candidate}; run from a source checkout "
        f"or set {MIGRATIONS_DIR_ENV} to the repository's migrations/ directory"
    )


def retarget_sql(sql: str, schema: str) -> str:
    """Rewrite every ``atus.`` reference in a migration or query to ``schema``.

    Refuses SQL that mentions the ``atus`` schema unqualified (``ON SCHEMA
    atus``, ``search_path``), because such a statement would act on the live
    schema while building a copy.
    """
    if schema == LIVE_SCHEMA:
        return sql
    if not _SCHEMA_NAME.match(schema):
        raise MigrationError(f"invalid schema name {schema!r}")
    retargeted = _CREATE_SCHEMA.sub(f"CREATE SCHEMA IF NOT EXISTS {schema}", sql)
    retargeted = _QUALIFIED_REF.sub(f"{schema}.", retargeted)
    leftover = _BARE_SCHEMA_REF.search(_SQL_COMMENT.sub("", retargeted))
    if leftover:
        raise MigrationError(
            "cannot retarget SQL that references the atus schema unqualified "
            f"(near {retargeted[max(leftover.start() - 30, 0):leftover.end() + 30]!r})"
        )
    return retargeted


def assert_schema_current(conn: psycopg.Connection, migrations_dir: Path) -> None:
    """Fail unless the database has exactly this code's migrations applied.

    A schema copy is built from the migration files, so swapping it in is
    only safe when the live schema was built from the same files.
    """
    migrations = discover_migrations(migrations_dir)
    try:
        with conn.transaction():
            applied = dict(
                conn.execute("SELECT version, checksum FROM public.schema_migrations")
            )
    except psycopg.errors.UndefinedTable as exc:
        raise MigrationError("the schema is not initialized: run `atus migrate`") from exc
    expected = {m.version: m.checksum for m in migrations}
    behind = sorted(v for v in expected if v not in applied)
    ahead = sorted(v for v in applied if v not in expected)
    changed = sorted(v for v in expected if v in applied and applied[v] != expected[v])
    if behind:
        raise MigrationError(
            f"the database schema is behind this code (pending: {', '.join(behind)}); "
            "run `atus migrate` first"
        )
    if ahead or changed:
        raise MigrationError(
            "the database schema does not match this code's migrations "
            f"(unknown: {', '.join(ahead) or 'none'}; modified: {', '.join(changed) or 'none'})"
        )


def build_schema_copy(conn: psycopg.Connection, migrations_dir: Path, schema: str) -> None:
    """Create ``schema`` from scratch by replaying every migration into it.

    Runs in the caller's transaction and is not recorded in
    ``schema_migrations`` (the copy is a build artifact, not a database
    version). An existing ``schema`` is dropped first.
    """
    if schema == LIVE_SCHEMA or not _SCHEMA_NAME.match(schema):
        raise MigrationError(f"refusing to rebuild schema {schema!r}")
    conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.execute(f"CREATE SCHEMA {schema}")
    for migration in discover_migrations(migrations_dir):
        conn.execute(retarget_sql(migration.sql, schema))
