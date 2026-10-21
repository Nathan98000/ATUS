"""The in-process data snapshot against a real PostgreSQL (audit F14).

* The lexicon answered from memory is identical to the SQL it replaced:
  the Python search against ``ILIKE`` for thousands of terms, ASCII and not
  (the database's own character lowering, and a scan of every code point
  proving none outside ``CASED_RANGES`` lowers to anything else), the
  detail view for every code, ``/meta`` against its old queries.
* Statistics are unchanged: engines given the snapshot's resolver and years
  return exactly what engines that read them return.
* The snapshot is read in one REPEATABLE READ, read-only transaction, and no
  API transaction can write (I05).
* Statements per request, counted by the API (the access log's
  ``db_statements``): 0 for cache hits, 4xx and metadata once the snapshot
  is fresh. The server-side count (PostgreSQL's own statement log) is in
  ``test_statement_log.py``.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from contextlib import contextmanager
from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.analytics.spec import ActivitySelector, AnalysisSpec, ComparisonSpec, Measure
from atus_pipeline.api import database as dbmod
from atus_pipeline.api.app import create_app
from atus_pipeline.api.routes.activities import lexicon_query
from atus_pipeline.api.snapshot import CASED_RANGES, LEXICON_SQL, load_snapshot
from atus_pipeline.database.versions import read_data_version
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import E1, E3, analytics_settings

pytestmark = pytest.mark.integration

ESTIMATE = "/api/v1/analysis/estimate"
OPS_TOKEN = "m" * 40


@pytest.fixture(scope="module")
def loaded_settings(tmp_path_factory, migrated_db):
    settings = analytics_settings(tmp_path_factory.mktemp("snapshot"), migrated_db)
    with psycopg.connect(settings.database_url) as conn:
        load_all(conn, settings)
    return settings


@pytest.fixture(scope="module")
def snapshot(loaded_settings):
    with psycopg.connect(loaded_settings.database_url) as conn:
        return load_snapshot(conn)


@contextmanager
def api(settings, **overrides):
    app = create_app(replace(settings, **overrides))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, app


# --------------------------------------------------------------------------- #
# the lexicon from memory == the lexicon from SQL                             #
# --------------------------------------------------------------------------- #


def _search_terms(snapshot) -> list[str]:
    """Every word of every name in several casings, random substrings, and
    LIKE metacharacters (seeded, so the run is reproducible)."""
    names = [entry.name for entry in snapshot.lexicon]
    words = {w for name in names for w in re.split(r"[^0-9A-Za-zÀ-ÿ]+", name) if len(w) >= 2}
    terms = set()
    for word in words:
        terms.update({word, word.lower(), word.upper(), word.swapcase()})
    rng = random.Random(20261020)
    for _ in range(400):
        name = rng.choice(names)
        start = rng.randrange(max(1, len(name) - 2))
        terms.add(name[start:start + rng.randint(2, 12)])
    terms.update({
        "%%", "__", "\\\\", "a_b", "%a", "_e", "n.e.c.", "N.E.C", "(not", "w/", "/ ",
        "  ", "e,", "*", "self)", "décor", "DÉCOR", "Décor", "Ü", "İs", "KELVIN",
        "sleep", "SLEEP", "sLeEp", "tv", "01", "12",
    })
    # Non-ASCII terms: every non-ASCII character of the lexicon in each case,
    # alone and inside words; the characters a database may lower into ASCII
    # (dotted capital I, Kelvin and Angstrom signs, capital sharp s); scripts
    # with context-sensitive or special lowering; decomposed accents.
    specials = ["\u0130", "\u0131", "\u212a", "\u212b", "\u1e9e", "ß", "\u03a3", "\u03c2",
                "\u01c5", "\u2160", "\uff33", "e\u0301", "\u0307"]
    accented = sorted({ch for name in names for ch in name if not ch.isascii()})
    for ch in accented + specials:
        for variant in {ch, ch.upper(), ch.lower(), ch.casefold()}:
            terms.update({variant * 2, f"d{variant}cor", f"D{variant}COR", f"{variant}s",
                          f"{variant}SLAND", f"sl{variant}{variant}p", f"x{variant}"})
    terms.update({"\u0130SLAND", "\u0130s", "\u212aELVIN", "STRA\u1e9eE", "ΣΑ", "ΑΣ",
                  "décor svcs", "maint/repair/DÉCOR", "\u2160\u2161"})
    for _ in range(200):  # random non-ASCII substrings of the names that have one
        name = rng.choice([n for n in names if not n.isascii()])
        start = rng.randrange(max(1, len(name) - 2))
        piece = name[start:start + rng.randint(2, 12)]
        terms.update({piece, piece.upper(), piece.swapcase()})
    return sorted(t for t in terms if 2 <= len(t) <= 80)


class TestLexiconEquivalence:
    def test_python_search_returns_exactly_the_sql_rows(self, snapshot, loaded_settings):
        terms = _search_terms(snapshot)
        non_ascii = [t for t in terms if not t.isascii()]
        assert len(terms) > 800 and len(non_ascii) > 200
        assert snapshot.lowering is not None  # a libc collation: lowered per character
        matched_non_ascii = 0
        with psycopg.connect(loaded_settings.database_url) as conn:
            for search in terms:
                for level, parent in ((None, None), (3, None), (None, "0101"), (2, "18")):
                    entries = snapshot.search_lexicon(level=level, parent=parent, search=search)
                    sql_rows = conn.execute(*lexicon_query(level, parent, search)).fetchall()
                    assert entries is not None, search
                    assert [e.row() for e in entries] == sql_rows, (search, level, parent)
                    if sql_rows and not search.isascii():
                        matched_non_ascii += 1
        assert matched_non_ascii > 50  # "décor" and friends really match

    def test_no_code_point_outside_the_cased_ranges_changes_when_lowered(self, loaded_settings,
                                                                          snapshot):
        """The search's premise, on this database's own collation (every valid
        code point; 0 and the surrogates cannot be text): a code point outside
        CASED_RANGES lowers to itself. The search stays exact as long as none
        lowers into a character of the lowered lexicon, which is asserted
        separately, so a cased script newer than this list fails the first
        assertion here before it could ever change a result."""
        inside = " OR ".join(f"cp BETWEEN {lo} AND {hi}" for lo, hi in CASED_RANGES)
        with psycopg.connect(loaded_settings.database_url) as conn:
            changed = conn.execute(
                "SELECT cp, lower(chr(cp)) FROM generate_series(1, 1114111) AS cp "
                f"WHERE cp NOT BETWEEN 55296 AND 57343 AND NOT ({inside}) "
                "AND lower(chr(cp)) <> chr(cp)"
            ).fetchall()
        lexicon_chars = {ch for entry in snapshot.lexicon for ch in entry.name_lower}
        assert [cp for cp, lowered in changed if set(lowered) <= lexicon_chars] == []
        assert [hex(cp) for cp, _ in changed] == []

    def test_the_table_is_the_databases_lowering(self, snapshot, loaded_settings):
        with psycopg.connect(loaded_settings.database_url) as conn:
            for ch in ("\u0130", "\u212a", "É", "Σ", "I", "\u1e9e", "\u2160"):
                assert snapshot.lowering.get(ch, ch) == conn.execute(
                    "SELECT lower(%s)", (ch,)).fetchone()[0], ch

    def test_unfiltered_and_filtered_listings(self, snapshot, loaded_settings):
        with psycopg.connect(loaded_settings.database_url) as conn:
            for level in (None, 1, 2, 3):
                for parent in (None, "01", "0101", "18", "1803", "99"):
                    entries = snapshot.search_lexicon(level=level, parent=parent, search=None)
                    sql_rows = conn.execute(*lexicon_query(level, parent, None)).fetchall()
                    assert [e.row() for e in entries] == sql_rows, (level, parent)

    def test_detail_matches_the_sql_for_every_code(self, snapshot, loaded_settings):
        codes = [entry.code for entry in snapshot.lexicon] + ["99", "9999", "999999", "0100"]
        with psycopg.connect(loaded_settings.database_url) as conn:
            for code in codes:
                assert _detail_from_snapshot(snapshot, code) == _detail_from_sql(conn, code), code

    def test_routes_answer_from_memory_identically(self, loaded_settings, snapshot):
        with psycopg.connect(loaded_settings.database_url) as conn:
            expected = [list(r) for r in conn.execute(*lexicon_query(3, None, "sleep")).fetchall()]
            non_ascii = [list(r) for r in conn.execute(*lexicon_query(None, None, "DÉCOR"))]
        with api(loaded_settings) as (client, _):
            listing = client.get("/api/v1/activities", params={"level": 3, "search": "sleep"})
            fallback = client.get("/api/v1/activities", params={"search": "DÉCOR"})
        keys = ("code", "name", "level", "parent_code", "harmonization_note")
        assert [[a[k] for k in keys] for a in listing.json()["activities"]] == expected
        assert [[a[k] for k in keys] for a in fallback.json()["activities"]] == non_ascii
        assert len(non_ascii) >= 5  # "Décor" is in the real lexicon

    def test_meta_matches_its_old_queries(self, loaded_settings, snapshot):
        with psycopg.connect(loaded_settings.database_url) as conn:
            run = conn.execute(
                "SELECT id, release, finished_at FROM atus.ingestion_runs "
                "WHERE status = 'succeeded' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            years = [r[0] for r in conn.execute(
                "SELECT DISTINCT data_year FROM atus.respondents ORDER BY 1")]
            respondents = conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0]
        with api(loaded_settings) as (client, _):
            data = client.get("/api/v1/meta").json()["data"]
        assert data == {
            "release": run[1], "years": years, "respondents": respondents,
            "last_ingested_at": run[2].isoformat() if run[2] else None,
            "ingestion_run_id": run[0],
        }


def _detail_from_snapshot(snapshot, code):
    detail = snapshot.detail(code)
    if detail is None:
        return None
    return (detail.entry.name, detail.entry.level, [p.row() for p in detail.parents],
            [c.row() for c in detail.children], detail.entry.harmonization_note,
            detail.descendant_leaf_count)


def _detail_from_sql(conn, code):
    """The detail endpoint's implementation before the snapshot (verbatim
    queries), as the reference."""
    def fetch(one_code):
        return conn.execute(LEXICON_SQL + " WHERE code = %(code)s", {"code": one_code}).fetchone()

    row = fetch(code)
    if row is None:
        return None
    _, name, level, _, note = row
    parents = []
    for prefix_len in (2, 4):
        if prefix_len < len(code):
            parent_row = fetch(code[:prefix_len])
            if parent_row:
                parents.append(tuple(parent_row))
    children = conn.execute(
        LEXICON_SQL + " WHERE parent_code = %(code)s ORDER BY code", {"code": code}
    ).fetchall()
    leaves = conn.execute(
        "SELECT count(*) FROM atus.activity_codes WHERE code LIKE %(prefix)s",
        {"prefix": code + "%"},
    ).fetchone()[0]
    return (name, level, parents, [tuple(c) for c in children], note, leaves)


# --------------------------------------------------------------------------- #
# statistics unchanged                                                        #
# --------------------------------------------------------------------------- #


def _specs():
    sleep = ActivitySelector(include=("010101",))
    base = AnalysisSpec(measure=Measure.AVERAGE_MINUTES_PER_DAY, activity=sleep, years=(2023,))
    return {
        "estimate": base,
        "pooled": replace(base, years=(2019, 2023)),
        "rate": replace(base, measure=Measure.PARTICIPATION_RATE),
        "pandemic": replace(base, years=(2019, 2020), weights="pandemic"),
        "tier": replace(base, activity=ActivitySelector(include=("01",), exclude=("010102",))),
    }


class TestStatisticsUnchanged:
    def test_engine_with_snapshot_parts_returns_identical_results(self, snapshot, loaded_settings):
        parts = snapshot.engine_kwargs()
        with psycopg.connect(loaded_settings.database_url) as conn:
            for name, spec in _specs().items():
                plain = AnalysisEngine(conn).estimate(spec).to_dict()
                reused = AnalysisEngine(conn, **parts).estimate(spec).to_dict()
                assert reused == plain, name
            trend_spec = replace(_specs()["estimate"], years=(2019, 2020, 2023))
            assert (AnalysisEngine(conn, **parts).trend(trend_spec).to_dict()
                    == AnalysisEngine(conn).trend(trend_spec).to_dict())
            comparison = ComparisonSpec(
                base=_specs()["estimate"], group_a=_population("male"),
                group_b=_population("female"),
            )
            assert (AnalysisEngine(conn, **parts).compare(comparison).to_dict()
                    == AnalysisEngine(conn).compare(comparison).to_dict())

    def test_http_results_equal_the_plain_engine(self, loaded_settings):
        spec = _specs()["pooled"]
        with psycopg.connect(loaded_settings.database_url) as conn:
            expected = AnalysisEngine(conn).estimate(spec).to_dict()
        with api(loaded_settings) as (client, _):
            response = client.post(ESTIMATE, json=spec.to_dict())
        assert response.json() == json.loads(json.dumps(expected))  # tuples as lists


def _population(sex):
    from atus_pipeline.analytics.spec import PopulationFilter

    return PopulationFilter(sex=sex)


# --------------------------------------------------------------------------- #
# transactions                                                                #
# --------------------------------------------------------------------------- #


class TestTransactions:
    def test_snapshot_is_one_repeatable_read_read_only_transaction(
        self, loaded_settings, monkeypatch
    ):
        seen = {}
        real_load = dbmod.load_snapshot

        def recording_load(conn):
            seen["isolation"] = conn.execute("SHOW transaction_isolation").fetchone()[0]
            seen["read_only"] = conn.execute("SHOW transaction_read_only").fetchone()[0]
            seen["timeout"] = conn.execute("SHOW statement_timeout").fetchone()[0]
            return real_load(conn)

        monkeypatch.setattr(dbmod, "load_snapshot", recording_load)
        with api(loaded_settings) as (client, app):
            assert client.get("/api/v1/meta").status_code == 200
            with app.state.database.transaction("metadata") as conn:
                after = conn.execute("SHOW transaction_isolation").fetchone()[0]
        assert seen == {"isolation": "repeatable read", "read_only": "on", "timeout": "10s"}
        assert after == "read committed"  # the pooled connection was reset

    def test_api_transactions_cannot_write(self, loaded_settings):
        with api(loaded_settings) as (_, app):
            database = app.state.database
            for kind in ("analysis", "metadata"):
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    with database.transaction(kind) as conn:
                        conn.execute("UPDATE atus.ingestion_runs SET release = release")
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                with database.transaction("metadata", repeatable_read=True) as conn:
                    conn.execute("CREATE TEMP TABLE scratch (x int)")

    @pytest.mark.parametrize("threshold", [0, 1, 5, None])
    def test_the_snapshot_read_works_with_every_prepare_threshold(self, loaded_settings,
                                                                  threshold):
        """The snapshot's opening query is several statements (SET LOCAL ...;
        LOCK ...), which PostgreSQL cannot prepare; with
        ATUS_API_PREPARE_THRESHOLD=0 psycopg prepared it on first use and
        every snapshot read failed (review r1 verification). Repeated on one
        connection past psycopg's default threshold of 5."""
        with api(loaded_settings, api_prepare_threshold=threshold, api_pool_max_size=3,
                 api_snapshot_ttl_s=0) as (client, app):
            statuses = [client.get("/api/v1/meta").status_code for _ in range(8)]
            assert client.post(ESTIMATE, json={"activity": {"include": ["0101"]},
                                               "years": [2023]}).status_code == 200
            assert app.state.database.pool.get_stats()["connections_num"] == 1
        assert statuses == [200] * 8


