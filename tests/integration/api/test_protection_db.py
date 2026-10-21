"""Protection against a real PostgreSQL: per-transaction limits, query
timeouts and the request deadline, pool exhaustion, stale pooled connections,
edge auth costing zero statements, lanes before pool checkout,
single-flight over the real engine, and an empty database.

Runs the real pool, engine and fixture dataset (tests/integration/
analytics_dataset.py); "slow" engines only add ``pg_sleep`` statements in the
request's own transaction around the unmodified engine.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.api import database as dbmod
from atus_pipeline.api.app import create_app
from atus_pipeline.api.dependencies import get_engine_factory
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings
from tests.integration.conftest import MIGRATIONS_DIR

pytestmark = pytest.mark.integration

ESTIMATE = "/api/v1/analysis/estimate"
SECRET = "w" * 40
SLEEP_BODY = {"activity": {"include": ["010101"]}, "years": [2023]}


@pytest.fixture(scope="module")
def loaded_settings(tmp_path_factory, migrated_db):
    settings = analytics_settings(tmp_path_factory.mktemp("protection"), migrated_db)
    with psycopg.connect(settings.database_url) as conn:
        load_all(conn, settings)
    return settings


@contextmanager
def api(settings, engine_factory=None, **overrides):
    app = create_app(replace(settings, **overrides))
    if engine_factory is not None:
        app.dependency_overrides[get_engine_factory] = lambda: engine_factory
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, app


def body(year=2023, **extra):
    return {"activity": {"include": ["010101"]}, "years": [year], **extra}


class SlowEngine:
    """The real engine, preceded by ``pg_sleep`` statements in the request's
    own transaction (so the request's limits apply to them)."""

    def __init__(self, conn, *, sleeps=(), gate=None, **snapshot_parts):
        self._conn = conn
        self._engine = AnalysisEngine(conn, **snapshot_parts)
        self._sleeps = sleeps
        self._gate = gate

    def estimate(self, spec):
        if self._gate is not None:
            self._gate.enter()
        for seconds in self._sleeps:
            self._conn.execute("SELECT pg_sleep(%s)", (seconds,))
        return self._engine.estimate(spec)


class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def enter(self):
        self.entered.set()
        assert self.release.wait(10)


class TestTransactionLimits:
    def test_limits_are_applied_and_transaction_local(self, loaded_settings):
        seen = {}

        def factory(conn, **snapshot_parts):
            for name in ("statement_timeout", "lock_timeout", "jit",
                         "idle_in_transaction_session_timeout",
                         "max_parallel_workers_per_gather", "application_name"):
                seen[name] = conn.execute(f"SHOW {name}").fetchone()[0]
            seen["transaction_read_only"] = conn.execute(
                "SHOW transaction_read_only"
            ).fetchone()[0]
            return AnalysisEngine(conn, **snapshot_parts)

        with api(loaded_settings, factory, api_statement_timeout_ms=42_000,
                 api_max_parallel_workers=0) as (client, app):
            assert client.post(ESTIMATE, json=body()).status_code == 200
            database = app.state.database
            with database.transaction("metadata") as conn:
                meta_timeout = conn.execute("SHOW statement_timeout").fetchone()[0]
            with database.pool.connection() as conn:  # outside our transactions
                plain = conn.execute("SHOW statement_timeout").fetchone()[0]
        assert seen["statement_timeout"] == "42s"
        assert seen["lock_timeout"] == "5s"
        assert seen["jit"] == "off"
        assert seen["idle_in_transaction_session_timeout"] == "30s"
        assert seen["max_parallel_workers_per_gather"] == "0"
        assert seen["application_name"] == "atus-api"
        assert seen["transaction_read_only"] == "on"  # BEGIN READ ONLY (I05)
        assert meta_timeout == "10s"
        assert plain == "0"  # SET LOCAL: nothing leaks into the session


class TestTimeouts:
    def test_statement_timeout_is_analysis_timeout_and_never_cached(self, loaded_settings):
        def slow(conn, **snapshot_parts):
            return SlowEngine(conn, sleeps=(2.0,), **snapshot_parts)

        with api(loaded_settings, slow, api_statement_timeout_ms=200) as (client, app):
            response = client.post(ESTIMATE, json=body())
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "analysis_timeout"
            assert response.headers["Retry-After"] == "60"
            app.dependency_overrides[get_engine_factory] = lambda: AnalysisEngine
            retry = client.post(ESTIMATE, json=body())
            assert retry.status_code == 200 and retry.headers["X-Cache"] == "miss"

    def test_request_deadline_cancels_a_multi_statement_analysis(
        self, loaded_settings, monkeypatch
    ):
        """Each statement stays under statement_timeout; the request as a
        whole exceeds statement_timeout + margin and is cancelled."""
        monkeypatch.setattr(dbmod, "DEADLINE_MARGIN_S", 0.1)

        def slow(conn, **snapshot_parts):
            return SlowEngine(conn, sleeps=(0.3, 0.3, 0.3, 0.3), **snapshot_parts)

        with api(loaded_settings, slow, api_statement_timeout_ms=400) as (client, _):
            started = time.monotonic()
            response = client.post(ESTIMATE, json=body())
            elapsed = time.monotonic() - started
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "analysis_timeout"
        assert elapsed < 1.1  # 0.4 s + 0.1 s deadline, not 1.2 s of sleeps

    def test_a_deadline_that_passes_between_statements_still_ends_the_analysis(
        self, loaded_settings, monkeypatch
    ):
        """Review r1 (deadline-lost-between-statements): the deadline passes
        while the transaction is idle between statements, where PostgreSQL
        ignores a cancel. It used to be lost: the later statements ran,
        each under statement_timeout, and the answer was 200. Now no
        statement is sent after the deadline."""
        monkeypatch.setattr(dbmod, "DEADLINE_MARGIN_S", 0.1)   # deadline 0.4 + 0.1 s

        class GapEngine(SlowEngine):
            def estimate(self, spec):
                time.sleep(1.0)  # work between statements: the deadline passes here
                for _ in range(3):
                    self._conn.execute("SELECT pg_sleep(0.3)")  # each under 0.4 s
                return self._engine.estimate(spec)

        with api(loaded_settings, lambda conn, **parts: GapEngine(conn, **parts),
                 api_statement_timeout_ms=400) as (client, app):
            response = client.post(ESTIMATE, json=body())
            assert app.state.database.query_timeouts == 1
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "analysis_timeout"


LONG_SLEEP_S = 60.0
"""Longer than any wait below: a query that ends before it was cancelled."""


def running_sleeps(url: str) -> int:
    """API backends of this test database running ``pg_sleep`` right now."""
    with psycopg.connect(url, autocommit=True) as admin:
        return admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND application_name = %s AND state = 'active' AND query ILIKE %s "
            "AND pid <> pg_backend_pid()",
            (dbmod.APPLICATION_NAME, "%pg_sleep%"),
        ).fetchone()[0]


