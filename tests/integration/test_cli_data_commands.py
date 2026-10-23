"""`atus load`, `atus migrate` and `atus validate-db` through the CLI: the
switch options, validation before the switch, and the serving-database
guard (audit F57)."""

from __future__ import annotations

import re
import shutil

import psycopg
import pytest
from click.testing import CliRunner

from atus_pipeline.cli import cli
from atus_pipeline.database.migrate import MIGRATIONS_DIR_ENV
from atus_pipeline.database.versions import (
    format_serving_version,
    new_build_id,
    read_data_version,
)
from atus_pipeline.loading.loader import load_all
from atus_pipeline.serving.guard import (
    SERVING_MARKER,
    ServingDatabaseError,
    refuse_if_serving,
    serving_marker,
)
from tests.integration.analytics_dataset import analytics_settings
from tests.integration.conftest import MIGRATIONS_DIR

pytestmark = pytest.mark.integration


@pytest.fixture
def fixture_db(scratch_database, tmp_path):
    url = scratch_database("cli")
    return analytics_settings(tmp_path, url)


def run(settings, *args: str):
    return CliRunner().invoke(
        cli, list(args),
        env={
            "ATUS_DATABASE_URL": settings.database_url,
            "ATUS_DATA_DIR": str(settings.data_dir),
            "ATUS_RELEASE": settings.release,
            "ATUS_LOG_LEVEL": "WARNING",
        },
    )


def respondents(url: str) -> int:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT count(*) FROM atus.respondents").fetchone()[0]


class TestLoad:
    def test_load_switches_and_prints_the_new_version(self, fixture_db):
        result = run(fixture_db, "load", "--switch-timeout", "30", "--lock-timeout-ms", "20")
        assert result.exit_code == 0, result.output
        printed = re.search(r"Data version: (\S+)", result.output).group(1)
        with psycopg.connect(fixture_db.database_url) as conn:
            assert read_data_version(conn) == printed
        assert printed.startswith("0325:load-")
        assert respondents(fixture_db.database_url) == 6

    def test_validate_checks_the_new_data_and_refuses_to_switch(self, fixture_db):
        assert run(fixture_db, "load").exit_code == 0
        with psycopg.connect(fixture_db.database_url) as conn:
            before = read_data_version(conn)
        # the fixture cannot match the official BLS row counts: validation fails
        result = run(fixture_db, "load", "--validate")
        assert result.exit_code == 1
        assert "Validating the new data before switching" in result.output
        assert "[FAIL] row count: respondents" in result.output
        assert "load failed:" in result.output and "not switched" in result.output
        with psycopg.connect(fixture_db.database_url) as conn:
            assert read_data_version(conn) == before


    def test_failures_are_reported_without_a_traceback(self, fixture_db, tmp_path, monkeypatch):
        assert run(fixture_db, "load").exit_code == 0
        staged = fixture_db.staging_dir / "atusact_0325.dat"
        staged.write_text(staged.read_text().replace("120303", "120397"))  # not in the lexicon
        result = run(fixture_db, "load")
        assert result.exit_code == 1
        assert "load failed, live data unchanged: ForeignKeyViolation" in result.output
        assert "Traceback" not in result.output

        newer = tmp_path / "migrations"
        shutil.copytree(MIGRATIONS_DIR, newer)
        (newer / "0099_future.sql").write_text("SELECT 1;\n")
        monkeypatch.setenv(MIGRATIONS_DIR_ENV, str(newer))
        result = run(fixture_db, "load")
        assert result.exit_code == 1
        assert "load failed: the database schema is behind this code" in result.output
        assert respondents(fixture_db.database_url) == 6


class TestServingGuard:
    @pytest.fixture
    def loaded(self, fixture_db):
        with psycopg.connect(fixture_db.database_url) as conn:
            load_all(conn, fixture_db)
        return fixture_db

    @staticmethod
    def assert_refused(settings, reason: str) -> None:
        for command in ("migrate", "load", "validate-db"):
            result = run(settings, command)
            assert result.exit_code != 0, command
            assert f"Refusing to run `atus {command}` against a serving database" in (
                result.output
            ), result.output
            assert reason in result.output
        with psycopg.connect(settings.database_url) as conn:
            with pytest.raises(ServingDatabaseError):
                load_all(conn, settings)

    def test_a_full_database_is_not_a_serving_database(self, loaded):
        with psycopg.connect(loaded.database_url) as conn:
            assert serving_marker(conn) is None
            refuse_if_serving(conn, "load")  # no error
        assert run(loaded, "migrate").output.strip() == "Schema already up to date."

    def test_an_uninitialized_database_is_not_a_serving_database(self, scratch_database):
        url = scratch_database("empty")
        with psycopg.connect(url) as conn:
            conn.execute("DROP SCHEMA atus CASCADE")
            assert serving_marker(conn) is None

    def test_the_schema_comment_marks_a_serving_database(self, loaded):
        with psycopg.connect(loaded.database_url) as conn:
            conn.execute(f"COMMENT ON SCHEMA atus IS '{SERVING_MARKER} build=test'")
        before = respondents(loaded.database_url)
        self.assert_refused(loaded, "schema comment")
        assert respondents(loaded.database_url) == before

    def test_a_serving_data_version_marks_a_serving_database(self, loaded):
        version = format_serving_version("0325", new_build_id(), "ab" * 32)
        with psycopg.connect(loaded.database_url) as conn:
            conn.execute(
                "INSERT INTO atus.ingestion_runs (release, pipeline_version, status, "
                "data_version) VALUES ('0325', 'serving-builder', 'succeeded', %s)",
                (version,),
            )
        self.assert_refused(loaded, f"data version {version}")
