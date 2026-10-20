"""Analytical error hierarchy.

Statistical software must fail loudly: an invalid or methodologically
unsupported analysis raises one of these errors with an explanation, and the
engine never silently falls back to a different weight, estimator, or
population than the one requested.
"""

from __future__ import annotations


class AnalyticsError(Exception):
    """Base class for all analytical-engine errors."""


class InvalidSpecError(AnalyticsError):
    """The analysis specification is malformed (bad enum value, empty years,
    impossible age range, unknown field, ...)."""


class UnknownActivityError(AnalyticsError):
    """An activity code does not exist in the loaded BLS coding lexicon."""


class UnsupportedAnalysisError(AnalyticsError):
    """The request is well-formed but cannot be answered with valid
    methodology (e.g. 2020 under the multi-year weight, or a variance request
    the available replicate weights cannot support)."""


class InsufficientDataError(AnalyticsError):
    """The selected population contains no (or too little) data to produce
    the requested estimate — returned instead of a misleading number."""