# --------------------------------------------------------------------------- #
# statements per request, as the API counts them                              #
# --------------------------------------------------------------------------- #

SNAPSHOT_READ = 11  # check, limits, version, run, years, respondents, lexicon,
#                     lowering table, the resolver's 3 lexicon reads
ANALYSIS_TXN = 2    # the pool's connection check and the limits


class TestStatementCounts:
    def requests(self, client):
        sleep = {"activity": {"include": ["010101"]}, "years": [2023]}
        compare = {**sleep, "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}
        return [
            ("meta (reads the snapshot)", lambda: client.get("/api/v1/meta")),
            ("meta (fresh snapshot)", lambda: client.get("/api/v1/meta")),
            ("activities", lambda: client.get("/api/v1/activities")),
            ("activities search", lambda: client.get("/api/v1/activities",
                                                     params={"search": "sleep"})),
            ("activities non-ASCII search", lambda: client.get("/api/v1/activities",
                                                               params={"search": "décor"})),
            ("activity detail", lambda: client.get("/api/v1/activities/0101")),
            ("unknown activity 404", lambda: client.get("/api/v1/activities/999999")),
            ("estimate miss", lambda: client.post(ESTIMATE, json=sleep)),
            ("estimate hit", lambda: client.post(ESTIMATE, json=sleep)),
            ("trend miss", lambda: client.post("/api/v1/analysis/trend",
                                               json={**sleep, "years": [2019, 2020, 2023]})),
            ("compare miss", lambda: client.post("/api/v1/analysis/compare", json=compare)),
            ("compare hit", lambda: client.post("/api/v1/analysis/compare", json=compare)),
            ("422 schema", lambda: client.post(ESTIMATE, json={**sleep, "years": []})),
            ("422 unknown activity", lambda: client.post(
                ESTIMATE, json={**sleep, "activity": {"include": ["999999"]}})),
            ("422 2020 multiyear", lambda: client.post(ESTIMATE, json={**sleep, "years": [2020]})),
            ("400 malformed", lambda: client.post(
                ESTIMATE, content=b"{", headers={"Content-Type": "application/json"})),
            ("population metadata", lambda: client.get("/api/v1/population/metadata")),
            ("health", lambda: client.get("/health")),
        ]

    def test_counts(self, loaded_settings, caplog):
        with api(loaded_settings, api_snapshot_ttl_s=600.0) as (client, _):
            counts = {}
            for name, send in self.requests(client):
                caplog.clear()
                with caplog.at_level(logging.INFO, logger="atus.api"):
                    response = send()
                [record] = [r for r in caplog.records if r.name == "atus.api.access"]
                counts[name] = (response.status_code, record.fields["db_statements"])
        assert counts == {
            "meta (reads the snapshot)": (200, SNAPSHOT_READ),
            "meta (fresh snapshot)": (200, 0),
            "activities": (200, 0),
            "activities search": (200, 0),
            "activities non-ASCII search": (200, 0),  # the database's lowering, in memory
            "activity detail": (200, 0),
            "unknown activity 404": (404, 0),
            # check, limits, the data-version read, the statistics query
            "estimate miss": (200, ANALYSIS_TXN + 2),
            "estimate hit": (200, 0),
            "trend miss": (200, ANALYSIS_TXN + 2),
            "compare miss": (200, ANALYSIS_TXN + 3),  # one statistics query per group
            "compare hit": (200, 0),
            "422 schema": (422, 0),
            "422 unknown activity": (422, 0),
            "422 2020 multiyear": (422, 0),
            "400 malformed": (400, 0),
            "population metadata": (200, 0),
            "health": (200, 0),
        }

    def test_db_free_routes_never_open_a_connection(self, loaded_settings):
        """No startup connection, no background refresh, no idle traffic
        (I08): until a request needs data, the pool stays empty."""
        with api(loaded_settings, api_snapshot_ttl_s=0.0) as (client, app):
            for path in ("/health", "/api/v1/population/metadata",
                         "/api/v1/activities/presets", "/api/v1/activities/abc"):
                client.get(path)
            client.post(ESTIMATE, json={"activity": {"include": ["0101"]}, "years": []})
            stats = app.state.database.pool.get_stats()
            assert stats["pool_size"] == 0 and stats.get("requests_num", 0) == 0
            assert app.state.snapshots.refreshes == 0

    def test_metrics_report_the_pool(self, loaded_settings):
        auth = {"Authorization": f"Bearer {OPS_TOKEN}"}
        with api(loaded_settings, ops_token=OPS_TOKEN) as (client, _):
            client.get("/api/v1/meta")
            text = client.get("/metrics", headers=auth).text
        lines = set(text.splitlines())
        assert "atus_db_pool_pool_max 4" in lines
        assert "atus_db_pool_pool_size 1" in lines
        assert any(line.startswith("atus_db_pool_requests_num_total ") for line in lines)
        assert ('atus_db_statements_per_request_bucket{route="/api/v1/meta",le="13"} 1'
                in lines)


# --------------------------------------------------------------------------- #
# a real outage: stale if error, and pinning                                  #
# --------------------------------------------------------------------------- #


def _allow_connections(url: str, allowed: bool) -> None:
    """Open or close a scratch database to new connections (a real outage
    as the API sees one: every connect is refused by the server), and drop
    the connections it already has."""
    from urllib.parse import urlsplit

    from tests.integration.conftest import _admin_url

    name = urlsplit(url).path.lstrip("/")
    with psycopg.connect(_admin_url(url), autocommit=True) as conn:
        conn.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS {str(allowed).lower()}')
        if not allowed:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))


