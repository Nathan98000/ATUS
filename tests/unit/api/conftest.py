"""DB-less API test fixtures: the app with every database-touching dependency
overridden, so contract/error tests run fast and offline."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from atus_pipeline.api.app import create_app
from atus_pipeline.api.dependencies import get_database, get_engine_factory
from atus_pipeline.config import Settings
from atus_pipeline.database import versions


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
    """Answers only the data-version query (used by the cache recheck) and
    the loaded-data probe; any other SQL in a DB-less test is a bug and fails
    loudly."""

    def execute(self, sql, params=None):
        if "ingestion_runs" in sql:
            class _Cursor:
                @staticmethod
                def fetchone():
                    # to_jsonb(row) of the latest succeeded run
                    return ({"id": 1, "release": "0325", "data_version": "0325:run1"},)
            return _Cursor()
        if "EXISTS (SELECT 1 FROM atus.respondents)" in sql:
            class _Exists:
                @staticmethod
                def fetchone():
                    return (True,)
            return _Exists()
        raise AssertionError(f"DB-less tests must not run SQL: {sql[:80]}")


class FakeDatabase:
    """Stands in for ``api.database.Database``: no pool; each transaction
    gets a fresh connection from ``connection_factory``. Records every
    transaction so tests can assert that a request did no database work."""

    def __init__(self, connection_factory=FakeVersionConnection, data_version: str | None = None):
        self.connection_factory = connection_factory
        self.data_version = data_version  # None: ask the connection
        self.transactions: list[str] = []

    @contextmanager
    def transaction(self, kind: str):
        self.transactions.append(kind)
        yield self.connection_factory()

    def read_data_version(self) -> str:
        if self.data_version is not None:
            self.transactions.append("metadata")
            return self.data_version
        with self.transaction("metadata") as conn:
            return versions.read_data_version(conn)


@pytest.fixture
def fake_db() -> FakeDatabase:
    return FakeDatabase()


def make_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        database_url="postgresql://nobody:nobody@127.0.0.1:1/none",
        data_dir=tmp_path, release="0325", log_level="WARNING", **overrides,
    )


def make_app(settings: Settings, stub_engine: StubEngine, fake_db: FakeDatabase):
    application = create_app(settings, open_pool=False)
    application.dependency_overrides[get_engine_factory] = lambda: (lambda conn: stub_engine)
    application.dependency_overrides[get_database] = lambda: fake_db
    return application


@pytest.fixture
def app(stub_engine, fake_db, tmp_path):
    return make_app(make_settings(tmp_path), stub_engine, fake_db)


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
