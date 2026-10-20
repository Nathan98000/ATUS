"""DB-less API test fixtures: the app with every database-touching dependency
overridden, so contract/error tests run fast and offline."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from atus_pipeline.api import observability
from atus_pipeline.api.app import create_app
from atus_pipeline.api.dependencies import get_database, get_engine_factory
from atus_pipeline.api.snapshot import DataSnapshot, IngestionRun, LexiconEntry
from atus_pipeline.config import Settings
from atus_pipeline.database import versions

# A small lexicon for DB-less tests (a real-looking slice of the 2003-25 tree).
FAKE_LEXICON = (
    LexiconEntry("01", "Personal Care", 1, None, None),
    LexiconEntry("0101", "Sleeping", 2, "01", None),
    LexiconEntry("010101", "Sleeping", 3, "0101", None),
    LexiconEntry("010102", "Sleeplessness", 3, "0101", "harmonized"),
    LexiconEntry("12", "Socializing, Relaxing, and Leisure", 1, None, None),
    LexiconEntry("1203", "Relaxing and Leisure", 2, "12", None),
    LexiconEntry("120303", "Television and movies (not religious)", 3, "1203", None),
)


def fake_snapshot(data_version: str = "0325:run1", **overrides) -> DataSnapshot:
    lexicon = tuple(
        LexiconEntry(e.code, e.name, e.level, e.parent_code, e.harmonization_note,
                     e.name.lower())
        for e in overrides.pop("lexicon", FAKE_LEXICON)
    )
    loaded = data_version != versions.UNLOADED
    fields = {
        "data_version": data_version,
        "run": IngestionRun(1, "0325", None) if loaded else None,
        # every year DB-less tests use, so the dry run accepts them
        "years": tuple(range(2003, 2026)) if loaded else (),
        "respondents": 6 if loaded else 0,
        "lexicon": lexicon,
        **overrides,
    }
    return DataSnapshot(**fields)


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
    loudly. Every statement is counted against the current request, like the
    real pool's cursors do."""

    def execute(self, sql, params=None):
        observability.note_statement()
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
    transaction so tests can assert that a request did no database work.

    ``read_snapshot`` (one "metadata" transaction) returns
    :func:`fake_snapshot` for ``data_version``, or for the version the
    connection reports when ``data_version`` is None; ``snapshot_overrides``
    change its other fields. Setting ``snapshot_error`` makes the read fail
    with it (a database outage)."""

    def __init__(self, connection_factory=FakeVersionConnection, data_version: str | None = None,
                 **snapshot_overrides):
        self.connection_factory = connection_factory
        self.data_version = data_version  # None: ask the connection
        self.snapshot_overrides = snapshot_overrides
        self.snapshot_error: BaseException | None = None
        self.transactions: list[str] = []

    @contextmanager
    def transaction(self, kind: str, *, repeatable_read: bool = False):
        self.transactions.append(kind)
        with observability.database_time():
            yield self.connection_factory()

    def read_data_version(self) -> str:
        if self.data_version is not None:
            self.transactions.append("metadata")
            return self.data_version
        with self.transaction("metadata") as conn:
            return versions.read_data_version(conn)

    def read_snapshot(self) -> DataSnapshot:
        if self.snapshot_error is not None:
            self.transactions.append("metadata")
            raise self.snapshot_error
        if self.data_version is not None:
            with self.transaction("metadata"):
                version = self.data_version
        else:
            with self.transaction("metadata") as conn:
                version = versions.read_data_version(conn)
        return fake_snapshot(version, **self.snapshot_overrides)


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
    application.dependency_overrides[get_engine_factory] = (
        lambda: (lambda conn, **_snapshot_parts: stub_engine)
    )
    application.dependency_overrides[get_database] = lambda: fake_db
    return application


@pytest.fixture
def app(stub_engine, fake_db, tmp_path):
    return make_app(make_settings(tmp_path), stub_engine, fake_db)


@pytest.fixture
def client(app):
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