def wait_for(condition, what: str, deadline_s: float = 15.0) -> None:
    """Poll until ``condition()`` holds (a deadline, not a measurement)."""
    deadline = time.monotonic() + deadline_s
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


class TestShutdownCancel:
    """F52: on shutdown the API cancels the queries still running
    (``Database.cancel_active``) instead of waiting for them or leaving them
    to run on after the process is gone."""

    def test_cancel_active_cancels_a_running_query(self, loaded_settings):
        database = dbmod.Database(loaded_settings)
        outcome: dict = {}

        def analysis() -> None:
            try:
                with database.transaction("analysis") as conn:
                    conn.execute("SELECT pg_sleep(%s)", (LONG_SLEEP_S,))
                outcome["result"] = "finished"
            except Exception as exc:  # what the request's worker thread would see
                outcome["result"] = exc

        worker = threading.Thread(target=analysis, daemon=True)
        try:
            assert database.cancel_active() == 0  # nothing in flight yet
            worker.start()
            wait_for(lambda: running_sleeps(loaded_settings.database_url) == 1,
                     "the pg_sleep query to start")
            assert database.cancel_active() == 1
            worker.join(LONG_SLEEP_S / 2)
            assert not worker.is_alive(), "the query was not cancelled"
            assert isinstance(outcome["result"], psycopg.errors.QueryCanceled)
            assert database.query_timeouts == 1  # counted like a timeout (metrics)
            assert running_sleeps(loaded_settings.database_url) == 0
            assert database.cancel_active() == 0  # the connection left the active set
            # The cancelled connection went back to the pool in a usable state.
            with database.transaction("metadata") as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
        finally:
            database.close()

    def test_app_shutdown_cancels_an_analysis_in_flight(self, loaded_settings, caplog):
        def sleeping(conn, **snapshot_parts):
            return SlowEngine(conn, sleeps=(LONG_SLEEP_S,), **snapshot_parts)

        url = loaded_settings.database_url
        caplog.set_level("WARNING", logger="atus.api")
        with ThreadPoolExecutor(1) as pool:
            with api(loaded_settings, sleeping) as (client, app):
                in_flight = pool.submit(client.post, ESTIMATE, json=body())
                wait_for(lambda: running_sleeps(url) == 1, "the analysis query to start")
            # Leaving the client ran the lifespan shutdown with the query running.
            response = in_flight.result(LONG_SLEEP_S / 2)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "analysis_timeout"
        assert app.state.database.query_timeouts == 1
        assert len(app.state.cache) == 0  # errors are never cached
        assert "shutdown: cancelled 1 running queries" in caplog.text
        assert running_sleeps(url) == 0

    def test_an_analysis_queued_for_a_lane_never_starts_during_shutdown(self, loaded_settings):
        """Review r1 (shutdown-admits-queued-analyses-after-cancel): with the
        only lane busy, a second analysis waits for it. Shutdown cancelled
        the running query; the queued analysis then took the freed lane and
        started a query that nothing cancelled, which outlived the app. Now
        shutdown closes the lanes and the database to new work first."""
        def sleeping(conn, **snapshot_parts):
            return SlowEngine(conn, sleeps=(LONG_SLEEP_S,), **snapshot_parts)

        url = loaded_settings.database_url
        try:
            with ThreadPoolExecutor(3) as pool:
                context = api(loaded_settings, sleeping, api_max_inflight=1,
                              api_max_inflight_heavy=1, api_admission_wait_s=60)
                client, app = context.__enter__()
                first = pool.submit(client.post, ESTIMATE, json=body(2023))
                wait_for(lambda: running_sleeps(url) == 1, "the first analysis query")
                second = pool.submit(client.post, ESTIMATE, json=body(2019))
                wait_for(lambda: app.state.protection.lanes.waits == 1,
                         "the second analysis to queue for the lane")
                context.__exit__(None, None, None)              # the lifespan shutdown
                still_running = running_sleeps(url)
                responses = first.result(30), second.result(30)
        finally:
            with psycopg.connect(url, autocommit=True) as admin:  # never leave one behind
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND application_name = %s",
                    (dbmod.APPLICATION_NAME,))
        assert still_running == 0
        assert [r.json()["error"]["code"] for r in responses] == ["analysis_timeout",
                                                                 "overloaded"]


