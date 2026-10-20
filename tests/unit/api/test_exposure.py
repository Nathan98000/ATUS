"""What the API exposes: the documentation switch (F43), the published
OpenAPI snapshot, release-neutral texts (F23), error bodies that never leak
internals (I07), and the engine's optional snapshot parts."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.analytics.errors import InvalidSpecError
from atus_pipeline.analytics.spec import ActivitySelector, AnalysisSpec, Measure
from atus_pipeline.api.app import create_app
from atus_pipeline.config import Settings
from tests.unit.api.conftest import FakeDatabase, make_app, make_settings

REPO = Path(__file__).resolve().parents[3]
DOCS_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")
ESTIMATE = "/api/v1/analysis/estimate"
BODY = {"activity": {"preset": "sleep"}, "years": [2023], "variance": "none"}


class TestDocumentationSwitch:
    def test_off_removes_every_docs_route(self, stub_engine, tmp_path):
        app = make_app(make_settings(tmp_path, api_docs=False), stub_engine, FakeDatabase())
        with TestClient(app) as client:
            for path in DOCS_PATHS:
                response = client.get(path)
                assert response.status_code == 404, path
                assert response.json()["error"]["code"] == "not_found"

    def test_on_serves_them_in_development(self, stub_engine, tmp_path):
        app = make_app(make_settings(tmp_path), stub_engine, FakeDatabase())
        with TestClient(app) as client:
            for path in DOCS_PATHS:
                assert client.get(path).status_code == 200, path

    def test_the_schema_is_exported_with_docs_off(self, tmp_path):
        """scripts/export_openapi.py needs no docs route: the schema of a
        production-configured app is the published one."""
        on = create_app(make_settings(tmp_path), open_pool=False).openapi()
        off = create_app(make_settings(tmp_path, api_docs=False), open_pool=False).openapi()
        assert on == off

    def test_published_snapshot_is_current(self):
        """frontend/openapi.json is what scripts/export_openapi.py writes;
        regenerate it (and the frontend types) after any schema change."""
        settings = Settings(
            database_url="postgresql://unused:unused@localhost:1/unused",
            data_dir=Path("data"), release="0325", log_level="WARNING",
        )
        expected = json.dumps(create_app(settings, open_pool=False).openapi(), indent=1) + "\n"
        published = (REPO / "frontend" / "openapi.json").read_text(encoding="utf-8")
        assert published == expected, "run: python scripts/export_openapi.py"


class TestReleaseNeutralText:
    def test_api_texts_name_no_release(self, tmp_path):
        spec = create_app(make_settings(tmp_path), open_pool=False).openapi()
        assert "2003-2025" not in spec["info"]["description"]
        listing = spec["paths"]["/api/v1/activities"]["get"]["description"]
        assert "2003-25" not in listing and "431" not in listing


class TestNoInternalsInErrors:
    """I07: no stack traces, SQL, paths or connection strings in responses."""

    LEAKY = ("Traceback", "SELECT", "/Users/", "/home/", "site-packages", "postgresql://",
             "password", "atus.respondents", "psycopg")

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError('SELECT * FROM atus.respondents -- /home/app/site-packages/x.py'),
            psycopg.OperationalError(
                "connection to server at db.internal failed: password authentication failed "
                "for user atus_api (postgresql://atus_api:pw@db.internal/atus)"),
            psycopg.errors.QueryCanceled("canceling statement due to statement timeout\n"
                                         "CONTEXT: SQL statement SELECT pg_sleep(9)"),
            psycopg.DataError('invalid input syntax for type integer: "x" in SELECT 1'),
        ],
    )
    def test_error_bodies_are_generic(self, client, stub_engine, exc):
        def raise_it(spec):
            raise exc

        stub_engine.estimate_behavior = raise_it
        response = client.post(ESTIMATE, json=BODY)
        assert response.status_code in (500, 503)
        for marker in self.LEAKY:
            assert marker not in response.text, marker

    def test_framework_errors_are_generic(self, client):
        for response in (client.get("/api/v1/nope"), client.put("/health"),
                         client.post(ESTIMATE, content=b"{bad",
                                     headers={"Content-Type": "application/json"})):
            for marker in self.LEAKY:
                assert marker not in response.text, (marker, response.text)


class _NoDatabase:
    def __getattr__(self, name):
        raise AssertionError(f"the engine used the connection ({name})")


class TestEngineSnapshotParts:
    """The additive constructor arguments: supplied years are used instead
    of a query, an empty tuple means "not supplied"."""

    def spec(self, year: int) -> AnalysisSpec:
        return AnalysisSpec(
            measure=Measure.AVERAGE_MINUTES_PER_DAY,
            activity=ActivitySelector(include=("0101",)), years=(year,),
        )

    def test_supplied_years_are_used(self):
        engine = AnalysisEngine(_NoDatabase(), loaded_years=(2023,))
        with pytest.raises(InvalidSpecError, match="2024"):
            engine.estimate(self.spec(2024))

    def test_empty_years_are_read_from_the_database(self):
        engine = AnalysisEngine(_NoDatabase(), loaded_years=())
        with pytest.raises(AssertionError, match="the engine used the connection"):
            engine.estimate(self.spec(2023))
