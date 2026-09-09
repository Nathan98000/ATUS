"""API contract tests: serialization of known results, OpenAPI invariants,
and example validity — no database involved."""

from __future__ import annotations

from atus_pipeline.analytics.results import (
    ActivityInfo,
    EstimateResult,
    EstimateValue,
    TrendPoint,
    TrendResult,
    WeightInfo,
)
from atus_pipeline.api.schemas.analysis import (
    CompareRequest,
    EstimateRequest,
    EstimateResponse,
    TrendRequest,
)

_ACTIVITY = ActivityInfo(label="Sleeping", include=("0101",), exclude=(), leaf_code_count=3)
_WEIGHT = WeightInfo(scheme="multiyear", bls_variable="TUFNWGTP", column="respondents.final_weight")


def known_estimate_result() -> EstimateResult:
    return EstimateResult(
        measure="average_minutes_per_day",
        estimate=EstimateValue(
            value=75.0, unit="minutes_per_day", standard_error=5.5901699,
            confidence_level=0.95, ci_lower=64.04, ci_upper=85.96,
        ),
        years=(2023,),
        activity=_ACTIVITY,
        population="civilian noninstitutional population age 15+",
        n_respondents=3,
        n_participants=2,
        weighted_population_per_day=4 / 365,
        days_in_period=365,
        weight=_WEIGHT,
        variance_method="replicate",
        analytics_version="0.1",
        spec={"measure": "average_minutes_per_day"},
        warnings=("a methodological note",),
    )


class TestSerializationThroughHTTP:
    def test_estimate_result_roundtrips(self, client, stub_engine):
        stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        response = client.post(
            "/api/v1/analysis/estimate",
            json={"activity": {"preset": "sleep"}, "years": [2023]},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["estimate"]["value"] == 75.0
        assert payload["estimate"]["standard_error"] == 5.5901699
        assert payload["n_respondents"] == 3          # unweighted sample size
        assert payload["weighted_population_per_day"] == 4 / 365
        assert payload["warnings"] == ["a methodological note"]
        assert response.headers["X-Cache"] == "miss"
        assert len(response.headers["X-Analysis-Key"]) == 64
        assert "X-Request-ID" in response.headers

    def test_trend_unavailable_point_is_null_not_zero(self, client, stub_engine):
        stub_engine.trend_behavior = lambda spec: TrendResult(
            measure="average_minutes_per_day",
            points=(
                TrendPoint(
                    year=2019,
                    estimate=EstimateValue(value=120.0, unit="minutes_per_day"),
                    n_respondents=1, n_participants=1,
                    weighted_population_per_day=3 / 312,
                ),
                TrendPoint(
                    year=2020, estimate=None, n_respondents=None,
                    n_participants=None, weighted_population_per_day=None,
                    unavailable_reason="TUFNWGTP is undefined for 2020",
                ),
            ),
            activity=_ACTIVITY, population="all", weight=_WEIGHT,
            variance_method="none", analytics_version="0.1", spec={}, warnings=(),
        )
        response = client.post(
            "/api/v1/analysis/trend",
            json={"activity": {"preset": "sleep"}, "years": [2019, 2020], "variance": "none"},
        )
        assert response.status_code == 200
        points = {p["year"]: p for p in response.json()["points"]}
        assert points[2020]["estimate"] is None       # null, never zero
        assert "2020" in points[2020]["unavailable_reason"]
        assert points[2019]["estimate"]["value"] == 120.0

    def test_cached_response_identical_including_warnings(self, client, stub_engine):
        calls = {"n": 0}

        def once(spec):
            calls["n"] += 1
            return known_estimate_result()

        stub_engine.estimate_behavior = once
        body = {"activity": {"preset": "sleep"}, "years": [2023]}
        first = client.post("/api/v1/analysis/estimate", json=body)
        second = client.post("/api/v1/analysis/estimate", json=body)
        assert calls["n"] == 1, "second request must be served from the cache"
        assert second.headers["X-Cache"] == "hit"
        assert first.json() == second.json()

    def test_errors_are_not_cached(self, client, stub_engine):
        from atus_pipeline.analytics.errors import InsufficientDataError

        calls = {"n": 0}

        def fail_then_succeed(spec):
            calls["n"] += 1
            if calls["n"] == 1:
                raise InsufficientDataError("first attempt fails")
            return known_estimate_result()

        stub_engine.estimate_behavior = fail_then_succeed
        body = {"activity": {"preset": "sleep"}, "years": [2023]}
        assert client.post("/api/v1/analysis/estimate", json=body).status_code == 422
        ok = client.post("/api/v1/analysis/estimate", json=body)
        assert ok.status_code == 200 and calls["n"] == 2


class TestOpenAPI:
    def test_core_routes_and_schemas_exist(self, client):
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        for route in (
            "/api/v1/analysis/estimate", "/api/v1/analysis/trend",
            "/api/v1/analysis/compare", "/api/v1/activities",
            "/api/v1/activities/{code}", "/api/v1/activities/presets",
            "/api/v1/population/metadata", "/api/v1/meta", "/health", "/ready",
        ):
            assert route in paths, route
        schemas = spec["components"]["schemas"]
        for name in ("EstimateRequest", "EstimateResponse", "TrendResponse",
                     "CompareResponse", "ErrorResponse", "MetaResponse"):
            assert name in schemas, name
        estimate_fields = schemas["EstimateResponse"]["properties"]
        for field in ("estimate", "n_respondents", "weighted_population_per_day",
                      "warnings", "spec", "analytics_version"):
            assert field in estimate_fields, field

    def test_documented_request_examples_validate(self, client):
        for model, examples in (
            (EstimateRequest, EstimateRequest.model_config["json_schema_extra"]["examples"]),
            (TrendRequest, TrendRequest.model_config["json_schema_extra"]["examples"]),
            (CompareRequest, CompareRequest.model_config["json_schema_extra"]["examples"]),
        ):
            for example in examples:
                model.model_validate(example)  # raises if documentation drifts

    def test_documented_response_example_validates(self, client):
        example = EstimateResponse.model_config["json_schema_extra"]["examples"][0]
        EstimateResponse.model_validate(example)
