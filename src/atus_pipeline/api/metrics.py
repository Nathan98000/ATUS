"""Process metrics in the Prometheus text exposition format (ADR-014).

Served by ``GET /metrics`` behind the ops bearer token. The format is
written by hand (about a hundred lines) rather than adding
``prometheus_client`` as a runtime dependency: the API exposes a fixed,
small set of series and needs no registry, push gateway or multiprocess
mode.

Two kinds of series:

* **recorded per request** by the request-context middleware: request
  counts and latency per route template, error codes, analysis cache
  outcomes, database statements per request, client disconnects;
* **read at scrape time** from the objects that already count them: the
  admission lanes, token buckets, daily cap, analysis runner, result cache,
  data snapshot and the psycopg pool.

Every label value comes from a bounded vocabulary (route templates, HTTP
methods and statuses, error codes, cache outcomes); no client input becomes
a label. Counters reset when the process restarts.

**Metrics events.** Render's free plan cannot scrape anything, so the same
numbers also go to the log: every ``METRICS_EVENT_INTERVAL_S`` the process
writes one ``event: "metrics"`` record with the cumulative counters
(:meth:`Metrics.summary`), but only if requests arrived since the previous
one; an idle process writes nothing and touches nothing.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Iterable, Sequence

log = logging.getLogger("atus.api.metrics")

METRICS_EVENT_INTERVAL_S = 300.0

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

LATENCY_BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
                     60.0, 90.0, 120.0)
STATEMENT_BUCKETS = (0, 1, 2, 3, 5, 8, 13, 21)
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
CACHE_OUTCOMES = ("hit", "miss", "bypass", "join")

# psycopg_pool.get_stats() keys exported, and whether each is a gauge or a
# cumulative counter.
_POOL_GAUGES = ("pool_min", "pool_max", "pool_size", "pool_available", "requests_waiting")
_POOL_COUNTERS = ("requests_num", "requests_queued", "requests_wait_ms", "requests_errors",
                  "connections_num", "connections_ms", "connections_errors",
                  "connections_lost", "returns_bad")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(names: Sequence[str], values: Sequence[str]) -> str:
    if not names:
        return ""
    pairs = ",".join(f'{n}="{_escape(str(v))}"' for n, v in zip(names, values, strict=True))
    return "{" + pairs + "}"


def _number(value: float) -> str:
    if value == math.inf:
        return "+Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


class _Family:
    """One metric family with a fixed label set."""

    def __init__(self, name: str, kind: str, help_text: str, labels: Sequence[str] = ()):
        self.name, self.kind, self.help, self.label_names = name, kind, help_text, tuple(labels)

    def header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Family):
    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()):
        super().__init__(name, "counter", help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        key = tuple(label_values)
        self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, *label_values: str) -> float:
        return self._values.get(tuple(label_values), 0.0)

    def render(self) -> list[str]:
        return self.header() + [
            f"{self.name}{_labels(self.label_names, key)} {_number(v)}"
            for key, v in sorted(self._values.items())
        ]


class Histogram(_Family):
    def __init__(
        self, name: str, help_text: str, buckets: Sequence[float], labels: Sequence[str] = ()
    ):
        super().__init__(name, "histogram", help_text, labels)
        self.buckets = tuple(buckets) + (math.inf,)
        self._series: dict[tuple[str, ...], list[float]] = {}  # bucket counts..., sum, count

    def observe(self, value: float, *label_values: str) -> None:
        key = tuple(label_values)
        series = self._series.get(key)
        if series is None:
            series = self._series[key] = [0.0] * (len(self.buckets) + 2)
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                series[index] += 1
        series[-2] += value
        series[-1] += 1

    def count(self, *label_values: str) -> float:
        series = self._series.get(tuple(label_values))
        return series[-1] if series else 0.0

    def render(self) -> list[str]:
        lines = self.header()
        names = self.label_names + ("le",)
        for key, series in sorted(self._series.items()):
            for index, bound in enumerate(self.buckets):
                lines.append(
                    f"{self.name}_bucket{_labels(names, key + (_number(bound),))} "
                    f"{_number(series[index])}"
                )
            lines.append(f"{self.name}_sum{_labels(self.label_names, key)} {_number(series[-2])}")
            lines.append(
                f"{self.name}_count{_labels(self.label_names, key)} {_number(series[-1])}"
            )
        return lines


def _sample(name: str, kind: str, help_text: str, value: float,
            labels: Iterable[tuple[str, str]] = ()) -> list[str]:
    """A single-sample family read at scrape time."""
    pairs = tuple(labels)
    label_text = _labels([n for n, _ in pairs], [v for _, v in pairs])
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}",
            f"{name}{label_text} {_number(value)}"]


class Metrics:
    """The per-process registry (built once in ``create_app``)."""

    def __init__(self, build: str, *, clock=time.monotonic):
        self.build = build
        self._lock = threading.Lock()
        self._clock = clock
        self.started = clock()
        self.total_requests = 0
        self.requests = Counter(
            "atus_http_requests_total", "HTTP requests by route template, method and status.",
            ("route", "method", "status"),
        )
        self.latency = Histogram(
            "atus_http_request_duration_seconds",
            "Time from receiving a request to the end of its response, by route template.",
            LATENCY_BUCKETS_S, ("route",),
        )
        self.errors = Counter(
            "atus_http_errors_total", "Error responses by envelope code.", ("code",),
        )
        self.cache = Counter(
            "atus_analysis_cache_total",
            "Analysis responses by X-Cache outcome (hit, miss, bypass, join).", ("result",),
        )
        self.db_statements = Histogram(
            "atus_db_statements_per_request",
            "Database statements each request sent (connection check and limits included; "
            "BEGIN/COMMIT excluded).",
            STATEMENT_BUCKETS, ("route",),
        )
        # Audit F53: abandoned requests were indistinguishable from answered
        # ones. Only requests with a body are watched (middleware.py), i.e.
        # the analyses; an admitted analysis still completes and is cached.
        self.disconnects = Counter(
            "atus_client_disconnects_total",
            "Requests whose client disconnected before the response started, by route "
            "template (requests with a body are watched: the analyses).", ("route",),
        )

    def observe_request(
        self, *, route: str, method: str, status: int | None, seconds: float,
        db_statements: int, cache: str | None, error: str | None,
        disconnected: bool = False,
    ) -> None:
        method = method if method in _METHODS else "OTHER"
        with self._lock:
            self.total_requests += 1
            self.requests.inc(route, method, str(status if status is not None else 0))
            self.latency.observe(seconds, route)
            self.db_statements.observe(db_statements, route)
            if error:
                self.errors.inc(error)
            if cache in CACHE_OUTCOMES:
                self.cache.inc(cache)
            if disconnected:
                self.disconnects.inc(route)

    def render(self, state) -> str:
        """The exposition text. ``state`` is the application state (any of
        its parts may be missing, e.g. outside the lifespan)."""
        with self._lock:
            lines = [*_sample("atus_build_info", "gauge", "Build of the running API.", 1,
                              [("build", self.build)])]
            for family in (self.requests, self.latency, self.errors, self.cache,
                           self.db_statements, self.disconnects):
                lines += family.render()
        lines += _protection_lines(getattr(state, "protection", None))
        lines += _runner_lines(getattr(state, "runner", None))
        lines += _cache_lines(getattr(state, "cache", None))
        lines += _snapshot_lines(getattr(state, "snapshots", None))
        lines += _database_lines(getattr(state, "database", None))
        return "\n".join(lines) + "\n"


    def summary(self, state) -> dict:
        """The headline numbers as one flat dict (the metrics log event)."""
        with self._lock:
            by_class: dict[str, int] = {}
            for (_, _, status), count in self.requests._values.items():
                key = f"{status[:1]}xx" if status != "0" else "unknown"
                by_class[key] = by_class.get(key, 0) + int(count)
            summary: dict = {
                "event": "metrics",
                "build": self.build,
                "uptime_s": round(self._clock() - self.started),
                "requests": self.total_requests,
                "responses": dict(sorted(by_class.items())),
                "errors": {code: int(n) for (code,), n in sorted(self.errors._values.items())},
                "cache": {r: int(n) for (r,), n in sorted(self.cache._values.items())},
                "client_disconnects": int(sum(self.disconnects._values.values())),
            }
        protection = getattr(state, "protection", None)
        if protection is not None:
            summary.update(
                in_flight=protection.lanes.in_flight,
                lane_waits=protection.lanes.waits,
                lane_sheds=protection.lanes.rejections,
                rate_limited={"request": protection.request_limiter.rejections,
                              "analysis": protection.analysis_limiter.rejections},
                daily_cap_used=protection.daily_cap.used_today(),
                daily_cap_rejections=protection.daily_cap.rejections,
                requests_in_flight=protection.requests.in_flight,
                requests_shed=protection.requests.rejections,
            )
        runner = getattr(state, "runner", None)
        if runner is not None:
            summary.update(computations=runner.computations, joins=runner.joins)
        database = getattr(state, "database", None)
        if database is not None:
            summary["query_timeouts"] = getattr(database, "query_timeouts", 0)
            pool = getattr(database, "pool", None)
            if pool is not None:
                stats = pool.get_stats()
                summary["pool"] = {key: stats.get(key, 0) for key in
                                   ("pool_size", "pool_available", "requests_waiting",
                                    "connections_errors", "connections_lost")}
        snapshots = getattr(state, "snapshots", None)
        if snapshots is not None:
            age = snapshots.age_s()
            summary["snapshot_age_s"] = round(age) if age is not None else None
        return summary


async def emit_metrics_events(metrics: Metrics, state,
                              interval_s: float = METRICS_EVENT_INTERVAL_S) -> None:
    """Log :meth:`Metrics.summary` every ``interval_s`` while requests keep
    arriving (runs for the application's lifetime; cancelled at shutdown)."""
    last = metrics.total_requests
    while True:
        await asyncio.sleep(interval_s)
        current = metrics.total_requests
        if current == last:
            continue
        last = current
        try:
            summary = metrics.summary(state)
            log.info("metrics: %d requests since start", current, extra={"fields": summary})
        except Exception:  # observability must never take the process down
            log.exception("metrics event failed")


def _protection_lines(protection) -> list[str]:
    if protection is None:
        return []
    lanes, cap = protection.lanes, protection.daily_cap
    lines: list[str] = []
    lines += _sample("atus_analyses_in_flight", "gauge",
                     "Uncached analyses computing now.", lanes.in_flight)
    lines += _sample("atus_analyses_heavy_in_flight", "gauge",
                     "Heavy uncached analyses computing now.", lanes.heavy_in_flight)
    lines += _sample("atus_admission_waits_total", "counter",
                     "Analyses that had to wait for an admission lane.", lanes.waits)
    lines += _sample("atus_admission_shed_total", "counter",
                     "Analyses shed with 503 overloaded after the admission wait.",
                     lanes.rejections)
    lines += ["# HELP atus_rate_limit_rejections_total Requests refused by a per-client "
              "token bucket.", "# TYPE atus_rate_limit_rejections_total counter"]
    for bucket, limiter in (("request", protection.request_limiter),
                            ("analysis", protection.analysis_limiter)):
        lines.append(f'atus_rate_limit_rejections_total{{bucket="{bucket}"}} '
                     f"{_number(limiter.rejections)}")
    lines += _sample("atus_daily_cap_rejections_total", "counter",
                     "Analyses refused by the daily cap.", cap.rejections)
    lines += _sample("atus_daily_cap_used", "gauge",
                     "Uncached analyses admitted today (UTC).", cap.used_today())
    lines += _sample("atus_daily_cap_limit", "gauge",
                     "Daily cap on uncached analyses.", cap.limit)
    requests = protection.requests
    lines += _sample("atus_requests_in_flight", "gauge",
                     "Requests the app is serving now (liveness excepted), "
                     "single-flight joiners included.", requests.in_flight)
    lines += _sample("atus_requests_in_flight_limit", "gauge",
                     "Requests the app serves at once before it sheds with 503 overloaded.",
                     requests.limit)
    lines += _sample("atus_requests_shed_total", "counter",
                     "Requests shed with 503 overloaded because the app was serving "
                     "its limit.", requests.rejections)
    return lines


def _runner_lines(runner) -> list[str]:
    if runner is None:
        return []
    return [
        *_sample("atus_analysis_computations_total", "counter",
                 "Analyses computed (admitted to a lane).", runner.computations),
        *_sample("atus_analysis_joins_total", "counter",
                 "Requests that joined an identical computation in progress.", runner.joins),
    ]


def _cache_lines(cache) -> list[str]:
    if cache is None or not hasattr(cache, "__len__"):
        return []
    return _sample("atus_analysis_cache_entries", "gauge",
                   "Results held in the in-process analysis cache.", len(cache))


def _snapshot_lines(snapshots) -> list[str]:
    if snapshots is None:
        return []
    age = snapshots.age_s()
    lines = [
        *_sample("atus_snapshot_refreshes_total", "counter",
                 "Data snapshot reads.", snapshots.refreshes),
        *_sample("atus_snapshot_refresh_failures_total", "counter",
                 "Data snapshot reads that failed.", snapshots.refresh_failures),
        *_sample("atus_snapshot_stale_serves_total", "counter",
                 "Requests served from the previous snapshot after a failed read.",
                 getattr(snapshots, "stale_serves", 0)),
        *_sample("atus_snapshot_pinned", "gauge",
                 "1 while the snapshot is pinned to ATUS_EXPECTED_DATA_VERSION.",
                 int(snapshots.is_pinned()) if hasattr(snapshots, "is_pinned") else 0),
    ]
    if age is not None:
        lines += _sample("atus_snapshot_age_seconds", "gauge",
                         "Age of the data snapshot in memory.", round(age, 3))
    return lines


def _database_lines(database) -> list[str]:
    if database is None:
        return []
    lines = _sample("atus_query_timeouts_total", "counter",
                    "Queries cancelled: statement timeout, request deadline or shutdown.",
                    getattr(database, "query_timeouts", 0))
    pool = getattr(database, "pool", None)
    if pool is None:
        return lines
    stats = pool.get_stats()
    for key in _POOL_GAUGES:
        lines += _sample(f"atus_db_pool_{key}", "gauge",
                         f"psycopg_pool {key} (gauge).", stats.get(key, 0))
    for key in _POOL_COUNTERS:
        lines += _sample(f"atus_db_pool_{key}_total", "counter",
                         f"psycopg_pool {key} since start.", stats.get(key, 0))
    return lines