@pytest.fixture
def outage_settings(tmp_path, scratch_database):
    url = scratch_database("outage")
    settings = analytics_settings(tmp_path, url)
    with psycopg.connect(url) as conn:
        load_all(conn, settings)
    yield settings
    _allow_connections(url, True)  # so the fixture can drop it


class TestDatabaseOutage:
    def test_cache_hits_and_metadata_survive_while_analyses_fail_honestly(
        self, outage_settings
    ):
        sleep = {"activity": {"include": ["010101"]}, "years": [2023]}
        auth = {"Authorization": f"Bearer {OPS_TOKEN}"}
        with api(outage_settings, api_snapshot_ttl_s=0.5, ops_token=OPS_TOKEN) as (client, app):
            assert client.post(ESTIMATE, json=sleep).headers["X-Cache"] == "miss"
            before = client.get("/api/v1/meta").json()
            _allow_connections(outage_settings.database_url, False)
            time.sleep(0.6)  # the snapshot is past its TTL
            meta = client.get("/api/v1/meta")
            hit = client.post(ESTIMATE, json=sleep)
            new = client.post(ESTIMATE, json={**sleep, "years": [2019]})
            ready = client.get("/ready", headers=auth)
            search = client.get("/api/v1/activities", params={"search": "DÉCOR"})
            assert (meta.status_code, meta.json()) == (200, before)
            assert hit.status_code == 200 and hit.headers["X-Cache"] == "hit"
            assert search.status_code == 200 and search.json()["total"] >= 5
            assert new.status_code == 503
            assert new.json()["error"]["code"] == "database_unavailable"
            assert ready.status_code == 503  # readiness is never answered from memory
            assert app.state.snapshots.stale_serves >= 3

            _allow_connections(outage_settings.database_url, True)
            time.sleep(0.6)  # past the retry wait (min(30 s, TTL))
            assert client.post(ESTIMATE, json={**sleep, "years": [2019]}).status_code == 200
            assert app.state.snapshots.is_fresh()

    def test_a_pinned_snapshot_never_needs_the_database(self, outage_settings):
        with psycopg.connect(outage_settings.database_url) as conn:
            version = read_data_version(conn)
        with api(outage_settings, api_snapshot_ttl_s=0.0,
                 expected_data_version=version) as (client, app):
            assert client.get("/api/v1/meta").status_code == 200
            _allow_connections(outage_settings.database_url, False)
            for _ in range(3):
                assert client.get("/api/v1/meta").status_code == 200
                assert client.get("/api/v1/activities/0101").status_code == 200
            assert app.state.snapshots.refreshes == 1
            assert app.state.snapshots.refresh_failures == 0


