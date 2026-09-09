"""Weight schemes: which ATUS weight answers which question, and how many
person-days it represents.

ATUS final weights are calibrated to **person-days**: summing them over a
period's respondents yields (population x days in the period), so ratio
estimates (means, rates) use weight sums directly while person counts divide
by the number of days the weights represent (User's Guide ch. 7.1/7.4).

Two schemes exist in the multi-year database:

* ``multiyear`` — ``respondents.final_weight`` (BLS ``TUFNWGTP``, 2006
  weighting method, comparable across all years) with replicates
  ``atus.replicate_weights.tufnwgtp001..160``. **Undefined for 2020** (NULL by
  schema constraint), so any 2020-inclusive request under this scheme is
  refused rather than silently dropping 2020 rows.
* ``pandemic`` — ``respondents.pandemic_weight`` (BLS ``TU20FWGT``, the
  2020-adjusted method) with replicates
  ``atus.pandemic_replicate_weights.tu20fwgt001..160``. Defined only for 2019
  and 2020, and only for the comparable collection windows Jan 1-Mar 17 and
  May 10-Dec 31 (2019 diaries inside the excluded window carry zero weight),
  representing 312 person-days in 2019 and 313 in 2020.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from .errors import UnsupportedAnalysisError
from .spec import PopulationFilter

# The 2020 data-collection suspension: no diary days Mar 18 - May 9, 2020, and
# TU20FWGT (for both 2019 and 2020) represents only the days outside the
# equivalent window (source: bls.gov/tus/notices/2021/covid19tech.htm and
# User's Guide ch. 2/7).
PANDEMIC_EXCLUDED_WINDOW = ((3, 18), (5, 9))


@dataclass(frozen=True)
class WeightScheme:
    name: str
    point_column: str            # column on atus.respondents
    replicate_table: str
    replicate_prefix: str        # 160 columns <prefix>001 .. <prefix>160
    bls_variable: str

    @property
    def replicate_columns(self) -> tuple[str, ...]:
        return tuple(f"{self.replicate_prefix}{i:03d}" for i in range(1, 161))


MULTIYEAR = WeightScheme(
    name="multiyear",
    point_column="final_weight",
    replicate_table="atus.replicate_weights",
    replicate_prefix="tufnwgtp",
    bls_variable="TUFNWGTP",
)

PANDEMIC = WeightScheme(
    name="pandemic",
    point_column="pandemic_weight",
    replicate_table="atus.pandemic_replicate_weights",
    replicate_prefix="tu20fwgt",
    bls_variable="TU20FWGT",
)

_SCHEMES = {scheme.name: scheme for scheme in (MULTIYEAR, PANDEMIC)}


def get_scheme(name: str) -> WeightScheme:
    return _SCHEMES[name]


def validate_scheme_for_years(scheme: WeightScheme, years: tuple[int, ...]) -> None:
    """Refuse combinations the ATUS weighting structure cannot support."""
    if scheme is MULTIYEAR and 2020 in years:
        others = [y for y in years if y != 2020]
        hint = (
            "Estimate 2020 separately with weights='pandemic' (years may only be "
            "2019 and/or 2020 under that scheme)"
        )
        if others:
            hint += f", and the remaining years {others} under weights='multiyear'"
        raise UnsupportedAnalysisError(
            "TUFNWGTP (the multi-year weight) is undefined for 2020 because ATUS "
            "data collection was suspended Mar 18 - May 9, 2020 and the year was "
            "reweighted with a different method (TU20FWGT). A single estimate "
            f"pooling 2020 with this scheme is not statistically valid. {hint}."
        )
    if scheme is PANDEMIC:
        invalid = [y for y in years if y not in (2019, 2020)]
        if invalid:
            raise UnsupportedAnalysisError(
                f"TU20FWGT (the pandemic weight) exists only for 2019 and 2020; "
                f"requested years {invalid} are not covered. Use weights='multiyear' "
                "for years other than 2019-2020."
            )


def _overlap_days(start_a: date, end_a: date, start_b: date, end_b: date) -> int:
    """Inclusive-day overlap between two date ranges."""
    start, end = max(start_a, start_b), min(end_a, end_b)
    return max(0, (end - start).days + 1)


def days_represented(
    scheme: WeightScheme, year: int, population: PopulationFilter
) -> int:
    """Person-days per person that the weights represent for one year,
    honoring any diary-date window in the population filter.

    Under the pandemic scheme the excluded Mar 18 - May 9 window (in both 2019
    and 2020) is subtracted, matching how TU20FWGT was calibrated.
    """
    year_start, year_end = date(year, 1, 1), date(year, 12, 31)
    window_start = population.diary_date_min or date.min
    window_end = population.diary_date_max or date.max
    days = _overlap_days(year_start, year_end, window_start, window_end)

    if scheme is PANDEMIC:
        (m1, d1), (m2, d2) = PANDEMIC_EXCLUDED_WINDOW
        excluded = _overlap_days(
            max(year_start, window_start), min(year_end, window_end),
            date(year, m1, d1), date(year, m2, d2),
        )
        days -= excluded
    return days


def total_days_represented(
    scheme: WeightScheme, years: tuple[int, ...], population: PopulationFilter
) -> int:
    """Denominator D for person-count estimates over the whole period
    (User's Guide: for multi-year estimates, D is the sum of days across the
    years, e.g. 1,461 for 2003-06)."""
    return sum(days_represented(scheme, year, population) for year in years)


def year_calendar_days(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


__all__ = [
    "WeightScheme", "MULTIYEAR", "PANDEMIC", "get_scheme",
    "validate_scheme_for_years", "days_represented", "total_days_represented",
    "year_calendar_days", "PANDEMIC_EXCLUDED_WINDOW",
]
