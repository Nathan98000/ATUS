"""DB-less API test fixtures: the app with every database-touching dependency
overridden, so contract/error tests run fast and offline."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atus_pipeline.api.app import create_app
from atus_pipeline.api.dependencies import get_connection, get_data_version, get_engine
from atus_pipeline.config import Settings


class StubEngine:
    """Programmable AnalysisEngine stand-in."""

    def __init__(self):
        self.estimate_behavior = None
        self.trend_behavior = None
        self.compare_behavior = None

    def _run(self, behavior, *args):
        if callable(behavior):
            return behavior(*args)
        raise AssertionError("stub behavior not configured")

    def estimate(self, spec):
        return self._run(self.estimate_behavior, spec)

    def trend(self, spec):
        return self._run(self.trend_behavior, spec)

    def compare(self, comparison):
        return self._run(self.compare_behavior, comparison)


@pytest.fixture
def stub_engine() -> StubEngine:
    return StubEngine()


class FakeVersionConnection:
    """Answers only the data-version query (used by the cache recheck); any
    other SQL in a DB-less test is a bug and fails loudly."""

    def execute(self, sql, params=None):
        if "ingestion_runs" in sql:
            class _Cursor:
                @staticmethod
                def fetchone():
                    return ("0325", 1)   # -> data version "0325:run1"
            return _Cursor()
        raise AssertionError(f"DB-less tests must not run SQL: {sql[:80]}")


@pytest.fixture
def app(stub_engine, tmp_path):
    settings = Settings(
        database_url="postgresql://nobody:nobody@127.0.0.1:1/none",
        data_dir=tmp_path, release="0325", log_level="WARNING",
    )
    application = create_app(settings, open_pool=False)
    application.dependency_overrides[get_engine] = lambda: stub_engine
    application.dependency_overrides[get_data_version] = lambda: "0325:run1"
    application.dependency_overrides[get_connection] = FakeVersionConnection
    return application


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
