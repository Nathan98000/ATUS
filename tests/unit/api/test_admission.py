"""Analysis admission: the per-client analysis bucket (charged only for
computations), the daily cap, concurrency lanes, single-flight joins, the
data-not-loaded state, and the mapping of database failures to error codes.

Blocking stub engines run in the API's worker threads; the test drives
concurrent requests from its own threads through one TestClient."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from atus_pipeline.analytics.errors import InsufficientDataError
from atus_pipeline.config import RateLimit
from atus_pipeline.database import versions
from tests.unit.api.conftest import FakeDatabase, FakeVersionConnection, make_app, make_settings
from tests.unit.api.test_contract import known_estimate_result

SECRET = "s" * 40
ESTIMATE = "/api/v1/analysis/estimate"
REMOTE = ("203.0.113.5", 40000)


def body(year: int = 2023, n_years: int = 1, **extra) -> dict:
    return {
        "activity": {"preset": "sleep"},
        "years": list(range(year, year + n_years)),
        "variance": "none",
        **extra,
    }


def as_client(ip: str) -> dict[str, str]:
    return {"X-ATUS-Edge-Auth": SECRET, "X-ATUS-Client-IP": ip}


class Gate:
    """Lets a test hold a stub computation open and know when it started."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def behavior(self, spec):
        with self._lock:
            self.calls += 1
        self.entered.set()
        assert self.release.wait(10), "test never released the computation"
        return known_estimate_result()


@pytest.fixture
def build(stub_engine, tmp_path):
    clients = []

    def _build(client_kwargs=None, fake_db=None, **overrides):
        fake_db = fake_db or FakeDatabase()
        if stub_engine.estimate_behavior is None:
            stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        app = make_app(make_settings(tmp_path, **overrides), stub_engine, fake_db)
        client = TestClient(app, raise_server_exceptions=False, **(client_kwargs or {}))
        client.__enter__()
        clients.append(client)
        return client, app

    yield _build
    for client in clients:
        client.__exit__(None, None, None)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


class TestAnalysisRateLimit:
    def test_only_computations_spend_analysis_tokens(self, build):
        client, _ = build(client_kwargs={"client": REMOTE}, api_rate_analysis=RateLimit(2, 60.0))
        assert client.post(ESTIMATE, json=body(2021)).headers["X-Cache"] == "miss"
        assert client.post(ESTIMATE, json=body(2022)).headers["X-Cache"] == "miss"
        limited = client.post(ESTIMATE, json=body(2023))
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"
        assert limited.json()["error"]["details"]["scope"] == "client"
        assert int(limited.headers["Retry-After"]) >= 1
        # cached results stay available to the limited client
        hit = client.post(ESTIMATE, json=body(2021))
        assert hit.status_code == 200 and hit.headers["X-Cache"] == "hit"

    def test_rate_limited_errors_are_not_cached(self, build):
        client, _ = build(client_kwargs={"client": REMOTE}, api_rate_analysis=RateLimit(1, 60.0))
        client.post(ESTIMATE, json=body(2021))
        assert client.post(ESTIMATE, json=body(2022)).status_code == 429
        assert client.post(ESTIMATE, json=body(2022)).status_code == 429


class TestDailyCap:
    def test_503_until_midnight_utc_and_hits_still_served(self, build):
        client, _ = build(api_daily_analysis_cap=2)
        client.post(ESTIMATE, json=body(2021))
        client.post(ESTIMATE, json=body(2022))
        refused = client.post(ESTIMATE, json=body(2023))
        assert refused.status_code == 503
        error = refused.json()["error"]
        assert error["code"] == "daily_capacity_reached"
        assert error["details"]["scope"] == "daily"
        assert 1 <= int(refused.headers["Retry-After"]) <= 86_400
        assert error["details"]["retry_after_seconds"] == int(refused.headers["Retry-After"])
        assert client.post(ESTIMATE, json=body(2021)).headers["X-Cache"] == "hit"


