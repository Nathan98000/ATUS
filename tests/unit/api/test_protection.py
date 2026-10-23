"""Protection primitives: token buckets, the daily cap, admission lanes, the
heavy classifier, and which client a request is charged to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anyio
import pytest

from atus_pipeline.api.errors import OverloadedError
from atus_pipeline.api.protection import (
    EDGE_UNKNOWN_CLIENT,
    HEAVY_YEAR_QUERIES,
    AdmissionLanes,
    ClientIdentity,
    DailyCap,
    TokenBucketLimiter,
    client_identity,
    is_heavy,
)
from atus_pipeline.config import RateLimit


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class TestTokenBucket:
    def test_burst_then_refill(self):
        clock = FakeClock()
        limiter = TokenBucketLimiter(RateLimit(3, 60.0), clock=clock)
        assert [limiter.acquire("ip") for _ in range(3)] == [None, None, None]
        wait = limiter.acquire("ip")
        assert wait == pytest.approx(20.0)  # one token per 20 s
        clock.now += 10
        assert limiter.acquire("ip") == pytest.approx(10.0)
        clock.now += 10
        assert limiter.acquire("ip") is None
        assert limiter.rejections == 2

    def test_keys_are_independent(self):
        limiter = TokenBucketLimiter(RateLimit(1, 60.0), clock=FakeClock())
        assert limiter.acquire("a") is None
        assert limiter.acquire("a") is not None
        assert limiter.acquire("b") is None

    def test_tokens_never_exceed_the_burst(self):
        clock = FakeClock()
        limiter = TokenBucketLimiter(RateLimit(2, 10.0), clock=clock)
        limiter.acquire("ip")
        clock.now += 10_000
        assert [limiter.acquire("ip") is None for _ in range(3)] == [True, True, False]

    def test_memory_is_bounded_by_lru(self):
        limiter = TokenBucketLimiter(RateLimit(1, 60.0), max_keys=100, clock=FakeClock())
        for i in range(1000):
            limiter.acquire(f"10.0.{i // 256}.{i % 256}")
        assert len(limiter) == 100

    def test_refund_returns_one_token(self):
        limiter = TokenBucketLimiter(RateLimit(1, 60.0), clock=FakeClock())
        assert limiter.acquire("ip") is None
        assert limiter.acquire("ip") is not None
        limiter.refund("ip")
        assert limiter.acquire("ip") is None


class TestDailyCap:
    def test_counts_and_resets_at_utc_midnight(self):
        now = [datetime(2026, 10, 20, 23, 59, 30, tzinfo=UTC)]
        cap = DailyCap(2, clock=lambda: now[0])
        assert cap.try_take() and cap.try_take()
        assert cap.exhausted() and not cap.try_take()
        assert cap.seconds_until_reset() == 30
        now[0] += timedelta(seconds=31)
        assert not cap.exhausted() and cap.try_take()

    def test_retry_after_is_at_least_one_second(self):
        cap = DailyCap(1, clock=lambda: datetime(2026, 10, 20, 23, 59, 59, 999_999, tzinfo=UTC))
        assert cap.seconds_until_reset() == 1

    def test_refusals_are_counted_for_metrics(self):
        now = [datetime(2026, 10, 20, 12, 0, tzinfo=UTC)]
        cap = DailyCap(1, clock=lambda: now[0])
        assert cap.try_take() and cap.used_today() == 1
        error = cap.refuse()
        assert error.code == "daily_capacity_reached" and error.retry_after == 12 * 3600
        assert cap.rejections == 1
        now[0] += timedelta(days=1)
        assert cap.used_today() == 0  # the day rolled over

    def test_refund_gives_back_one_unit_never_below_zero(self):
        now = [datetime(2026, 10, 20, 12, 0, tzinfo=UTC)]
        cap = DailyCap(1, clock=lambda: now[0])
        assert cap.try_take() and cap.exhausted()
        cap.refund()
        assert cap.used_today() == 0 and cap.try_take()
        now[0] += timedelta(days=1)
        cap.refund()  # taken yesterday: the new day's count stays at zero
        assert cap.used_today() == 0


class TestHeavyClassifier:
    @pytest.mark.parametrize(
        ("operation", "n_years", "heavy"),
        [
            ("estimate", 1, False),
            ("estimate", HEAVY_YEAR_QUERIES - 1, False),
            ("estimate", HEAVY_YEAR_QUERIES, True),
            ("estimate", 22, True),   # pooled 22 years: 21.35 s p50 at 0.25 CU (ADR-012)
            ("trend", 23, True),      # 23-year trend: 21.50-21.81 s p50 at 0.25 CU
            ("trend", 2, False),
            ("compare", 1, False),
            ("compare", 3, False),
            ("compare", 4, True),     # two queries over 4 years each
        ],
    )
    def test_classifier(self, operation, n_years, heavy):
        assert is_heavy(operation, tuple(range(2003, 2003 + n_years))) is heavy


class TestClientIdentity:
    def test_edge_client_ip_is_trusted_only_when_authenticated(self):
        trusted = client_identity(
            peer="10.1.2.3", edge_client_ip="198.51.100.7", authenticated=True,
            edge_auth_enabled=True,
        )
        assert trusted.rate_key == "198.51.100.7"
        forged = client_identity(
            peer="203.0.113.9", edge_client_ip="198.51.100.7", authenticated=False,
            edge_auth_enabled=False,
        )
        assert forged.rate_key == "203.0.113.9"

    @pytest.mark.parametrize("header", [None, "", "not-an-ip", "198.51.100.7, 10.0.0.1"])
    def test_missing_or_malformed_edge_ip_shares_one_edge_bucket(self, header):
        """Never the peer: behind the edge it is the platform's proxy. Every
        edge request without a usable address shares one bucket (as at the
        governor), whatever proxy node it came through, and logs no IP."""
        identities = {
            client_identity(peer=peer, edge_client_ip=header, authenticated=True,
                            edge_auth_enabled=True)
            for peer in ("10.1.2.3", "10.9.8.7", None)
        }
        assert identities == {ClientIdentity(ip=None, rate_key=EDGE_UNKNOWN_CLIENT)}

    def test_ipv6_is_keyed_by_64_prefix(self):
        a = client_identity(peer=None, edge_client_ip="2001:db8:1:2::1", authenticated=True,
                            edge_auth_enabled=True)
        b = client_identity(peer=None, edge_client_ip="2001:db8:1:2:ffff::9",
                            authenticated=True, edge_auth_enabled=True)
        assert a.rate_key == b.rate_key == "2001:db8:1:2::/64"
        mapped = client_identity(peer=None, edge_client_ip="::ffff:198.51.100.7",
                                 authenticated=True, edge_auth_enabled=True)
        assert mapped.rate_key == "198.51.100.7"

    def test_development_exempts_loopback_and_in_process_clients(self):
        for peer in ("127.0.0.1", "::1", "testclient", None):
            identity = client_identity(peer=peer, edge_client_ip=None, authenticated=False,
                                       edge_auth_enabled=False)
            assert identity.rate_key is None, peer

    def test_loopback_is_limited_when_edge_auth_is_on(self):
        authenticated = client_identity(peer="127.0.0.1", edge_client_ip=None,
                                        authenticated=True, edge_auth_enabled=True)
        assert authenticated.rate_key == EDGE_UNKNOWN_CLIENT
        unauthenticated = client_identity(peer="127.0.0.1", edge_client_ip=None,
                                          authenticated=False, edge_auth_enabled=True)
        assert unauthenticated.rate_key == "127.0.0.1"


class TestAdmissionLanes:
    def test_overloaded_after_the_wait(self):
        async def scenario():
            lanes = AdmissionLanes(max_inflight=1, max_heavy=1, wait_s=0.05)
            async with lanes.admit(heavy=False):
                assert lanes.in_flight == 1
                with pytest.raises(OverloadedError) as info:
                    async with lanes.admit(heavy=False):
                        pass
                assert info.value.retry_after == 10
            assert lanes.in_flight == 0 and lanes.rejections == 1
            assert lanes.waits == 1  # the shed request had waited
            async with lanes.admit(heavy=False):  # free again
                pass
            assert lanes.waits == 1  # a free lane is not a wait

        anyio.run(scenario)

    def test_heavy_lane_leaves_room_for_light_work(self):
        async def scenario():
            lanes = AdmissionLanes(max_inflight=2, max_heavy=1, wait_s=0.05)
            async with lanes.admit(heavy=True):
                assert lanes.heavy_in_flight == 1
                with pytest.raises(OverloadedError):
                    async with lanes.admit(heavy=True):
                        pass
                async with lanes.admit(heavy=False):
                    assert lanes.in_flight == 2
            assert (lanes.in_flight, lanes.heavy_in_flight) == (0, 0)

        anyio.run(scenario)

    def test_waiting_request_is_admitted_when_a_lane_frees(self):
        async def scenario():
            lanes = AdmissionLanes(max_inflight=1, max_heavy=1, wait_s=2.0)
            admitted = []

            async def second():
                async with lanes.admit(heavy=False):
                    admitted.append("second")

            async with anyio.create_task_group() as tg:
                async with lanes.admit(heavy=False):
                    tg.start_soon(second)
                    await anyio.sleep(0.05)
                    assert admitted == []
            assert admitted == ["second"]
            assert (lanes.waits, lanes.rejections) == (1, 0)

        anyio.run(scenario)

    def test_zero_wait_still_admits_when_free(self):
        async def scenario():
            lanes = AdmissionLanes(max_inflight=1, max_heavy=1, wait_s=0.0)
            async with lanes.admit(heavy=True):
                pass

        anyio.run(scenario)

    def test_closed_lanes_admit_nothing_not_even_a_waiter(self):
        """Shutdown (review r1): a request waiting for a lane when the lanes
        close must not take the lane that frees; later requests are refused."""
        async def scenario():
            lanes = AdmissionLanes(max_inflight=1, max_heavy=1, wait_s=2.0)
            outcome = []

            async def waiter():
                try:
                    async with lanes.admit(heavy=False):
                        outcome.append("admitted")
                except OverloadedError as exc:
                    outcome.append(str(exc))

            async with anyio.create_task_group() as tg:
                async with lanes.admit(heavy=False):
                    tg.start_soon(waiter)
                    await anyio.sleep(0.05)
                    lanes.close()
            assert outcome == ["server shutting down"]
            with pytest.raises(OverloadedError):
                async with lanes.admit(heavy=True):
                    pass
            assert lanes.in_flight == 0

        anyio.run(scenario)
