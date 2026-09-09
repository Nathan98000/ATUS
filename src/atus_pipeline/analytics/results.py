"""Structured, serializable analysis results.

Every result carries enough metadata to answer: what was calculated, for
whom, over which period, with which weight and variance method, from how many
respondents, and under which analytical-engine version — plus explicit
methodological warnings that a future API/UI is expected to surface, not
hide. ``to_dict()`` output is JSON-ready.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class EstimateValue:
    """A number with its uncertainty and unit."""

    value: float
    unit: str
    standard_error: float | None = None
    confidence_level: float | None = None
    ci_lower: float | None = None
    ci_upper: float | None = None


@dataclass(frozen=True)
class ActivityInfo:
    label: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    leaf_code_count: int


@dataclass(frozen=True)
class WeightInfo:
    scheme: str            # "multiyear" | "pandemic"
    bls_variable: str      # TUFNWGTP | TU20FWGT
    column: str            # respondents.<column>


@dataclass(frozen=True)
class EstimateResult:
    """A single (possibly pooled multi-year) estimate."""

    measure: str
    estimate: EstimateValue
    years: tuple[int, ...]
    activity: ActivityInfo
    population: str                    # human-readable population definition
    n_respondents: int                 # unweighted sample size
    n_participants: int                # unweighted respondents with any time in the activity
    weighted_population_per_day: float  # persons represented on an average day (Σw / D)
    days_in_period: int
    weight: WeightInfo
    variance_method: str
    analytics_version: str
    spec: dict                         # the exact serialized AnalysisSpec
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TrendPoint:
    """One year of a trend. ``estimate`` is None when the year cannot be
    validly estimated under the requested scheme (the reason says why)."""

    year: int
    estimate: EstimateValue | None
    n_respondents: int | None
    n_participants: int | None
    weighted_population_per_day: float | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class TrendResult:
    measure: str
    points: tuple[TrendPoint, ...]
    activity: ActivityInfo
    population: str
    weight: WeightInfo
    variance_method: str
    analytics_version: str
    spec: dict
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ComparisonResult:
    """Group A vs group B plus their difference (A − B).

    The difference's standard error is computed from the per-replicate
    differences, which accounts for the covariance between the two group
    estimates (the groups come from the same sample and share the replicate
    structure).
    """

    measure: str
    label_a: str
    label_b: str
    group_a: EstimateResult
    group_b: EstimateResult
    difference: EstimateValue
    analytics_version: str
    warnings: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict:
        return asdict(self)