class TestLanes:
    def test_overloaded_when_no_lane_frees_in_time(self, build, stub_engine):
        gate = Gate()
        stub_engine.estimate_behavior = gate.behavior
        client, app = build(api_max_inflight=1, api_max_inflight_heavy=1,
                            api_admission_wait_s=0.2)
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(client.post, ESTIMATE, json=body(2021))
            assert gate.entered.wait(5)
            second = client.post(ESTIMATE, json=body(2022))
            assert second.status_code == 503
            assert second.json()["error"]["code"] == "overloaded"
            assert second.headers["Retry-After"] == "10"
            gate.release.set()
            assert first.result(10).status_code == 200
        assert app.state.protection.lanes.rejections == 1

    def test_heavy_lane_does_not_block_light_requests(self, build, stub_engine):
        gate = Gate()
        light_calls = []

        def behavior(spec):
            if len(spec.years) >= 8:
                return gate.behavior(spec)
            light_calls.append(spec)
            return known_estimate_result()

        stub_engine.estimate_behavior = behavior
        client, _ = build(api_max_inflight=2, api_max_inflight_heavy=1,
                          api_admission_wait_s=0.2)
        with ThreadPoolExecutor(1) as pool:
            heavy = pool.submit(client.post, ESTIMATE, json=body(2003, n_years=10))
            assert gate.entered.wait(5)
            other_heavy = client.post(ESTIMATE, json=body(2004, n_years=10))
            assert other_heavy.status_code == 503
            assert other_heavy.json()["error"]["code"] == "overloaded"
            light = client.post(ESTIMATE, json=body(2023))
            assert light.status_code == 200 and len(light_calls) == 1
            gate.release.set()
            assert heavy.result(10).status_code == 200

    def test_refused_admission_refunds_the_analysis_token(self, build, stub_engine):
        gate = Gate()
        stub_engine.estimate_behavior = gate.behavior
        client, _ = build(edge_secrets=(SECRET,), api_max_inflight=1,
                          api_max_inflight_heavy=1, api_admission_wait_s=0.1,
                          api_rate_analysis=RateLimit(1, 3600.0))
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(
                client.post, ESTIMATE, json=body(2021), headers=as_client("198.51.100.1")
            )
            assert gate.entered.wait(5)
            shed = client.post(ESTIMATE, json=body(2022), headers=as_client("198.51.100.2"))
            assert shed.json()["error"]["code"] == "overloaded"
            gate.release.set()
            assert first.result(10).status_code == 200
        retry = client.post(ESTIMATE, json=body(2022), headers=as_client("198.51.100.2"))
        assert retry.status_code == 200  # the shed attempt did not spend the only token


class TestSingleFlight:
    def test_identical_concurrent_requests_share_one_computation(self, build, stub_engine):
        gate = Gate()
        stub_engine.estimate_behavior = gate.behavior
        client, app = build(api_max_inflight=1, api_max_inflight_heavy=1)
        runner = app.state.runner
        with ThreadPoolExecutor(2) as pool:
            leader = pool.submit(client.post, ESTIMATE, json=body(2021))
            assert gate.entered.wait(5)
            # a different spelling of the same canonical spec
            joiner = pool.submit(client.post, ESTIMATE, json=body(2021, weights="multiyear"))
            wait_for(lambda: runner.joins == 1)
            gate.release.set()
            first, second = leader.result(10), joiner.result(10)
        assert gate.calls == 1 and runner.computations == 1
        assert first.status_code == second.status_code == 200
        assert {first.headers["X-Cache"], second.headers["X-Cache"]} == {"miss", "join"}
        assert first.json() == second.json()
        assert first.headers["X-Analysis-Key"] == second.headers["X-Analysis-Key"]
        assert client.post(ESTIMATE, json=body(2021)).headers["X-Cache"] == "hit"

    def test_joiners_bypass_lanes_and_tokens(self, build, stub_engine):
        gate = Gate()
        stub_engine.estimate_behavior = gate.behavior
        client, app = build(edge_secrets=(SECRET,), api_max_inflight=1,
                            api_max_inflight_heavy=1, api_admission_wait_s=0.05,
                            api_rate_analysis=RateLimit(1, 3600.0))
        with ThreadPoolExecutor(2) as pool:
            leader = pool.submit(
                client.post, ESTIMATE, json=body(2021), headers=as_client("198.51.100.1")
            )
            assert gate.entered.wait(5)
            # lanes are full and this client's token would be spent: it still joins
            joiner = pool.submit(
                client.post, ESTIMATE, json=body(2021), headers=as_client("198.51.100.1")
            )
            wait_for(lambda: app.state.runner.joins == 1)
            gate.release.set()
            assert leader.result(10).status_code == 200
            assert joiner.result(10).headers["X-Cache"] == "join"

    def test_joiners_receive_the_same_error_and_nothing_is_cached(self, build, stub_engine):
        gate = Gate()

        def failing(spec):
            gate.behavior(spec)
            raise InsufficientDataError("no respondents match")

        stub_engine.estimate_behavior = failing
        client, app = build()
        with ThreadPoolExecutor(2) as pool:
            leader = pool.submit(client.post, ESTIMATE, json=body(2021))
            assert gate.entered.wait(5)
            joiner = pool.submit(client.post, ESTIMATE, json=body(2021))
            wait_for(lambda: app.state.runner.joins == 1)
            gate.release.set()
            assert leader.result(10).json()["error"]["code"] == "insufficient_data"
            assert joiner.result(10).json()["error"]["code"] == "insufficient_data"
        stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        assert client.post(ESTIMATE, json=body(2021)).headers["X-Cache"] == "miss"


