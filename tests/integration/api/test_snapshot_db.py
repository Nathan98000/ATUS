"""The in-process data snapshot against a real PostgreSQL (audit F14).

* The lexicon answered from memory is identical to the SQL it replaced:
  the Python search against ``ILIKE`` for thousands of terms, the detail
  view for every code, ``/meta`` against its old queries.
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
from atus_pipeline.api.snapshot import LEXICON_SQL, load_snapshot
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings

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
    return sorted(t for t in terms if 2 <= len(t) <= 80)


class TestLexiconEquivalence:
    def test_python_search_returns_exactly_the_sql_rows(self, snapshot, loaded_settings):
        terms = _search_terms(snapshot)
        assert len(terms) > 500
        python_answered = 0
        with psycopg.connect(loaded_settings.database_url) as conn:
            for search in terms:
                for level, parent in ((None, None), (3, None), (None, "0101"), (2, "18")):
                    entries = snapshot.search_lexicon(level=level, parent=parent, search=search)
                    sql_rows = conn.execute(*lexicon_query(level, parent, search)).fetchall()
                    if entries is None:  # answered by the same SQL at runtime
                        assert not search.isascii(), search
                        continue
                    python_answered += 1
                    assert [e.row() for e in entries] == sql_rows, (search, level, parent)
        assert python_answered > 2000

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
            non_ascii = [list(r) for r in conn.execute(*lexicon_query(None, None, "décor"))]
        with api(loaded_settings) as (client, _):
            listing = client.get("/api/v1/activities", params={"level": 3, "search": "sleep"})
            fallback = client.get("/api/v1/activities", params={"search": "décor"})
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


# --------------------------------------------------------------------------- #
# statements per request, as the API counts them                              #
# --------------------------------------------------------------------------- #

SNAPSHOT_READ = 11  # check, limits, version, run, years, respondents, lexicon,
#                     lowering probe, the resolver's 3 lexicon reads
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
            "activities non-ASCII search": (200, 3),  # check, limits, the ILIKE query
            "activity detail": (200, 0),
            "unknown activity 404": (404, 0),
            # check, limits, the statistics query, the data-version recheck
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
