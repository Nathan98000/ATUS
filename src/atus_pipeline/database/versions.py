"""The data version: one string that identifies the exact data a database serves.

Caches key results by it (the API's result cache, the edge cache, the
browser's query cache), so it must change whenever the data changes and must
never repeat for different data — including across rebuilt or replacement
databases, whose ``ingestion_runs`` identity restarts at 1.

Writers record an explicit, globally unique ``ingestion_runs.data_version``
(the loader and the serving-DB builder). Rows written before that column
existed fall back to ``<release>:run<id>:<started_at epoch micros>``: the
start timestamp distinguishes a rebuilt database from the one it replaced.

The query reads the row through ``to_jsonb`` so it works whether or not the
``data_version`` column exists yet (databases migrated before it was added).
"""

from __future__ import annotations

from datetime import datetime

import psycopg

UNLOADED = "unloaded:run0"

_LATEST_SUCCEEDED = (
    "SELECT to_jsonb(r) FROM atus.ingestion_runs r "
    "WHERE r.status = 'succeeded' ORDER BY r.id DESC LIMIT 1"
)


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
