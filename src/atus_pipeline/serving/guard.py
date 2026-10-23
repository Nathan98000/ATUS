"""The serving marker and the guard that honours it (audit finding F57).

A serving database records migrations 0001-0003 as applied although its
tables are reshaped, so nothing in ``schema_migrations`` tells the full-DB
commands apart from it: ``atus migrate`` would report it up to date,
``atus load`` would try to rebuild it, and ``atus validate-db`` fails on it by
design. Two explicit markers identify it instead, and either one is enough:

* the comment on schema ``atus`` starts with :data:`SERVING_MARKER`
  (``COMMENT ON SCHEMA`` survives ``pg_dump``/``pg_restore``);
* the data version of the latest succeeded ingestion run is a serving version
  (``<release>:serving-<build_id>:<hash>``).
"""

from __future__ import annotations

import psycopg

from ..database.versions import is_serving_version, read_data_version

SERVING_MARKER = "atus-serving"


class ServingDatabaseError(RuntimeError):
    """A full-database command was pointed at a serving database."""


def serving_marker(conn: psycopg.Connection) -> str | None:
    """Why this database is a serving build, or ``None`` if it is not.

    Leaves the connection's transaction state as it found it (each probe runs
    in its own transaction, or savepoint inside the caller's).
    """
    with conn.transaction():
        row = conn.execute(
            "SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace "
            "WHERE nspname = 'atus'"
        ).fetchone()
    comment = row[0] if row else None
    if comment and comment.startswith(SERVING_MARKER):
        return f"schema comment {comment!r}"
    try:
        with conn.transaction():
            version = read_data_version(conn)
    except psycopg.errors.UndefinedTable:
        return None  # no schema yet: certainly not a serving build
    if is_serving_version(version):
        return f"data version {version}"
    return None


def refuse_if_serving(conn: psycopg.Connection, command: str) -> None:
    """Raise :class:`ServingDatabaseError` if ``conn`` is a serving database."""
    reason = serving_marker(conn)
    if reason:
        raise ServingDatabaseError(
            f"Refusing to run `atus {command}` against a serving database ({reason}). "
            "Serving databases are built from the full database and are never "
            "migrated, loaded, or checked with validate-db in place; point "
            "ATUS_DATABASE_URL at the full database instead."
        )
