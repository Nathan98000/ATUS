"""Process metrics in the Prometheus text exposition format (ADR-014).

Served by ``GET /metrics`` behind the ops bearer token. The format is
written by hand (about a hundred lines) rather than adding
``prometheus_client`` as a runtime dependency: the API exposes a fixed,
small set of series and needs no registry, push gateway or multiprocess
mode.

Two kinds of series:

* **recorded per request** by the request-context middleware: request
  counts and latency per route template, error codes, analysis cache
  outcomes, database statements per request;
* **read at scrape time** from the objects that already count them: the
  admission lanes, token buckets, daily cap, analysis runner, result cache,
  data snapshot and the psycopg pool.

Every label value comes from a bounded vocabulary (route templates, HTTP
methods and statuses, error codes, cache outcomes); no client input becomes
a label. Counters reset when the process restarts.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Sequence

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

    def __init__(self, build: str):
        self.build = build
        self._lock = threading.Lock()
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

    def observe_request(
        self, *, route: str, method: str, status: int | None, seconds: float,
        db_statements: int, cache: str | None, error: str | None,
    ) -> None:
        method = method if method in _METHODS else "OTHER"
        with self._lock:
            self.requests.inc(route, method, str(status if status is not None else 0))
            self.latency.observe(seconds, route)
            self.db_statements.observe(db_statements, route)
            if error:
                self.errors.inc(error)
            if cache in CACHE_OUTCOMES:
                self.cache.inc(cache)

    def render(self, state) -> str:
        """The exposition text. ``state`` is the application state (any of
        its parts may be missing, e.g. outside the lifespan)."""
        with self._lock:
            lines = [*_sample("atus_build_info", "gauge", "Build of the running API.", 1,
                              [("build", self.build)])]
            for family in (self.requests, self.latency, self.errors, self.cache,
                           self.db_statements):
                lines += family.render()
        lines += _protection_lines(getattr(state, "protection", None))
        lines += _runner_lines(getattr(state, "runner", None))
        lines += _cache_lines(getattr(state, "cache", None))
        lines += _snapshot_lines(getattr(state, "snapshots", None))
        lines += _database_lines(getattr(state, "database", None))
        return "\n".join(lines) + "\n"


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
    ]
    if age is not None:
        lines += _sample("atus_snapshot_age_seconds", "gauge",
                         "Age of the data snapshot in memory.", round(age, 3))
    return lines


def _database_lines(database) -> list[str]:
    if database is None:
        return []
    lines = _sample("atus_query_timeouts_total", "counter",
                    "Queries cancelled by the statement timeout or the request deadline.",
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
