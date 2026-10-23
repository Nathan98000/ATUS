"""Replicate-weight variance (successive difference replication).

ATUS User's Guide ch. 7.5:

    Var(Ŷ₀) = (4 / 160) · Σᵢ₌₁¹⁶⁰ (Ŷᵢ − Ŷ₀)²

where Ŷ₀ is the full-sample estimate and Ŷᵢ the estimate recomputed with
replicate weight i. The factor 4 comes from the replicate factors
(1.7, 1.0, 0.3) of the Fay-style modified balanced half-sample design that
ATUS inherits from the CPS. The implementation reproduces the guide's worked
example (2007 TV watching: SE = 0.0293 hours) exactly — see the benchmark
suite.

Confidence intervals use the normal approximation, estimate ± z·SE, with a
documented default of 95%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

REPLICATE_COUNT = 160
VARIANCE_FACTOR = 4.0 / REPLICATE_COUNT


def replicate_variance(point: float, replicates: tuple[float, ...]) -> float:
    if len(replicates) != REPLICATE_COUNT:
        raise ValueError(
            f"expected {REPLICATE_COUNT} replicate estimates, got {len(replicates)}"
        )
    return VARIANCE_FACTOR * sum((r - point) ** 2 for r in replicates)


def standard_error(point: float, replicates: tuple[float, ...]) -> float:
    return math.sqrt(replicate_variance(point, replicates))


def z_value(confidence_level: float) -> float:
    return NormalDist().inv_cdf(0.5 + confidence_level / 2.0)


@dataclass(frozen=True)
class Interval:
    lower: float
    upper: float


def confidence_interval(point: float, se: float, confidence_level: float) -> Interval:
    z = z_value(confidence_level)
    return Interval(lower=point - z * se, upper=point + z * se)
