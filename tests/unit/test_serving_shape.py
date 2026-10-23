"""The serving schema derived from the canonical shape (serving/shape.py)."""

from __future__ import annotations

from atus_pipeline.serving.contract import READ
from atus_pipeline.serving.shape import (
    DROPPED_TABLES,
    RESHAPED,
    Column,
    Constraint,
    Index,
    TableShape,
    create_table_sql,
    missing_checks,
    post_load_sql,
    reshape_sql,
)


def _respondents() -> TableShape:
    return TableShape(
        "respondents",
        columns=(
            Column("tucaseid", "bigint", True, None),
            Column("data_year", "smallint", True, None),
            Column("is_holiday", "boolean", True, None),
            Column("diary_date", "date", True, None),
            Column("diary_day_of_week", "smallint", True, None),
            Column("labor_force_status", "smallint", True, None),
            Column("household_children", "smallint", False, None),
            Column("usual_hours_vary", "boolean", True, "false"),
            Column("final_weight", "numeric(18,6)", False, None),
            Column("pandemic_weight", "numeric(18,6)", False, None),
        ),
        constraints=(
            Constraint("respondents_pkey", "p", "PRIMARY KEY (tucaseid)", ("tucaseid",), None),
            Constraint("final_weight_2020_rule", "c",
                       "CHECK (((data_year = 2020) = (final_weight IS NULL)))",
                       ("data_year", "final_weight"), None),
            Constraint("respondents_household_children_check", "c",
                       "CHECK (((household_children >= 0) AND (household_children <= 30)))",
                       ("household_children",), None),
            Constraint("holiday_rule", "c", "CHECK (is_holiday IS NOT NULL)", ("is_holiday",),
                       None),
        ),
        indexes=(
            Index("idx_respondents_data_year",
                  "CREATE INDEX idx_respondents_data_year ON atus.respondents USING btree "
                  "(data_year)", ("data_year",)),
            Index("idx_holiday", "CREATE INDEX idx_holiday ON atus.respondents USING btree "
                  "(is_holiday)", ("is_holiday",)),
        ),
    )


def _activities() -> TableShape:
    return TableShape(
        "activities",
        columns=(
            Column("tucaseid", "bigint", True, None),
            Column("activity_number", "integer", True, None),
            Column("activity_code", "character(6)", True, None),
            Column("tier1_code", "character(2)", True, None),
            Column("duration_minutes", "smallint", True, None),
        ),
        constraints=(
            Constraint("activities_pkey", "p", "PRIMARY KEY (tucaseid, activity_number)",
                       ("tucaseid", "activity_number"), None),
            Constraint("activities_duration_minutes_check", "c",
                       "CHECK (((duration_minutes >= 0) AND (duration_minutes <= 1440)))",
                       ("duration_minutes",), None),
            Constraint("tier1_matches_code", "c",
                       "CHECK ((\"left\"((activity_code)::text, 2) = (tier1_code)::text))",
                       ("activity_code", "tier1_code"), None),
            Constraint("activities_tucaseid_fkey", "f",
                       "FOREIGN KEY (tucaseid) REFERENCES atus.respondents(tucaseid)",
                       ("tucaseid",), "respondents"),
            Constraint("activities_tier1_code_fkey", "f",
                       "FOREIGN KEY (tier1_code) REFERENCES atus.activity_tier1(code)",
                       ("tier1_code",), "activity_tier1"),
        ),
        indexes=(
            Index("idx_activities_activity_code",
                  "CREATE INDEX idx_activities_activity_code ON atus.activities USING btree "
                  "(activity_code)", ("activity_code",)),
            Index("idx_activities_tier1_code",
                  "CREATE INDEX idx_activities_tier1_code ON atus.activities USING btree "
                  "(tier1_code)", ("tier1_code",)),
        ),
    )


