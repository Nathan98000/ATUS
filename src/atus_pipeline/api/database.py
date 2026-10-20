"""The API's only path to PostgreSQL: a lazy pool and bounded transactions.

Pool (audit F05, F17, F18, F36, I08):

* ``min_size=0`` — no connection is opened at startup, so an API cold start
  never wakes a scale-to-zero database; connections open on first use.
* ``max_idle`` below Neon's 5-minute suspend, sizes and timeouts from the
  environment, libpq ``connect_timeout`` and TCP keepalives so a vanished
  server is noticed, ``application_name`` for server-side attribution, and a
  configurable ``prepare_threshold`` (``None`` behind transaction poolers).
* Every checkout runs ``ConnectionPool.check_connection`` (one empty query).
  When it finds a dead connection — the server restarted or the compute
  suspended — the pool is drained, because the other idle connections died
  the same way; the next attempt gets a fresh connection.

Checkout (audit F34): when every connection is busy, a request waits
``ATUS_API_POOL_TIMEOUT_S`` and then gets 503 ``overloaded``. When the pool
must open a connection, it may wait ``POOL_TIMEOUT + CONNECT_TIMEOUT`` (a
waking database is slow to accept), but fails as soon as a connection attempt
has failed: 503 ``database_paused`` for Neon's quota refusals, otherwise 503
``database_unavailable``.

Transactions: each request's work runs in one transaction whose first
statement applies the request's limits with ``set_config(..., true)`` — the
function form of ``SET LOCAL``, so the values end with the transaction and
are safe behind a transaction-mode pooler (never a session ``SET`` or a
startup ``options`` parameter). Analyses also get a wall-clock deadline that
cancels the running query, so a request issuing several statements is
bounded too. The transaction ends (commit/rollback) and the connection goes
back to the pool before the HTTP response is sent (audit I10).

Every transaction is ``READ ONLY`` (psycopg sends ``BEGIN READ ONLY``; no
extra statement): the API never writes (audit I05), so even a role that
could write cannot be made to by the API. Statements and transaction time
are counted per request for the access log, ``Server-Timing`` and metrics
(``observability.py``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from ..config import Settings
from ..database import versions
from . import observability
from .errors import ApiError, DatabaseUnavailableError, OverloadedError, classify_connect_failure
from .snapshot import DataSnapshot, load_snapshot

log = logging.getLogger("atus.api")

APPLICATION_NAME = "atus-api"

# Per-transaction limits that are not deployment knobs (ADR-011 role defaults,
# repeated per request so they hold even where role defaults cannot be set).
LOCK_TIMEOUT_MS = 5_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 30_000
# The server checks this often whether the API's socket is still open while
# a query runs, so a killed API process does not leave its query running
# (audit F52). Needs PostgreSQL >= 14.
CLIENT_CONNECTION_CHECK_INTERVAL_MS = 5_000

DEADLINE_MARGIN_S = 5.0
"""An analysis may run ``statement_timeout + DEADLINE_MARGIN_S`` in total
before its current query is cancelled (85 s + 5 s = the ADR's 90 s)."""

_ACQUIRE_SLICE_S = 1.0  # while a connection is being opened, re-check for failures this often

KINDS = ("analysis", "metadata")


def connection_kwargs(settings: Settings) -> dict:
    """Connection parameters added to ``ATUS_DATABASE_URL`` (they override
    the same keys in the URL)."""
    return {
        "connect_timeout": settings.api_connect_timeout_s,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "application_name": APPLICATION_NAME,
        "prepare_threshold": settings.api_prepare_threshold,
    }


def session_limits(settings: Settings, kind: str) -> list[tuple[str, str]]:
    """The ``(parameter, value)`` pairs applied at the start of a request's
    transaction."""
    if kind not in KINDS:
        raise ValueError(f"unknown transaction kind {kind!r}")
    timeout_ms = (
        settings.api_statement_timeout_ms if kind == "analysis"
        else settings.api_meta_statement_timeout_ms
    )
    limits = [
        ("statement_timeout", str(timeout_ms)),
        ("lock_timeout", str(LOCK_TIMEOUT_MS)),
        ("idle_in_transaction_session_timeout", str(IDLE_IN_TRANSACTION_TIMEOUT_MS)),
        ("client_connection_check_interval", str(CLIENT_CONNECTION_CHECK_INTERVAL_MS)),
        ("jit", "on" if settings.api_jit else "off"),
    ]
    if settings.api_max_parallel_workers is not None:
        limits.append(("max_parallel_workers_per_gather", str(settings.api_max_parallel_workers)))
    return limits


def session_limits_statement(limits: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """One statement applying every limit transaction-locally. Parameter
    names come from the fixed list above; values are bound parameters."""
    sql = "SELECT " + ", ".join(f"set_config('{name}', %s, true)" for name, _ in limits)
    return sql, [value for _, value in limits]


@dataclass
class _ConnectEvent:
    at: float
    error: BaseException | None


class ConnectTracker:
    """Remembers the outcome of the most recent connection attempt.

    psycopg_pool opens connections in background workers and only tells a
    waiting client "timed out"; this is how a checkout learns *why* (the
    server refused, the quota is exhausted, or it is merely busy).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: _ConnectEvent | None = None

    def record(self, error: BaseException | None) -> None:
        with self._lock:
            self._last = _ConnectEvent(monotonic(), error)

    def failing(self) -> BaseException | None:
        """The error of the latest attempt, if the latest attempt failed."""
        with self._lock:
            return self._last.error if self._last is not None else None


class CountingCursor(psycopg.Cursor):
    """Counts every statement it sends against the current request
    (``Connection.execute`` goes through the connection's cursor factory)."""

    def execute(self, *args, **kwargs):
        observability.note_statement()
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        observability.note_statement()
        return super().executemany(*args, **kwargs)

    def stream(self, *args, **kwargs):
        observability.note_statement()
        return super().stream(*args, **kwargs)


def _tracking_connection_class(base: type[psycopg.Connection], tracker: ConnectTracker):
    class TrackedConnection(base):  # type: ignore[valid-type, misc]
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):
            try:
                conn = super().connect(conninfo, **kwargs)
            except Exception as exc:
                tracker.record(exc)
                raise
            tracker.record(None)
            conn.read_only = True  # every transaction: BEGIN READ ONLY (I05)
            conn.cursor_factory = CountingCursor
            return conn

    return TrackedConnection


class Database:
    """Pool owner and the per-request transaction context manager."""

    def __init__(
        self, settings: Settings, *, open: bool = True,
        connection_class: type[psycopg.Connection] = psycopg.Connection,
    ):
        self._settings = settings
        self.tracker = ConnectTracker()
        self.pool = ConnectionPool(
            conninfo=settings.database_url,
            connection_class=_tracking_connection_class(connection_class, self.tracker),
            kwargs=connection_kwargs(settings),
            min_size=0,
            max_size=settings.api_pool_max_size,
            max_idle=settings.api_pool_max_idle_s,
            timeout=settings.api_pool_timeout_s,
            # Give up a failing background connect quickly, so the next
            # request starts a fresh attempt instead of waiting out a long
            # exponential backoff.
            reconnect_timeout=float(settings.api_connect_timeout_s + settings.api_pool_timeout_s),
            check=self._check,
            open=False,
            name=APPLICATION_NAME,
        )
        self._active_lock = threading.Lock()
        self._active: set[psycopg.Connection] = set()
        self.query_timeouts = 0  # queries cancelled by statement_timeout or the deadline
        if open:
            self.pool.open(wait=False)  # min_size=0: starts workers, opens nothing

    # ------------------------------------------------------------------ #
    # pool callbacks
    # ------------------------------------------------------------------ #

    def _check(self, conn: psycopg.Connection) -> None:
        try:
            ConnectionPool.check_connection(conn)
        except Exception:
            log.info("pooled connection failed its check; draining idle connections")
            drain = getattr(self.pool, "drain", None)  # psycopg_pool >= 3.3
            if drain is not None:
                drain()
            raise

    # ------------------------------------------------------------------ #
    # checkout
    # ------------------------------------------------------------------ #

    def _saturated(self) -> bool:
        stats = self.pool.get_stats()
        return stats.get("pool_available", 0) == 0 and (
            stats.get("pool_size", 0) >= stats.get("pool_max", self._settings.api_pool_max_size)
        )

    def _timeout_error(self) -> ApiError:
        failure = self.tracker.failing()
        if failure is not None:
            return classify_connect_failure(failure)
        if self._saturated():
            return OverloadedError(
                f"no database connection within {self._settings.api_pool_timeout_s:g} s"
            )
        return DatabaseUnavailableError("could not open a database connection in time")

    def acquire(self) -> psycopg.Connection:
        settings = self._settings
        if self._saturated():
            try:
                return self.pool.getconn(timeout=settings.api_pool_timeout_s)
            except PoolTimeout:
                raise self._timeout_error() from None
        deadline = monotonic() + settings.api_pool_timeout_s + settings.api_connect_timeout_s
        while True:
            remaining = deadline - monotonic()
            try:
                return self.pool.getconn(timeout=max(0.01, min(_ACQUIRE_SLICE_S, remaining)))
            except PoolTimeout:
                failure = self.tracker.failing()
                if failure is not None:
                    raise classify_connect_failure(failure) from None
                if monotonic() >= deadline:
                    raise self._timeout_error() from None

    # ------------------------------------------------------------------ #
    # transactions
    # ------------------------------------------------------------------ #

    @contextmanager
    def transaction(
        self, kind: str, *, repeatable_read: bool = False
    ) -> Iterator[psycopg.Connection]:
        """One read-only transaction with the request's limits applied (see
        module docstring). Commits on success, rolls back on error, and
        returns the connection to the pool before the caller continues.
        ``repeatable_read`` makes every statement see the same snapshot of
        the data (``BEGIN ISOLATION LEVEL REPEATABLE READ``)."""
        with observability.database_time():
            with self._transaction(kind, repeatable_read) as conn:
                yield conn

    @contextmanager
    def _transaction(self, kind: str, repeatable_read: bool) -> Iterator[psycopg.Connection]:
        sql, params = session_limits_statement(session_limits(self._settings, kind))
        conn = self.acquire()
        deadline: _Deadline | None = None
        with self._active_lock:
            self._active.add(conn)
        try:
            if repeatable_read:
                conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            if kind == "analysis":
                deadline = _Deadline(
                    conn, self._settings.api_statement_timeout_ms / 1000 + DEADLINE_MARGIN_S
                )
            with conn:
                conn.execute(sql, params)
                yield conn
        except psycopg.errors.QueryCanceled:
            with self._active_lock:
                self.query_timeouts += 1
            raise
        finally:
            if deadline is not None:
                deadline.stop()  # after this, no cancel can reach the reused connection
            with self._active_lock:
                self._active.discard(conn)
            if repeatable_read:
                try:
                    conn.isolation_level = None  # back to the server default
                except Exception:  # a broken connection is discarded by putconn
                    pass
            self.pool.putconn(conn)

    def read_data_version(self) -> str:
        """The served data version (one short metadata transaction)."""
        with self.transaction("metadata") as conn:
            return versions.read_data_version(conn)

    def read_snapshot(self) -> DataSnapshot:
        """Everything the in-process data snapshot holds, read in one
        consistent metadata transaction (``snapshot.py``)."""
        with self.transaction("metadata", repeatable_read=True) as conn:
            return load_snapshot(conn)

    # ------------------------------------------------------------------ #
    # cancellation and shutdown
    # ------------------------------------------------------------------ #

    def cancel_active(self) -> int:
        """Cancel every query in flight (process shutdown). Returns how many
        connections were signalled."""
        with self._active_lock:
            active = list(self._active)
        for conn in active:
            cancel_query(conn, "shutting down")
        return len(active)

    def close(self) -> None:
        self.pool.close(timeout=5.0)


def cancel_query(conn: psycopg.Connection, reason: str) -> None:
    try:
        conn.cancel_safe(timeout=5.0)
        log.warning("cancelled a running query: %s", reason)
    except Exception as exc:  # the query may have just finished
        log.info("query cancel (%s) not delivered: %s", reason, type(exc).__name__)


class _Deadline:
    """Cancels a connection's running query once ``seconds`` have passed,
    unless :meth:`stop` was called first (a cancel can never reach the
    connection after it went back to the pool)."""

    def __init__(self, conn: psycopg.Connection, seconds: float):
        self._conn = conn
        self._lock = threading.Lock()
        self._done = False
        self.fired = False
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._done:
                return
            self.fired = True
            cancel_query(self._conn, "request deadline exceeded")

    def stop(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._done = True
