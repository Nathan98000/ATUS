"""Unit tests for the per-table row transformers."""

from datetime import date, time
from decimal import Decimal

import pytest

from atus_pipeline.transformation.common import TransformError
from atus_pipeline.transformation.tables import (
    TABLE_SPECS,
    transform_activity,
    transform_companion,
    transform_cps_person,
    transform_household_member,
    transform_pandemic_weights,
    transform_replicate_weights,
    transform_respondent,
)
from tests.fixtures import (
    activity_row,
    cps_row,
    pandemic_weights_row,
    replicate_weights_row,
    respondent_row,
    roster_row,
    who_row,
)


class TestRespondentTransform:
    def test_basic_fields(self):
        row = respondent_row(
            TELFS="2", TEMJOT="2", TRDPFTPT="1", TEHRUSLT="40",
            TRERNWA="66000", TRERNHLY="2200", TRNUMHOU="3",
        )
        result = dict(zip(TABLE_SPECS[0].columns, transform_respondent(row), strict=True))
        assert result["tucaseid"] == 20230101230001
        assert result["data_year"] == 2023
        assert result["diary_date"] == date(2023, 1, 15)
        assert result["diary_day_of_week"] == 1
        assert result["is_holiday"] is False
        assert result["labor_force_status"] == 2
        assert result["has_multiple_jobs"] is False
        assert result["full_or_part_time"] == 1
        assert result["usual_weekly_hours"] == 40
        assert result["usual_hours_vary"] is False
        assert result["weekly_earnings"] == Decimal("660.00")
        assert result["hourly_earnings"] == Decimal("22.00")
        assert result["household_size"] == 3
        assert result["final_weight"] == Decimal("5000000.123456")
        assert result["pandemic_weight"] is None

    def test_2020_respondent_has_no_final_weight(self):
        row = respondent_row(
            TUCASEID="20200101200001", TUYEAR="2020", TUDIARYDATE="20200126",
            TUFNWGTP="-1.000000", TU20FWGT="5541150.024906",
        )
        result = dict(zip(TABLE_SPECS[0].columns, transform_respondent(row), strict=True))
        assert result["final_weight"] is None
        assert result["pandemic_weight"] == Decimal("5541150.024906")

    def test_hours_vary_preserved_as_flag(self):
        row = respondent_row(TEHRUSLT="-4")
        result = dict(zip(TABLE_SPECS[0].columns, transform_respondent(row), strict=True))
        assert result["usual_weekly_hours"] is None
        assert result["usual_hours_vary"] is True

    def test_sentinels_become_null(self):
        result = dict(
            zip(TABLE_SPECS[0].columns, transform_respondent(respondent_row()), strict=True)
        )
        for column in (
            "has_multiple_jobs", "full_or_part_time", "usual_weekly_hours",
            "weekly_earnings", "eldercare_minutes", "youngest_child_age",
        ):
            assert result[column] is None, column


class TestActivityTransform:
    def test_basic_episode(self):
        result = dict(zip(TABLE_SPECS[2].columns, transform_activity(activity_row()), strict=True))
        assert result["activity_code"] == "010101"
        assert result["tier1_code"] == "01"
        assert result["tier2_code"] == "0101"
        assert result["start_time"] == time(4, 0)
        assert result["stop_time"] == time(12, 0)
        assert result["duration_minutes"] == 480
        assert result["location_code"] is None  # -1: where not collected for sleep

    def test_leading_zeros_must_be_present(self):
        with pytest.raises(TransformError, match="TRCODEP"):
            transform_activity(activity_row(TRCODEP="10101"))

    def test_midnight_crossing_episode(self):
        row = activity_row(
            TUSTARTTIM="21:00:00", TUSTOPTIME="01:30:00",
            TUACTDUR24="270", TUACTDUR="270", TUCUMDUR24="1440",
        )
        result = dict(zip(TABLE_SPECS[2].columns, transform_activity(row), strict=True))
        assert result["start_time"] == time(21, 0)
        assert result["stop_time"] == time(1, 30)
        assert result["duration_minutes"] == 270


class TestCompanionTransform:
    def test_household_member_present(self):
        row = who_row(TULINENO="2", TRWHONA="0", TUWHO_CODE="20")
        result = dict(zip(TABLE_SPECS[3].columns, transform_companion(row), strict=True))
        assert result["who_lineno"] == 2
        assert result["who_code"] == 20
        assert result["who_not_asked"] is False

    def test_not_asked_placeholder_keeps_sentinels(self):
        result = dict(zip(TABLE_SPECS[3].columns, transform_companion(who_row()), strict=True))
        assert result["who_lineno"] == -1
        assert result["who_code"] == -1
        assert result["who_not_asked"] is True


class TestHouseholdMemberTransform:
    def test_roster_person(self):
        result = dict(
            zip(
                TABLE_SPECS[1].columns,
                transform_household_member(roster_row(TULINENO="3")),
                strict=True,
            )
        )
        assert result["lineno"] == 3
        assert result["relationship"] == 18
        assert result["age"] == 40
        assert result["sex"] == 2


class TestCpsPersonTransform:
    def test_state_fips_zero_padded(self):
        result = dict(
            zip(TABLE_SPECS[4].columns, transform_cps_person(cps_row(GESTFIPS="6")), strict=True)
        )
        assert result["state_fips"] == "06"

    def test_implausible_fips_raises(self):
        with pytest.raises(TransformError, match="GESTFIPS"):
            transform_cps_person(cps_row(GESTFIPS="99"))

    def test_hispanic_flag(self):
        result = dict(
            zip(TABLE_SPECS[4].columns, transform_cps_person(cps_row(PEHSPNON="2")), strict=True)
        )
        assert result["is_hispanic"] is False


class TestWeightTransforms:
    def test_replicate_row_width(self):
        values = transform_replicate_weights(replicate_weights_row())
        assert len(values) == 161
        assert values[0] == 20230101230001
        assert values[1] == Decimal("1000001.000001")
        assert values[160] == Decimal("1000160.000001")

    def test_2020_replicates_are_null(self):
        row = replicate_weights_row(
            TUCASEID="20200101200001",
            **{f"TUFNWGTP{i:03d}": "-1.000000" for i in range(1, 161)},
        )
        values = transform_replicate_weights(row)
        assert all(v is None for v in values[1:])

    def test_pandemic_row(self):
        values = transform_pandemic_weights(pandemic_weights_row())
        assert len(values) == 161
        assert values[1] == Decimal("2000001.000002")


def test_every_spec_column_list_matches_transformer_output():
    """The COPY column list and the transformer output must stay in lockstep."""
    sample_rows = {
        "respondents": respondent_row(),
        "household_members": roster_row(),
        "activities": activity_row(),
        "activity_companions": who_row(),
        "cps_persons": cps_row(),
        "replicate_weights": replicate_weights_row(),
        "pandemic_replicate_weights": pandemic_weights_row(),
    }
    for spec in TABLE_SPECS:
        values = spec.transform(sample_rows[spec.table])
        assert len(values) == len(spec.columns), spec.table
