"""ATUS analytical engine (Phase 2).

Turns the canonical Phase 1 database into statistically defensible estimates
using official BLS methodology (ATUS User's Guide ch. 7): weighted time-use
means, participation rates, participant averages, persons-per-day totals,
replicate-weight standard errors, trends, and group comparisons.

Entry point::

    from atus_pipeline.analytics import AnalysisEngine, AnalysisSpec, ...
    engine = AnalysisEngine(connection)
    result = engine.estimate(spec)

``ANALYTICS_VERSION`` identifies the statistical implementation; it is stamped
on every result so a future change in methodology is detectable in stored or
compared outputs.
"""

from .engine import AnalysisEngine
from .errors import (
    AnalyticsError,
    InsufficientDataError,
    InvalidSpecError,
    UnknownActivityError,
    UnsupportedAnalysisError,
)
from .spec import (
    ActivitySelector,
    AnalysisSpec,
    ComparisonSpec,
    Measure,
    PopulationFilter,
)

ANALYTICS_VERSION = "0.1"

__all__ = [
    "ANALYTICS_VERSION",
    "AnalysisEngine",
    "AnalysisSpec",
    "ComparisonSpec",
    "ActivitySelector",
    "PopulationFilter",
    "Measure",
    "AnalyticsError",
    "InvalidSpecError",
    "UnknownActivityError",
    "UnsupportedAnalysisError",
    "InsufficientDataError",
]
