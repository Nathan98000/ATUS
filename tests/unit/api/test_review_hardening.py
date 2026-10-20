"""Tests added from the Phase 3 adversarial review: cache-key
canonicalization, cache failure/bypass behavior, compare serialization and
caching, CORS contract, error-envelope coverage for framework responses,
header behavior on 500s, and readiness failure paths."""

from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from atus_pipeline.analytics.results import ComparisonResult, EstimateValue
from atus_pipeline.api.dependencies import get_cache
from tests.unit.api.conftest import FakeDatabase, make_app, make_settings
from tests.unit.api.test_contract import known_estimate_result


def _estimate_body(**overrides) -> dict:
    body = {"activity": {"preset": "sleep"}, "years": [2023], "variance": "none"}
    body.update(overrides)
    return body


class TestCacheKeyCanonicalizationThroughHTTP:
    def test_year_order_is_canonical(self, client, stub_engine):
        stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        a = client.post("/api/v1/analysis/estimate",
                        json=_estimate_body(years=[2023, 2024]))
        b = client.post("/api/v1/analysis/estimate",
                        json=_estimate_body(years=[2024, 2023]))
        assert a.headers["X-Analysis-Key"] == b.headers["X-Analysis-Key"]
        assert b.headers["X-Cache"] == "hit"

    def test_activity_code_order_is_canonical(self, client, stub_engine):
        stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        a = client.post(
            "/api/v1/analysis/estimate",
            json=_estimate_body(activity={"include": ["120303", "010101"]}),
        )
        b = client.post(
            "/api/v1/analysis/estimate",
            json=_estimate_body(activity={"include": ["010101", "120303"]}),
        )
        assert a.headers["X-Analysis-Key"] == b.headers["X-Analysis-Key"]
        assert b.headers["X-Cache"] == "hit"


class BrokenCache:
    def get(self, key):
        raise RuntimeError("cache backend down")

    def put(self, key, value):
        raise RuntimeError("cache backend down")


class TestCacheDegradation:
    def test_broken_cache_never_breaks_analysis(self, app, client, stub_engine):
        stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        app.dependency_overrides[get_cache] = BrokenCache
        response = client.post("/api/v1/analysis/estimate", json=_estimate_body())
        assert response.status_code == 200
        assert response.json()["estimate"]["value"] == 75.0

    def test_data_version_change_mid_request_bypasses_the_cache(
        self, app, client, stub_engine, fake_db
    ):
        """If `atus load` finishes while a result is being computed, the result
        must not be stored under the pre-reload version key."""
        stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        # The recheck (via the connection) sees run1; pretend the request
        # started when the version was run0.
        fake_db.data_version = "0325:run0"
        first = client.post("/api/v1/analysis/estimate", json=_estimate_body())
        assert first.headers["X-Cache"] == "bypass"
        second = client.post("/api/v1/analysis/estimate", json=_estimate_body())
        assert second.headers["X-Cache"] == "bypass"  # still never cached


