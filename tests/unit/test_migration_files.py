"""Unit tests for migration discovery and retargeting (no database needed)."""

import re
from pathlib import Path

import pytest

from atus_pipeline.database.migrate import (
    MIGRATIONS_DIR_ENV,
    MigrationError,
    default_migrations_dir,
    discover_migrations,
    retarget_sql,
)

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


class TestRetarget:
    def test_qualified_references_and_schema_creation_are_rewritten(self):
        sql = (
            "CREATE SCHEMA IF NOT EXISTS atus;\n"
            "CREATE TABLE atus.a (id int REFERENCES atus.b (id));\n"
            "CREATE INDEX idx_a ON atus.a (id);"
        )
        assert retarget_sql(sql, "atus_next") == (
            "CREATE SCHEMA IF NOT EXISTS atus_next;\n"
            "CREATE TABLE atus_next.a (id int REFERENCES atus_next.b (id));\n"
            "CREATE INDEX idx_a ON atus_next.a (id);"
        )

    def test_the_live_schema_is_left_alone(self):
        assert retarget_sql("SELECT 1 FROM atus.a", "atus") == "SELECT 1 FROM atus.a"

    def test_unqualified_schema_references_are_refused(self):
        # would act on the live schema while building the copy
        for sql in ("GRANT USAGE ON SCHEMA atus TO x", "SET search_path = atus, public"):
            with pytest.raises(MigrationError, match="unqualified"):
                retarget_sql(sql, "atus_next")

    def test_comments_and_longer_identifiers_are_not_references(self):
        sql = "-- the atus schema (see atus_pipeline.database)\nSELECT 1 FROM atus.a"
        assert retarget_sql(sql, "atus_next").endswith("SELECT 1 FROM atus_next.a")

    def test_invalid_schema_names_are_refused(self):
        with pytest.raises(MigrationError, match="invalid schema"):
            retarget_sql("SELECT 1 FROM atus.a", "atus_next; DROP TABLE x")

    def test_every_repo_migration_can_build_a_copy(self):
        for migration in discover_migrations(REPO_MIGRATIONS):
            copied = retarget_sql(migration.sql, "atus_next")
            body = re.sub(r"--[^\n]*", "", copied)
            assert not re.search(r"\batus\.", body), migration.name


class TestMigrationsDir:
    def test_source_checkout_is_found(self, monkeypatch):
        monkeypatch.delenv(MIGRATIONS_DIR_ENV, raising=False)
        assert default_migrations_dir() == REPO_MIGRATIONS

    def test_environment_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv(MIGRATIONS_DIR_ENV, str(tmp_path))
        assert default_migrations_dir() == tmp_path

    def test_bad_override_is_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv(MIGRATIONS_DIR_ENV, str(tmp_path / "missing"))
        with pytest.raises(MigrationError, match="not a directory"):
            default_migrations_dir()


def test_data_version_migration_is_additive():
    """0003 only adds the nullable, unique data_version column and its checks."""
    migration = {m.version: m for m in discover_migrations(REPO_MIGRATIONS)}["0003"]
    statements = [s.strip() for s in re.sub(r"--[^\n]*", "", migration.sql).split(";")]
    statements = [s for s in statements if s]
    assert all(s.startswith("ALTER TABLE atus.ingestion_runs") for s in statements)
    assert "NOT NULL" not in migration.sql


def test_repo_migrations_discoverable_and_ordered():
    migrations = discover_migrations(REPO_MIGRATIONS)
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)
    assert migrations[0].name == "0001_core_schema.sql"


def test_bad_names_rejected(tmp_path):
    (tmp_path / "1_bad.sql").write_text("SELECT 1")
    with pytest.raises(MigrationError, match="does not match"):
        discover_migrations(tmp_path)


def test_duplicate_versions_rejected(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1")
    (tmp_path / "0001_b.sql").write_text("SELECT 2")
    with pytest.raises(MigrationError, match="Duplicate"):
        discover_migrations(tmp_path)


def test_checksums_change_with_content(tmp_path):
    path = tmp_path / "0001_a.sql"
    path.write_text("SELECT 1")
    first = discover_migrations(tmp_path)[0].checksum
    path.write_text("SELECT 2")
    second = discover_migrations(tmp_path)[0].checksum
    assert first != second
