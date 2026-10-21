"""Load and quota protection inside the API process (ADR-012).

The edge (Cloudflare Worker + quota governor) is the primary defense; these
are the second, independent layer that also covers direct hits on the
provider hostname and edge misconfiguration. Everything here is in memory and
per process, so it resets when the process restarts — which is why the
durable daily budget lives at the edge.

* :class:`TokenBucketLimiter` — per-client-IP token buckets with bounded
  memory (an LRU of at most ``max_keys`` clients).
* :class:`DailyCap` — a process-wide count of uncached analyses per UTC day.
* :class:`AdmissionLanes` — at most N uncached analyses computing at once, of
  which at most M heavy; a request waits a bounded time for a lane.
* :class:`RequestSlots` — at most :func:`max_inflight_requests` requests
  (liveness excepted) in the app at once, single-flight joiners included.
* :func:`is_heavy` — the deterministic cost classifier the lanes use.
* :func:`client_identity` — which client a request is charged to.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio

from ..config import RateLimit, Settings
from .errors import DailyCapacityReachedError, OverloadedError

# --------------------------------------------------------------------------- #
# cost classification                                                          #
# --------------------------------------------------------------------------- #

HEAVY_YEAR_QUERIES = 8
"""Year-statistics an analysis must aggregate before it counts as heavy.

An analysis's database cost is dominated by its statistics queries, which
scale with the number of survey years scanned. Calibrated on the emulation of
Neon's 0.25 CU floor, one analysis at a time (ADR-012 "Calibration (§13)",
2026-10-21): a single-year estimate with replicate SEs took 0.99 s at p50
(4.60 s at most), pooled 8-22-year estimates 5.79-8.39 s, and pooled 22-year
estimates and 23-year trends 21.35-21.81 s at p50. A compare runs one query
per group, so it scans its years twice. The classifier is therefore

    year_queries = len(years) x (2 if compare else 1)
    heavy        = year_queries >= 8

No measured request was misclassified, but the margin at the boundary is
narrow at 0.25 CU (the costliest light request, 4.60 s, against the cheapest
heavy one, 5.33-5.79 s), and requests of 2-7 year queries were not measured.
It ignores ``variance``: point-only (``variance: none``) requests were not
measured separately, so they are classified as conservatively as replicate
ones. It is a pure function of the validated spec — the same request is
always in the same lane.
"""


def is_heavy(operation: str, years: Sequence[int]) -> bool:
    year_queries = len(years) * (2 if operation == "compare" else 1)
    return year_queries >= HEAVY_YEAR_QUERIES


# --------------------------------------------------------------------------- #
# client identity                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClientIdentity:
    ip: str | None          # the client address as trusted (None if unknown)
    rate_key: str | None    # bucket key; None = not per-client limited


EDGE_UNKNOWN_CLIENT = "edge:unknown"
"""The one bucket of edge-authenticated requests that carry no usable
``X-ATUS-Client-IP``."""


def _ip_key(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """IPv4 addresses are keyed whole; IPv6 by /64, the smallest prefix one
    subscriber normally controls (keying a full v6 address would let one host
    rotate through 2^64 buckets)."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


