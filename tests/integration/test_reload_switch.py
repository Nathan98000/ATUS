"""The loader's build-and-switch reload (audit F08, F19, F20, F21; ADR-013).

Each test loads the hand-computed analytics fixture (version 1) and a second
version of it (``reload_dataset.py``) into a scratch database, and checks what
readers and the run metadata see: the old data or the new data, never a mix,
never an empty table, never a killed reader, and a data version that changes
in the same commit as the data. Concurrency is orchestrated through the
loader's ``validate`` hook, which runs after the new data has committed in the
shadow schema and before the switch.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace

import psycopg
import pytest

from atus_pipeline.analytics import ActivitySelector, AnalysisEngine, AnalysisSpec, Measure
from atus_pipeline.api import snapshot as snapshot_module
from atus_pipeline.api.database import Database
from atus_pipeline.database import versions
from atus_pipeline.database.migrate import MigrationError
from atus_pipeline.database.versions import read_data_version
from atus_pipeline.loading import loader
from atus_pipeline.loading.loader import LoadError, SwitchOptions, load_all
from atus_pipeline.loading.switch import (
    CANONICAL_TABLES,
    METADATA_TABLES,
    SHADOW_SCHEMA,
    create_shadow,
    sees_all_sessions,
)
from atus_pipeline.validation.db_checks import run_db_checks
from atus_pipeline.validation.results import CheckResult
from tests.integration.analytics_dataset import analytics_settings
from tests.integration.conftest import (
    MIGRATIONS_DIR,
    create_scratch_database,
    drop_scratch_database,
)
from tests.integration.reload_dataset import (
    V1_RESPONDENTS,
    V1_SLEEP_2023,
    V2_RESPONDENTS,
    V2_SLEEP_2023,
    make_second_version,
)

# `atus load` holds a session-level advisory lock for the whole load, and the switch
# relies on session settings and on lock waits between sessions: none of it survives a
# transaction pooler, and the loader only ever runs against the full database directly.
pytestmark = [pytest.mark.integration, pytest.mark.direct_connection]

LOAD_VERSION = re.compile(r"^0325:load-(\d+)-\d{8}T\d{6}Z-[0-9a-f]{6}$")
SLEEP_2023 = AnalysisSpec(
    Measure.AVERAGE_MINUTES_PER_DAY, ActivitySelector(("010101",), label="Sleeping"), (2023,)
)


@pytest.fixture(scope="module")
def db_url(test_db_url):
    url = create_scratch_database(test_db_url, "reload")
    yield url
    drop_scratch_database(url)


@pytest.fixture
def v1(tmp_path, db_url):
    return analytics_settings(tmp_path / "v1", db_url)


@pytest.fixture
def v2(tmp_path, db_url):
    settings = analytics_settings(tmp_path / "v2", db_url)
    make_second_version(settings)
    return settings


def load(settings, **kwargs):
    with psycopg.connect(settings.database_url) as conn:
        return load_all(conn, settings, **kwargs)


def live_state(url: str) -> tuple[str, int]:
    """(data version, respondent count) as a fresh reader sees them."""
    with psycopg.connect(url) as conn:
        return (
            read_data_version(conn),
            conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0],
        )


def sleep_2023(url: str) -> float:
    with psycopg.connect(url) as conn:
        return AnalysisEngine(conn).estimate(SLEEP_2023).estimate.value


def last_run(url: str) -> tuple:
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT id, status, data_version, notes FROM atus.ingestion_runs "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()


def shadow_exists(url: str) -> bool:
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)", (SHADOW_SCHEMA,)
        ).fetchone()[0]


def wait_for(predicate: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.002)


def assert_unchanged(url: str, before: tuple[str, int]) -> None:
    assert live_state(url) == before
    assert sleep_2023(url) == pytest.approx(V1_SLEEP_2023)
    assert not shadow_exists(url)


class TestSwitch:
    def test_reload_switches_data_and_version_in_one_commit(self, v1, v2, db_url):
        load(v1)
        version1, count1 = live_state(db_url)
        run1 = last_run(db_url)
        assert count1 == V1_RESPONDENTS
        assert sleep_2023(db_url) == pytest.approx(V1_SLEEP_2023)
        assert LOAD_VERSION.match(version1).group(1) == str(run1[0])
        assert run1[1:3] == ("succeeded", version1)

        load(v2)
        version2, count2 = live_state(db_url)
        run2 = last_run(db_url)
        assert version2 != version1 and LOAD_VERSION.match(version2)
        assert count2 == V2_RESPONDENTS
        assert sleep_2023(db_url) == pytest.approx(V2_SLEEP_2023)
        assert run2[0] > run1[0] and run2[1:3] == ("succeeded", version2)
        assert not shadow_exists(db_url)

        with psycopg.connect(db_url) as conn:
            # metadata is history, never swapped: the first run is still there
            assert conn.execute(
                "SELECT status, data_version FROM atus.ingestion_runs WHERE id = %s",
                (run1[0],),
            ).fetchone() == ("succeeded", version1)
            assert conn.execute(
                "SELECT count(*) FROM atus.ingestion_runs WHERE status = 'running'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM atus.source_files WHERE ingestion_run_id = %s",
                (run2[0],),
            ).fetchone()[0] == 7

    def test_identical_reloads_still_get_distinct_versions(self, v1, db_url):
        load(v1)
        first, _ = live_state(db_url)
        load(v1)
        second, _ = live_state(db_url)
        assert first != second

    def test_shadow_is_structurally_identical_to_the_live_schema(self, db_url):
        def catalog(conn, schema):
            def norm(rows):
                return [
                    tuple(v.replace(f"{schema}.", "atus.") if isinstance(v, str) else v
                          for v in row)
                    for row in rows
                ]
            columns = conn.execute(
                """
                SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                       a.attnotnull, pg_get_expr(d.adbin, d.adrelid), a.attidentity
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                WHERE n.nspname = %s AND c.relkind = 'r' AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY 1, a.attnum
                """,
                (schema,),
            ).fetchall()
            constraints = conn.execute(
                """
                SELECT c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid)
                FROM pg_constraint con
                JOIN pg_class c ON c.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s ORDER BY 1, 2
                """,
                (schema,),
            ).fetchall()
            indexes = conn.execute(
                """
                SELECT t.relname, i.relname, pg_get_indexdef(i.oid)
                FROM pg_index x
                JOIN pg_class i ON i.oid = x.indexrelid
                JOIN pg_class t ON t.oid = x.indrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = %s ORDER BY 1, 2
                """,
                (schema,),
            ).fetchall()
            return norm(columns), norm(constraints), norm(indexes)

        with psycopg.connect(db_url) as conn:
            create_shadow(conn, MIGRATIONS_DIR)
            # qualify every name in the deparsed definitions (a role named
            # "atus" otherwise sees schema atus through "$user")
            conn.execute("SET LOCAL search_path = pg_catalog")
            live, shadow = catalog(conn, "atus"), catalog(conn, SHADOW_SCHEMA)
            conn.rollback()
        assert all(live)  # columns, constraints and indexes were all found
        assert shadow == live

    def test_every_table_is_swapped_or_kept_and_no_foreign_key_crosses(self, db_url):
        with psycopg.connect(db_url) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'atus'"
                )
            }
            foreign_keys = conn.execute(
                """
                SELECT src.relname, dst.relname FROM pg_constraint con
                JOIN pg_class src ON src.oid = con.conrelid
                JOIN pg_class dst ON dst.oid = con.confrelid
                JOIN pg_namespace n ON n.oid = src.relnamespace
                WHERE con.contype = 'f' AND n.nspname = 'atus'
                """
            ).fetchall()
        assert tables == set(CANONICAL_TABLES) | set(METADATA_TABLES)
        # A new table must be classified; a key across the two groups would be
        # silently dropped with the shadow's metadata copies.
        for src, dst in foreign_keys:
            assert (src in CANONICAL_TABLES) == (dst in CANONICAL_TABLES), (src, dst)


class TestFailedLoads:
    def test_copy_failure_changes_nothing(self, v1, v2, db_url):
        load(v1)
        before = live_state(db_url)
        path = v2.staging_dir / "atusact_0325.dat"
        path.write_text(path.read_text().replace("120303", "120397"))  # not in the lexicon

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            load(v2)
        assert_unchanged(db_url, before)
        _, status, version, notes = last_run(db_url)
        assert status == "failed" and version is None
        assert notes.startswith("ForeignKeyViolation")

    def test_failure_inside_the_switch_transaction_changes_nothing(
        self, v1, v2, db_url, monkeypatch
    ):
        """F20: the status, the version and the data commit together, so a
        failure between them is impossible to observe."""
        load(v1)
        before = live_state(db_url)

        def explode(*args, **kwargs):
            raise RuntimeError("injected failure while publishing the run")

        monkeypatch.setattr(loader, "_record_source_file", explode)
        with pytest.raises(RuntimeError, match="injected"):
            load(v2)
        assert_unchanged(db_url, before)
        assert last_run(db_url)[1] == "failed"

    def test_failed_validation_refuses_the_switch(self, v1, v2, db_url):
        load(v1)
        before = live_state(db_url)

        def reject(conn, schema):
            return [CheckResult("probe", passed=False, severity="error", observed="bad")]

        with pytest.raises(LoadError, match="validation check"):
            load(v2, validate=reject)
        assert_unchanged(db_url, before)

    def test_abandoned_run_and_leftover_shadow_are_cleaned_up(self, v1, db_url):
        load(v1)
        with psycopg.connect(db_url) as conn:
            stale = conn.execute(
                "INSERT INTO atus.ingestion_runs (release, pipeline_version) "
                "VALUES ('0325', 'killed') RETURNING id"
            ).fetchone()[0]
            conn.execute(f"CREATE SCHEMA {SHADOW_SCHEMA}")
            conn.execute(f"CREATE TABLE {SHADOW_SCHEMA}.respondents (x int)")
        load(v1)
        with psycopg.connect(db_url) as conn:
            status, notes = conn.execute(
                "SELECT status, notes FROM atus.ingestion_runs WHERE id = %s", (stale,)
            ).fetchone()
        assert status == "failed" and notes.startswith("abandoned")
        assert last_run(db_url)[1] == "succeeded"
        assert not shadow_exists(db_url)

    def test_a_second_concurrent_load_is_refused(self, v1, db_url):
        load(v1)
        before = live_state(db_url)
        with psycopg.connect(db_url, autocommit=True) as other:
            other.execute("SELECT pg_advisory_lock(%s)", (loader._LOAD_LOCK_KEY,))
            with pytest.raises(LoadError, match="another `atus load`"):
                load(v1)
        assert live_state(db_url) == before

    def test_a_connection_inside_a_transaction_is_refused(self, v1, db_url):
        with psycopg.connect(db_url) as conn:
            conn.execute("SELECT 1")
            with pytest.raises(LoadError, match="no open transaction"):
                load_all(conn, v1)

    def test_the_schema_must_match_the_migrations(self, v1, db_url, tmp_path):
        load(v1)
        before = live_state(db_url)
        newer = tmp_path / "migrations"
        shutil.copytree(MIGRATIONS_DIR, newer)
        (newer / "0099_future.sql").write_text("SELECT 1;\n")
        with pytest.raises(MigrationError, match="pending: 0099"):
            load(v1, migrations_dir=newer)
        assert live_state(db_url) == before

    def test_a_dependent_view_fails_the_switch_loudly(self, v1, v2, db_url):
        load(v1)
        before = live_state(db_url)
        with psycopg.connect(db_url) as conn:
            conn.execute(
                "CREATE VIEW public.reload_test_dependent AS SELECT count(*) AS n "
                "FROM atus.respondents"
            )
        try:
            with pytest.raises(LoadError, match="depend on the live tables"):
                load(v2)
            assert_unchanged(db_url, before)
            with psycopg.connect(db_url) as conn:
                assert conn.execute(
                    "SELECT n FROM public.reload_test_dependent"
                ).fetchone()[0] == V1_RESPONDENTS
        finally:
            with psycopg.connect(db_url) as conn:
                conn.execute("DROP VIEW IF EXISTS public.reload_test_dependent")


class TestValidation:
    def test_validate_db_checks_run_against_the_new_data(self, v1, v2, db_url):
        """`atus load --validate` runs the validate-db suite on the shadow,
        before anything is live, and records it with the switch."""
        load(v1)
        seen: dict[str, str] = {}

        def check_shadow(conn, schema):
            results = run_db_checks(conn, v2, schema=schema, persist=False)
            seen.update({r.name: r.observed for r in results})
            # the full-release expectations fail on the fixture by design; keep
            # only checks that hold on it so the switch goes ahead
            return [r for r in results if r.passed]

        load(v2, validate=check_shadow)
        assert seen["row count: respondents"] == f"{V2_RESPONDENTS:,}"  # not the live 6
        assert seen["lexicon loaded completely: activity_codes"] == "431"
        with psycopg.connect(db_url) as conn:
            recorded = conn.execute(
                "SELECT count(*) FROM atus.validation_results "
                "WHERE check_name = 'lexicon loaded completely: activity_codes'"
            ).fetchone()[0]
            # Recorded in the switch transaction, so at exactly the run's
            # finished_at: the serving builder counts results from then on as
            # evidence for this data (serving/builder.py validation_evidence).
            at_switch = conn.execute(
                "SELECT bool_and(v.run_at = r.finished_at) "
                "FROM atus.ingestion_runs r JOIN atus.validation_results v "
                "ON v.run_at >= r.started_at "
                "WHERE r.id = (SELECT max(id) FROM atus.ingestion_runs "
                "WHERE status = 'succeeded')"
            ).fetchone()[0]
        assert recorded >= 1
        assert at_switch is True


class TestReaders:
    def test_grants_survive_the_switch(self, v1, v2, db_url):
        role = f"{db_url.rsplit('/', 1)[1]}_reader"
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')
            conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            load(v1)
            with psycopg.connect(db_url) as conn:
                conn.execute(f'GRANT USAGE ON SCHEMA atus TO "{role}"')
                conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA atus TO "{role}"')
                conn.execute(f'GRANT SELECT ON atus.activities TO "{role}" WITH GRANT OPTION')

            def acl(conn):
                return conn.execute(
                    """
                    SELECT c.relname, a.privilege_type, a.is_grantable
                    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
                         aclexplode(c.relacl) a
                    WHERE n.nspname = 'atus' AND a.grantee = %s::regrole
                    ORDER BY 1, 2
                    """,
                    (role,),
                ).fetchall()

            with psycopg.connect(db_url) as conn:
                before = acl(conn)
            load(v2)
            with psycopg.connect(db_url) as conn:
                assert acl(conn) == before
                assert ("activities", "SELECT", True) in before
                conn.execute(f'SET ROLE "{role}"')
                assert conn.execute(
                    "SELECT count(*) FROM atus.respondents"
                ).fetchone()[0] == V2_RESPONDENTS
        finally:
            with psycopg.connect(db_url, autocommit=True) as conn:
                conn.execute(f'DROP OWNED BY "{role}"')
                conn.execute(f'DROP ROLE "{role}"')

    def test_prepared_statements_and_long_lived_engines_see_the_new_data(
        self, v1, v2, db_url
    ):
        load(v1)
        with psycopg.connect(db_url) as conn:
            count = "SELECT count(*) FROM atus.respondents"
            engine = AnalysisEngine(conn)
            for _ in range(6):  # past psycopg's auto-prepare threshold
                assert conn.execute(count, prepare=True).fetchone()[0] == V1_RESPONDENTS
                assert engine.estimate(SLEEP_2023).estimate.value == pytest.approx(
                    V1_SLEEP_2023
                )
            conn.rollback()  # idle between requests, like a pooled connection
            load(v2)
            assert conn.execute(count, prepare=True).fetchone()[0] == V2_RESPONDENTS
            assert engine.estimate(SLEEP_2023).estimate.value == pytest.approx(V2_SLEEP_2023)

    def test_a_reader_holding_the_tables_defers_the_switch_until_it_ends(
        self, v1, v2, db_url, monkeypatch
    ):
        load(v1)
        reader = psycopg.connect(db_url)
        attempts: list[int] = []
        real_switch = loader.switch

        def counting_switch(*args, **kwargs):
            attempts.append(real_switch(*args, **kwargs))
            return attempts[-1]

        monkeypatch.setattr(loader, "switch", counting_switch)
        seen: list[int] = []

        def start_reader(conn, schema):
            # a request that began after the new data committed, mid-flight
            seen.append(reader.execute("SELECT count(*) FROM atus.respondents").fetchone()[0])
            threading.Timer(0.5, reader.commit).start()
            return []

        try:
            load(v2, validate=start_reader)
        finally:
            reader.close()
        assert seen == [V1_RESPONDENTS]       # the reader's whole transaction saw old data
        assert attempts[0] > 1                # the switch retried instead of waiting
        assert live_state(db_url)[1] == V2_RESPONDENTS

    def test_the_switch_gives_up_when_the_tables_stay_busy(self, v1, v2, db_url):
        load(v1)
        before = live_state(db_url)
        reader = psycopg.connect(db_url)

        def start_reader(conn, schema):
            reader.execute("SELECT count(*) FROM atus.activities").fetchone()
            return []

        try:
            with pytest.raises(LoadError, match="tables busy"):
                load(v2, validate=start_reader, options=SwitchOptions(timeout_s=1))
            # the reader was never disturbed
            assert reader.execute("SELECT count(*) FROM atus.respondents").fetchone()[0] == 6
        finally:
            reader.close()
        assert_unchanged(db_url, before)
        assert last_run(db_url)[1] == "failed"

    def test_a_lock_order_cycle_with_a_reader_never_kills_the_reader(self, v1, v2, db_url):
        """F19: a reader touches activities, then respondents; the switch locks
        respondents, then activities. Before, TRUNCATE in the same position
        deadlocked and PostgreSQL killed one side. Now the switch's lock wait
        always times out first, so the reader completes and the switch retries."""
        load(v1)
        reader = psycopg.connect(db_url)
        watcher = psycopg.connect(db_url, autocommit=True)
        outcome: dict[str, object] = {"reader_waited": False}

        def switch_waits_on_activities() -> bool:
            return watcher.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted "
                "AND mode = 'AccessExclusiveLock' AND relation = 'atus.activities'::regclass)"
            ).fetchone()[0]

        def reader_side() -> None:
            wait_for(switch_waits_on_activities)
            pid = reader.info.backend_pid

            def watch():
                while "count" not in outcome and "error" not in outcome:
                    if watcher.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted)",
                        (pid,),
                    ).fetchone()[0]:
                        outcome["reader_waited"] = True
                    time.sleep(0.002)

            spy = threading.Thread(target=watch)
            spy.start()
            try:
                outcome["count"] = reader.execute(
                    "SELECT count(*) FROM atus.respondents"
                ).fetchone()[0]
                reader.commit()
            except Exception as exc:  # a deadlock victim would land here
                outcome["error"] = exc
            spy.join()

        side = threading.Thread(target=reader_side)

        def start_reader(conn, schema):
            reader.execute("SELECT count(*) FROM atus.activities").fetchone()
            side.start()
            return []

        try:
            with psycopg.connect(db_url) as conn:
                # widen each switch lock wait so the cycle reliably forms; the
                # cap keeps a whole attempt below this session's deadlock_timeout
                # and the reader's default 1 s
                conn.execute("SET deadlock_timeout = '5s'")
                conn.commit()
                load_all(
                    conn, v2, validate=start_reader,
                    options=SwitchOptions(lock_timeout_ms=400, timeout_s=60),
                )
            side.join()
        finally:
            reader.close()
            watcher.close()
        assert "error" not in outcome, outcome
        assert outcome["reader_waited"] is True   # the cycle really formed
        assert outcome["count"] == V1_RESPONDENTS
        assert live_state(db_url)[1] == V2_RESPONDENTS


class TestRepeatableRead:
    def test_the_pitfall_the_switch_guards_against(self, db_url):
        """PostgreSQL behaviour, not loader code: a REPEATABLE READ snapshot
        taken before a table's rows committed, resolving the table's name only
        after a switch, sees the new table empty (F21's failure mode)."""
        with psycopg.connect(db_url, autocommit=True) as admin:
            admin.execute("DROP SCHEMA IF EXISTS rr_demo CASCADE")
            admin.execute("DROP SCHEMA IF EXISTS rr_demo_next CASCADE")
            admin.execute("CREATE SCHEMA rr_demo")
            admin.execute("CREATE TABLE rr_demo.t AS SELECT 1 AS x")
            reader = psycopg.connect(db_url)
            reader.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            try:
                reader.execute("SELECT 1")  # snapshot taken; rr_demo not touched yet
                admin.execute("CREATE SCHEMA rr_demo_next")
                admin.execute("CREATE TABLE rr_demo_next.t AS SELECT 2 AS x UNION ALL SELECT 3")
                with admin.transaction():
                    admin.execute("DROP TABLE rr_demo.t")
                    admin.execute("ALTER TABLE rr_demo_next.t SET SCHEMA rr_demo")
                assert reader.execute("SELECT count(*) FROM rr_demo.t").fetchone()[0] == 0
            finally:
                reader.close()
                admin.execute("DROP SCHEMA rr_demo CASCADE")
                admin.execute("DROP SCHEMA rr_demo_next CASCADE")

    def test_the_switch_waits_for_transactions_older_than_the_new_data(
        self, v1, v2, db_url, caplog
    ):
        load(v1)
        version1, _ = live_state(db_url)
        old = psycopg.connect(db_url)
        old.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        old.execute("SELECT 1")  # snapshot predates the new data
        errors: list[BaseException] = []

        def run_load():
            try:
                load(v2, options=SwitchOptions(timeout_s=60))
            except BaseException as exc:
                errors.append(exc)

        caplog.set_level(logging.DEBUG, logger="atus_pipeline.loading.switch")
        loader_thread = threading.Thread(target=run_load)
        loader_thread.start()
        try:
            wait_for(lambda: any("older than the new data" in r.getMessage()
                                 for r in caplog.records))
            assert live_state(db_url)[0] == version1   # not switched while it is open
            # the old snapshot still reads the old tables: complete, not empty
            assert old.execute("SELECT count(*) FROM atus.respondents").fetchone()[0] == 6
            old.commit()
        finally:
            loader_thread.join()
            old.close()
        assert not errors
        assert live_state(db_url)[1] == V2_RESPONDENTS

    def test_the_api_snapshot_read_never_pairs_the_old_version_with_the_new_data(
        self, v1, v2, db_url, monkeypatch, caplog
    ):
        """The API's REPEATABLE READ snapshot read (``Database.read_snapshot``)
        starts after the new data committed, so the guard above does not wait
        for it, and is held right after its opening statement, before it
        reads anything. Its snapshot must still describe one version: it
        locks ``ingestion_runs`` before its snapshot is taken, so the switch
        waits for it and it reads the old version with the old data. Before,
        its first statement fixed the snapshot without a lock; the switch
        committed, and the snapshot paired the old data version and run with
        the new respondents (review r1, rr-snapshot-mixes-versions-across-switch)."""
        load(v1)
        version1, _ = live_state(db_url)
        database = Database(v1)
        held, resume = threading.Event(), threading.Event()
        outcome: dict[str, object] = {}

        def held_version_read(conn):
            held.set()
            assert resume.wait(60)
            return versions.read_data_version(conn)

        monkeypatch.setattr(snapshot_module, "versions", SimpleNamespace(
            read_data_version=held_version_read, UNLOADED=versions.UNLOADED))

        def read_snapshot():
            try:
                outcome["snapshot"] = database.read_snapshot()
            except BaseException as exc:
                outcome["error"] = exc

        reader = threading.Thread(target=read_snapshot)

        def start_reader(conn, schema):  # the new data has committed; no switch yet
            reader.start()
            assert held.wait(30)
            return []

        def run_load():
            try:
                load(v2, validate=start_reader, options=SwitchOptions(timeout_s=60))
            except BaseException as exc:
                outcome["load_error"] = exc

        caplog.set_level(logging.DEBUG, logger="atus_pipeline.loading.switch")
        loader_thread = threading.Thread(target=run_load)
        loader_thread.start()
        try:
            # until the switch has given way to the reader, or has run without it
            wait_for(lambda: not loader_thread.is_alive()
                     or any("tables busy" in r.getMessage() for r in caplog.records))
        finally:
            resume.set()
            reader.join(30)
            loader_thread.join(90)
            database.close()
        assert "error" not in outcome and "load_error" not in outcome, outcome
        snapshot = outcome["snapshot"]
        assert (snapshot.data_version, snapshot.respondents) == (version1, V1_RESPONDENTS)
        assert snapshot.run.id == int(LOAD_VERSION.match(version1).group(1))
        assert live_state(db_url)[1] == V2_RESPONDENTS   # the switch went ahead afterwards

    def test_waiting_needs_a_role_that_sees_other_sessions(self, db_url):
        role = f"{db_url.rsplit('/', 1)[1]}_plain"
        with psycopg.connect(db_url, autocommit=True) as admin:
            admin.execute(f'DROP ROLE IF EXISTS "{role}"')
            admin.execute(f'CREATE ROLE "{role}" NOLOGIN')
            try:
                with psycopg.connect(db_url, autocommit=True) as conn:
                    assert sees_all_sessions(conn)          # the owner/superuser
                    conn.execute(f'SET ROLE "{role}"')
                    assert not sees_all_sessions(conn)      # a plain role
                    admin.execute(f'GRANT pg_read_all_stats TO "{role}"')
                    assert sees_all_sessions(conn)
            finally:
                admin.execute(f'DROP ROLE "{role}"')

    def test_an_old_transaction_that_never_ends_fails_the_load_safely(self, v1, v2, db_url):
        load(v1)
        before = live_state(db_url)
        old = psycopg.connect(db_url)
        old.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        old.execute("SELECT 1")
        try:
            with pytest.raises(LoadError, match="older than the new data"):
                load(v2, options=SwitchOptions(timeout_s=1))
        finally:
            old.close()
        assert_unchanged(db_url, before)
        assert last_run(db_url)[1] == "failed"