def test_reshaped_tables_keep_exactly_the_contract_columns():
    assert set(RESHAPED) == {
        "respondents", "household_members", "cps_persons", "activities",
        "replicate_weights", "pandemic_replicate_weights",
    }
    for table, reshape in RESHAPED.items():
        assert reshape.columns == READ[table]
    assert DROPPED_TABLES == ("activity_companions",)


def test_create_table_keeps_types_null_rules_and_checks_on_kept_columns():
    sql = create_table_sql(_respondents(), RESHAPED["respondents"])
    assert sql.splitlines()[0] == "CREATE TABLE atus.respondents ("
    assert "    tucaseid bigint NOT NULL," in sql
    assert "    household_children smallint," in sql
    assert "    final_weight numeric(18,6)," in sql
    assert ("CONSTRAINT final_weight_2020_rule "
            "CHECK (((data_year = 2020) = (final_weight IS NULL)))") in sql
    assert "respondents_household_children_check" in sql
    assert "is_holiday" not in sql and "usual_hours_vary" not in sql and "holiday_rule" not in sql
    assert "PRIMARY KEY" not in sql            # added after the data is in


def test_replicates_become_float8_and_line1_tables_gain_their_check():
    shape = TableShape(
        "household_members",
        columns=(Column("tucaseid", "bigint", True, None), Column("lineno", "smallint", True, None),
                 Column("relationship", "smallint", False, None),
                 Column("age", "smallint", False, None), Column("sex", "smallint", True, None)),
        constraints=(), indexes=(),
    )
    sql = create_table_sql(shape, RESHAPED["household_members"])
    assert "CONSTRAINT serving_line1_only CHECK (lineno = 1)" in sql
    assert "relationship" not in sql
    reshape = RESHAPED["replicate_weights"]
    assert reshape.type_overrides["tufnwgtp001"] == "double precision"
    assert "tucaseid" not in reshape.type_overrides


def test_post_load_adds_keys_then_indexes_then_foreign_keys_it_can_keep():
    others = [t for t in RESHAPED if t not in ("respondents", "activities")]
    shapes = {
        "respondents": _respondents(), "activities": _activities(),
        **{t: TableShape(t, (), (), ()) for t in others},
    }
    statements = post_load_sql(shapes)
    assert statements == [
        "ALTER TABLE atus.respondents ADD CONSTRAINT respondents_pkey PRIMARY KEY (tucaseid)",
        "CREATE INDEX idx_respondents_data_year ON atus.respondents USING btree (data_year)",
        "CREATE INDEX idx_activities_activity_code ON atus.activities USING btree "
        "(activity_code)",
        "ALTER TABLE atus.activities ADD CONSTRAINT activities_tucaseid_fkey "
        "FOREIGN KEY (tucaseid) REFERENCES atus.respondents(tucaseid)",
    ]


def test_reshape_drops_every_reshaped_table_in_one_statement():
    shapes = {t: TableShape(t, tuple(Column(c, "bigint", True, None) for c in READ[t]), (), ())
              for t in RESHAPED}
    statements = reshape_sql(shapes)
    assert statements[0].startswith("DROP TABLE atus.activity_companions, atus.respondents")
    assert statements[0].endswith(" RESTRICT")
    assert sum(s.startswith("CREATE TABLE") for s in statements) == len(RESHAPED)
    assert any(s.startswith("COMMENT ON TABLE atus.activities IS 'Serving database: one row")
               for s in statements)


def test_missing_checks_reports_a_lost_constraint():
    canonical = {"respondents": _respondents(), "activities": _activities(),
                 **{t: TableShape(t, (), (), ()) for t in RESHAPED
                    if t not in ("respondents", "activities")}}
    assert missing_checks(canonical, canonical) == []
    damaged = dict(canonical)
    damaged["respondents"] = TableShape(
        "respondents", (), tuple(c for c in _respondents().constraints
                                 if c.name != "final_weight_2020_rule"), ())
    assert missing_checks(canonical, damaged) == ["respondents.final_weight_2020_rule"]
