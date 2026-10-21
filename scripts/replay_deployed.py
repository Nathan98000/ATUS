#!/usr/bin/env python3
"""URL-targeted, quota-budgeted full-data quality replay (brief §16; deployment.md step 9).

The data-quality suites connect through ``ATUS_DATABASE_URL`` and replay HTTP
in-process, so none of them can target a URL. This script replays them
against a running site through its public API paths, within a hard request
budget:

* the **24 BLS benchmarks** of ``atus_pipeline.analytics.benchmarks`` (23
  distinct specifications; their canonical ``to_dict()`` is a valid request
  body), each checked against the published value with the benchmark's own
  tolerance, as ``tests/data_quality/test_api_benchmarks.py`` does in-process;
* a handful of **HTTP replays**: ``/api/v1/meta``, a full-period trend, a
  by-sex comparison checked against published BLS values, and the **F66 broad
  selector** (every tier-1 code pooled over every multi-year year, replicate
  variance): the costliest specification on the serving DB, which ADR-011
  adds to this replay so the timeouts are sized with it. It covers every
  minute of the diary day, so its estimate must be exactly 1,440 minutes with
  a zero standard error;
* optionally **every response against a local full-database run** under the
  §9 equivalence criterion (``atus_pipeline.serving.equivalence``): record the
  expected responses once with ``--record`` against a local API serving the
  full database, then replay against the deployed site with ``--expected``.

    # 1. locally, against `atus api` on the full database (loopback only):
    python scripts/replay_deployed.py --base-url http://127.0.0.1:8000 \\
        --record expected-0325.json
    # 2. against the public site (at most 40 requests, paced):
    python scripts/replay_deployed.py --base-url https://atus.<sub>.workers.dev \\
        --expected expected-0325.json --report replay.json

Quota safety. The plan is counted before anything is sent and refused when it
exceeds ``--max-requests`` (default and hard maximum 40); a retry is a request
like any other and comes out of the same budget. Analyses are paced
(``--pace``, default 10.5 s to a remote site: the API's per-client bucket
refills one analysis every 10 s, ADR-012) and retried only on transient
answers (waking, overloaded, a short ``Retry-After``). A capacity or quota
refusal (``daily_capacity_reached``, ``capacity_paused``, ``database_paused``,
the per-network daily cap) stops the run: the governor is saying no, and
retrying would only spend Worker requests. The edge result cache makes a
second run of the same requests cost no Render or Neon work (``X-Cache:
edge``). Each run sends 26 analyses (plus any retries) of the per-network 60
daily uncached analyses (ADR-015).

Nothing secret is sent or printed. Exit status: 0 every check passed, 1 a
check failed (or the budget ran out), 2 usage error or refused plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent / "ops"))

from httpcheck import (  # noqa: E402
    BudgetExceeded,
    Client,
    OpsError,
    Reply,
    TransportError,
    is_loopback,
)

from atus_pipeline.analytics import benchmarks as bm  # noqa: E402
from atus_pipeline.analytics.spec import ActivitySelector, AnalysisSpec, Measure  # noqa: E402
from atus_pipeline.serving.equivalence import compare_results  # noqa: E402

HARD_MAX_REQUESTS = 40
EXPECTED_FORMAT = 1
REMOTE_PACE_S = 10.5
ANALYSIS_TIMEOUT_S = 150.0
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT_S = 120.0
#: Answers that say "try again shortly" (ADR-012, ADR-015 error contract).
TRANSIENT = frozenset({"service_waking", "overloaded", "database_unavailable",
                       "upstream_unavailable", "capacity_unavailable", "rate_limited"})
#: Answers that say "not today / not this month": stop spending requests.
REFUSALS = frozenset({"daily_capacity_reached", "capacity_paused", "database_paused"})

# Same extraction semantics as tests/data_quality/test_api_benchmarks.py.
EXTRACTORS: dict[Callable, Callable[[dict], float]] = {
    bm._value: lambda p: p["estimate"]["value"],
    bm._mean_hours: lambda p: p["estimate"]["value"] / 60.0,
    bm._se_hours: lambda p: p["estimate"]["standard_error"] / 60.0,
    bm._participation_pct: lambda p: p["estimate"]["value"] * 100.0,
    bm._sample_participants: lambda p: float(p["n_participants"]),
}

#: BLS Table A-1 2025 household activities (A-1 definition), hours/day by sex
#: (also in tests/data_quality/test_api_benchmarks.py).
COMPARE_PUBLISHED = {"group_a": 1.58, "group_b": 2.38}
COMPARE_TOLERANCE = 0.006

#: Every tier-1 code of the 2003-25 harmonized lexicon (atus.activity_tier1;
#: there is no 17). Selected together and pooled over every multi-year year,
#: this is audit F66's broad selector, the costliest analysis on the serving DB.
F66_TIER1_CODES = ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12",
                   "13", "14", "15", "16", "18", "50")
#: Every minute of the diary day belongs to some tier-1 code (F40/I12), so the
#: broad selector's estimate is exactly this, with a zero standard error.
MINUTES_PER_DAY = 1440.0
EXACT_TOLERANCE = 1e-6


class StopRun(OpsError):
    """The site refused for capacity or quota reasons: stop sending requests."""


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass
class PlannedRequest:
    id: str
    method: str
    path: str
    body: str | None = None
    benchmarks: list[bm.Benchmark] = field(default_factory=list)
    replay: str | None = None  # name of a published-value check


def plan_requests(selected: list[bm.Benchmark], *, replays: bool,
                  health_path: str) -> list[PlannedRequest]:
    """The requests a run sends, in order (one per distinct benchmark spec)."""
    plan = [PlannedRequest("health", "GET", health_path),
            PlannedRequest("meta", "GET", "/api/v1/meta", replay="meta")]
    by_body: dict[str, PlannedRequest] = {}
    for benchmark in selected:
        body = stable_json(benchmark.spec.to_dict())
        if body not in by_body:
            key = hashlib.sha256(body.encode()).hexdigest()[:16]
            by_body[body] = PlannedRequest(f"benchmark:{key}", "POST",
                                           "/api/v1/analysis/estimate", body)
            plan.append(by_body[body])
        by_body[body].benchmarks.append(benchmark)
    if replays:
        plan.append(PlannedRequest("replay:trend_sleep_full_period", "POST",
                                   "/api/v1/analysis/trend", None, replay="trend"))
        plan.append(PlannedRequest(
            "replay:compare_household_by_sex_2025", "POST", "/api/v1/analysis/compare",
            stable_json({"activity": {"preset": "household_activities_bls_table"},
                         "years": [2025], "group_a": {"sex": "male"},
                         "group_b": {"sex": "female"}, "label_a": "Men",
                         "label_b": "Women"}),
            replay="compare"))
        # Last: the costliest request, so a slow or refused answer delays nothing else.
        plan.append(PlannedRequest("replay:f66_broad_selector_pooled", "POST",
                                   "/api/v1/analysis/estimate", None, replay="broad"))
    return plan


def trend_body(years: list[int]) -> str:
    return stable_json({"activity": {"preset": "sleep"}, "years": sorted(years),
                        "variance": "none"})


def broad_selector_body(years: list[int]) -> str:
    """F66: every tier-1 code, every year the multi-year weight covers (not 2020),
    replicate variance; the engine's canonical form, like the benchmarks."""
    spec = AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, ActivitySelector(F66_TIER1_CODES),
                        tuple(year for year in sorted(years) if year != 2020))
    return stable_json(spec.to_dict())