class TestPoolExhaustion:
    def test_busy_pool_is_overloaded_with_retry_after(self, loaded_settings):
        gate = Gate()

        def held(conn, **snapshot_parts):
            return SlowEngine(conn, gate=gate, **snapshot_parts)

        # TTL 0: every data request wants a new snapshot read. /ready is
        # strict (no stale snapshot stands in for the database), so it needs
        # the pool; /meta is served from the previous snapshot instead.
        auth = {"Authorization": f"Bearer {'o' * 40}"}
        with api(loaded_settings, held, api_pool_max_size=1, api_pool_timeout_s=0.3,
                 api_max_inflight=1, api_max_inflight_heavy=1,
                 api_snapshot_ttl_s=0, ops_token="o" * 40) as (client, _):
            with ThreadPoolExecutor(1) as pool:
                analysis = pool.submit(client.post, ESTIMATE, json=body())
                assert gate.entered.wait(10)
                started = time.monotonic()
                ready = client.get("/ready", headers=auth)
                waited = time.monotonic() - started
                meta = client.get("/api/v1/meta")
                gate.release.set()
                assert analysis.result(10).status_code == 200
        assert ready.status_code == 503
        assert ready.json()["error"]["code"] == "overloaded"
        assert ready.headers["Retry-After"] == "10"
        assert 0.25 <= waited < 3.0
        assert meta.status_code == 200  # stale if error: the snapshot read a moment ago

    def test_lane_is_taken_before_a_pool_connection(self, loaded_settings):
        """With the only analysis lane busy, a second uncached analysis is
        shed without ever asking the pool for a connection (the data version
        comes from the fresh snapshot)."""
        gate = Gate()

        def held(conn, **snapshot_parts):
            return SlowEngine(conn, gate=gate, **snapshot_parts)

        with api(loaded_settings, held, api_pool_max_size=3, api_max_inflight=1,
                 api_max_inflight_heavy=1, api_admission_wait_s=0.1,
                 api_snapshot_ttl_s=600) as (client, app):
            pool_stats = app.state.database.pool.get_stats
            with ThreadPoolExecutor(1) as pool:
                first = pool.submit(client.post, ESTIMATE, json=body(2023))
                assert gate.entered.wait(10)
                before = pool_stats().get("requests_num", 0)
                shed = client.post(ESTIMATE, json=body(2019))
                after = pool_stats().get("requests_num", 0)
                gate.release.set()
                assert first.result(10).status_code == 200
        assert shed.json()["error"]["code"] == "overloaded"
        assert after - before == 0
        assert app.state.protection.lanes.rejections == 1


