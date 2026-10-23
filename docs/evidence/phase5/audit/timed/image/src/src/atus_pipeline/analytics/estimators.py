"""Point and replicate estimators (ATUS User's Guide ch. 7.4).

Each measure is a deterministic function of the sufficient statistics
(Σw, Σw·x, Σw·p) plus, for person counts, the number of days D the weights
represent:

* average minutes per day        T̄ = Σ(w·x) / Σ(w)
* daily participation rate       P  = Σ(w·p) / Σ(w)            (proportion, 0-1)
* average minutes per participant T̄ᴾ = Σ(w·x) / Σ(w·p)
  (Σ(w·x·p) equals Σ(w·x) because x > 0 implies p = 1)
* participants per day           Num = Σ(w·p) / D              (persons)

The same functions evaluate the 160 replicate estimates by substituting each
replicate weight's sums, which is exactly how the User's Guide instructs
replicate estimates to be formed.
"""

from __future__ import annotations

from .errors import InsufficientDataError
from .queries import YearStatistics
from .spec import Measure

MEASURE_UNITS = {
    Measure.AVERAGE_MINUTES_PER_DAY: "minutes_per_day",
    Measure.PARTICIPATION_RATE: "proportion_of_population",
    Measure.AVERAGE_MINUTES_PER_PARTICIPANT: "minutes_per_day_of_participants",
    Measure.PARTICIPANTS_PER_DAY: "persons_per_day",
}


def _ratio(numerator: float, denominator: float, *, what: str, context: str) -> float:
    if denominator <= 0:
        raise InsufficientDataError(
            f"Cannot compute {what}: the {context} is zero for the selected "
            "population. Widen the population or years."
        )
    return numerator / denominator


def point_estimate(measure: Measure, stats: YearStatistics, days: int | None) -> float:
    if measure is Measure.AVERAGE_MINUTES_PER_DAY:
        return _ratio(stats.sum_wx, stats.sum_w, what=measure.value, context="weight sum")
    if measure is Measure.PARTICIPATION_RATE:
        return _ratio(stats.sum_wp, stats.sum_w, what=measure.value, context="weight sum")
    if measure is Measure.AVERAGE_MINUTES_PER_PARTICIPANT:
        return _ratio(
            stats.sum_wx, stats.sum_wp, what=measure.value,
            context="weighted participant count (nobody in the selection did this activity)",
        )
    if measure is Measure.PARTICIPANTS_PER_DAY:
        if not days or days <= 0:
            raise InsufficientDataError(
                "participants_per_day requires a positive number of days in the period"
            )
        return stats.sum_wp / days
    raise AssertionError(f"unhandled measure {measure}")  # pragma: no cover


def replicate_estimates(
    measure: Measure, stats: YearStatistics, days: int | None
) -> tuple[float, ...]:
    """Evaluate the estimator under each of the 160 replicate weights."""
    if stats.rep_sum_w is None:
        raise InsufficientDataError("replicate sums were not fetched for this analysis")
    estimates = []
    for i in range(160):
        replicate = YearStatistics(
            year=stats.year,
            n_respondents=stats.n_respondents,
            n_participants=stats.n_participants,
            sum_w=stats.rep_sum_w[i],
            sum_wx=stats.rep_sum_wx[i],
            sum_wp=stats.rep_sum_wp[i],
        )
        try:
            estimates.append(point_estimate(measure, replicate, days))
        except InsufficientDataError as exc:
            raise InsufficientDataError(
                f"Replicate weight {i + 1} has a zero denominator for this "
                "population — the selection is too small for a valid "
                "replicate-weight standard error. Widen the population, or "
                "request variance='none' for a point estimate without SE."
            ) from exc
    return tuple(estimates)
