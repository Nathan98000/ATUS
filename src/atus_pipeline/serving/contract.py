"""The serving-DB contract: what the engine, the API and validate-analytics
touch in schema ``atus`` (audit fact I03).

A serving database may drop every other table and column, but must keep
these with their names, types, NULL semantics and constraints, so the engine
code runs unchanged on it. ``tests/integration/test_serving_contract.py``
proves the contract is complete: it runs the engine, the HTTP API and the
benchmark suite as a role that holds privileges on exactly these columns
(plus SELECT on the :data:`LOCKED` tables as a whole) and nothing else.
The serving-DB builder asserts its output with :func:`missing_contract_columns`.

``atus validate-db`` is deliberately outside the contract: it checks the
full database (episode arithmetic, companions, row counts against the BLS
files) and fails on a serving database by design; the CLI refuses to run it
there (``serving/guard.py``).
"""

from __future__ import annotations

import psycopg

from ..analytics.weights import MULTIYEAR, PANDEMIC

# Columns read with SELECT, per table.
READ: dict[str, tuple[str, ...]] = {
    # the engine's base population, weights and filters; /meta's years and count
    "respondents": (
        "tucaseid", "data_year", "diary_date", "diary_day_of_week",
        "labor_force_status", "household_children", "final_weight", "pandemic_weight",
    ),
    # line-1 rows only: the respondent's own age and sex
    "household_members": ("tucaseid", "lineno", "age", "sex"),
    # line-1 rows only: region, state and education filters
    "cps_persons": ("tucaseid", "lineno", "region", "state_fips", "education"),
    # summed per respondent for the selected leaf codes
    "activities": ("tucaseid", "activity_code", "duration_minutes"),
    "replicate_weights": ("tucaseid", *MULTIYEAR.replicate_columns),
    "pandemic_replicate_weights": ("tucaseid", *PANDEMIC.replicate_columns),
    # the lexicon (engine resolver and the API's lexicon endpoints)
    "activity_tier1": ("code", "name"),
    "activity_tier2": ("code", "name"),
    "activity_codes": ("code", "tier2_code", "name", "harmonization_note"),
    # the data version is read as a whole row (to_jsonb), so every column
    "ingestion_runs": (
        "id", "release", "pipeline_version", "started_at", "finished_at", "status",
        "notes", "data_version",
    ),
}

# Tables the API locks, so their role needs SELECT on the table itself, not
# only on its columns (LOCK TABLE ... IN ACCESS SHARE MODE checks the table's
# privilege): the data snapshot read locks ingestion_runs before its
# REPEATABLE READ snapshot is taken (api/database.py, SNAPSHOT_LOCK).
# scripts/ops/roles.sql grants SELECT on every READ table as a whole.
LOCKED: tuple[str, ...] = ("ingestion_runs",)

# Columns written with INSERT (validate-analytics records its results; owner
# role only, never the API role).
WRITE: dict[str, tuple[str, ...]] = {
    "validation_results": ("check_name", "severity", "passed", "observed", "detail"),
}


def missing_contract_columns(conn: psycopg.Connection, schema: str = "atus") -> list[str]:
    """Contract columns absent from ``schema`` (empty when the contract holds)."""
    wanted = {(table, column) for table, columns in READ.items() for column in columns}
    wanted |= {(table, column) for table, columns in WRITE.items() for column in columns}
    present = set(
        conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %s",
            (schema,),
        ).fetchall()
    )
    return sorted(f"{table}.{column}" for table, column in wanted - present)
