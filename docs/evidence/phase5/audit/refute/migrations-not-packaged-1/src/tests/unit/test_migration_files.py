"""Unit tests for migration discovery (no database needed)."""

from pathlib import Path

import pytest

from atus_pipeline.database.migrate import MigrationError, discover_migrations

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


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
