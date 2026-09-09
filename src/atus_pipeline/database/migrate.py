"""Minimal, transparent SQL migration runner.

Migrations are plain ``.sql`` files in ``migrations/``, named
``NNNN_description.sql`` and applied in filename order. Each migration runs in
its own transaction and is recorded in ``public.schema_migrations`` together
with a SHA-256 of its content; re-running is a no-op, and editing an
already-applied migration fails loudly (write a new migration instead).

A full framework (Alembic) was deliberately not used: Phase 1 has a small,
SQL-first schema, and a ~100-line runner keeps the entire mechanism readable.
The tracking table makes switching to a heavier tool later straightforward.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

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
