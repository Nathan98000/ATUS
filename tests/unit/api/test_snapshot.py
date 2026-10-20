"""The in-process data snapshot: TTL, shared refreshes, invalidation, the
expected data version, the lexicon views, and the zero-statement paths it
enables (audit F14), including the engine dry run that answers domain 422s
without touching the database. No database: ``FakeDatabase`` records every
transaction, so "no database work" means an unchanged transaction list."""

from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.api.errors import DataVersionMismatchError
from atus_pipeline.api.snapshot import (
    ASCII_PROBE,
    DataSnapshot,
    IngestionRun,
    LexiconEntry,
    SnapshotStore,
    lexicon_label,
    load_snapshot,
)
from atus_pipeline.database import versions
from tests.unit.api.conftest import (
    FAKE_LEXICON,
    FakeDatabase,
    FakeVersionConnection,
    fake_snapshot,
    make_app,
    make_settings,
)
from tests.unit.api.test_contract import known_estimate_result

ESTIMATE = "/api/v1/analysis/estimate"
REMOTE = ("203.0.113.5", 40000)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CountingDatabase:
    """``read_snapshot`` only; counts calls and can block or fail."""

    def __init__(self, version="0325:run1"):
        self.version = version
        self.reads = 0
        self.fail: Exception | None = None
        self.gate: threading.Event | None = None

    def read_snapshot(self):
        self.reads += 1
        if self.gate is not None:
            assert self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        return fake_snapshot(self.version)


def run(coro):
    return asyncio.run(coro)


class TestSnapshotStore:
    def test_fresh_snapshot_is_reused_until_the_ttl(self):
        clock, db = Clock(), CountingDatabase()
        store = SnapshotStore(10.0, clock=clock)

        async def scenario():
            first = await store.get(db)
            clock.now += 9.9
            again = await store.get(db)
            clock.now += 0.2
            later = await store.get(db)
            return first, again, later

        first, again, later = run(scenario())
        assert first is again and later is not first
        assert db.reads == 2 and store.refreshes == 2

    def test_zero_ttl_reads_every_time(self):
        db = CountingDatabase()
        store = SnapshotStore(0.0)

        async def scenario():
            for _ in range(3):
                await store.get(db)

        run(scenario())
        assert db.reads == 3

    def test_concurrent_requests_share_one_refresh(self):
        db = CountingDatabase()
        db.gate = threading.Event()
        store = SnapshotStore(60.0)

        async def scenario():
            tasks = [asyncio.ensure_future(store.get(db)) for _ in range(5)]
            await asyncio.sleep(0.05)
            db.gate.set()
            return await asyncio.gather(*tasks)

        results = run(scenario())
        assert db.reads == 1
        assert all(result is results[0] for result in results)

    def test_a_failed_refresh_reaches_every_waiter_and_is_retried(self):
        db = CountingDatabase()
        db.fail = RuntimeError("database down")
        store = SnapshotStore(60.0)

        async def scenario():
            outcomes = await asyncio.gather(store.get(db), store.get(db), return_exceptions=True)
            db.fail = None
            return outcomes, await store.get(db)

        outcomes, recovered = run(scenario())
        assert all(isinstance(o, RuntimeError) for o in outcomes)
        assert store.refresh_failures == 1
        assert recovered.data_version == "0325:run1" and db.reads == 2

    def test_invalidate_forces_the_next_read(self):
        db = CountingDatabase()
        store = SnapshotStore(600.0)

        async def scenario():
            await store.get(db)
            store.invalidate()
            await store.get(db)

        run(scenario())
        assert db.reads == 2

    def test_expected_version_mismatch(self):
        db = CountingDatabase("0325:serving-new:abc")
        store = SnapshotStore(600.0, expected_version="0325:serving-old:def")

        async def scenario():
            with pytest.raises(DataVersionMismatchError):
                await store.get(db)
            with pytest.raises(DataVersionMismatchError):  # still fresh: no second read
                await store.get(db)
            return await store.get(db, check_expected=False)

        snapshot = run(scenario())
        assert snapshot.data_version == "0325:serving-new:abc" and db.reads == 1

    def test_age_and_current(self):
        clock, db = Clock(), CountingDatabase()
        store = SnapshotStore(60.0, clock=clock)
        assert store.current is None and store.age_s() is None
        run(store.get(db))
        clock.now += 7
        assert store.age_s() == pytest.approx(7.0)
        assert store.current.data_version == "0325:run1"


