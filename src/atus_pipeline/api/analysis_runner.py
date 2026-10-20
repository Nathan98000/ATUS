"""How one analysis request is served: cache, single-flight, admission, compute.

    data version -> cache lookup ---------------------------> hit  (X-Cache: hit)
                 -> identical computation already running --> join (X-Cache: join)
                 -> daily cap, per-client analysis bucket
                 -> lane (bounded wait) -> pool connection -> engine
                 -> store in cache --------------------------> miss (X-Cache: miss)

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
from ..analytics.errors import InsufficientDataError
from ..database import versions
from . import RESPONSE_FORMAT
from .cache import AnalysisCache, cache_key
from .database import Database
from .errors import (
    DailyCapacityReachedError,
    DataNotLoadedError,
    OverloadedError,
    RateLimitedError,
)
from .protection import Protection, is_heavy

log = logging.getLogger("atus.api")

EngineFactory = Callable[[psycopg.Connection], AnalysisEngine]


@dataclass(frozen=True)
class AnalysisJob:
    operation: str                          # estimate | trend | compare
    spec_payload: dict                      # canonical spec (the cache key's content)
    years: tuple[int, ...]
    compute: Callable[[AnalysisEngine], dict]

    @property
    def heavy(self) -> bool:
        return is_heavy(self.operation, self.years)


class _Flight:
    """One in-progress computation that identical requests can join."""

    def __init__(self, future: asyncio.Future):
        self.future = future
        self.joiners = 0


def _mark_retrieved(future: asyncio.Future) -> None:
    # Every waiter may have gone (disconnected); don't log "never retrieved".
    if not future.cancelled():
        future.exception()


def _respondents_loaded(conn: psycopg.Connection) -> bool:
    return bool(conn.execute("SELECT EXISTS (SELECT 1 FROM atus.respondents)").fetchone()[0])


class AnalysisRunner:
    """Per-process analysis orchestration (built once per lifespan)."""

    def __init__(self, protection: Protection):
        self.protection = protection
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
        data_version = await anyio.to_thread.run_sync(db.read_data_version)
        if data_version == versions.UNLOADED:
            raise DataNotLoadedError("no succeeded ingestion run")
        key = cache_key(job.operation, job.spec_payload, ANALYTICS_VERSION, data_version)
        headers = response.headers
        headers["X-Analysis-Key"] = key
        headers["X-Data-Version"] = data_version
        headers["X-Analytics-Version"] = ANALYTICS_VERSION
        headers["X-Response-Format"] = RESPONSE_FORMAT

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
            payload, _ = await asyncio.shield(flight.future)
            headers["X-Cache"] = "join"
            return payload

        # This request will compute: charge it (nothing above awaited since
        # the flight lookup, so registering the flight below is atomic).
        protection = self.protection
        if protection.daily_cap.exhausted():
            raise DailyCapacityReachedError(
                "daily analysis cap reached",
                retry_after=protection.daily_cap.seconds_until_reset(),
            )
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
            self._lead(flight, key, job, rate_key, cache, db, engine_factory, data_version)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        payload, cache_status = await asyncio.shield(flight.future)
        headers["X-Cache"] = cache_status
        return payload

    async def _lead(
        self, flight: _Flight, key: str, job: AnalysisJob, rate_key: str | None,
        cache: AnalysisCache, db: Database, engine_factory: EngineFactory, data_version: str,
    ) -> None:
        protection = self.protection
        try:
            async with protection.lanes.admit(job.heavy):
                if not protection.daily_cap.try_take():
                    raise DailyCapacityReachedError(
                        "daily analysis cap reached",
                        retry_after=protection.daily_cap.seconds_until_reset(),
                    )
                self.computations += 1
                result = await anyio.to_thread.run_sync(
                    self._compute, job, key, cache, db, engine_factory, data_version
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

    @staticmethod
    def _compute(
        job: AnalysisJob, key: str, cache: AnalysisCache, db: Database,
        engine_factory: EngineFactory, data_version: str,
    ) -> tuple[dict, str]:
        """Runs in a worker thread: one analysis transaction, then the cache.

        Concurrent-reload guard: the data version is re-read in the same
        transaction after computing, and the result is cached only if it is
        unchanged — a result computed against reloaded (or mixed-snapshot)
        data is returned once, uncached, as ``X-Cache: bypass``.
        """
        with db.transaction("analysis") as conn:
            engine = engine_factory(conn)
            try:
                payload = job.compute(engine)
            except InsufficientDataError:
                # An empty database is an operational state, not a client
                # error, and its message must not leak operator hints (F51).
                if not _respondents_loaded(conn):
                    raise DataNotLoadedError("database has no respondents") from None
                raise
            current_version = versions.read_data_version(conn)
        if current_version != data_version:
            log.warning(
                "data version changed during %s computation; result returned uncached",
                job.operation,
            )
            return payload, "bypass"
        try:
            cache.put(key, payload)
        except Exception:
            log.exception("analysis cache put failed; returning uncached result")
        return payload, "miss"

    async def shutdown(self, timeout_s: float = 5.0) -> None:
        """Wait (bounded) for detached computations; the caller cancels their
        queries first so they end promptly."""
        if not self._tasks:
            return
        with anyio.move_on_after(timeout_s):
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