# --------------------------------------------------------------------------- #
# data changed in place after the snapshot was read (non-pinned)              #
# --------------------------------------------------------------------------- #

SLEEP_TIER2 = {"activity": {"include": ["0101"]}, "years": [2023], "variance": "none"}
NEW_VERSION = "0325:load-in-place-test"


def _change_data_in_place(url: str) -> None:
    """One committed change, as a switch makes it: a new leaf 010150 under
    0101, 60 minutes of it for E3, E1's weight 1.0 -> 5.0, and a new data
    version. Sleep (0101), 2023: old data 75.0; new data 102.5; the new
    data read with the old lexicon (no 010150) 87.5."""
    with psycopg.connect(url) as conn:
        conn.execute("INSERT INTO atus.activity_codes (code, tier2_code, name) "
                     "VALUES ('010150', '0101', 'Sleeping (in-place test leaf)')")
        n = conn.execute("SELECT max(activity_number) + 1 FROM atus.activities "
                         "WHERE tucaseid = %s", (E3,)).fetchone()[0]
        conn.execute(
            "INSERT INTO atus.activities (tucaseid, activity_number, activity_code, tier1_code, "
            "tier2_code, start_time, stop_time, duration_minutes, duration_uncapped_minutes, "
            "cumulative_minutes) VALUES (%s, %s, '010150', '01', '0101', '04:00', '05:00', "
            "60, 60, 60)", (E3, n))
        conn.execute("UPDATE atus.respondents SET final_weight = 5.0 WHERE tucaseid = %s", (E1,))
        conn.execute(
            "INSERT INTO atus.ingestion_runs (release, pipeline_version, started_at, finished_at, "
            "status, data_version) VALUES ('0325', 'test', now(), now(), 'succeeded', %s)",
            (NEW_VERSION,))


