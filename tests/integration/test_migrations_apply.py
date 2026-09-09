"""The migration runner against a real PostgreSQL."""

import psycopg
import pytest

from atus_pipeline.database.migrate import MigrationError, apply_migrations
from tests.integration.conftest import MIGRATIONS_DIR

pytestmark = pytest.mark.integration


def test_schema_objects_exist(db):
    tables = {
        row[0]
        for row in db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'atus'"
        )
    }
    assert {
        "respondents", "household_members", "activities", "activity_companions",
        "cps_persons", "replicate_weights", "pandemic_replicate_weights",
        "activity_tier1", "activity_tier2", "activity_codes",
        "ingestion_runs", "source_files", "validation_results",
    } <= tables


def test_reapply_is_noop(migrated_db):
    with psycopg.connect(migrated_db) as conn:
        assert apply_migrations(conn, MIGRATIONS_DIR) == []


def test_modified_applied_migration_is_rejected(migrated_db, tmp_path):
    # copy real migrations, then tamper with an applied one
    for path in MIGRATIONS_DIR.glob("*.sql"):
        (tmp_path / path.name).write_text(path.read_text())
    target = tmp_path / "0001_core_schema.sql"
    target.write_text(target.read_text() + "\n-- tampered\n")
    with psycopg.connect(migrated_db) as conn:
        with pytest.raises(MigrationError, match="modified after being applied"):
            apply_migrations(conn, tmp_path)


def test_respondents_2020_weight_constraint(db):
    """The documented 2020 weight rule is enforced by the schema itself."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute(
                """
                INSERT INTO atus.respondents
                    (tucaseid, data_year, diary_date, diary_day_of_week, is_holiday,
                     labor_force_status, final_weight)
                VALUES (1, 2020, '2020-02-01', 7, false, 1, 123.0)
                """
            )