def _parse_ip(text: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not text:
        return None
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        return None


def client_identity(
    *, peer: str | None, edge_client_ip: str | None, authenticated: bool,
    edge_auth_enabled: bool,
) -> ClientIdentity:
    """Who a request is charged to.

    * Edge-authenticated requests are charged to ``X-ATUS-Client-IP`` (the
      Worker copies it from Cloudflare's ``CF-Connecting-IP``). If it is
      missing or malformed (the Worker omits it when Cloudflare gave no
      usable address), they share one bucket, :data:`EDGE_UNKNOWN_CLIENT`,
      as they share the governor's ``unknown`` prefix at the edge. Never the
      socket peer: behind the edge that is the platform's proxy, so its
      address would be logged as the client's and its buckets would mix
      unrelated visitors by proxy node.
    * Otherwise the socket peer. ``X-Forwarded-For``, ``Forwarded``,
      ``CF-Connecting-IP`` and ``True-Client-IP`` are never consulted: any
      client can set them (and the edge -> provider hop appends to XFF).
    * Development convenience: with edge auth disabled (never in
      production, which refuses to start without it), loopback peers and
      peers that are not IP addresses (in-process test clients) are not
      per-client rate limited. The daily cap and the lanes still apply.
    """
    if authenticated:
        trusted = _parse_ip(edge_client_ip)
        if trusted is not None:
            return ClientIdentity(ip=str(trusted), rate_key=_ip_key(trusted))
        return ClientIdentity(ip=None, rate_key=EDGE_UNKNOWN_CLIENT)
    address = _parse_ip(peer)
    if address is None:
        exempt = not edge_auth_enabled
        return ClientIdentity(ip=None, rate_key=None if exempt else f"peer:{peer}")
    if not edge_auth_enabled and address.is_loopback:
        return ClientIdentity(ip=str(address), rate_key=None)
    return ClientIdentity(ip=str(address), rate_key=_ip_key(address))


# --------------------------------------------------------------------------- #
# token buckets                                                                #
# --------------------------------------------------------------------------- #


class TokenBucketLimiter:
    """Per-key token buckets: ``rate.limit`` tokens of burst, refilled at
    ``rate.limit / rate.period_s`` per second.

    Memory is bounded by an LRU of ``max_keys`` buckets. Evicting a bucket
    forgets its debt, so a client whose key was pushed out by 10,000 newer
    ones starts again with a full bucket — the price of a fixed memory bound,
    and why the edge governor stays the primary limit.
    """

    def __init__(
        self, rate: RateLimit, *, max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.rate = rate
        self._capacity = float(rate.limit)
        self._refill = rate.refill_per_second
        self._max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self.rejections = 0

    def acquire(self, key: str) -> float | None:
        """Take one token. Returns ``None`` when allowed, otherwise the
        seconds until a token will be available."""
        now = self._clock()
        with self._lock:
            tokens, stamp = self._buckets.pop(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - stamp) * self._refill)
            if tokens >= 1.0:
                tokens -= 1.0
                wait = None
            else:
                wait = (1.0 - tokens) / self._refill
                self.rejections += 1
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
        return wait

    def refund(self, key: str) -> None:
        """Give back a token taken for work that was then refused."""
        with self._lock:
            if key in self._buckets:
                tokens, stamp = self._buckets[key]
                self._buckets[key] = (min(self._capacity, tokens + 1.0), stamp)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)


# --------------------------------------------------------------------------- #
# daily cap                                                                    #
# --------------------------------------------------------------------------- #


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DailyCap:
    """At most ``limit`` uncached analyses per UTC day in this process.

    A backstop for the edge governor (which persists across restarts); it
    resets at 00:00 UTC, matching Cloudflare's daily quotas. Only analyses
    that reached their statistics queries keep their unit (:meth:`refund`).
    """

    def __init__(self, limit: int, *, clock: Callable[[], datetime] = _utc_now):
        self.limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._day = clock().date()
        self.used = 0
        self.rejections = 0  # counted by the caller via refuse()

    def _roll(self, now: datetime) -> None:
        if now.date() != self._day:
            self._day, self.used = now.date(), 0

    def exhausted(self) -> bool:
        with self._lock:
            self._roll(self._clock())
            return self.used >= self.limit

    def try_take(self) -> bool:
        with self._lock:
            self._roll(self._clock())
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

    def refund(self) -> None:
        """Give back a unit taken for a computation that never reached its
        statistics queries (``AnalysisRunner._lead``). Never below zero: a
        unit taken just before 00:00 UTC and given back after it comes off
        the new day's count (at most the few analyses failing at midnight)."""
        with self._lock:
            self._roll(self._clock())
            if self.used > 0:
                self.used -= 1

    def used_today(self) -> int:
        with self._lock:
            self._roll(self._clock())
            return self.used

    def refuse(self) -> DailyCapacityReachedError:
        """Count a refusal and build its error (``Retry-After`` = the time
        until 00:00 UTC)."""
        with self._lock:
            self.rejections += 1
        return DailyCapacityReachedError(
            "daily analysis cap reached", retry_after=self.seconds_until_reset()
        )

    def seconds_until_reset(self) -> int:
        now = self._clock()
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
        return max(1, math.ceil((midnight - now).total_seconds()))


# --------------------------------------------------------------------------- #
# admission lanes                                                              #
# --------------------------------------------------------------------------- #


