"""FastAPI dependencies: settings, pooled connections, engine, cache, versions.

Connections come from the application's ``psycopg_pool.ConnectionPool``
(opened in the lifespan): one connection per request, returned to the pool
afterwards (psycopg_pool resets/rolls back on return). The Phase 2
``AnalysisEngine`` is instantiated per request around that connection — it is
cheap (a few small lexicon lookups) and guarantees no analysis state is shared
between concurrent requests.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
from fastapi import Depends, Request
from psycopg_pool import PoolTimeout

from ..analytics import AnalysisEngine
from ..config import Settings
from .cache import AnalysisCache
from .errors import DatabaseUnavailableError

_CONNECTION_TIMEOUT_SECONDS = 10.0


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_cache(request: Request) -> AnalysisCache:
    return request.app.state.cache


def get_connection(request: Request) -> Iterator[psycopg.Connection]:
    pool = request.app.state.pool
    try:
        with pool.connection(timeout=_CONNECTION_TIMEOUT_SECONDS) as conn:
            yield conn
    except PoolTimeout as exc:
        raise DatabaseUnavailableError(
            "Timed out waiting for a database connection."
        ) from exc


def get_engine(conn: psycopg.Connection = Depends(get_connection)) -> AnalysisEngine:
    return AnalysisEngine(conn)


def read_data_version(conn: psycopg.Connection) -> str:
    """Cache-key component that changes whenever `atus load` rebuilds the data:
    '<release>:run<latest successful ingestion run id>'."""
    row = conn.execute(
        "SELECT release, id FROM atus.ingestion_runs "
        "WHERE status = 'succeeded' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "unloaded:run0"
    release, run_id = row
    return f"{release}:run{run_id}"


def get_data_version(conn: psycopg.Connection = Depends(get_connection)) -> str:
    return read_data_version(conn)