class TestDataSnapshot:
    def snapshot(self, **overrides) -> DataSnapshot:
        return fake_snapshot(**overrides)

    def test_listing_filters_keep_the_database_order(self):
        snapshot = self.snapshot()
        everything = snapshot.search_lexicon(level=None, parent=None, search=None)
        assert [e.code for e in everything] == [e.code for e in FAKE_LEXICON]
        majors = snapshot.search_lexicon(level=1, parent=None, search=None)
        assert [e.code for e in majors] == ["01", "12"]
        children = snapshot.search_lexicon(level=None, parent="0101", search=None)
        assert [e.code for e in children] == ["010101", "010102"]

    def test_search_is_a_case_insensitive_substring(self):
        snapshot = self.snapshot()
        hits = snapshot.search_lexicon(level=None, parent=None, search="SLEEP")
        assert [e.code for e in hits] == ["0101", "010101", "010102"]
        assert snapshot.search_lexicon(level=3, parent=None, search="sleep")[0].code == "010101"
        # LIKE wildcards are literal characters
        assert snapshot.search_lexicon(level=None, parent=None, search="%%") == []
        assert snapshot.search_lexicon(level=None, parent=None, search="__") == []

    def test_search_matches_the_database_lowered_names(self):
        """The name side is never lowered in Python: what the database's
        lower() returned is what the term must be a substring of."""
        lexicon = (
            LexiconEntry("09", "Home Décor", 1, None, None, "home décor"),
            LexiconEntry("10", "KELVIN", 1, None, None, "kelvin-as-the-db-lowers-it"),
        )
        snapshot = DataSnapshot(
            data_version="v", run=None, years=(), respondents=0, lexicon=lexicon,
        )
        found = snapshot.search_lexicon(level=None, parent=None, search="HOME D")
        assert [e.code for e in found] == ["09"]
        found = snapshot.search_lexicon(level=None, parent=None, search="AS-THE-DB")
        assert [e.code for e in found] == ["10"]

    def test_non_ascii_terms_go_to_the_database(self):
        snapshot = self.snapshot()
        assert snapshot.search_lexicon(level=None, parent=None, search="décor") is None
        assert snapshot.search_lexicon(level=None, parent=None, search="İs") is None

    def test_unusual_database_lowering_sends_every_search_to_the_database(self):
        snapshot = self.snapshot(ascii_lowering_matches=False)
        assert snapshot.search_lexicon(level=None, parent=None, search="sleep") is None
        assert snapshot.search_lexicon(level=1, parent=None, search=None) is not None

    def test_detail(self):
        snapshot = self.snapshot()
        leaf = snapshot.detail("010102")
        assert leaf.entry.name == "Sleeplessness"
        assert [p.code for p in leaf.parents] == ["01", "0101"]
        assert leaf.children == () and leaf.descendant_leaf_count == 1
        major = snapshot.detail("01")
        assert major.parents == () and [c.code for c in major.children] == ["0101"]
        assert major.descendant_leaf_count == 2
        assert snapshot.detail("999999") is None

    def test_lexicon_label_from_the_release(self):
        assert lexicon_label("0325") == "2003-25"
        assert lexicon_label("0326") == "2003-26"
        assert lexicon_label(None) is None and lexicon_label("bogus") is None
        assert self.snapshot().lexicon_label == "2003-25"

    def test_engine_parts(self):
        assert self.snapshot().engine_kwargs()["loaded_years"][0] == 2003
        empty = self.snapshot(data_version=versions.UNLOADED)
        assert empty.engine_kwargs() == {"resolver": None, "loaded_years": None}
        assert not empty.is_loaded


class LexiconConnection:
    """Answers exactly the statements ``load_snapshot`` sends."""

    def __init__(self, *, leaves=True, lower=str.lower):
        self.leaves = leaves
        self.lower = lower
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        rows: list[tuple]
        if "to_jsonb" in sql:
            rows = [({"id": 3, "release": "0325", "data_version": "0325:load-3"},)]
        elif "FROM atus.ingestion_runs" in sql:
            rows = [(3, "0325", None)]
        elif "DISTINCT data_year" in sql:
            rows = [(2023,), (2024,)]
        elif "count(*) FROM atus.respondents" in sql:
            rows = [(42,)]
        elif "lower(name)" in sql:
            rows = [(*e.row(), e.name.lower()) for e in FAKE_LEXICON
                    if self.leaves or e.level < 3]
        elif sql == "SELECT lower(%s)":
            rows = [(self.lower(params[0]),)]
        elif sql == "SELECT code FROM atus.activity_tier1":
            rows = [(e.code,) for e in FAKE_LEXICON if e.level == 1]
        elif sql == "SELECT code FROM atus.activity_tier2":
            rows = [(e.code,) for e in FAKE_LEXICON if e.level == 2]
        elif sql == "SELECT code, name FROM atus.activity_codes":
            rows = [(e.code, e.name) for e in FAKE_LEXICON if e.level == 3]
        else:
            raise AssertionError(f"unexpected SQL: {sql[:60]}")
        return _Rows(rows)


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class TestLoadSnapshot:
    def test_reads_every_part(self):
        conn = LexiconConnection()
        snapshot = load_snapshot(conn)
        assert snapshot.data_version == "0325:load-3"
        assert snapshot.run == IngestionRun(3, "0325", None)
        assert snapshot.years == (2023, 2024) and snapshot.respondents == 42
        assert len(snapshot.lexicon) == len(FAKE_LEXICON)
        assert snapshot.ascii_lowering_matches
        assert isinstance(snapshot.resolver, ActivityResolver)
        assert len(conn.statements) == 9  # 6 snapshot reads + the resolver's 3

    def test_no_leaves_leaves_the_resolver_to_the_engine(self):
        snapshot = load_snapshot(LexiconConnection(leaves=False))
        assert snapshot.resolver is None

    def test_a_turkish_style_lowering_disables_python_search(self):
        snapshot = load_snapshot(LexiconConnection(lower=lambda s: s.replace("I", "ı").lower()))
        assert not snapshot.ascii_lowering_matches
        assert ASCII_PROBE.isascii() and len(ASCII_PROBE) == 95


