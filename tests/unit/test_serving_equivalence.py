"""The §9 equivalence criterion (serving/equivalence.py)."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from atus_pipeline.analytics import AnalysisSpec, ComparisonSpec
from atus_pipeline.serving.equivalence import (
    REL_TOL,
    compare_results,
    compare_statistics,
    random_specs,
    seed_specs,
    spec_dict,
)


def _estimate(value=480.0, se=1.25, lo=477.55, hi=482.45, **extra):
    return {
        "measure": "average_minutes_per_day",
        "estimate": {"value": value, "unit": "minutes", "standard_error": se,
                     "confidence_level": 0.95, "ci_lower": lo, "ci_upper": hi},
        "n_respondents": 9000, "n_participants": 8900,
        "weighted_population_per_day": 2.6e8, "warnings": ["a"], **extra,
    }


def test_identical_results_are_equivalent():
    assert compare_results(_estimate(), _estimate()).equivalent


@pytest.mark.parametrize("field, value", [
    ("n_respondents", 9001), ("n_participants", 8899),
    ("weighted_population_per_day", 2.6e8 * (1 + 1e-15)), ("warnings", ["b"]),
])
def test_everything_but_se_and_ci_must_be_exactly_equal(field, value):
    result = compare_results(_estimate(), _estimate(**{field: value}))
    assert not result.equivalent and field in result.mismatches[0]


def test_point_values_must_be_exactly_equal():
    changed = _estimate()
    changed["estimate"]["value"] = 480.0 + 1e-13
    assert not compare_results(_estimate(), changed).equivalent


def test_se_and_ci_may_differ_by_plan_order_noise():
    noisy = _estimate(se=1.25 * (1 + 5e-13), lo=477.55 * (1 - 3e-15), hi=482.45)
    result = compare_results(_estimate(), noisy)
    assert result.equivalent
    assert 0 < result.max_relative < REL_TOL


def test_se_beyond_the_tolerance_is_not_equivalent():
    # the bound is 1e-12 x max(|SE|, |estimate|) = 4.8e-10 minutes here
    assert compare_results(_estimate(), _estimate(se=1.25 + 4e-10)).equivalent
    assert not compare_results(_estimate(), _estimate(se=1.25 + 6e-10)).equivalent
    assert not compare_results(_estimate(), _estimate(se=1.25 * (1 + 1e-6))).equivalent


def test_zero_variance_noise_passes_only_because_of_the_scale_floor():
    """F40: every minute of the day has true SE 0; the two databases print
    7.83e-12 and 7.93e-12 (1.3e-2 relative) on an estimate of 1440."""
    full = _estimate(value=1440.0, se=7.83e-12, lo=1440.0, hi=1440.0)
    serving = _estimate(value=1440.0, se=7.93e-12, lo=1440.0, hi=1440.0)
    result = compare_results(full, serving)
    assert result.equivalent
    assert result.max_relative > 1e-2           # a purely relative bound would fail
    assert result.max_scaled < REL_TOL


def test_a_comparison_difference_is_scaled_by_its_groups():
    def comparison(diff_se):
        return {"group_a": _estimate(), "group_b": _estimate(),
                "difference": {"value": 0.0, "unit": "minutes", "standard_error": diff_se,
                               "confidence_level": 0.95, "ci_lower": -diff_se,
                               "ci_upper": diff_se}}
    assert compare_results(comparison(1e-13), comparison(2e-13)).equivalent
    assert not compare_results(comparison(1e-6), comparison(2e-6)).equivalent


def test_errors_compare_as_exact_text():
    message = "InvalidSpecError: Year(s) [2002] are not in the database"
    assert compare_results(message, message).equivalent
    assert not compare_results(message, message + ".").equivalent
    assert not compare_results(message, _estimate()).equivalent


def test_raw_statistics_need_exact_sums_and_close_replicates():
    row = (2023, 10, 4, Decimal("12.5"), Decimal("100.25"), Decimal("4.5"),
           [1.0, 2.0], [3.0, 4.0], [0.0, 1.0])
    near = row[:6] + ([1.0, 2.0 * (1 + 1e-14)], [3.0, 4.0], [1e-300, 1.0])
    assert compare_statistics([row], [near]).equivalent
    inexact = row[:3] + (Decimal("12.500001"),) + row[4:]
    assert not compare_statistics([row], [inexact]).equivalent
    far = row[:6] + ([1.0, 2.1], [3.0, 4.0], [0.0, 1.0])
    assert not compare_statistics([row], [far]).equivalent
    assert not compare_statistics([row], []).equivalent


def test_random_specs_are_reproducible_and_cover_every_operation():
    years = [2018, 2019, 2020, 2021, 2023]
    tier1 = ["01", "02", "12", "18"]
    leaves = ["010101", "020101", "120303", "180101"]
    a = random_specs(random.Random(3), years=years, tier1=tier1, leaves=leaves, n=120)
    b = random_specs(random.Random(3), years=years, tier1=tier1, leaves=leaves, n=120)
    assert [spec_dict(s) for _, s in a] == [spec_dict(s) for _, s in b]
    assert {op for op, _ in a} == {"estimate", "trend", "compare"}
    assert any(isinstance(s, AnalysisSpec) and s.weights == "pandemic" for _, s in a)
    assert all(isinstance(s, ComparisonSpec) == (op == "compare") for op, s in a)


def test_seed_specs_include_the_audit_extremes():
    specs = seed_specs([2019, 2020, 2023], ["01", "02", "12"])
    zero_variance = specs[0][1]
    assert zero_variance.activity.include == ("01", "02", "12")
    assert zero_variance.years == (2023,)
    assert any(getattr(s, "weights", None) == "pandemic" for _, s in specs)


NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
@pytest.mark.parametrize("field", ["se", "lo", "hi"])
def test_a_non_finite_se_or_ci_never_matches_a_finite_one(field, bad):
    """Review r4: NaN made ``diff > bound`` False, and inf made the bound
    inf, so a NaN or infinite SE/CI passed as equal to any finite value."""
    result = compare_results(_estimate(), _estimate(**{field: bad}))
    assert not result.equivalent
    assert not compare_results(_estimate(**{field: bad}), _estimate()).equivalent


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_the_same_non_finite_value_on_both_sides_matches(bad):
    assert compare_results(_estimate(se=bad), _estimate(se=bad)).equivalent
    assert not compare_results(_estimate(se=INF), _estimate(se=-INF)).equivalent
    assert not compare_results(_estimate(se=NAN), _estimate(se=INF)).equivalent


def test_a_non_finite_point_value_does_not_widen_the_tolerance():
    """S is taken over finite point values: an infinite estimate on both
    sides would otherwise make the bound infinite for its SE and CI."""
    a = _estimate(value=INF)
    b = _estimate(value=INF, se=1.25 * (1 + 1e-6))
    assert not compare_results(a, b).equivalent


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_raw_statistics_reject_a_non_finite_replicate(bad):
    row = (2023, 10, 4, Decimal("12.5"), Decimal("100.25"), Decimal("4.5"),
           [1.0, 2.0], [3.0, 4.0], [0.0, 1.0])
    broken = row[:6] + ([1.0, bad], [3.0, 4.0], [0.0, 1.0])
    assert not compare_statistics([row], [broken]).equivalent
    assert not compare_statistics([broken], [row]).equivalent
    # one infinite replicate on both sides must not excuse its neighbours
    wide = row[:6] + ([2.0, INF], [3.0, 4.0], [0.0, 1.0])
    other = row[:6] + ([1.0, INF], [3.0, 4.0], [0.0, 1.0])
    assert not compare_statistics([wide], [other]).equivalent


def test_raw_statistics_reject_extra_columns_and_replicates():
    row = (2023, 10, 4, Decimal("12.5"), Decimal("100.25"), Decimal("4.5"),
           [1.0, 2.0], [3.0, 4.0], [0.0, 1.0])
    assert not compare_statistics([row], [row + ([9.0, 9.0],)]).equivalent
    assert not compare_statistics([row + ([9.0, 9.0],)], [row]).equivalent
    longer = row[:6] + ([1.0, 2.0, 3.0], [3.0, 4.0], [0.0, 1.0])
    result = compare_statistics([row], [longer])
    assert not result.equivalent
