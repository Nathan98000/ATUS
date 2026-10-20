"""Unit tests for field-level parsing helpers."""

from datetime import date, time
from decimal import Decimal

import pytest

from atus_pipeline.transformation.common import (
    TransformError,
    activity_code,
    flag_bool_zero_one,
    hhmmss,
    implied_decimal_2,
    sentinel_bool_yes_no,
    sentinel_int,
    usual_hours,
    weight,
    yyyymmdd,
)


class TestSentinelInt:
    def test_positive(self):
        assert sentinel_int("42", field="X") == 42

    def test_zero(self):
        assert sentinel_int("0", field="X") == 0

    @pytest.mark.parametrize("value", ["-1", "-2", "-3", "-4"])
    def test_sentinels_become_none(self, value):
        assert sentinel_int(value, field="X") is None

    def test_garbage_raises(self):
        with pytest.raises(TransformError, match="X"):
            sentinel_int("abc", field="X")


class TestBooleans:
    def test_yes_no(self):
        assert sentinel_bool_yes_no("1", field="X") is True
        assert sentinel_bool_yes_no("2", field="X") is False
        assert sentinel_bool_yes_no("-1", field="X") is None
        assert sentinel_bool_yes_no("-2", field="X") is None

    def test_yes_no_rejects_other_values(self):
        with pytest.raises(TransformError):
            sentinel_bool_yes_no("3", field="X")

    def test_flag(self):
        assert flag_bool_zero_one("0", field="X") is False
        assert flag_bool_zero_one("1", field="X") is True
        with pytest.raises(TransformError):
            flag_bool_zero_one("-1", field="X")


class TestImpliedDecimal:
    def test_two_implied_decimals(self):
        # TRERNWA-style: 66000 means $660.00
        assert implied_decimal_2("66000", field="X") == Decimal("660.00")

    def test_topcode_value(self):
        assert implied_decimal_2("288461", field="X") == Decimal("2884.61")

    def test_sentinel(self):
        assert implied_decimal_2("-1", field="X") is None

    def test_allocated_values_with_fractional_cents(self):
        # observed in the 2003-25 respondent file: TRERNHLY = 7211.525
        assert implied_decimal_2("7211.525", field="X") == Decimal("72.11525")


class TestWeight:
    def test_normal(self):
        assert weight("8155462.672158", field="X") == Decimal("8155462.672158")

    def test_minus_one_is_undefined(self):
        # TUFNWGTP is -1 for 2020 respondents (see docs/methodology.md)
        assert weight("-1.000000", field="X") is None

    def test_other_negative_raises(self):
        with pytest.raises(TransformError):
            weight("-2.5", field="X")

    def test_garbage_raises(self):
        with pytest.raises(TransformError):
            weight("n/a", field="X")


class TestDatesAndTimes:
    def test_yyyymmdd(self):
        assert yyyymmdd("20200126", field="X") == date(2020, 1, 26)

    def test_invalid_date_raises(self):
        with pytest.raises(TransformError):
            yyyymmdd("20201332", field="X")

    def test_hhmmss(self):
        assert hhmmss("04:00:00", field="X") == time(4, 0, 0)
        assert hhmmss("23:59:59", field="X") == time(23, 59, 59)

    def test_bad_time_raises(self):
        with pytest.raises(TransformError):
            hhmmss("25:00:00", field="X")


class TestActivityCode:
    def test_valid_codes_pass_through(self):
        assert activity_code("010101", field="X", width=6) == "010101"
        assert activity_code("01", field="X", width=2) == "01"
        assert activity_code("0101", field="X", width=4) == "0101"

    def test_wrong_width_raises(self):
        with pytest.raises(TransformError):
            activity_code("10101", field="X", width=6)

    def test_non_digits_raise(self):
        with pytest.raises(TransformError):
            activity_code("01010a", field="X", width=6)


class TestUsualHours:
    def test_normal_hours(self):
        assert usual_hours("40", field="X") == (40, False)

    def test_hours_vary_is_a_real_answer(self):
        assert usual_hours("-4", field="X") == (None, True)

    def test_missing(self):
        assert usual_hours("-1", field="X") == (None, False)