# --------------------------------------------------------------------------- #
# through HTTP                                                                 #
# --------------------------------------------------------------------------- #


def resolver_snapshot_db(**overrides) -> FakeDatabase:
    """A fake database whose snapshot carries a real ActivityResolver over
    the fake lexicon (so the engine dry run can decide)."""
    resolver = ActivityResolver(LexiconConnection())
    return FakeDatabase(data_version="0325:run1", resolver=resolver, **overrides)


@pytest.fixture
def build(stub_engine, tmp_path):
    clients = []

    def _build(fake_db=None, client_kwargs=None, **overrides):
        fake_db = fake_db or FakeDatabase()
        if stub_engine.estimate_behavior is None:
            stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        overrides.setdefault("api_snapshot_ttl_s", 600.0)
        app = make_app(make_settings(tmp_path, **overrides), stub_engine, fake_db)
        client = TestClient(app, raise_server_exceptions=False, **(client_kwargs or {}))
        client.__enter__()
        clients.append(client)
        return client, fake_db, app

    yield _build
    for client in clients:
        client.__exit__(None, None, None)


BODY = {"activity": {"preset": "sleep"}, "years": [2023], "variance": "none"}


class TestZeroStatementPaths:
    def test_hits_metadata_and_errors_cost_nothing_once_the_snapshot_is_fresh(self, build):
        client, fake_db, _ = build()
        assert client.post(ESTIMATE, json=BODY).headers["X-Cache"] == "miss"
        assert fake_db.transactions == ["metadata", "analysis"]  # first read + compute
        before = list(fake_db.transactions)
        responses = [
            client.post(ESTIMATE, json=BODY),                                  # hit
            client.post(ESTIMATE, json={**BODY, "years": []}),                 # 422 schema
            client.post(ESTIMATE, content=b"{", headers={"Content-Type": "application/json"}),
            client.post(ESTIMATE, content=b"x", headers={"Content-Type": "text/plain"}),
            client.get("/api/v1/meta"),
            client.get("/api/v1/activities"),
            client.get("/api/v1/activities", params={"search": "sleep", "level": 3}),
            client.get("/api/v1/activities/0101"),
            client.get("/api/v1/activities/999999"),
            client.get("/api/v1/activities/presets"),
            client.get("/api/v1/population/metadata"),
            client.head("/api/v1/meta"),
        ]
        assert [r.status_code for r in responses] == [
            200, 422, 400, 415, 200, 200, 200, 200, 404, 200, 200, 200]
        assert responses[0].headers["X-Cache"] == "hit"
        assert fake_db.transactions == before

    def test_a_stale_snapshot_is_read_once_per_request(self, build):
        client, fake_db, _ = build(api_snapshot_ttl_s=0.0)
        client.get("/api/v1/meta")
        client.get("/api/v1/activities")
        assert fake_db.transactions == ["metadata", "metadata"]

    def test_non_ascii_search_runs_the_sql(self, build):
        class LexiconSql(FakeVersionConnection):
            def execute(self, sql, params=None):
                if "ILIKE" in sql:
                    return _Rows([("0902", "Home Décor", 2, "09", None)])
                return super().execute(sql, params)

        client, fake_db, _ = build(fake_db=FakeDatabase(LexiconSql, data_version="0325:run1"))
        client.get("/api/v1/meta")
        response = client.get("/api/v1/activities", params={"search": "décor"})
        assert response.json()["activities"][0]["code"] == "0902"
        assert fake_db.transactions == ["metadata", "metadata"]

    def test_metadata_carries_the_data_version(self, build):
        client, _, _ = build()
        meta = client.get("/api/v1/meta")
        assert meta.headers["X-Data-Version"] == "0325:run1"
        assert meta.headers["X-Analytics-Version"] == "0.2"
        assert meta.json()["data"]["release"] == "0325"
        assert client.get("/api/v1/activities").headers["X-Data-Version"] == "0325:run1"
        assert client.get("/api/v1/activities/01").headers["X-Data-Version"] == "0325:run1"

    def test_detail_and_not_found_from_the_snapshot(self, build):
        client, _, _ = build()
        detail = client.get("/api/v1/activities/010102").json()
        assert [p["code"] for p in detail["parents"]] == ["01", "0101"]
        assert detail["harmonization_note"] == "harmonized"
        missing = client.get("/api/v1/activities/999999").json()["error"]
        assert missing["code"] == "activity_not_found"
        assert missing["message"] == (
            "Activity code '999999' does not exist in the 2003-25 activity lexicon."
        )

    def test_malformed_code_before_any_snapshot_is_release_neutral(self, build):
        client, fake_db, _ = build()
        error = client.get("/api/v1/activities/abc").json()["error"]
        assert error["message"] == "Activity code 'abc' does not exist in the activity lexicon."
        assert fake_db.transactions == []


