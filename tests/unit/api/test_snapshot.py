"""The in-process data snapshot: TTL, shared refreshes, invalidation,
pinning to the expected data version, stale-if-error, the lexicon views and
the database-lowering search, and the zero-statement paths it enables (audit
F14), including the engine dry run that answers domain 422s without touching
the database. No database: ``FakeDatabase`` records every transaction, so "no
database work" means an unchanged transaction list."""

from __future__ import annotations

import asyncio
import sys
import threading
import unicodedata

import pytest
from fastapi.testclient import TestClient

from atus_pipeline.analytics.activities import ActivityResolver
from atus_pipeline.api.dependencies import get_engine_factory
from atus_pipeline.api.errors import DataVersionMismatchError
from atus_pipeline.api.snapshot import (
    CASED_RANGES,
    CONTEXT_PROBE,
    DataSnapshot,
    IngestionRun,
    LexiconEntry,
    SnapshotStore,
    lexicon_label,
    load_snapshot,
    lower_with,
    python_lowering,
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

    def test_invalidate_during_a_read_does_not_keep_that_read_fresh(self):
        """Regression: a computation that saw the data version change calls
        invalidate() from its worker thread; a snapshot read already in
        progress (which may have seen the old data) then stored itself as
        fresh for a whole TTL, and invalidate() was lost."""
        db = CountingDatabase()
        db.gate = threading.Event()
        store = SnapshotStore(600.0)

        async def scenario():
            waiting = asyncio.ensure_future(store.get(db))
            await asyncio.sleep(0.05)  # the read is in progress
            store.invalidate()
            db.gate.set()
            during = await waiting
            fresh = store.is_fresh()
            after = await store.get(db)
            return during, fresh, after

        during, fresh, after = run(scenario())
        assert during.data_version == "0325:run1"  # its waiters still get it
        assert not fresh
        assert db.reads == 2 and after is not during
        assert store.is_fresh()  # an uninterrupted read is kept

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


class TestPinnedSnapshot:
    """With ATUS_EXPECTED_DATA_VERSION set, a snapshot of that version is
    never read again for its age: a flood of metadata or cache-hit requests
    cannot keep a scale-to-zero database awake (D2's quota note)."""

    def test_a_matching_snapshot_never_expires(self):
        clock, db = Clock(), CountingDatabase("0325:serving-x:abc")
        store = SnapshotStore(600.0, expected_version="0325:serving-x:abc", clock=clock)

        async def scenario():
            first = await store.get(db)
            for _ in range(50):
                clock.now += 3600  # days of requests, one an hour
                assert await store.get(db) is first

        run(scenario())
        assert db.reads == 1 and store.is_pinned() and not store.is_fresh()

    def test_invalidate_unpins(self):
        clock, db = Clock(), CountingDatabase("v1")
        store = SnapshotStore(600.0, expected_version="v1", clock=clock)

        async def scenario():
            await store.get(db)
            store.invalidate()
            assert not store.is_pinned()
            await store.get(db)
            clock.now += 10_000
            await store.get(db)

        run(scenario())
        assert db.reads == 2  # re-read after the invalidation, then pinned again

    def test_a_mismatched_snapshot_is_reread_after_its_ttl(self):
        clock, db = Clock(), CountingDatabase("v-old")
        store = SnapshotStore(600.0, expected_version="v-new", clock=clock)

        async def scenario():
            with pytest.raises(DataVersionMismatchError):
                await store.get(db)
            clock.now += 599
            with pytest.raises(DataVersionMismatchError):
                await store.get(db)
            db.version = "v-new"
            clock.now += 2
            return await store.get(db)

        assert run(scenario()).data_version == "v-new"
        assert db.reads == 2 and store.is_pinned()

    def test_without_an_expected_version_the_ttl_applies(self):
        clock, db = Clock(), CountingDatabase()
        store = SnapshotStore(600.0, clock=clock)

        async def scenario():
            await store.get(db)
            clock.now += 601
            await store.get(db)

        run(scenario())
        assert db.reads == 2 and not store.is_pinned()

    def test_strict_reads_ignore_the_pin(self):
        clock, db = Clock(), CountingDatabase("v1")
        store = SnapshotStore(600.0, expected_version="v1", clock=clock)

        async def scenario():
            await store.get(db)
            clock.now += 601
            await store.get(db, strict=True)

        run(scenario())
        assert db.reads == 2


class TestStaleIfError:
    """A failed read serves the previous trusted snapshot (A's wave-1 note:
    cache hits and metadata keep working while the database is down) and
    waits before trying the database again."""

    def test_a_failed_read_serves_the_previous_snapshot(self):
        clock, db = Clock(), CountingDatabase()
        store = SnapshotStore(60.0, clock=clock, stale_retry_s=30.0)

        async def scenario():
            first = await store.get(db)
            clock.now += 61
            db.fail = RuntimeError("database down")
            again = await store.get(db)            # read fails: the old one
            clock.now += 29
            during = await store.get(db)           # within the retry wait: no read
            clock.now += 2
            db.fail = None
            recovered = await store.get(db)        # tried again, and it works
            return first, again, during, recovered

        first, again, during, recovered = run(scenario())
        assert again is first and during is first and recovered is not first
        assert db.reads == 3 and store.refresh_failures == 1
        assert store.stale_serves == 2
        assert store.is_fresh()

    def test_the_retry_wait_is_never_longer_than_the_ttl(self):
        clock, db = Clock(), CountingDatabase()
        store = SnapshotStore(2.0, clock=clock, stale_retry_s=30.0)

        async def scenario():
            await store.get(db)
            clock.now += 3
            db.fail = RuntimeError("down")
            await store.get(db)
            clock.now += 2.1
            await store.get(db)

        run(scenario())
        assert store.stale_retry_s == 2.0 and db.reads == 3

    def test_no_previous_snapshot_means_the_error(self):
        db = CountingDatabase()
        db.fail = RuntimeError("database down")
        store = SnapshotStore(60.0)
        with pytest.raises(RuntimeError):
            run(store.get(db))
        assert store.stale_serves == 0

    def test_an_invalidated_snapshot_is_never_served_stale(self):
        """invalidate() means the data changed: the old snapshot's version
        would key cache hits to results of the old data."""
        db = CountingDatabase()
        store = SnapshotStore(60.0)

        async def scenario():
            await store.get(db)
            store.invalidate()
            db.fail = RuntimeError("database down")
            await store.get(db)

        with pytest.raises(RuntimeError):
            run(scenario())

    def test_strict_reads_never_serve_stale(self):
        clock, db = Clock(), CountingDatabase()
        store = SnapshotStore(60.0, clock=clock)

        async def scenario():
            await store.get(db)
            clock.now += 61
            db.fail = RuntimeError("database down")
            with pytest.raises(RuntimeError):
                await store.get(db, strict=True)
            return await store.get(db)  # the ordinary path still falls back

        assert run(scenario()).data_version == "0325:run1"

    def test_a_stale_snapshot_still_checks_the_expected_version(self):
        clock, db = Clock(), CountingDatabase("v-old")
        store = SnapshotStore(60.0, expected_version="v-new", clock=clock)

        async def scenario():
            with pytest.raises(DataVersionMismatchError):
                await store.get(db)
            clock.now += 61
            db.fail = RuntimeError("down")
            with pytest.raises(DataVersionMismatchError):
                await store.get(db)

        run(scenario())


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

    def test_non_ascii_terms_are_lowered_with_the_database_table(self):
        lexicon = (
            LexiconEntry("09", "Home Décor", 1, None, None, "home décor"),
            LexiconEntry("10", "Islands", 1, None, None, "islands"),
        )
        # A libc collation lowers İ to i (Python: i + combining dot).
        libc = {**python_lowering(), "\u0130": "i"}
        snapshot = DataSnapshot(data_version="v", run=None, years=(), respondents=0,
                                lexicon=lexicon, lowering=libc)
        for term in ("décor", "DÉCOR", "e D\u00c9"):
            assert [e.code for e in snapshot.search_lexicon(
                level=None, parent=None, search=term)] == ["09"], term
        assert [e.code for e in snapshot.search_lexicon(
            level=None, parent=None, search="\u0130SLAND")] == ["10"]
        # The same term under Python's lowering ("i̇") matches nothing: the
        # table decides, not Python.
        python = DataSnapshot(data_version="v", run=None, years=(), respondents=0,
                              lexicon=lexicon)
        assert python.search_lexicon(level=None, parent=None, search="\u0130SLAND") == []
        assert snapshot.search_lexicon(level=None, parent=None, search="ΣΑ") == []

    def test_a_turkish_style_table_is_followed_exactly(self):
        lexicon = (LexiconEntry("01", "Ice", 1, None, None, "ıce"),)
        turkish = {**python_lowering(), "I": "ı"}
        snapshot = DataSnapshot(data_version="v", run=None, years=(), respondents=0,
                                lexicon=lexicon, lowering=turkish)
        assert [e.code for e in snapshot.search_lexicon(
            level=None, parent=None, search="ICE")] == ["01"]
        assert snapshot.search_lexicon(level=None, parent=None, search="ice") == []

    def test_context_sensitive_lowering_sends_every_search_to_the_database(self):
        snapshot = self.snapshot(lowering=None)
        assert snapshot.search_lexicon(level=None, parent=None, search="sleep") is None
        assert snapshot.search_lexicon(level=None, parent=None, search="décor") is None
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


class TestCasedRanges:
    """Every code point outside CASED_RANGES lowers to itself (here: in
    Python's Unicode data; the integration suite checks the database's)."""

    def test_python_lowers_nothing_outside_the_ranges(self):
        def inside(cp):
            return any(lo <= cp <= hi for lo, hi in CASED_RANGES)

        outside = [
            cp for cp in range(sys.maxunicode + 1)
            if not 0xD800 <= cp <= 0xDFFF and not inside(cp) and chr(cp).lower() != chr(cp)
        ]
        assert outside == [], (unicodedata.unidata_version, [hex(cp) for cp in outside[:5]])

    def test_ranges_are_sorted_disjoint_and_valid(self):
        flat = [bound for pair in CASED_RANGES for bound in pair]
        assert flat == sorted(flat) and len(set(flat)) == len(flat)
        assert all(0 < lo <= hi <= sys.maxunicode for lo, hi in CASED_RANGES)
        assert not any(lo <= 0xDFFF and hi >= 0xD800 for lo, hi in CASED_RANGES)

    def test_lower_with(self):
        assert lower_with({"A": "a", "\u0130": "i"}, "A\u0130b") == "aib"
        assert lower_with(python_lowering(), "Home DÉCOR") == "home décor"

    def test_the_context_probe_tells_icu_from_libc(self):
        table = python_lowering()
        per_character = lower_with(table, CONTEXT_PROBE)
        # Python's str.lower applies Final_Sigma, as ICU does: they differ.
        assert per_character != CONTEXT_PROBE.lower()
        assert "\u03c2" in CONTEXT_PROBE.lower() and "\u03c2" not in per_character


class LexiconConnection:
    """Answers exactly the statements ``load_snapshot`` sends."""

    def __init__(self, *, leaves=True, lower=str.lower, whole=None):
        self.leaves = leaves
        self.lower = lower  # the "database's" lowering of one character
        # ... and of a whole string: character by character (libc) by default
        self.whole = whole or (lambda text: "".join(self.lower(ch) for ch in text))
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
        elif "generate_series" in sql:
            table = {}
            for lo, hi in zip(params["lo"], params["hi"], strict=True):
                for cp in range(lo, hi + 1):
                    if 0xD800 <= cp <= 0xDFFF:
                        continue
                    lowered = self.lower(chr(cp))
                    if lowered != chr(cp):
                        table[cp] = lowered
            rows = [(self.whole(params["probe"]), sorted(table),
                     [table[cp] for cp in sorted(table)])]
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
        assert snapshot.lowering == python_lowering()
        assert isinstance(snapshot.resolver, ActivityResolver)
        assert len(conn.statements) == 9  # 6 snapshot reads + the resolver's 3

    def test_no_leaves_leaves_the_resolver_to_the_engine(self):
        snapshot = load_snapshot(LexiconConnection(leaves=False))
        assert snapshot.resolver is None

    def test_a_turkish_style_lowering_is_read_into_the_table(self):
        snapshot = load_snapshot(LexiconConnection(lower=lambda s: s.replace("I", "ı").lower()))
        assert snapshot.lowering["I"] == "ı"
        hits = snapshot.search_lexicon(level=None, parent=None, search="SLEEP")
        assert [e.code for e in hits] == ["0101", "010101", "010102"]

    def test_context_sensitive_lowering_disables_the_table(self):
        snapshot = load_snapshot(LexiconConnection(whole=str.lower))  # Final_Sigma, like ICU
        assert snapshot.lowering is None


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

    def test_non_ascii_search_costs_nothing(self, build):
        lexicon = (*FAKE_LEXICON, LexiconEntry("0902", "Home Décor", 2, "09", None))
        fake_db = FakeDatabase(data_version="0325:run1", lexicon=lexicon)
        client, fake_db, _ = build(fake_db=fake_db)
        client.get("/api/v1/meta")
        for term in ("décor", "DÉCOR", "İs", "ΣΑ", "Kelvin\u212a"):
            response = client.get("/api/v1/activities", params={"search": term})
            assert response.status_code == 200, term
        response = client.get("/api/v1/activities", params={"search": "DÉCOR"})
        assert [a["code"] for a in response.json()["activities"]] == ["0902"]
        assert fake_db.transactions == ["metadata"]

    def test_context_sensitive_database_lowering_runs_the_sql(self, build):
        class LexiconSql(FakeVersionConnection):
            def execute(self, sql, params=None):
                if "ILIKE" in sql:
                    return _Rows([("0902", "Home Décor", 2, "09", None)])
                return super().execute(sql, params)

        client, fake_db, _ = build(fake_db=FakeDatabase(
            LexiconSql, data_version="0325:run1", lowering=None))
        client.get("/api/v1/meta")
        response = client.get("/api/v1/activities", params={"search": "décor"})
        assert response.json()["activities"][0]["code"] == "0902"
        assert fake_db.transactions == ["metadata", "metadata"]

    def test_metadata_survives_a_database_outage_after_the_first_read(self, build):
        """Stale if error, through HTTP: the snapshot's TTL has passed and
        the database is down, yet metadata and cache hits are answered."""
        client, fake_db, app = build(api_snapshot_ttl_s=0.0)
        assert client.post(ESTIMATE, json=BODY).headers["X-Cache"] == "miss"
        fake_db.snapshot_error = RuntimeError("database down")
        assert client.get("/api/v1/meta").status_code == 200
        hit = client.post(ESTIMATE, json=BODY)
        assert hit.status_code == 200 and hit.headers["X-Cache"] == "hit"
        assert app.state.snapshots.stale_serves >= 2

    def test_pinned_snapshot_reads_the_database_once(self, build):
        client, fake_db, app = build(api_snapshot_ttl_s=0.0,
                                     expected_data_version="0325:run1")
        for _ in range(5):
            assert client.get("/api/v1/meta").status_code == 200
            assert client.get("/api/v1/activities",
                              params={"search": "décor"}).status_code == 200
        assert fake_db.transactions == ["metadata"]
        assert app.state.snapshots.is_pinned()

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

    def test_version_change_during_a_computation_is_refused_when_bound(self, build,
                                                                       stub_engine):
        """Snapshot says run0 (= expected); the computation's own transaction
        reads run1 first: with a bound version the request is refused before
        anything is computed."""
        computed = []
        stub_engine.estimate_behavior = lambda spec: computed.append(spec)
        client, fake_db, _ = build(fake_db=FakeDatabase(data_version="0325:run0"),
                                   expected_data_version="0325:run0")
        response = client.post(ESTIMATE, json=BODY)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "data_version_mismatch"
        assert computed == []
        # the snapshot was invalidated: the next request reads it again
        count = fake_db.transactions.count("metadata")
        client.get("/api/v1/meta")
        assert fake_db.transactions.count("metadata") == count + 1

    def test_unbound_version_change_bypasses_and_invalidates(self, build, stub_engine):
        """Snapshot says run0; the computation's transaction reads run1. The
        result is computed by an engine that reads its own lexicon and years
        (no snapshot parts), labelled with run1 and its key, and not cached
        (review r1, stale-snapshot-mixed-bypass: it carried run0's labels and
        the run0 snapshot's resolver and years)."""
        client, fake_db, app = build(fake_db=FakeDatabase(data_version="0325:run0"))
        parts = []
        app.dependency_overrides[get_engine_factory] = lambda: (
            lambda conn, **snapshot_parts: parts.append(snapshot_parts) or stub_engine
        )
        response = client.post(ESTIMATE, json=BODY)
        assert response.status_code == 200
        assert response.headers["X-Cache"] == "bypass"
        assert parts == [{}]
        assert response.headers["X-Data-Version"] == "0325:run1"
        current, _, _ = build()              # a process whose snapshot is run1
        same = current.post(ESTIMATE, json=BODY)
        assert same.headers["X-Cache"] == "miss"
        assert same.headers["X-Data-Version"] == "0325:run1"
        assert response.headers["X-Analysis-Key"] == same.headers["X-Analysis-Key"]
        count = fake_db.transactions.count("metadata")
        client.get("/api/v1/meta")
        assert fake_db.transactions.count("metadata") == count + 1

    def test_the_data_version_is_read_before_the_computation(self, build, stub_engine):
        """Its share lock on ingestion_runs keeps the switch out of the rest of
        the transaction, so it must come first (review r1)."""
        order = []

        class Recording(FakeVersionConnection):
            def execute(self, sql, params=None):
                order.append("version" if "ingestion_runs" in sql else sql)
                return super().execute(sql, params)

        def compute(spec):
            order.append("engine")
            return known_estimate_result()

        stub_engine.estimate_behavior = compute
        client, _, _ = build(fake_db=FakeDatabase(Recording, data_version="0325:run1"))
        assert client.post(ESTIMATE, json=BODY).headers["X-Cache"] == "miss"
        assert order == ["version", "engine"]


class TestProductionWithoutAnExpectedVersion:
    SECRET = "s" * 40

    def test_warns_that_the_snapshot_will_be_reread(self, build, caplog):
        with caplog.at_level("WARNING", logger="atus.api"):
            build(env="production", edge_secrets=(self.SECRET,), api_docs=False)
        assert any("ATUS_EXPECTED_DATA_VERSION is not set" in r.getMessage()
                   for r in caplog.records)

    def test_no_warning_when_the_version_is_bound(self, build, caplog):
        with caplog.at_level("WARNING", logger="atus.api"):
            build(env="production", edge_secrets=(self.SECRET,), api_docs=False,
                  expected_data_version="0325:run1")
        assert not any("ATUS_EXPECTED_DATA_VERSION" in r.getMessage() for r in caplog.records)