def strip_versions(payload: Any) -> Any:
    """GET bodies differ between databases only in their data-version fields."""
    if isinstance(payload, dict):
        return {k: strip_versions(v) for k, v in payload.items() if "data_version" not in k}
    if isinstance(payload, list):
        return [strip_versions(v) for v in payload]
    return payload


@dataclass
class Outcome:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


class Replayer:
    def __init__(self, client: Client, *, pace_s: float, wake_timeout_s: float,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.client = client
        self.pace_s = pace_s
        self.wake_timeout_s = wake_timeout_s
        self.sleep = sleep if sleep is not None else (lambda seconds: time.sleep(seconds))
        self.outcomes: list[Outcome] = []
        self.responses: dict[str, dict] = {}
        self.meta: dict = {}
        self._analyses_sent = 0

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.outcomes.append(Outcome(name, status, detail))
        print(f"[{status}] {name}" + (f": {detail}" if detail else ""), flush=True)

    # -- sending -------------------------------------------------------------
    def analysis(self, request: PlannedRequest) -> Reply:
        assert request.body is not None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._analyses_sent:
                self.sleep(self.pace_s)
            self._analyses_sent += 1
            try:
                reply = self.client.post_json(request.path, request.body.encode(),
                                              timeout=ANALYSIS_TIMEOUT_S)
            except TransportError:
                if attempt == MAX_ATTEMPTS:
                    raise
                continue
            code = reply.error_code
            if reply.status == 200 or reply.status in (400, 404, 413, 415, 422):
                return reply
            wait = reply.retry_after_s
            if code in REFUSALS or (code == "rate_limited" and (wait or 0) > MAX_RETRY_WAIT_S):
                after = "" if wait is None else f", Retry-After {wait:.0f} s"
                raise StopRun(f"{request.id}: the site refused for capacity "
                              f"({reply.describe()}{after}); stopping")
            if code not in TRANSIENT and reply.status not in (502, 503, 504):
                return reply
            if attempt < MAX_ATTEMPTS:
                self.sleep(min(MAX_RETRY_WAIT_S, max(0.0, (wait or 0.0) - self.pace_s)))
        return reply

    # -- checks --------------------------------------------------------------
    def run(self, plan: list[PlannedRequest], expected: dict | None) -> None:
        stopped: str | None = None
        for request in plan:
            if stopped:
                self.record(request.id, "FAIL", f"not sent: {stopped}")
                continue
            try:
                self.one(request, expected)
            except StopRun as exc:
                stopped = str(exc)
                self.record(request.id, "FAIL", stopped)
            except BudgetExceeded as exc:
                stopped = str(exc)
                self.record(request.id, "FAIL", stopped)
            except OpsError as exc:
                self.record(request.id, "FAIL", str(exc))

    def one(self, request: PlannedRequest, expected: dict | None) -> None:
        if request.id == "health":
            reply = self.client.wait_for(
                "GET", request.path,
                lambda r: r.status == 200 and r.is_json,
                deadline_s=self.wake_timeout_s, min_interval_s=15.0,
                max_attempts=max(1, min(8, self.client.remaining - 1)), timeout=120.0)
            self.record("health", "PASS", f"build {reply.json().get('build')}")
            return
        if request.id == "meta":
            reply = self.client.get(request.path, timeout=60.0)
            if reply.status != 200 or not reply.is_json:
                raise OpsError(f"/api/v1/meta: {reply.describe()}")
            self.meta = reply.json()
            self.meta["_x_data_version"] = reply.header("x-data-version")
            self.responses[request.id] = {"status": 200, "response": reply.json(),
                                          "data_version": reply.header("x-data-version")}
            self.record("meta", "PASS", f"api {self.meta.get('api_version')}, analytics "
                        f"{self.meta.get('analytics_version')}, data "
                        f"{reply.header('x-data-version')}")
            self.compare_expected(request, expected)
            return
        if request.replay in ("trend", "broad"):
            years = (self.meta.get("data") or {}).get("years") or []
            if not years:
                raise OpsError(f"no years from /api/v1/meta for the {request.replay} replay")
            request.body = trend_body(years) if request.replay == "trend" \
                else broad_selector_body(years)
        reply = self.analysis(request)
        if not reply.is_json:
            raise OpsError(f"{request.id}: {reply.describe()} (not JSON)")
        self.responses[request.id] = {"status": reply.status, "response": reply.json(),
                                      "data_version": reply.header("x-data-version"),
                                      "x_cache": reply.header("x-cache"),
                                      "server_timing": reply.header("server-timing"),
                                      "elapsed_s": round(reply.elapsed_s, 3),
                                      "body": request.body}
        if reply.status != 200:
            raise OpsError(f"{request.id}: {reply.describe()}")
        payload = reply.json()
        for benchmark in request.benchmarks:
            observed = EXTRACTORS[benchmark.extract](payload)
            delta = abs(observed - benchmark.expected)
            self.record(f"BLS {benchmark.name}", "PASS" if delta <= benchmark.tolerance
                        else "FAIL", f"{observed:,.4f} vs published {benchmark.expected:,.4f} "
                        f"(|delta| {delta:.5f}, tolerance {benchmark.tolerance:g}; "
                        f"X-Cache {reply.header('x-cache')})")
        if request.replay == "trend":
            self.check_trend(payload)
        elif request.replay == "broad":
            self.check_broad(payload)
        elif request.replay == "compare":
            for group, published in COMPARE_PUBLISHED.items():
                hours = payload[group]["estimate"]["value"] / 60.0
                ok = abs(hours - published) <= COMPARE_TOLERANCE
                self.record(f"BLS A1-2025 household activities, {payload['label_' + group[-1]]}",
                            "PASS" if ok else "FAIL",
                            f"{hours:.4f} h vs published {published}")
        self.compare_expected(request, expected)

    def check_trend(self, payload: dict) -> None:
        points = {p["year"]: p for p in payload.get("points", [])}
        years = (self.meta.get("data") or {}).get("years") or []
        bad = [y for y, p in points.items()
               if y != 2020 and not (p.get("estimate") and 480 <= p["estimate"]["value"] <= 600)]
        if len(points) != len(years) or bad or (2020 in points and points[2020]["estimate"]):
            self.record("replay sleep trend", "FAIL", f"{len(points)} points for {len(years)} "
                        f"years; implausible years {bad}")
        else:
            self.record("replay sleep trend", "PASS", f"{len(points)} years, 2020 unavailable "
                        "under multiyear weights, every other year 8-10 h")

    def check_broad(self, payload: dict) -> None:
        estimate = payload.get("estimate") or {}
        value, se = estimate.get("value"), estimate.get("standard_error")
        exact = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    for v in (value, se)) \
            and abs(value - MINUTES_PER_DAY) <= EXACT_TOLERANCE and abs(se) <= EXACT_TOLERANCE
        name = "replay F66 broad selector (every tier-1 code, pooled)"
        if exact:
            self.record(name, "PASS", f"{value:,.6f} min/day, SE {se:.2g}: the whole diary day")
        else:
            self.record(name, "FAIL", f"estimate {value!r} (SE {se!r}), expected exactly "
                                      f"{MINUTES_PER_DAY:,.0f} with a zero SE: a tier-1 code "
                                      "is missing from F66_TIER1_CODES, or the result is wrong")

    def compare_expected(self, request: PlannedRequest, expected: dict | None) -> None:
        if expected is None:
            return
        name = f"§9 {request.id}"
        recorded = (expected.get("responses") or {}).get(request.id)
        if recorded is None:
            self.record(name, "SKIP", "not in the expected file")
            return
        live = self.responses[request.id]
        if recorded.get("body") is not None and recorded["body"] != request.body:
            self.record(name, "FAIL", "the expected file was recorded with another request "
                                      "body (different years or benchmark set?)")
            return
        if recorded["status"] != live["status"]:
            self.record(name, "FAIL", f"status {live['status']}, expected {recorded['status']}")
            return
        if request.id == "meta":
            ok = strip_versions(recorded["response"]) == strip_versions(live["response"])
            self.record(name, "PASS" if ok else "FAIL",
                        "identical apart from the data version" if ok else "metadata differs")
            return
        result = compare_results(recorded["response"], live["response"])
        if result.equivalent:
            self.record(name, "PASS", f"{result.compared} values; largest scaled SE/CI "
                                      f"difference {result.max_scaled:.3g}")
        else:
            self.record(name, "FAIL", "; ".join(result.mismatches[:3]))