class TestDryRun:
    """Domain errors the engine raises before its first statement cost no
    token, no lane and no statement."""

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"activity": {"include": ["999999"]}, "years": [2023]}, "unknown_activity"),
            ({"activity": {"include": ["0101"]}, "years": [2020]}, "unsupported_analysis"),
            ({"activity": {"include": ["0101"]}, "years": [2099]}, "invalid_spec"),
        ],
    )
    def test_domain_422_without_database_or_tokens(self, build, stub_engine, body, code):
        calls = []
        stub_engine.estimate_behavior = lambda spec: calls.append(spec)
        client, fake_db, app = build(fake_db=resolver_snapshot_db(),
                                     client_kwargs={"client": REMOTE})
        client.get("/api/v1/meta")  # read the snapshot
        before = list(fake_db.transactions)
        response = client.post(ESTIMATE, json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == code
        assert fake_db.transactions == before and calls == []
        protection = app.state.protection
        assert protection.daily_cap.used_today() == 0
        assert app.state.runner.computations == 0

    def test_valid_requests_still_compute(self, build):
        client, fake_db, _ = build(fake_db=resolver_snapshot_db())
        response = client.post(ESTIMATE, json={"activity": {"include": ["0101"]},
                                               "years": [2023]})
        assert response.status_code == 200 and response.headers["X-Cache"] == "miss"
        assert fake_db.transactions[-1] == "analysis"

    def test_the_dry_run_never_answers_for_the_engine_double(self, build, stub_engine):
        """Without a resolver the dry run stops at the resolver's first query
        and the (stub) computation decides."""
        client, _, _ = build()
        assert client.post(ESTIMATE, json={"activity": {"include": ["999999"]},
                                           "years": [2023]}).status_code == 200


class TestExpectedDataVersion:
    def test_every_data_route_refuses_a_mismatch(self, build):
        client, _, _ = build(expected_data_version="0325:serving-expected")
        for method, path in (("POST", ESTIMATE), ("GET", "/api/v1/meta"),
                             ("GET", "/api/v1/activities"), ("GET", "/api/v1/activities/01")):
            response = client.request(method, path, json=BODY if method == "POST" else None)
            assert response.status_code == 503, path
            error = response.json()["error"]
            assert error["code"] == "data_version_mismatch"
            assert error["details"]["scope"] == "global"
            assert response.headers["Retry-After"] == "60"
            assert "serving-expected" not in response.text  # versions stay in the log
        # data-independent vocabularies keep working
        assert client.get("/api/v1/population/metadata").status_code == 200
        assert client.get("/api/v1/activities/presets").status_code == 200

    def test_matching_version_is_served(self, build):
        client, _, _ = build(expected_data_version="0325:run1")
        assert client.post(ESTIMATE, json=BODY).status_code == 200

    def test_version_change_during_a_computation_is_refused_when_bound(self, build):
        """Snapshot says run0 (= expected); the computation's own recheck sees
        run1: with a bound version the result is refused, not returned."""
        client, fake_db, _ = build(fake_db=FakeDatabase(data_version="0325:run0"),
                                   expected_data_version="0325:run0")
        response = client.post(ESTIMATE, json=BODY)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "data_version_mismatch"
        # the snapshot was invalidated: the next request reads it again
        count = fake_db.transactions.count("metadata")
        client.get("/api/v1/meta")
        assert fake_db.transactions.count("metadata") == count + 1

    def test_unbound_version_change_bypasses_and_invalidates(self, build):
        client, fake_db, _ = build(fake_db=FakeDatabase(data_version="0325:run0"))
        assert client.post(ESTIMATE, json=BODY).headers["X-Cache"] == "bypass"
        count = fake_db.transactions.count("metadata")
        client.get("/api/v1/meta")
        assert fake_db.transactions.count("metadata") == count + 1
