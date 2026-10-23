"""PostgreSQL connection helper."""

from __future__ import annotations

import psycopg


def connect(database_url: str) -> psycopg.Connection:
    """Open a psycopg3 connection. Autocommit stays off: callers own transactions."""
    return psycopg.connect(database_url)