def select_benchmarks(names: list[str] | None) -> list[bm.Benchmark]:
    all_benchmarks = list(bm.build_benchmarks())
    if not names:
        return all_benchmarks
    by_name = {b.name: b for b in all_benchmarks}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise OpsError(f"unknown benchmark(s): {unknown}")
    return [by_name[n] for n in names]


def load_expected(path: Path) -> dict:
    try:
        expected = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OpsError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    if expected.get("format") != EXPECTED_FORMAT or not isinstance(expected.get("responses"),
                                                                   dict):
        raise OpsError(f"{path.name} is not a format-{EXPECTED_FORMAT} expected-responses file")
    return expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True,
                        help="the public origin, or a local API for --record")
    parser.add_argument("--expected", type=Path, help="responses recorded with --record")
    parser.add_argument("--record", type=Path, help="write the responses here (loopback only)")
    parser.add_argument("--max-requests", type=int, default=HARD_MAX_REQUESTS)
    parser.add_argument("--pace", type=float, default=None,
                        help=f"seconds between analyses (default {REMOTE_PACE_S} remote, "
                             "0 to loopback)")
    parser.add_argument("--wake-timeout", type=float, default=240.0)
    parser.add_argument("--health-path", default=None,
                        help="default /api/health (through the Worker); /health to loopback")
    parser.add_argument("--benchmark", action="append", default=None, metavar="NAME",
                        help="only these benchmarks (repeatable; default all 24)")
    parser.add_argument("--no-replays", action="store_true", help="benchmarks only")
    parser.add_argument("--report", type=Path, help="write every outcome as JSON")
    args = parser.parse_args(argv)

    def refuse(message: str) -> int:
        print(f"error: {message}", file=sys.stderr)
        return 2

    if not 1 <= args.max_requests <= HARD_MAX_REQUESTS:
        return refuse(f"--max-requests must be 1-{HARD_MAX_REQUESTS} (quota budget)")
    if args.record and args.expected:
        return refuse("--record and --expected are exclusive")
    try:
        host = urlsplit(args.base_url).hostname or ""
    except ValueError:
        return refuse("--base-url is not a URL")
    loopback = is_loopback(host)
    if args.record and not loopback:
        return refuse("--record runs only against a local API (the full database's answers)")
    pace = args.pace if args.pace is not None else (0.0 if loopback else REMOTE_PACE_S)
    if not 0 <= pace <= 120:
        return refuse("--pace must be 0-120 s")
    health_path = args.health_path or ("/health" if loopback else "/api/health")
    try:
        selected = select_benchmarks(args.benchmark)
        expected = load_expected(args.expected) if args.expected else None
        client = Client(args.base_url, label="site", max_requests=args.max_requests,
                        timeout=60.0)
    except OpsError as exc:
        return refuse(str(exc))
    plan = plan_requests(selected, replays=not args.no_replays, health_path=health_path)
    if len(plan) > args.max_requests:
        return refuse(f"the plan needs {len(plan)} requests, over the budget of "
                      f"{args.max_requests}; select fewer with --benchmark/--no-replays")
    print(f"plan: {len(plan)} requests ({len(selected)} benchmarks), budget "
          f"{args.max_requests} (spare {args.max_requests - len(plan)} for retries), "
          f"pace {pace:g} s", flush=True)

    replayer = Replayer(client, pace_s=pace, wake_timeout_s=args.wake_timeout)
    replayer.run(plan, expected)
    if expected is not None and replayer.meta:
        recorded_av = expected.get("analytics_version")
        if recorded_av != replayer.meta.get("analytics_version"):
            replayer.record("analytics version", "FAIL", f"expected file recorded with "
                            f"{recorded_av}, site runs {replayer.meta.get('analytics_version')}"
                            ": the §9 comparison needs the same engine")
    failed = sum(1 for o in replayer.outcomes if o.status == "FAIL")
    print(f"replay: {failed} failed, {client.sent} of {args.max_requests} requests sent",
          flush=True)

    if args.record:
        args.record.write_text(json.dumps({
            "format": EXPECTED_FORMAT,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "analytics_version": replayer.meta.get("analytics_version"),
            "api_version": replayer.meta.get("api_version"),
            "data_version": replayer.meta.get("_x_data_version"),
            "responses": {k: {key: v[key] for key in ("status", "response", "body")
                              if key in v}
                          for k, v in replayer.responses.items()},
        }, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"recorded {len(replayer.responses)} responses to {args.record}")
    if args.report:
        args.report.write_text(json.dumps({
            "requests_sent": client.sent, "max_requests": args.max_requests,
            "data_version": replayer.meta.get("_x_data_version"),
            "analytics_version": replayer.meta.get("analytics_version"),
            "outcomes": [o.__dict__ for o in replayer.outcomes],
            "responses": {k: {key: v.get(key) for key in ("status", "x_cache", "server_timing",
                                                          "elapsed_s", "data_version")}
                          for k, v in replayer.responses.items()},
        }, indent=1) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