class TestDataChangedAfterTheSnapshot:
    def test_an_uncached_analysis_is_computed_and_labelled_on_one_version(
        self, tmp_path, scratch_database
    ):
        """Review r1 (stale-snapshot-mixed-bypass): the snapshot (TTL 600 s)
        predates the change. The next uncached analysis used the snapshot's
        lexicon on the new tables (87.5, neither version's answer) and
        carried the old X-Data-Version and X-Analysis-Key. Now it is computed
        on the new data with the new lexicon (102.5), labelled with the new
        version, and not cached."""
        url = scratch_database("inplace")
        settings = analytics_settings(tmp_path, url)
        with psycopg.connect(url) as conn:
            load_all(conn, settings)
        with api(settings, api_snapshot_ttl_s=600.0) as (client, _):
            old_version = client.get("/api/v1/meta").headers["X-Data-Version"]
            _change_data_in_place(url)
            during = client.post(ESTIMATE, json=SLEEP_TIER2)
            after = client.post(ESTIMATE, json=SLEEP_TIER2)
        assert old_version != NEW_VERSION
        assert during.status_code == 200 and during.headers["X-Cache"] == "bypass"
        assert during.headers["X-Data-Version"] == NEW_VERSION
        assert during.json()["estimate"]["value"] == pytest.approx(102.5)
        assert during.json()["activity"]["leaf_code_count"] == 4
        assert after.headers["X-Cache"] == "miss"            # the bypass was not cached
        assert after.headers["X-Data-Version"] == NEW_VERSION
        assert after.headers["X-Analysis-Key"] == during.headers["X-Analysis-Key"]
        assert after.json() == during.json()
