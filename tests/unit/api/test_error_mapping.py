"""Domain exceptions and bad input -> the documented HTTP error contract."""

from __future__ import annotations

import pytest

from atus_pipeline.analytics.errors import (
    InsufficientDataError,
    InvalidSpecError,
    UnknownActivityError,
    UnsupportedAnalysisError,
)
from atus_pipeline.api.errors import DatabaseUnavailableError

VALID_BODY = {"activity": {"preset": "sleep"}, "years": [2025]}


def _raise(exc):
    def behavior(*_args):
        raise exc
    return behavior


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (InvalidSpecError("bad spec"), 422, "invalid_spec"),
        (UnknownActivityError("no such code"), 422, "unknown_activity"),
        (UnsupportedAnalysisError("2020 rules"), 422, "unsupported_analysis"),
        (InsufficientDataError("empty population"), 422, "insufficient_data"),
        (DatabaseUnavailableError("pool timeout"), 503, "database_unavailable"),
    ],
)
def test_domain_errors_map_to_envelope(client, stub_engine, exc, status, code):
    stub_engine.estimate_behavior = _raise(exc)
    response = client.post("/api/v1/analysis/estimate", json=VALID_BODY)
    assert response.status_code == status
    payload = response.json()
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]


def test_unexpected_exception_is_sanitized_500(client, stub_engine):
    stub_engine.estimate_behavior = _raise(RuntimeError("secret internal detail"))
    response = client.post("/api/v1/analysis/estimate", json=VALID_BODY)
    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "internal_error"
    assert "secret" not in response.text

def test_malformed_json_is_400(client):
    response = client.post(
        "/api/v1/analysis/estimate",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_json"


@pytest.mark.parametrize(
    "body",
    [
        {"activity": {"preset": "sleep"}, "years": []},                       # empty years
        {"activity": {"preset": "sleep"}, "years": [1999]},                   # pre-ATUS year
        {"activity": {"preset": "sleep"}, "years": [2025], "confidence_level": 1.5},
        {"activity": {"preset": "sleep"}, "years": [2025], "weights": "bootstrap"},
        {"activity": {}, "years": [2025]},                                    # empty selection
        {"activity": {"preset": "sleep", "include": ["0101"]}, "years": [2025]},  # both forms
        {"activity": {"include": ["01x1"]}, "years": [2025]},                 # malformed code
        {"activity": {"preset": "sleep"}, "years": [2025], "bogus": 1},       # unknown field
        {"activity": {"preset": "sleep"}, "years": [2025],
         "population": {"sex": "M"}},                                         # bad enum
    ],
)
def test_schema_violations_are_422_validation_error(client, body):
    response = client.post("/api/v1/analysis/estimate", json=body)
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["errors"], "details must locate the violation"


def test_domain_cross_field_validation_flows_through(client, stub_engine):
    """age_min > age_max is caught by the domain layer (PopulationFilter),
    proving the engine stays the authoritative validator."""
    body = {
        "activity": {"preset": "sleep"}, "years": [2025],
        "population": {"age_min": 54, "age_max": 25},
    }
    response = client.post("/api/v1/analysis/estimate", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_spec"


@pytest.mark.parametrize(
    ("operation", "extra"),
    [("estimate", {}), ("trend", {}),
     ("compare", {"group_a": {"sex": "male"}, "group_b": {"sex": "female"}})],
)
def test_a_confidence_level_without_a_quantile_is_invalid_spec_before_any_work(
        client, fake_db, operation, extra):
    """Review r2 (confidence-level-max-double-500): 1 - 2**-53 passes the
    schema's (0.5, 1) bounds, but 0.5 + cl/2 rounds to 1.0. It used to reach
    the engine and fail with a 500 after the statistics query; the domain
    spec now refuses it before the request is charged."""
    body = {"activity": {"preset": "sleep"}, "years": [2025],
            "confidence_level": 1 - 2**-53, **extra}
    response = client.post(f"/api/v1/analysis/{operation}", json=body)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_spec" and "confidence_level" in error["message"]
    assert fake_db.transactions == []
    assert client.app.state.runner.computations == 0


def test_duplicate_years_rejected_by_domain(client):
    body = {"activity": {"preset": "sleep"}, "years": [2025, 2025]}
    response = client.post("/api/v1/analysis/estimate", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_spec"


def test_unknown_preset_maps_to_invalid_spec(client):
    body = {"activity": {"preset": "napping"}, "years": [2025]}
    response = client.post("/api/v1/analysis/estimate", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_spec"
    assert "Known presets" in response.json()["error"]["message"]


def test_compare_requires_both_groups(client):
    body = {"activity": {"preset": "sleep"}, "years": [2025], "group_a": {"sex": "male"}}
    response = client.post("/api/v1/analysis/compare", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_health_needs_no_database(client, fake_db):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "build": "dev"}
    assert fake_db.transactions == []