class TestCompareContract:
    @staticmethod
    def _comparison_result() -> ComparisonResult:
        group = known_estimate_result()
        return ComparisonResult(
            measure="average_minutes_per_day",
            label_a="men", label_b="women",
            group_a=group, group_b=group,
            difference=EstimateValue(
                value=33.33, unit="minutes_per_day", standard_error=11.79,
                confidence_level=0.95, ci_lower=10.2, ci_upper=56.4,
            ),
            analytics_version="0.2",
            warnings=("difference SE uses per-replicate differences",),
        )

    def test_compare_serialization(self, client, stub_engine):
        stub_engine.compare_behavior = lambda comparison: self._comparison_result()
        body = _estimate_body(group_a={"sex": "male"}, group_b={"sex": "female"})
        response = client.post("/api/v1/analysis/compare", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["difference"]["standard_error"] == 11.79
        assert payload["group_a"]["estimate"]["value"] == 75.0
        assert payload["warnings"]

    def test_compare_is_cached_and_labels_separate_keys(self, client, stub_engine):
        calls = {"n": 0}

        def count(comparison):
            calls["n"] += 1
            return self._comparison_result()

        stub_engine.compare_behavior = count
        body = _estimate_body(group_a={"sex": "male"}, group_b={"sex": "female"})
        first = client.post("/api/v1/analysis/compare", json=body)
        second = client.post("/api/v1/analysis/compare", json=body)
        assert calls["n"] == 1 and second.headers["X-Cache"] == "hit"
        relabeled = client.post(
            "/api/v1/analysis/compare", json={**body, "label_a": "hommes"}
        )
        assert relabeled.headers["X-Analysis-Key"] != first.headers["X-Analysis-Key"]
        assert calls["n"] == 2


class TestErrorEnvelopeCoverage:
    def test_unknown_route_uses_the_envelope(self, client):
        response = client.get("/api/v1/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_wrong_method_uses_the_envelope(self, client):
        response = client.get("/api/v1/analysis/estimate")
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "method_not_allowed"

    def test_500_carries_request_id(self, client, stub_engine):
        stub_engine.estimate_behavior = lambda spec: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        response = client.post("/api/v1/analysis/estimate", json=_estimate_body())
        assert response.status_code == 500
        assert response.headers.get("X-Request-ID")


def _cors_app(stub_engine, tmp_path, origins):
    settings = make_settings(tmp_path, api_cors_origins=origins)
    return make_app(settings, stub_engine, FakeDatabase())


class TestCORS:
    ORIGIN = "http://localhost:3000"

    def test_disabled_by_default(self, client):
        response = client.get("/health", headers={"Origin": self.ORIGIN})
        assert "access-control-allow-origin" not in response.headers

    def test_allowed_origin_gets_cors_headers(self, stub_engine, tmp_path):
        app = _cors_app(stub_engine, tmp_path, (self.ORIGIN,))
        with TestClient(app) as cors_client:
            preflight = cors_client.options(
                "/api/v1/analysis/estimate",
                headers={
                    "Origin": self.ORIGIN,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"] == self.ORIGIN
            assert "access-control-allow-credentials" not in preflight.headers

    def test_disallowed_origin_gets_no_cors_headers(self, stub_engine, tmp_path):
        app = _cors_app(stub_engine, tmp_path, (self.ORIGIN,))
        with TestClient(app) as cors_client:
            response = cors_client.get(
                "/health", headers={"Origin": "http://evil.example"}
            )
            assert "access-control-allow-origin" not in response.headers

    def test_500_still_reaches_allowed_browser_clients(self, stub_engine, tmp_path):
        app = _cors_app(stub_engine, tmp_path, (self.ORIGIN,))
        stub_engine.estimate_behavior = lambda spec: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        with TestClient(app, raise_server_exceptions=False) as cors_client:
            response = cors_client.post(
                "/api/v1/analysis/estimate",
                json=_estimate_body(),
                headers={"Origin": self.ORIGIN},
            )
            assert response.status_code == 500
            assert response.headers["access-control-allow-origin"] == self.ORIGIN


class _ReadyConnBase:
    """Minimal connection stub for readiness-path tests."""

    def execute(self, sql, params=None):
        raise NotImplementedError


OPS_TOKEN = "o" * 40
OPS = {"Authorization": f"Bearer {OPS_TOKEN}"}


class TestReadinessFailurePaths:
    """/ready failures are ordinary 503 envelopes without operator hints
    (the hints go to the log; F16)."""

    def _client(self, stub_engine, tmp_path, fake_db):
        app = make_app(make_settings(tmp_path, ops_token=OPS_TOKEN), stub_engine, fake_db)
        return TestClient(app, raise_server_exceptions=False)

    def test_schema_not_initialized(self, stub_engine, tmp_path):
        class NoSchemaConn(_ReadyConnBase):
            def execute(self, sql, params=None):
                raise psycopg.errors.UndefinedTable('relation "atus.ingestion_runs" missing')

        with self._client(stub_engine, tmp_path, FakeDatabase(NoSchemaConn)) as client:
            response = client.get("/ready", headers=OPS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "schema_not_initialized"
        assert "atus migrate" not in response.text

    def test_data_not_loaded(self, stub_engine, tmp_path):
        from atus_pipeline.database import versions

        fake_db = FakeDatabase(data_version=versions.UNLOADED)
        with self._client(stub_engine, tmp_path, fake_db) as client:
            response = client.get("/ready", headers=OPS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "data_not_loaded"
        assert "atus load" not in response.text
