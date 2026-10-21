"""The database layer without a server: per-transaction limits, connection
parameters, checkout failure classification (a real psycopg_pool whose
connection attempts fail), and the request deadline."""

from __future__ import annotations

import threading
import time

import psycopg
import pytest

from atus_pipeline.api import database as dbmod
from atus_pipeline.api.database import (
    APPLICATION_NAME,
    ConnectTracker,
    Database,
    _Deadline,
    connection_kwargs,
    session_limits,
    session_limits_statement,
    snapshot_limits_statement,
)
from atus_pipeline.api.errors import (
    DatabasePausedError,
    DatabaseUnavailableError,
    OverloadedError,
    classify_connect_failure,
    is_neon_paused,
)
from tests.unit.api.conftest import make_settings


class TestSessionLimits:
    def test_analysis_and_metadata_timeouts(self, tmp_path):
        settings = make_settings(tmp_path)
        analysis = dict(session_limits(settings, "analysis"))
        metadata = dict(session_limits(settings, "metadata"))
        assert analysis["statement_timeout"] == "85000"
        assert metadata["statement_timeout"] == "10000"
        assert analysis["jit"] == metadata["jit"] == "off"
        assert analysis["lock_timeout"] == "5000"
        assert analysis["idle_in_transaction_session_timeout"] == "30000"
        assert analysis["client_connection_check_interval"] == "5000"
        assert "max_parallel_workers_per_gather" not in analysis

    def test_optional_parallel_workers_and_jit(self, tmp_path):
        settings = make_settings(tmp_path, api_max_parallel_workers=0, api_jit=True)
        limits = dict(session_limits(settings, "analysis"))
        assert limits["max_parallel_workers_per_gather"] == "0"
        assert limits["jit"] == "on"

    def test_one_statement_with_bound_values(self, tmp_path):
        sql, params = session_limits_statement(session_limits(make_settings(tmp_path), "analysis"))
        assert sql.startswith("SELECT set_config('statement_timeout', %s, true), ")
        assert sql.count("set_config(") == len(params) == 5
        assert ";" not in sql

    def test_snapshot_transactions_lock_before_any_snapshot_taking_statement(self, tmp_path):
        """REPEATABLE READ: only SET and LOCK before the snapshot exists (a
        SELECT would fix it before the lock; review r1)."""
        limits = session_limits(make_settings(tmp_path), "metadata")
        text = snapshot_limits_statement(limits).as_string(None)
        statements = text.split("; ")
        assert statements[:-1] == [f"SET LOCAL {name} = '{value}'" for name, value in limits]
        assert statements[-1] == "LOCK TABLE atus.ingestion_runs IN ACCESS SHARE MODE"
        assert "SELECT" not in text and "%s" not in text

    def test_unknown_kind_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            session_limits(make_settings(tmp_path), "admin")

    def test_connection_kwargs(self, tmp_path):
        kwargs = connection_kwargs(make_settings(tmp_path, api_prepare_threshold=None))
        assert kwargs["connect_timeout"] == 10
        assert kwargs["keepalives"] == 1 and kwargs["keepalives_idle"] == 30
        assert kwargs["application_name"] == APPLICATION_NAME
        assert kwargs["prepare_threshold"] is None


class TestClassification:
    @pytest.mark.parametrize(
        "text",
        [
            "ERROR:  Your account or project has exceeded the compute time quota. "
            "Upgrade your plan to increase limits.",
            "Your project has exceeded the data transfer quota.",
            "Couldn't connect to compute node: endpoint is temporarily unavailable. "
            "Check your quotas and/or contact our support.",
            "ERROR: endpoint is disabled",
        ],
    )
    def test_neon_quota_texts(self, text):
        assert is_neon_paused(text)
        assert isinstance(classify_connect_failure(psycopg.OperationalError(text)),
                          DatabasePausedError)

    @pytest.mark.parametrize(
        "text",
        [
            "connection refused",
            "password authentication failed for user \"atus_api\"",
            "terminating connection due to administrator command",
            "remaining connection slots are reserved",
        ],
    )
    def test_other_failures_are_unavailable(self, text):
        assert not is_neon_paused(text)
        assert isinstance(classify_connect_failure(psycopg.OperationalError(text)),
                          DatabaseUnavailableError)

    def test_too_many_connections_is_overload(self):
        assert isinstance(
            classify_connect_failure(psycopg.errors.TooManyConnections("too many clients")),
            OverloadedError,
        )


