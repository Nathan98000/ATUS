"""The data version: one string that identifies the exact data a database serves.

Caches key results by it (the API's result cache, the edge cache, the
browser's query cache), so it must change whenever the data changes and must
never repeat for different data — including across rebuilt or replacement
databases, whose ``ingestion_runs`` identity restarts at 1.

Writers record an explicit, globally unique ``ingestion_runs.data_version``
(migration 0003) in the same transaction that makes their data live:

* ``atus load``: ``<release>:load-<run_id>-<UTC YYYYMMDDTHHMMSSZ>-<6 hex>``;
* the serving-DB builder: ``<release>:serving-<build_id>:<content_hash[:12]>``
  with ``build_id = <UTC YYYYMMDDTHHMMSSZ>-<8 hex>``.

The random suffix makes two loads in the same second (or two databases that
both restart at run 1) differ; the column is UNIQUE besides. Rows written
before that column existed fall back to
``<release>:run<id>:<started_at epoch micros>``: the start timestamp
distinguishes a rebuilt database from the one it replaced.

The query reads the row through ``to_jsonb`` so it works whether or not the
``data_version`` column exists yet (databases migrated before it was added).
Every format starts with ``<release>:``.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

import psycopg

UNLOADED = "unloaded:run0"

_LATEST_SUCCEEDED = (
    "SELECT to_jsonb(r) FROM atus.ingestion_runs r "
    "WHERE r.status = 'succeeded' ORDER BY r.id DESC LIMIT 1"
)

_SERVING_MARK = ":serving-"


def _utc_stamp(now: datetime | None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def format_load_version(
    release: str, run_id: int, *, now: datetime | None = None, token: str | None = None
) -> str:
    """Data version written by ``atus load`` for a run that went live."""
    token = token if token is not None else secrets.token_hex(3)
    return f"{release}:load-{run_id}-{_utc_stamp(now)}-{token}"


def new_build_id(*, now: datetime | None = None, token: str | None = None) -> str:
    """A serving build id: ``<UTC YYYYMMDDTHHMMSSZ>-<8 hex>``."""
    token = token if token is not None else secrets.token_hex(4)
    return f"{_utc_stamp(now)}-{token}"


def format_serving_version(release: str, build_id: str, content_hash: str) -> str:
    """Data version written by the serving-DB builder."""
    return f"{release}{_SERVING_MARK}{build_id}:{content_hash[:12]}"


def is_serving_version(version: str | None) -> bool:
    return bool(version) and _SERVING_MARK in version


def release_of(version: str) -> str | None:
    """The release code a data version was written for (``None`` when unloaded)."""
    if not version or version == UNLOADED:
        return None
    return version.split(":", 1)[0]


def format_legacy_version(release: str, run_id: int, started_at: datetime | str | None) -> str:
    """Version string for a run row that carries no explicit data_version."""
    if isinstance(started_at, str):
        started_at = datetime.fromisoformat(started_at)
    stamp = int(started_at.timestamp() * 1_000_000) if started_at else 0
    return f"{release}:run{run_id}:{stamp}"


def version_from_row(row: dict | None) -> str:
    if not row:
        return UNLOADED
    explicit = row.get("data_version")
    if explicit:
        return explicit
    return format_legacy_version(row["release"], row["id"], row.get("started_at"))


def read_data_version(conn: psycopg.Connection) -> str:
    """The data version of the latest succeeded ingestion run (one statement)."""
    row = conn.execute(_LATEST_SUCCEEDED).fetchone()
    return version_from_row(row[0] if row else None)


def read_release(conn: psycopg.Connection) -> str | None:
    """The BLS release code of the loaded data (one statement; the same query
    as :func:`read_data_version`), or ``None`` when nothing is loaded."""
    row = conn.execute(_LATEST_SUCCEEDED).fetchone()
    return row[0]["release"] if row else None
