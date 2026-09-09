"""Tests for the replicate-weight variance formula (User's Guide ch. 7.5)."""

import math

import pytest

from atus_pipeline.analytics.variance import (
    confidence_interval,
    replicate_variance,
    standard_error,
    z_value,
)


def reps(*head: float, fill: float = 0.0) -> tuple[float, ...]:
    return tuple(list(head) + [fill] * (160 - len(head)))


class TestVarianceFormula:
    def test_hand_computed_variance(self):
        # deviations: (100-150)^2 + (200-150)^2 = 5000; rest identical to the
        # point estimate. Var = (4/160) * 5000 = 125; SE = sqrt(125).
        replicates = reps(100.0, 200.0, fill=150.0)
        assert replicate_variance(150.0, replicates) == pytest.approx(125.0)
        assert standard_error(150.0, replicates) == pytest.approx(math.sqrt(125.0))

    def test_identical_replicates_give_zero_variance(self):
        assert replicate_variance(42.0, reps(fill=42.0)) == 0.0

    def test_variance_is_nonnegative_and_symmetric(self):
        up = reps(160.0, fill=150.0)
        down = reps(140.0, fill=150.0)
        assert replicate_variance(150.0, up) == replicate_variance(150.0, down) > 0

    def test_wrong_replicate_count_rejected(self):
        with pytest.raises(ValueError, match="160"):
            replicate_variance(1.0, (1.0, 2.0))


class TestConfidenceIntervals:
    def test_z_for_95(self):
        assert z_value(0.95) == pytest.approx(1.959964, abs=1e-6)

    def test_z_for_90(self):
        assert z_value(0.90) == pytest.approx(1.644854, abs=1e-6)

    def test_interval_brackets_estimate(self):
        interval = confidence_interval(100.0, 5.0, 0.95)
        assert interval.lower < 100.0 < interval.upper
        assert interval.upper - 100.0 == pytest.approx(100.0 - interval.lower)

    def test_wider_confidence_gives_wider_interval(self):
        narrow = confidence_interval(0.0, 1.0, 0.90)
        wide = confidence_interval(0.0, 1.0, 0.99)
        assert wide.upper > narrow.upper