class TestConnectTracker:
    def test_only_failures_since_the_given_time_count(self):
        tracker = ConnectTracker()
        assert tracker.failing(since=0.0) is None
        error = psycopg.OperationalError("connection refused")
        tracker.record(error)
        recorded = time.monotonic()
        assert tracker.failing(since=recorded - 60) is error
        assert tracker.failing(since=recorded + 1) is None  # older than the checkout

    def test_a_later_success_clears_the_failure(self):
        tracker = ConnectTracker()
        started = time.monotonic()
        tracker.record(psycopg.OperationalError("connection refused"))
        tracker.record(None)
        assert tracker.failing(since=started) is None


def _failing_connection_class(message: str):
    class Failing(psycopg.Connection):
        attempts = 0

        @classmethod
        def connect(cls, conninfo="", **kwargs):
            Failing.attempts += 1
            raise psycopg.OperationalError(message)

    return Failing


class TestCheckout:
    def _database(self, tmp_path, connection_class, **overrides):
        settings = make_settings(tmp_path, api_pool_timeout_s=0.5, api_connect_timeout_s=2,
                                 **overrides)
        return Database(settings, connection_class=connection_class)

    def test_neon_quota_refusal_is_database_paused_fast(self, tmp_path):
        failing = _failing_connection_class(
            "connection to server failed: ERROR:  Your project has exceeded the data "
            "transfer quota. Upgrade your plan to increase limits."
        )
        db = self._database(tmp_path, failing)
        try:
            start = time.monotonic()
            with pytest.raises(DatabasePausedError) as info, db.transaction("metadata"):
                pass
            assert time.monotonic() - start < 2.0
            assert info.value.retry_after == 3600
            assert failing.attempts >= 1
        finally:
            db.close()

    def test_unreachable_server_is_database_unavailable(self, tmp_path):
        db = self._database(tmp_path, _failing_connection_class("connection refused"))
        try:
            with pytest.raises(DatabaseUnavailableError), db.transaction("analysis"):
                pass
        finally:
            db.close()

    def test_a_failure_from_before_the_checkout_is_not_its_answer(self, tmp_path):
        """Regression: a quota refusal recorded earlier (e.g. last month's)
        used to answer a later checkout whose own connection attempt was
        merely slower than one acquire slice — 503 database_paused with
        Retry-After 3600 for a database that had recovered. The checkout must
        report the outcome of attempts made while it waits."""

        class SlowThenRefused(psycopg.Connection):
            @classmethod
            def connect(cls, conninfo="", **kwargs):
                time.sleep(dbmod._ACQUIRE_SLICE_S + 0.3)  # a database slow to wake
                raise psycopg.OperationalError("connection refused")

        db = self._database(tmp_path, SlowThenRefused)
        try:
            db.tracker.record(psycopg.OperationalError(
                "ERROR:  Your project has exceeded the data transfer quota."
            ))
            with pytest.raises(DatabaseUnavailableError), db.transaction("metadata"):
                pass
        finally:
            db.close()

    def test_pool_starts_without_connecting(self, tmp_path):
        failing = _failing_connection_class("must not connect at startup")
        db = self._database(tmp_path, failing)
        try:
            time.sleep(0.2)
            assert failing.attempts == 0
            stats = db.pool.get_stats()
            assert stats["pool_min"] == 0 and stats["pool_size"] == 0
            assert stats["pool_max"] == 4
        finally:
            db.close()


class _FakeConn:
    def __init__(self):
        self.cancels = 0

    def cancel_safe(self, timeout=None):
        self.cancels += 1


class TestDeadline:
    def test_fires_after_the_deadline(self):
        conn = _FakeConn()
        deadline = _Deadline(conn, 0.05)
        time.sleep(0.3)
        deadline.stop()
        assert deadline.fired and conn.cancels == 1

    def test_never_fires_after_stop(self):
        conn = _FakeConn()
        deadline = _Deadline(conn, 0.1)
        deadline.stop()
        time.sleep(0.3)
        assert not deadline.fired and conn.cancels == 0

    def test_stop_waits_for_an_in_progress_cancel(self, monkeypatch):
        started, finish = threading.Event(), threading.Event()

        class SlowCancelConn(_FakeConn):
            def cancel_safe(self, timeout=None):
                started.set()
                finish.wait(5)
                self.cancels += 1

        conn = SlowCancelConn()
        deadline = _Deadline(conn, 0.01)
        assert started.wait(5)
        stopper = threading.Thread(target=deadline.stop)
        stopper.start()
        time.sleep(0.05)
        assert stopper.is_alive()  # the connection is not released mid-cancel
        finish.set()
        stopper.join(5)
        assert conn.cancels == 1

    def test_margin_constant(self):
        assert dbmod.DEADLINE_MARGIN_S == 5.0
