"""How one analysis request is served: cache, single-flight, admission, compute.

    data snapshot -> cache lookup --------------------------> hit  (X-Cache: hit)
                  -> identical computation already running -> join (X-Cache: join,
                                                                   or the leader's bypass)
                  -> dry run of the engine up to its first statement (422s)
                  -> daily cap, per-client analysis bucket
                  -> lane (bounded wait) -> pool connection -> engine
                  -> store in cache -------------------------> miss (X-Cache: miss)

The data version, loaded years and activity resolver come from the
in-process data snapshot (``snapshot.py``), so everything before the lane
costs no database statement while the snapshot is fresh (audit F14). The
computation's own transaction reads the data version first and keeps
``atus load``'s switch out until it ends, so a result is computed on one
version and labelled (``X-Data-Version``, ``X-Analysis-Key``) with that
version, even when the data changed after the snapshot was read
(``_compute``).

**Dry run.** Before a request is charged, the real ``AnalysisEngine`` runs
the request on the snapshot's resolver and years with a stand-in connection
that stops it the moment it touches the connection in any way. Everything
the engine does before its first statement is deterministic given those
inputs, so an analytics error raised there (unknown activity code, year not
loaded, 2020 under the multi-year weights, ...) is exactly the error the
computation would raise first; it is answered as the usual 422 without a
token, a lane or a statement. Anything else the dry run meets is ignored and
the request computes as before. The dry run never produces a result.

Cache hits and joiners never take a lane or an analysis token; only a request
that will actually compute does. The computation runs as a task detached from
the request that started it: if that client disconnects, the admitted
computation still finishes (bounded by the statement timeout and the request
deadline) and its result is cached, so a retry after a proxy timeout hits the
cache or joins instead of paying twice (ADR-011). Errors are never cached;
every request waiting on a failed computation receives the same error.

No statistical logic lives here: ``compute`` calls the engine and serializes
its result exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

import anyio
import psycopg
from fastapi import Request, Response

from ..analytics import ANALYTICS_VERSION, AnalysisEngine
from ..analytics.errors import AnalyticsError, InsufficientDataError
from ..database import versions
from . import RESPONSE_FORMAT
from .cache import AnalysisCache, cache_key
from .database import Database
from .errors import (
    DailyCapacityReachedError,
    DataNotLoadedError,
    DataVersionMismatchError,
    OverloadedError,
    RateLimitedError,
)
from .protection import Protection, is_heavy
from .snapshot import DataSnapshot, SnapshotStore

log = logging.getLogger("atus.api")

EngineFactory = Callable[..., AnalysisEngine]
"""``factory(conn, *, resolver=..., loaded_years=...)``: the engine's
constructor, or a test double accepting the same arguments."""


@dataclass(frozen=True)
class AnalysisJob:
    operation: str                          # estimate | trend | compare
    spec_payload: dict                      # canonical spec (the cache key's content)
    years: tuple[int, ...]
    compute: Callable[[AnalysisEngine], dict]

    @property
    def heavy(self) -> bool:
        return is_heavy(self.operation, self.years)


@dataclass(frozen=True)
class _Outcome:
    """A computation's result and what it was computed on: the data version
    its transaction read, and the cache key for that version."""

    payload: dict
    cache_status: str                       # miss | bypass
    data_version: str
    key: str


class _Flight:
    """One in-progress computation that identical requests can join."""

    def __init__(self, future: asyncio.Future):
        self.future = future
        self.joiners = 0


def _mark_retrieved(future: asyncio.Future) -> None:
    # Every waiter may have gone (disconnected); don't log "never retrieved".
    if not future.cancelled():
        future.exception()


class _NeedsDatabase(Exception):
    """The engine reached its first use of the connection (dry run over)."""


class _DryRunConnection:
    """Ends a dry run on any use: every attribute raises ``_NeedsDatabase``."""

    def __getattr__(self, name: str):
        raise _NeedsDatabase(name)


def dry_run(job: AnalysisJob, snapshot: DataSnapshot) -> None:
    """Raise the analytics error the computation would raise before its
    first database statement, if any (see module docstring)."""
    engine = AnalysisEngine(_DryRunConnection(), **snapshot.engine_kwargs())
    try:
        job.compute(engine)
    except _NeedsDatabase:
        return
    except AnalyticsError:
        raise
    except Exception:  # not a domain answer: leave it to the real computation
        log.debug("analysis dry run inconclusive", exc_info=True)


def _label(headers, key: str, data_version: str) -> None:
    """The headers saying what an analysis response's bytes depend on."""
    headers["X-Analysis-Key"] = key
    headers["X-Data-Version"] = data_version
    headers["X-Analytics-Version"] = ANALYTICS_VERSION
    headers["X-Response-Format"] = RESPONSE_FORMAT


def _respondents_loaded(conn: psycopg.Connection) -> bool:
    return bool(conn.execute("SELECT EXISTS (SELECT 1 FROM atus.respondents)").fetchone()[0])


