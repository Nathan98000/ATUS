"""Weight-scheme rules and person-day arithmetic."""

from datetime import date

import pytest

from atus_pipeline.analytics.errors import UnsupportedAnalysisError
from atus_pipeline.analytics.spec import PopulationFilter
from atus_pipeline.analytics.weights import (
    MULTIYEAR,
    PANDEMIC,
    days_represented,
    total_days_represented,
    validate_scheme_for_years,
)

NO_WINDOW = PopulationFilter()


class TestSchemeValidation:
    def test_multiyear_refuses_2020(self):
        with pytest.raises(UnsupportedAnalysisError, match="undefined for 2020"):
            validate_scheme_for_years(MULTIYEAR, (2019, 2020, 2021))

    def test_multiyear_accepts_everything_else(self):
        validate_scheme_for_years(MULTIYEAR, tuple(range(2003, 2020)) + (2021, 2025))

    def test_pandemic_accepts_2019_2020(self):
        validate_scheme_for_years(PANDEMIC, (2019, 2020))
        validate_scheme_for_years(PANDEMIC, (2020,))

    def test_pandemic_refuses_other_years(self):
        with pytest.raises(UnsupportedAnalysisError, match="only for 2019 and 2020"):
            validate_scheme_for_years(PANDEMIC, (2018, 2019))


class TestDaysRepresented:
    def test_ordinary_and_leap_years(self):
        assert days_represented(MULTIYEAR, 2003, NO_WINDOW) == 365
        assert days_represented(MULTIYEAR, 2004, NO_WINDOW) == 366

    def test_users_guide_multi_year_example(self):
        # User's Guide: 2003-06 combined denominator is 1,461 days.
        assert total_days_represented(MULTIYEAR, (2003, 2004, 2005, 2006), NO_WINDOW) == 1461

    def test_pandemic_scheme_excludes_the_gap(self):
        # 2020: 366 - 53 excluded days; 2019: 365 - 53 (TU20FWGT is calibrated
        # to the comparable windows in both years).
        assert days_represented(PANDEMIC, 2020, NO_WINDOW) == 313
        assert days_represented(PANDEMIC, 2019, NO_WINDOW) == 312

    def test_diary_window_limits_days(self):
        window = PopulationFilter(diary_date_min=date(2020, 5, 10))
        # May 10 - Dec 31, 2020 = 236 days, entirely outside the excluded gap
        assert days_represented(PANDEMIC, 2020, window) == 236

    def test_window_overlapping_the_gap_subtracts_only_the_overlap(self):
        window = PopulationFilter(
            diary_date_min=date(2020, 3, 1), diary_date_max=date(2020, 5, 31)
        )
        # Mar 1 - May 31 = 92 days; gap Mar 18 - May 9 = 53 days inside it
        assert days_represented(PANDEMIC, 2020, window) == 92 - 53

    def test_multiyear_scheme_ignores_the_gap(self):
        assert days_represented(MULTIYEAR, 2019, NO_WINDOW) == 365