class TestConnectionLifetime:
    def test_transaction_ends_before_the_response_starts(self, stub_engine, tmp_path):
        """The pool connection goes back before any response byte is sent,
        so a slow-reading client cannot hold it (audit I10)."""
        from contextlib import contextmanager

        events: list[str] = []

        class RecordingDatabase(FakeDatabase):
            @contextmanager
            def transaction(self, kind):
                with super().transaction(kind) as conn:
                    yield conn
                events.append(f"end:{kind}")

        stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        app = make_app(make_settings(tmp_path), stub_engine, RecordingDatabase())

        async def recording_app(scope, receive, send):
            async def send_and_record(message):
                if message["type"] == "http.response.start":
                    events.append("response")
                await send(message)

            await app(scope, receive, send_and_record)

        with TestClient(recording_app) as client:
            assert client.post(ESTIMATE, json=body()).status_code == 200
        assert events == ["end:metadata", "end:analysis", "response"]


class TestDataNotLoaded:
    def test_unloaded_database_is_503_without_operator_hints(self, build):
        client, _ = build(fake_db=FakeDatabase(data_version=versions.UNLOADED))
        response = client.post(ESTIMATE, json=body())
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "data_not_loaded"
        assert "atus" not in error["message"].lower()
        assert response.headers["Retry-After"]

    def test_empty_respondents_turn_the_engine_hint_into_503(self, build, stub_engine):
        class EmptyConnection(FakeVersionConnection):
            def execute(self, sql, params=None):
                if "EXISTS (SELECT 1 FROM atus.respondents)" in sql:
                    class _Empty:
                        @staticmethod
                        def fetchone():
                            return (False,)
                    return _Empty()
                return super().execute(sql, params)

        def no_data(spec):
            raise InsufficientDataError("The database contains no respondents — run `atus load`")

        stub_engine.estimate_behavior = no_data
        client, _ = build(fake_db=FakeDatabase(EmptyConnection))
        response = client.post(ESTIMATE, json=body())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "data_not_loaded"
        assert "atus load" not in response.text


class TestDatabaseErrorMapping:
    @pytest.mark.parametrize(
        ("exc", "status", "code", "retry_after"),
        [
            (psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
             503, "analysis_timeout", "60"),
            (psycopg.errors.DiskFull("could not resize shared memory segment"),
             503, "overloaded", "10"),
            (psycopg.errors.DeadlockDetected("deadlock detected"), 503, "overloaded", "10"),
            (psycopg.errors.LockNotAvailable("canceling statement due to lock timeout"),
             503, "overloaded", "10"),
            (psycopg.errors.TooManyConnections("too many clients"), 503, "overloaded", "10"),
            (PoolTimeout("couldn't get a connection"), 503, "overloaded", "10"),
            (psycopg.OperationalError(
                "connection failed: ERROR:  Your account or project has exceeded the compute "
                "time quota. Upgrade your plan to increase limits."),
             503, "database_paused", "3600"),
            (psycopg.OperationalError("ERROR: Your project has exceeded the data transfer quota."),
             503, "database_paused", "3600"),
            (psycopg.errors.AdminShutdown("terminating connection due to administrator command"),
             503, "database_unavailable", "5"),
            (psycopg.OperationalError("server closed the connection unexpectedly"),
             503, "database_unavailable", "5"),
            (psycopg.InterfaceError("the connection is closed"),
             503, "database_unavailable", "5"),
            (psycopg.errors.UndefinedTable('relation "atus.respondents" does not exist'),
             503, "schema_not_initialized", "300"),
            (psycopg.DataError("PostgreSQL text fields cannot contain NUL (0x00) bytes"),
             500, "internal_error", None),
        ],
    )
    def test_mapping(self, build, stub_engine, exc, status, code, retry_after):
        def raise_it(spec):
            raise exc

        stub_engine.estimate_behavior = raise_it
        client, _ = build()
        response = client.post(ESTIMATE, json=body())
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert response.headers.get("Retry-After") == retry_after
        assert response.headers["Cache-Control"] == "no-store"
        assert "quota" not in response.json()["error"]["message"].lower() or code == (
            "database_paused"
        )

    def test_timeouts_are_never_cached(self, build, stub_engine):
        calls = {"n": 0}

        def slow_then_ok(spec):
            calls["n"] += 1
            if calls["n"] == 1:
                raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
            return known_estimate_result()

        stub_engine.estimate_behavior = slow_then_ok
        client, _ = build()
        assert client.post(ESTIMATE, json=body()).json()["error"]["code"] == "analysis_timeout"
        retry = client.post(ESTIMATE, json=body())
        assert retry.status_code == 200 and retry.headers["X-Cache"] == "miss"
        assert calls["n"] == 2
