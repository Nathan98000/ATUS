"""Per-request observability: database accounting, client-IP truncation, and
the access-log record (ADR-014).

**Database accounting.** The request-context middleware puts a fresh
:class:`RequestStats` in a context variable for every request. Database work
updates it from wherever it runs: worker threads (anyio copies the context
into them), the detached analysis computation and the shared snapshot
refresh (both are tasks created in the context of the request that started
them, so their cost is charged to that request and joiners pay nothing).

* ``db_statements``: statements the API sent through psycopg cursors,
  including the pool's connection check (an empty query) and the
  per-transaction limits. PostgreSQL additionally logs the ``BEGIN`` and
  ``COMMIT``/``ROLLBACK`` psycopg sends around each transaction.
* ``db_ms``: wall time inside :meth:`Database.transaction`, from asking the
  pool for a connection to giving it back, so a waking database shows up
  here and not as application time. A transaction that never got a
  connection counts nothing (:func:`database_time`). A transaction still
  running, once connected, counts up to now: a request answered while its
  computation runs (one cut off at shutdown, review r3) reports what that
  computation has spent so far.

**Client IPs** are personal data (brief §12). Raw addresses exist only in
memory, for the token buckets; logs and metrics only ever see
:func:`ip_prefix` (IPv4 /24, IPv6 /48).
"""

from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.routing import compile_path


@dataclass
class RequestStats:
    db_statements: int = 0
    db_seconds: float = 0.0
    transactions: int = 0
    open_since: float | None = None   # perf_counter() at the start of a running transaction

    @property
    def db_ms(self) -> float:
        running = self.open_since
        ongoing = time.perf_counter() - running if running is not None else 0.0
        return (self.db_seconds + ongoing) * 1000.0


_CURRENT: ContextVar[RequestStats | None] = ContextVar("atus_request_stats", default=None)


def start_request() -> tuple[RequestStats, object]:
    """Begin accounting for a request; returns the stats and a reset token."""
    stats = RequestStats()
    return stats, _CURRENT.set(stats)


def end_request(token) -> None:
    _CURRENT.reset(token)


def current_stats() -> RequestStats | None:
    return _CURRENT.get()


def note_statement() -> None:
    stats = _CURRENT.get()
    if stats is not None:
        stats.db_statements += 1


class DatabaseTime:
    """The block :func:`database_time` measures: charged only if ``connected``.

    Setting ``connected`` also marks the request's transaction as running
    (``RequestStats.open_since``, from the block's start, the wait for the
    connection included), so a response sent before the block ends reports
    its time so far; a block still waiting for a connection reports none."""

    def __init__(self, connected: bool, stats: RequestStats | None, started: float) -> None:
        self._stats = stats
        self.started = started
        self._connected = False
        self.connected = connected

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value: bool) -> None:
        self._connected = value
        if self._stats is not None:
            self._stats.open_since = self.started if value else None


@contextmanager
def database_time(*, connected: bool = True) -> Iterator[DatabaseTime]:
    """Charge the wall time of the enclosed block to the current request.

    ``Database.transaction`` passes ``connected=False`` and sets
    ``connected`` once the pool gave it a connection: a block that never got
    one (the connect was refused, failed authentication or TLS, or timed out)
    charges nothing and counts no transaction, so its ``Server-Timing`` says
    ``db;dur=0`` and the edge governor charges no Neon compute for it
    (review r3). While it runs with a connection it counts up to now
    (``RequestStats.db_ms``)."""
    stats = _CURRENT.get()
    started = time.perf_counter()
    timing = DatabaseTime(connected, stats, started)
    try:
        yield timing
    finally:
        if stats is not None:
            stats.open_since = None
            if timing.connected:
                stats.db_seconds += time.perf_counter() - started
                stats.transactions += 1


def ip_prefix(ip: str | None) -> str | None:
    """The loggable form of a client address: its IPv4 /24 or IPv6 /48
    network (an IPv4-mapped IPv6 address counts as IPv4). ``None`` when
    unknown or not an IP address."""
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    length = 24 if isinstance(address, ipaddress.IPv4Address) else 48
    return str(ipaddress.ip_network((address, length), strict=False))


UNMATCHED_ROUTE = "unmatched"


class RouteTemplates:
    """Maps a request path to the template of the route that serves it
    (``/api/v1/activities/{code}``): bounded cardinality for logs and
    metrics, also for requests rejected before routing. Built from the
    application's own routers in ``create_app``, independent of framework
    internals; a path that matches no route is ``"unmatched"``."""

    def __init__(self) -> None:
        self._routes: list[tuple[re.Pattern, str]] = []

    def add(self, template: str) -> None:
        regex, _, _ = compile_path(template)
        self._routes.append((regex, template))

    def add_router(self, router, prefix: str = "") -> None:
        for route in router.routes:
            path = getattr(route, "path", None)
            if path:
                self.add(prefix + path)

    def match(self, path: str) -> str:
        for regex, template in self._routes:
            if regex.match(path):
                return template
        return UNMATCHED_ROUTE
