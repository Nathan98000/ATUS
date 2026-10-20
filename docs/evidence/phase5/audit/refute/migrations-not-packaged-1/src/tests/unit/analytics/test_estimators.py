"""Toy-data tests for the statistical estimators, isolated from ATUS data.

Sufficient statistics are built by hand from tiny respondent tables so every
expected value is verifiable by mental arithmetic.
"""

import pytest

from atus_pipeline.analytics.errors import InsufficientDataError
from atus_pipeline.analytics.estimators import point_estimate, replicate_estimates
from atus_pipeline.analytics.queries import YearStatistics, combine
from atus_pipeline.analytics.spec import Measure


def stats_from_rows(rows: list[tuple[float, float]], year: int = 2023) -> YearStatistics:
    """rows = [(value_minutes, weight), ...] — one row per respondent."""
    return YearStatistics(
        year=year,
        n_respondents=len(rows),
        n_participants=sum(1 for x, _ in rows if x > 0),
        sum_w=sum(w for _, w in rows),
        sum_wx=sum(w * x for x, w in rows),
        sum_wp=sum(w for x, w in rows if x > 0),
    )


class TestWeightedMean:
    def test_equal_weights(self):
        # A: 10 min w=1, B: 20 min w=1 -> mean 15
        stats = stats_from_rows([(10, 1), (20, 1)])
        assert point_estimate(Measure.AVERAGE_MINUTES_PER_DAY, stats, None) == 15

    def test_unequal_weights(self):
        # A: 10 w=1, B: 20 w=3 -> (10 + 60) / 4 = 17.5
        stats = stats_from_rows([(10, 1), (20, 3)])
        assert point_estimate(Measure.AVERAGE_MINUTES_PER_DAY, stats, None) == 17.5

    def test_mean_within_observed_range(self):
        stats = stats_from_rows([(5, 2.5), (300, 1.25), (60, 7)])
        mean = point_estimate(Measure.AVERAGE_MINUTES_PER_DAY, stats, None)
        assert 5 <= mean <= 300

    def test_zero_weight_respondent_does_not_change_estimate(self):
        base = stats_from_rows([(10, 1), (20, 3)])
        with_zero = stats_from_rows([(10, 1), (20, 3), (1000, 0)])
        assert point_estimate(Measure.AVERAGE_MINUTES_PER_DAY, base, None) == \
            point_estimate(Measure.AVERAGE_MINUTES_PER_DAY, with_zero, None)

    def test_zero_weight_sum_raises(self):
        stats = stats_from_rows([(10, 0), (20, 0)])
        with pytest.raises(InsufficientDataError):
            point_estimate(Measure.AVERAGE_MINUTES_PER_DAY, stats, None)


class TestParticipationRate:
    def test_rate(self):
        # participants weigh 3 of total 4 -> 0.75
        stats = stats_from_rows([(0, 1), (30, 1), (500, 2)])
        assert point_estimate(Measure.PARTICIPATION_RATE, stats, None) == 0.75

    def test_rate_bounds(self):
        stats = stats_from_rows([(0, 1), (10, 2), (0, 5), (1440, 1)])
        rate = point_estimate(Measure.PARTICIPATION_RATE, stats, None)
        assert 0.0 <= rate <= 1.0

    def test_zero_minutes_is_not_participation(self):
        stats = stats_from_rows([(0, 1), (0, 1)])
        assert point_estimate(Measure.PARTICIPATION_RATE, stats, None) == 0.0


class TestPerParticipant:
    def test_mean_among_participants(self):
        # participants: 30 w=1 and 90 w=1 -> 60; the 0-minute respondent is
        # excluded from the denominator but not from the population rate
        stats = stats_from_rows([(0, 8), (30, 1), (90, 1)])
        assert point_estimate(Measure.AVERAGE_MINUTES_PER_PARTICIPANT, stats, None) == 60

    def test_no_participants_raises(self):
        stats = stats_from_rows([(0, 1), (0, 3)])
        with pytest.raises(InsufficientDataError, match="nobody"):
            point_estimate(Measure.AVERAGE_MINUTES_PER_PARTICIPANT, stats, None)


class TestParticipantsPerDay:
    def test_person_days_divided_by_days(self):
        # weights are person-days: 730 participant person-days / 365 days = 2 persons
        stats = stats_from_rows([(60, 730), (0, 365)])
        assert point_estimate(Measure.PARTICIPANTS_PER_DAY, stats, 365) == 2.0

    def test_days_required(self):
        stats = stats_from_rows([(60, 730)])
        with pytest.raises(InsufficientDataError):
            point_estimate(Measure.PARTICIPANTS_PER_DAY, stats, None)


class TestCombine:
    def test_pooling_sums_statistics(self):
        y1 = stats_from_rows([(10, 1)], year=2003)
        y2 = stats_from_rows([(20, 3)], year=2004)
        pooled = combine([y1, y2])
        assert pooled.n_respondents == 2
        assert point_estimate(Measure.AVERAGE_MINUTES_PER_DAY, pooled, None) == 17.5

    def test_pooling_replicates(self):
        y1 = YearStatistics(2003, 1, 1, 1.0, 10.0, 1.0, (1.0, 2.0), (10.0, 20.0), (1.0, 2.0))
        y2 = YearStatistics(2004, 1, 1, 3.0, 60.0, 3.0, (3.0, 2.0), (60.0, 40.0), (3.0, 2.0))
        pooled = combine([y1, y2])
        assert pooled.rep_sum_w == (4.0, 4.0)
        assert pooled.rep_sum_wx == (70.0, 60.0)


class TestReplicateEstimates:
    def test_each_replicate_is_the_same_estimator(self):
        rep_w = tuple([2.0] + [1.0] * 159)
        rep_wx = tuple([40.0] + [15.0] * 159)
        rep_wp = tuple([2.0] + [1.0] * 159)
        stats = YearStatistics(2023, 2, 2, 1.0, 15.0, 1.0, rep_w, rep_wx, rep_wp)
        reps = replicate_estimates(Measure.AVERAGE_MINUTES_PER_DAY, stats, None)
        assert len(reps) == 160
        assert reps[0] == 20.0
        assert reps[1] == 15.0

    def test_zero_replicate_denominator_raises_with_guidance(self):
        rep_w = tuple([0.0] + [1.0] * 159)
        stats = YearStatistics(
            2023, 1, 1, 1.0, 15.0, 1.0, rep_w, tuple([0.0] * 160), tuple([0.0] * 160)
        )
        with pytest.raises(InsufficientDataError, match="Replicate weight 1"):
            replicate_estimates(Measure.AVERAGE_MINUTES_PER_DAY, stats, None)
