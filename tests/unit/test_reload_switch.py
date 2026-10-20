"""The switch's retry policy and table classification, without a database
(the behaviour against PostgreSQL is in tests/integration/test_reload_switch.py)."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime

import psycopg
import pytest

from atus_pipeline.loading import switch
from atus_pipeline.loading.errors import LoadError
from atus_pipeline.loading.switch import (
    CANONICAL_TABLES,
    LOCK_ORDER,
    METADATA_TABLES,
    SwitchOptions,
)
from atus_pipeline.transformation.tables import TABLE_SPECS

CUTOFF = datetime(2026, 10, 20, tzinfo=UTC)


class FakeConnection:
    def transaction(self):
        return nullcontext()


class TestTables:
    def test_every_loaded_table_is_swapped(self):
        lexicon = {"activity_tier1", "activity_tier2", "activity_codes"}
        assert set(CANONICAL_TABLES) == {spec.table for spec in TABLE_SPECS} | lexicon

    def test_metadata_is_never_swapped(self):
        assert not set(METADATA_TABLES) & set(CANONICAL_TABLES)

    def test_lock_order_starts_where_every_request_starts(self):
        # every API request reads the data version first, the engine then
        # reads respondents and the lexicon
        assert LOCK_ORDER[:5] == (
            "ingestion_runs", "respondents", "activity_tier1", "activity_tier2", "activity_codes",
        )
        assert sorted(LOCK_ORDER[1:]) == sorted(CANONICAL_TABLES)


class TestRetries:
    @pytest.fixture
    def harness(self, monkeypatch):
        state = {"busy_attempts": 0, "blocked_checks": 0, "swaps": 0, "sleeps": [],
                 "sees_all": True}
        monkeypatch.setattr(switch, "effective_lock_timeout_ms", lambda conn, ms: ms)
        monkeypatch.setattr(switch, "sees_all_sessions", lambda conn: state["sees_all"])

        def older_transactions(conn, cutoff):
            if state["blocked_checks"]:
                state["blocked_checks"] -= 1
                return [4242]
            return []

        def swap(conn, lock_timeout_ms):
            if state["busy_attempts"]:
                state["busy_attempts"] -= 1
                raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")
            state["swaps"] += 1

        monkeypatch.setattr(switch, "older_transactions", older_transactions)
        monkeypatch.setattr(switch, "_swap", swap)
        return state

    def run(self, state, timeout_s=60.0):
        published = []
        attempts = switch.switch(
            FakeConnection(),
            publish=lambda conn: published.append(conn),
            cutoff=CUTOFF,
            options=SwitchOptions(timeout_s=timeout_s),
            sleep=state["sleeps"].append,
        )
        return attempts, published

    def test_busy_tables_are_retried_with_short_backoff(self, harness):
        harness["busy_attempts"] = 3
        attempts, published = self.run(harness)
        assert attempts == 4 and harness["swaps"] == 1 and len(published) == 1
        assert len(harness["sleeps"]) == 3
        assert all(0 < s <= 1.0 for s in harness["sleeps"])

    def test_old_transactions_are_waited_for_before_any_lock(self, harness):
        harness["blocked_checks"] = 2
        attempts, _ = self.run(harness)
        assert attempts == 3 and harness["swaps"] == 1

    def test_gives_up_with_the_reason_after_the_timeout(self, harness):
        harness["busy_attempts"] = 10**9
        with pytest.raises(LoadError, match="tables busy .*live data is unchanged"):
            self.run(harness, timeout_s=0)
        assert harness["swaps"] == 0

    def test_old_transactions_are_named_in_the_error(self, harness):
        harness["blocked_checks"] = 10**9
        with pytest.raises(LoadError, match="older than the new data .*pids 4242"):
            self.run(harness, timeout_s=0)

    def test_warns_when_other_sessions_are_invisible(self, harness, caplog):
        harness["sees_all"] = False
        self.run(harness)
        assert "Grant pg_read_all_stats" in caplog.text

    def test_no_warning_when_all_sessions_are_visible(self, harness, caplog):
        self.run(harness)
        assert "pg_read_all_stats" not in caplog.text

    def test_other_errors_are_not_retried(self, harness, monkeypatch):
        def broken(conn, lock_timeout_ms):
            raise psycopg.errors.InsufficientPrivilege("permission denied")

        monkeypatch.setattr(switch, "_swap", broken)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            self.run(harness)
        assert harness["sleeps"] == []


class TestLockTimeout:
    class Settings:
        def __init__(self, deadlock_ms):
            self.deadlock_ms = deadlock_ms

        def transaction(self):
            return nullcontext()

        def execute(self, sql, params=None):
            value = self.deadlock_ms

            class Cursor:
                @staticmethod
                def fetchone():
                    return (value,)

            return Cursor()

    def test_requested_value_when_it_is_small(self):
        assert switch.effective_lock_timeout_ms(self.Settings(1000), 50) == 50

    def test_capped_so_a_whole_attempt_ends_before_deadlock_detection(self):
        # 11 locked tables: 11 waits of at most 83 ms stay below 1 s
        capped = switch.effective_lock_timeout_ms(self.Settings(1000), 400)
        assert capped == 1000 // (len(LOCK_ORDER) + 1)
        assert capped * len(LOCK_ORDER) < 1000

    def test_never_zero(self):
        # lock_timeout = 0 would mean "wait forever"
        assert switch.effective_lock_timeout_ms(self.Settings(1), 50) == 1