class AdmissionLanes:
    """Bounds concurrent uncached analyses (ADR-012).

    Every computation needs a general lane; a heavy one also needs a heavy
    lane (taken first, so a light request never holds the heavy lane and the
    two cannot deadlock). A request waits at most ``wait_s`` in total, then
    gets 503 ``overloaded``. Lanes are taken before any pool connection is
    checked out, so the pool (sized above the lane count, which
    ``config.load_settings`` enforces) always has connections left for
    metadata and cache lookups.

    :meth:`close` (shutdown) admits nothing more: a request still waiting
    for a lane, or arriving later, gets 503 ``overloaded`` instead of taking
    a lane that a cancelled query frees.

    Create inside the running event loop (the application lifespan).
    """

    def __init__(self, max_inflight: int, max_heavy: int, wait_s: float):
        self._general = anyio.Semaphore(max_inflight)
        self._heavy = anyio.Semaphore(max_heavy)
        self.max_inflight = max_inflight
        self.max_heavy = max_heavy
        self.wait_s = wait_s
        self.in_flight = 0
        self.heavy_in_flight = 0
        self.waits = 0        # admissions that found no free lane and waited
        self.rejections = 0   # ... and were shed after the wait
        self.closed = False

    def close(self) -> None:
        """Admit nothing from now on (process shutdown)."""
        self.closed = True

    @asynccontextmanager
    async def admit(self, heavy: bool) -> AsyncIterator[None]:
        acquired: list[anyio.Semaphore] = []
        deadline = anyio.current_time() + self.wait_s
        waited = False
        try:
            for semaphore in ((self._heavy, self._general) if heavy else (self._general,)):
                if self.closed:
                    raise OverloadedError("server shutting down")
                if not waited and semaphore.value == 0:
                    waited = True
                    self.waits += 1
                if not await self._acquire_by(semaphore, deadline):
                    self.rejections += 1
                    raise OverloadedError(
                        f"no {'heavy ' if heavy else ''}analysis lane within {self.wait_s:g} s"
                    )
                acquired.append(semaphore)
            if self.closed:  # closed while this request waited for a lane
                raise OverloadedError("server shutting down")
        except BaseException:
            for semaphore in acquired:
                semaphore.release()
            raise
        self.in_flight += 1
        self.heavy_in_flight += heavy
        try:
            yield
        finally:
            self.in_flight -= 1
            self.heavy_in_flight -= heavy
            for semaphore in acquired:
                semaphore.release()

    @staticmethod
    async def _acquire_by(semaphore: anyio.Semaphore, deadline: float) -> bool:
        """Take ``semaphore`` now if free, else wait until ``deadline``."""
        try:
            semaphore.acquire_nowait()
            return True
        except anyio.WouldBlock:
            pass
        with anyio.move_on_after(max(0.0, deadline - anyio.current_time())):
            await semaphore.acquire()
            return True
        return False


# --------------------------------------------------------------------------- #
# requests in flight                                                           #
# --------------------------------------------------------------------------- #


def max_inflight_requests(settings: Settings) -> int:
    """Requests the app serves at once, liveness (``/health``) excepted.

    Four per worker thread, as uvicorn's connection bound was before review
    round 3: room for queued cheap requests and for many requests waiting on
    one analysis. uvicorn's own bound (``serve.limit_concurrency``) lies well
    above it, so ordinary traffic can never fill it and the platform's health
    check always reaches the app (review r3, health-503-under-joiners)."""
    return max(32, settings.api_threads * 4)


class RequestSlots:
    """At most ``limit`` requests in the app at once; the next one gets 503
    ``overloaded`` before routing (``GuardMiddleware``).

    Single-flight joiners take no lane and no analysis token, and wait as long
    as the computation they joined (up to the 90 s deadline), so without this
    bound enough of them filled uvicorn's ``limit_concurrency`` and its own
    503 reached ``/health`` too (review r3). Used on the event loop only, so it
    needs no lock.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.in_flight = 0
        self.rejections = 0

    def try_enter(self) -> bool:
        if self.in_flight >= self.limit:
            self.rejections += 1
            return False
        self.in_flight += 1
        return True

    def leave(self) -> None:
        self.in_flight -= 1


@dataclass
class Protection:
    """The per-process protection state, built once per application lifespan."""

    request_limiter: TokenBucketLimiter
    analysis_limiter: TokenBucketLimiter
    daily_cap: DailyCap
    lanes: AdmissionLanes
    requests: RequestSlots

    @classmethod
    def from_settings(cls, settings: Settings) -> Protection:
        return cls(
            request_limiter=TokenBucketLimiter(settings.api_rate_requests),
            analysis_limiter=TokenBucketLimiter(settings.api_rate_analysis),
            daily_cap=DailyCap(settings.api_daily_analysis_cap),
            lanes=AdmissionLanes(
                settings.api_max_inflight, settings.api_max_inflight_heavy,
                settings.api_admission_wait_s,
            ),
            requests=RequestSlots(max_inflight_requests(settings)),
        )