class AnalysisRunner:
    """Per-process analysis orchestration (built once per lifespan)."""

    def __init__(self, protection: Protection, snapshots: SnapshotStore):
        self.protection = protection
        self.snapshots = snapshots
        self._flights: dict[str, _Flight] = {}
        self._tasks: set[asyncio.Task] = set()
        self.computations = 0
        self.joins = 0

    def flight(self, key: str) -> _Flight | None:
        return self._flights.get(key)

    async def run(
        self, job: AnalysisJob, *, request: Request, response: Response,
        cache: AnalysisCache, db: Database, engine_factory: EngineFactory,
    ) -> dict:
        snapshot = await self.snapshots.get(db)
        if not snapshot.is_loaded:
            raise DataNotLoadedError("no succeeded ingestion run")
        data_version = snapshot.data_version
        key = cache_key(job.operation, job.spec_payload, ANALYTICS_VERSION, data_version)
        headers = response.headers
        _label(headers, key, data_version)

        try:
            hit = cache.get(key)
        except Exception:  # cache trouble must never break analysis
            log.exception("analysis cache get failed; computing without cache")
            hit = None
        if hit is not None:
            headers["X-Cache"] = "hit"
            return hit

        flight = self._flights.get(key)
        if flight is not None:
            flight.joiners += 1
            self.joins += 1
            outcome = await asyncio.shield(flight.future)
            # The leader's labels: a bypass stays visible to its joiners.
            _label(headers, outcome.key, outcome.data_version)
            headers["X-Cache"] = "bypass" if outcome.cache_status == "bypass" else "join"
            return outcome.payload

        # Requests the engine would refuse before querying stop here, free.
        dry_run(job, snapshot)
        # This request will compute: charge it (nothing above awaited since
        # the flight lookup, so registering the flight below is atomic).
        protection = self.protection
        if protection.daily_cap.exhausted():
            raise protection.daily_cap.refuse()
        rate_key = getattr(request.state, "rate_limit_key", None)
        if rate_key is not None:
            wait = protection.analysis_limiter.acquire(rate_key)
            if wait is not None:
                raise RateLimitedError("analysis rate limit", retry_after=wait)

        loop = asyncio.get_running_loop()
        flight = _Flight(loop.create_future())
        flight.future.add_done_callback(_mark_retrieved)
        self._flights[key] = flight
        task = loop.create_task(
            self._lead(flight, key, job, rate_key, cache, db, engine_factory, snapshot)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        outcome = await asyncio.shield(flight.future)
        _label(headers, outcome.key, outcome.data_version)
        headers["X-Cache"] = outcome.cache_status
        return outcome.payload

    async def _lead(
        self, flight: _Flight, key: str, job: AnalysisJob, rate_key: str | None,
        cache: AnalysisCache, db: Database, engine_factory: EngineFactory,
        snapshot: DataSnapshot,
    ) -> None:
        protection = self.protection
        try:
            async with protection.lanes.admit(job.heavy):
                if not protection.daily_cap.try_take():
                    raise protection.daily_cap.refuse()
                self.computations += 1
                result = await anyio.to_thread.run_sync(
                    self._compute, job, key, cache, db, engine_factory, snapshot
                )
        except (OverloadedError, DailyCapacityReachedError) as exc:
            if rate_key is not None:  # refused before computing: give the token back
                protection.analysis_limiter.refund(rate_key)
            flight.future.set_exception(exc)
        except Exception as exc:
            flight.future.set_exception(exc)
        except BaseException:  # cancelled (shutdown): release the waiters, then stop
            flight.future.set_exception(OverloadedError("server shutting down"))
            raise
        else:
            flight.future.set_result(result)
        finally:
            if self._flights.get(key) is flight:
                del self._flights[key]

    def _compute(
        self, job: AnalysisJob, key: str, cache: AnalysisCache, db: Database,
        engine_factory: EngineFactory, snapshot: DataSnapshot,
    ) -> _Outcome:
        """Runs in a worker thread: one analysis transaction, then the cache.

        Concurrent-reload guard: the transaction reads the data version
        first. That read holds a share lock on ``ingestion_runs`` until the
        transaction ends, and ``atus load``'s switch must lock that table
        (ACCESS EXCLUSIVE) before it swaps any table in, so every later
        statement reads the tables of the version read here. When it equals
        the snapshot's, the engine reuses the snapshot's resolver and years
        and the result is cached (``miss``). Otherwise the data changed since
        the snapshot was read: the snapshot is invalidated and, when the
        deployment expects a specific data version, the request is refused as
        ``data_version_mismatch`` before computing. If not, the engine reads
        its own resolver and years in this transaction, so the whole result
        is computed on the version just read; it is labelled with that
        version and returned once, uncached, as ``X-Cache: bypass``.
        """
        with db.transaction("analysis") as conn:
            current_version = versions.read_data_version(conn)
            changed = current_version != snapshot.data_version
            if changed:
                self.snapshots.invalidate()
                if self.snapshots.expected_version is not None:
                    raise DataVersionMismatchError(
                        f"data version changed to {current_version!r} since the snapshot"
                    )
                log.warning(
                    "data version changed since the snapshot; %s computed on the new data "
                    "and returned uncached", job.operation,
                )
                engine = engine_factory(conn)
            else:
                engine = engine_factory(conn, **snapshot.engine_kwargs())
            try:
                payload = job.compute(engine)
            except InsufficientDataError:
                # An empty database is an operational state, not a client
                # error, and its message must not leak operator hints (F51).
                if not _respondents_loaded(conn):
                    raise DataNotLoadedError("database has no respondents") from None
                raise
        if changed:
            return _Outcome(
                payload, "bypass", current_version,
                cache_key(job.operation, job.spec_payload, ANALYTICS_VERSION, current_version),
            )
        try:
            cache.put(key, payload)
        except Exception:
            log.exception("analysis cache put failed; returning uncached result")
        return _Outcome(payload, "miss", current_version, key)

    async def shutdown(self, timeout_s: float = 5.0) -> None:
        """Wait (bounded) for detached computations; the caller cancels their
        queries first so they end promptly."""
        if not self._tasks:
            return
        with anyio.move_on_after(timeout_s):
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