class TestStaleConnections:
    def test_requests_succeed_after_every_backend_is_terminated(self, loaded_settings):
        # TTL 0: every request below needs the database, not the snapshot
        with api(loaded_settings, api_snapshot_ttl_s=0) as (client, app):
            pool = app.state.database.pool
            held = [pool.getconn() for _ in range(3)]  # three idle connections
            for conn in held:
                pool.putconn(conn)
            assert client.get("/api/v1/meta").status_code == 200
            with psycopg.connect(loaded_settings.database_url, autocommit=True) as admin:
                killed = admin.execute(
                    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND application_name = 'atus-api' "
                    "AND pid <> pg_backend_pid()"
                ).fetchone()[0]
            assert killed >= 3
            statuses = [
                client.get("/api/v1/meta").status_code,
                client.post(ESTIMATE, json=body(2019)).status_code,
                client.get("/api/v1/activities/0101").status_code,
                client.post(ESTIMATE, json=body(2023)).status_code,
            ]
        assert statuses == [200, 200, 200, 200]


class TestSlowWakeAfterAnOldFailure:
    def test_an_earlier_refusal_does_not_answer_a_slow_successful_connect(
        self, loaded_settings
    ):
        """Regression: after a quota refusal (or any failed connect), the next
        checkout whose connection took longer than one acquire slice — a
        database waking from suspend — was answered 503 database_paused from
        the stale failure although the connection then succeeded."""

        class SlowWake(psycopg.Connection):
            @classmethod
            def connect(cls, conninfo="", **kwargs):
                time.sleep(dbmod._ACQUIRE_SLICE_S + 0.5)
                return super().connect(conninfo, **kwargs)

        database = dbmod.Database(loaded_settings, connection_class=SlowWake)
        try:
            database.tracker.record(psycopg.OperationalError(
                "ERROR:  Your project has exceeded the data transfer quota."
            ))
            with database.transaction("metadata") as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
        finally:
            database.close()


class TestEdgeAuthCostsNothing:
    def test_forbidden_origin_runs_zero_statements(self, loaded_settings):
        with api(loaded_settings, edge_secrets=(SECRET,)) as (client, app):
            stats = app.state.database.pool.get_stats
            for method, path in (("POST", ESTIMATE), ("GET", "/api/v1/meta"),
                                 ("GET", "/ready"), ("GET", "/api/v1/activities")):
                response = client.request(method, path, json=body())
                assert response.status_code == 403
            assert stats().get("requests_num", 0) == 0
            assert stats()["pool_size"] == 0  # no connection was ever opened
            ok = client.get("/api/v1/meta", headers={"X-ATUS-Edge-Auth": SECRET})
            assert ok.status_code == 200
            assert stats().get("requests_num", 0) >= 1


class TestSingleFlightOverTheEngine:
    def test_identical_requests_compute_once(self, loaded_settings):
        def slow(conn, **snapshot_parts):
            return SlowEngine(conn, sleeps=(0.5,), **snapshot_parts)

        with api(loaded_settings, slow) as (client, app):
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(client.post, ESTIMATE, json=SLEEP_BODY)
                time.sleep(0.15)
                second = pool.submit(client.post, ESTIMATE, json=SLEEP_BODY)
                a, b = first.result(10), second.result(10)
            runner = app.state.runner
            assert runner.computations == 1 and runner.joins == 1
            assert {a.headers["X-Cache"], b.headers["X-Cache"]} == {"miss", "join"}
            assert a.json() == b.json()
            assert a.json()["estimate"]["value"] == pytest.approx(75.0)
            assert client.post(ESTIMATE, json=SLEEP_BODY).headers["X-Cache"] == "hit"


class TestQueryParameters:
    def test_nul_in_search_is_422_not_500(self, loaded_settings):
        with api(loaded_settings) as (client, _):
            response = client.get("/api/v1/activities", params={"search": "sl\x00eep"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


@pytest.fixture(scope="module")
def empty_db_url(test_db_url):
    """A migrated database with no data loaded (own lifetime), named after the
    test database so concurrent runs against other test databases on the
    same server never share it."""
    parts = urlsplit(test_db_url)
    name = f"{parts.path.lstrip('/')}_empty"
    url = urlunsplit(parts._replace(path=f"/{name}"))
    admin = urlunsplit(parts._replace(path="/postgres"))
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{name}"')
    with psycopg.connect(url) as conn:
        with conn.transaction():
            conn.execute("DROP SCHEMA IF EXISTS atus CASCADE")
            conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
        apply_migrations(conn, MIGRATIONS_DIR)
    return url


class TestEmptyDatabase:
    def test_migrated_but_empty_database_is_data_not_loaded(self, loaded_settings, empty_db_url):
        with api(loaded_settings, database_url=empty_db_url) as (client, _):
            response = client.post(ESTIMATE, json=body())
            meta = client.get("/api/v1/meta")
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "data_not_loaded"
        assert "atus load" not in response.text and "pipeline" not in response.text
        assert response.headers["Retry-After"]
        assert meta.status_code == 200 and meta.json()["data"]["release"] == "unloaded"
