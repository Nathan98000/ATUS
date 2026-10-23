"""Full-stack API tests: HTTP -> FastAPI -> AnalysisEngine -> PostgreSQL.

Runs against the hand-computed analytics fixture dataset (see
tests/integration/analytics_dataset.py) loaded into the disposable test
database through the real Phase 1 loader, so every expected number below is
verifiable by pencil and paper.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from atus_pipeline.api.app import create_app
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client(tmp_path_factory, migrated_db):
    settings = analytics_settings(tmp_path_factory.mktemp("api"), migrated_db)
    with psycopg.connect(settings.database_url) as conn:
        load_all(conn, settings)
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestSystemEndpoints:
    def test_health(self, client):
        assert client.get("/health").json() == {"status": "ok"}

    def test_ready_with_loaded_data(self, client):
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["data_loaded"] is True

    def test_ready_reports_unreachable_database(self, tmp_path):
        settings = analytics_settings(
            tmp_path / "brokendb", "postgresql://atus:atus@127.0.0.1:6553/nope"
        )
        app = create_app(settings)
        with TestClient(app, raise_server_exceptions=False) as broken:
            response = broken.get("/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "database_unavailable"


class TestAnalysisEndpoints:
    BODY = {"activity": {"include": ["010101"], "label": "Sleeping"}, "years": [2023]}

    def test_estimate_matches_hand_computation(self, client):
        response = client.post("/api/v1/analysis/estimate", json=self.BODY)
        assert response.status_code == 200
        payload = response.json()
        assert payload["estimate"]["value"] == pytest.approx(75.0)
        assert payload["estimate"]["standard_error"] == pytest.approx(5.5901699, abs=1e-6)
        assert payload["n_respondents"] == 3
        assert payload["n_participants"] == 2
        assert payload["weighted_population_per_day"] == pytest.approx(4 / 365)
        assert payload["weight"]["bls_variable"] == "TUFNWGTP"
        assert any("harmonized" in w for w in payload["warnings"])

    def test_cache_hit_returns_identical_payload(self, client):
        first = client.post("/api/v1/analysis/estimate", json=self.BODY)
        second = client.post("/api/v1/analysis/estimate", json=self.BODY)
        assert second.headers["X-Cache"] == "hit"
        assert first.headers["X-Analysis-Key"] == second.headers["X-Analysis-Key"]
        assert first.json() == second.json()

    def test_equivalent_specs_share_a_cache_key(self, client):
        explicit_defaults = {
            **self.BODY,
            "population": {},
            "weights": "multiyear",
            "variance": "replicate",
            "confidence_level": 0.95,
        }
        a = client.post("/api/v1/analysis/estimate", json=self.BODY)
        b = client.post("/api/v1/analysis/estimate", json=explicit_defaults)
        assert a.headers["X-Analysis-Key"] == b.headers["X-Analysis-Key"]

    def test_different_population_gets_a_different_key(self, client):
        other = {**self.BODY, "population": {"sex": "female"}}
        a = client.post("/api/v1/analysis/estimate", json=self.BODY)
        b = client.post("/api/v1/analysis/estimate", json=other)
        assert a.headers["X-Analysis-Key"] != b.headers["X-Analysis-Key"]
        assert b.json()["estimate"]["value"] == pytest.approx(200 / 3)

    def test_pandemic_estimate_with_warning(self, client):
        body = {
            "activity": {"include": ["010101"]}, "years": [2019, 2020],
            "weights": "pandemic",
        }
        response = client.post("/api/v1/analysis/estimate", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["estimate"]["value"] == pytest.approx(150.0)
        assert payload["days_in_period"] == 312 + 313
        assert payload["n_respondents"] == 2  # zero-weight P8 excluded from the sample
        assert any("Pandemic weights" in w for w in payload["warnings"])

    def test_2020_multiyear_refused_with_guidance(self, client):
        body = {"activity": {"include": ["010101"]}, "years": [2020]}
        response = client.post("/api/v1/analysis/estimate", json=body)
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "unsupported_analysis"
        assert "pandemic" in error["message"]

    def test_trend_marks_2020_gap(self, client):
        body = {
            "activity": {"include": ["010101"]}, "years": [2019, 2020],
            "variance": "none",
        }
        response = client.post("/api/v1/analysis/trend", json=body)
        assert response.status_code == 200
        points = {p["year"]: p for p in response.json()["points"]}
        assert points[2019]["estimate"]["value"] == pytest.approx(240.0)  # P8 + P9
        assert points[2020]["estimate"] is None
        assert "TUFNWGTP" in points[2020]["unavailable_reason"]

    def test_compare_with_covariance_correct_difference(self, client):
        body = {
            "activity": {"include": ["010101"]}, "years": [2023],
            "group_a": {"sex": "male"}, "group_b": {"sex": "female"},
            "label_a": "men", "label_b": "women",
        }
        response = client.post("/api/v1/analysis/compare", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["group_a"]["estimate"]["value"] == pytest.approx(100.0)
        assert payload["group_b"]["estimate"]["value"] == pytest.approx(200 / 3)
        assert payload["difference"]["value"] == pytest.approx(100 - 200 / 3)
        assert payload["difference"]["standard_error"] == pytest.approx(11.785113, abs=1e-5)

    def test_unknown_activity_code_through_full_stack(self, client):
        body = {"activity": {"include": ["999999"]}, "years": [2023]}
        response = client.post("/api/v1/analysis/estimate", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unknown_activity"

    def test_response_spec_reproduces_the_result(self, client):
        """The `spec` embedded in a response is itself a valid request body
        that maps to the same analysis key and identical payload."""
        first = client.post("/api/v1/analysis/estimate", json=self.BODY)
        replay = client.post("/api/v1/analysis/estimate", json=first.json()["spec"])
        assert replay.status_code == 200
        assert replay.headers["X-Analysis-Key"] == first.headers["X-Analysis-Key"]
        assert replay.json() == first.json()

    def test_empty_population_is_insufficient_data(self, client):
        body = {**self.BODY, "population": {"age_min": 95}}
        response = client.post("/api/v1/analysis/estimate", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "insufficient_data"


class TestActivitiesEndpoints:
    def test_full_lexicon_listing(self, client):
        payload = client.get("/api/v1/activities").json()
        assert payload["total"] == 18 + 107 + 431
        levels = {entry["level"] for entry in payload["activities"]}
        assert levels == {1, 2, 3}

    def test_level_and_parent_filters(self, client):
        majors = client.get("/api/v1/activities", params={"level": 1}).json()
        assert majors["total"] == 18
        children = client.get("/api/v1/activities", params={"parent": "0101"}).json()
        assert {a["code"] for a in children["activities"]} == {"010101", "010102", "010199"}

    def test_search(self, client):
        payload = client.get("/api/v1/activities", params={"search": "television"}).json()
        codes = {a["code"] for a in payload["activities"]}
        assert "120303" in codes

    def test_search_wildcards_are_literal(self, client):
        payload = client.get("/api/v1/activities", params={"search": "%%"}).json()
        assert payload["total"] == 0

    def test_detail_with_hierarchy(self, client):
        payload = client.get("/api/v1/activities/120303").json()
        assert payload["name"].startswith("Television")
        assert [p["code"] for p in payload["parents"]] == ["12", "1203"]
        assert payload["descendant_leaf_count"] == 1
        tier = client.get("/api/v1/activities/12").json()
        assert tier["level"] == 1 and tier["descendant_leaf_count"] > 10

    def test_unknown_and_malformed_codes_are_404(self, client):
        for code in ("999999", "abc", "12345"):
            response = client.get(f"/api/v1/activities/{code}")
            assert response.status_code == 404, code
            assert response.json()["error"]["code"] == "activity_not_found"

    def test_presets(self, client):
        payload = client.get("/api/v1/activities/presets").json()
        names = {p["name"] for p in payload["presets"]}
        assert {"sleep", "watching_tv", "household_activities_bls_table"} <= names


class TestMetadataEndpoints:
    def test_population_metadata(self, client):
        payload = client.get("/api/v1/population/metadata").json()
        names = {d["name"] for d in payload["dimensions"]}
        assert {"age_min", "sex", "employment_status", "education_level",
                "region", "day_type"} <= names
        sex = next(d for d in payload["dimensions"] if d["name"] == "sex")
        assert sorted(sex["values"]) == ["female", "male"]
        assert "never treated as 'no'" in payload["missing_data_rule"]

    def test_meta_versions_and_capabilities(self, client):
        payload = client.get("/api/v1/meta").json()
        assert payload["api_version"] == "v1"
        assert payload["analytics_version"] == "0.2"
        assert payload["data"]["release"] == "0325"
        assert payload["data"]["years"] == [2019, 2020, 2023]   # fixture years
        assert payload["data"]["respondents"] == 6
        measures = {m["name"] for m in payload["capabilities"]["measures"]}
        assert "average_minutes_per_day" in measures
        assert payload["capabilities"]["unsupported"]

    def test_metadata_endpoints_are_cacheable(self, client):
        for path in ("/api/v1/meta", "/api/v1/activities", "/api/v1/population/metadata"):
            response = client.get(path)
            assert "max-age" in response.headers.get("Cache-Control", ""), path
