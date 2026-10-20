"""Analysis request/response contracts.

Requests are the HTTP representation of the Phase 2 ``AnalysisSpec`` (same
field names and vocabulary — no second terminology), with fast structural
validation at the boundary; the analytical engine remains the authoritative
domain validator. Each request model converts to the domain spec via
``to_spec()``, canonicalizing order-insensitive collections (years, activity
code lists) so equivalent requests share one cache key.

Responses mirror the engine result objects' ``to_dict()`` exactly — the API
adds no fields to and removes none from the analytical result, so CLI and API
consumers see identical structures. All example values in this module are
real outputs verified against the loaded 2003-25 database (see
docs/examples.md); they are illustrative examples, not contractual constants.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...analytics.spec import (
    ActivitySelector,
    AnalysisSpec,
    ComparisonSpec,
    Measure,
    PopulationFilter,
)

_STRICT = ConfigDict(extra="forbid")

Year = Annotated[int, Field(ge=2003, le=2100, description="ATUS survey year")]
ActivityCode = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{2}([0-9]{2})?([0-9]{2})?$",
        description="2-, 4-, or 6-digit harmonized lexicon code",
    ),
]


class ActivitySelection(BaseModel):
    """Which activities to measure: a named preset, or explicit lexicon codes.

    A 2- or 4-digit code selects that tier and all of its descendants; an
    episode matches when its 6-digit code falls under any `include` entry and
    under no `exclude` entry. Codes are validated against the loaded lexicon.
    """

    model_config = _STRICT

    preset: str | None = Field(
        default=None,
        description="Named selection (see GET /api/v1/activities/presets).",
    )
    include: list[ActivityCode] | None = Field(
        default=None, max_length=100, description="Codes/tier prefixes to include."
    )
    exclude: list[ActivityCode] = Field(
        default_factory=list, max_length=100, description="Codes/tier prefixes to exclude."
    )
    label: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _exactly_one_form(self) -> ActivitySelection:
        if self.preset is not None:
            if self.include is not None or self.exclude or self.label is not None:
                raise ValueError("Use either 'preset' alone, or 'include'/'exclude'/'label'.")
        elif not self.include:
            raise ValueError("Provide 'preset' or a non-empty 'include' list.")
        return self

    def to_selector(self) -> ActivitySelector:
        if self.preset is not None:
            return ActivitySelector.preset(self.preset)
        # Prefix matching is set-based, so sorting is canonical, not semantic.
        return ActivitySelector(
            include=tuple(sorted(self.include or ())),
            exclude=tuple(sorted(self.exclude)),
            label=self.label,
        )


class PopulationRequest(BaseModel):
    """Respondent-level population restriction (all dimensions optional).

    Filtering on a dimension excludes respondents whose value is missing for
    it — missing is never treated as "no". See GET /api/v1/population/metadata
    for dimension semantics and valid values.
    """

    model_config = _STRICT

    age_min: int | None = Field(default=None, ge=0, le=130)
    age_max: int | None = Field(default=None, ge=0, le=130)
    sex: Literal["male", "female"] | None = None
    employment_status: Literal["employed", "unemployed", "not_in_labor_force"] | None = None
    has_household_children: bool | None = None
    region: int | None = Field(
        default=None, ge=1, le=4, description="Census region: 1 NE, 2 MW, 3 South, 4 West"
    )
    state_fips: str | None = Field(default=None, pattern=r"^[0-9]{2}$")
    education_level: (
        Literal[
            "less_than_high_school", "high_school",
            "some_college_or_associate", "bachelor_or_higher",
        ] | None
    ) = None
    day_type: Literal["weekday", "weekend"] | None = None
    diary_date_min: date | None = None
    diary_date_max: date | None = None

    def to_filter(self) -> PopulationFilter:
        return PopulationFilter(**self.model_dump())


class AnalysisRequestBase(BaseModel):
    model_config = _STRICT

    measure: Literal[
        "average_minutes_per_day", "participation_rate",
        "average_minutes_per_participant", "participants_per_day",
    ] = Field(
        default="average_minutes_per_day",
        description="ATUS User's Guide ch. 7.4 estimator (see GET /api/v1/meta).",
    )
    activity: ActivitySelection
    years: list[Year] = Field(
        min_length=1, max_length=50,
        description="Survey years. Order-insensitive; duplicates are rejected.",
    )
    population: PopulationRequest = Field(default_factory=PopulationRequest)
    weights: Literal["multiyear", "pandemic"] = Field(
        default="multiyear",
        description="Weight scheme: 'multiyear' (TUFNWGTP; refuses 2020) or "
                    "'pandemic' (TU20FWGT; 2019/2020 only).",
    )
    variance: Literal["replicate", "none"] = Field(
        default="replicate",
        description="'replicate' = official 160-replicate-weight standard errors; "
                    "'none' = point estimate only (faster).",
    )
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1.0)

    def to_spec(self) -> AnalysisSpec:
        return AnalysisSpec(
            measure=Measure(self.measure),
            activity=self.activity.to_selector(),
            years=tuple(sorted(self.years)),  # canonical; duplicates rejected by the engine
            population=self.population.to_filter(),
            weights=self.weights,
            variance=self.variance,
            confidence_level=self.confidence_level,
        )


class EstimateRequest(AnalysisRequestBase):
    """One estimate for the whole requested period (pooled if several years)."""

    model_config = _STRICT | {
        "json_schema_extra": {
            "examples": [
                {
                    "measure": "average_minutes_per_day",
                    "activity": {"preset": "sleep"},
                    "years": [2025],
                },
                {
                    "measure": "participation_rate",
                    "activity": {"include": ["120303", "120304"], "label": "Watching TV"},
                    "years": [2024],
                    "variance": "replicate",
                },
                {
                    "measure": "average_minutes_per_day",
                    "activity": {"preset": "sleep"},
                    "years": [2020],
                    "weights": "pandemic",
                    "population": {"diary_date_min": "2020-05-10"},
                },
            ]
        }
    }


class TrendRequest(AnalysisRequestBase):
    """Per-year estimates. Years that cannot be validly estimated (e.g. 2020
    under the multiyear weights) come back as explicit unavailable points."""

    model_config = _STRICT | {
        "json_schema_extra": {
            "examples": [
                {
                    "measure": "average_minutes_per_day",
                    "activity": {"preset": "leisure_and_sports_bls_table"},
                    "years": list(range(2003, 2026)),
                }
            ]
        }
    }


class CompareRequest(AnalysisRequestBase):
    """Two populations, one measure. Group filters are merged onto the shared
    `population` filter; the difference's standard error is computed from
    per-replicate differences (covariance-correct)."""

    group_a: PopulationRequest
    group_b: PopulationRequest
    label_a: str = Field(default="group_a", max_length=80)
    label_b: str = Field(default="group_b", max_length=80)

    model_config = _STRICT | {
        "json_schema_extra": {
            "examples": [
                {
                    "measure": "average_minutes_per_day",
                    "activity": {"preset": "household_activities_bls_table"},
                    "years": [2025],
                    "group_a": {"sex": "male"},
                    "group_b": {"sex": "female"},
                    "label_a": "Men",
                    "label_b": "Women",
                }
            ]
        }
    }

    def to_comparison(self) -> ComparisonSpec:
        return ComparisonSpec(
            base=self.to_spec(),
            group_a=self.group_a.to_filter(),
            group_b=self.group_b.to_filter(),
            label_a=self.label_a,
            label_b=self.label_b,
        )


# --------------------------------------------------------------------------- #
# Responses (1:1 with the engine result objects' to_dict())                   #
# --------------------------------------------------------------------------- #


class EstimateValueModel(BaseModel):
    value: float = Field(description="The estimate, in `unit`.")
    unit: str = Field(
        description="minutes_per_day | proportion_of_population (0-1) | "
                    "minutes_per_day_of_participants | persons_per_day"
    )
    standard_error: float | None = Field(
        default=None,
        description="Replicate-weight SE (User's Guide ch. 7.5); null when variance='none'.",
    )
    confidence_level: float | None = None
    ci_lower: float | None = None
    ci_upper: float | None = None


class ActivityInfoModel(BaseModel):
    label: str
    include: list[str]
    exclude: list[str]
    leaf_code_count: int = Field(description="Number of 6-digit codes the selection resolves to.")


class WeightInfoModel(BaseModel):
    scheme: str
    bls_variable: str = Field(description="TUFNWGTP or TU20FWGT")
    column: str


class EstimateResponse(BaseModel):
    measure: str
    estimate: EstimateValueModel
    years: list[int]
    activity: ActivityInfoModel
    population: str = Field(description="Human-readable population definition.")
    n_respondents: int = Field(description="UNWEIGHTED sample size (respondents).")
    n_participants: int = Field(
        description="UNWEIGHTED respondents with any time in the activity."
    )
    weighted_population_per_day: float = Field(
        description="Persons represented on an average day of the period (Σweights / days)."
    )
    days_in_period: int
    weight: WeightInfoModel
    variance_method: str
    analytics_version: str
    spec: dict = Field(
        description="The exact canonical analysis specification — resubmit it to "
                    "reproduce this result."
    )
    warnings: list[str] = Field(
        description="Methodological notes a client should display, never discard."
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "measure": "average_minutes_per_day",
                    "estimate": {
                        "value": 541.9642003795799,
                        "unit": "minutes_per_day",
                        "standard_error": 2.166215578745084,
                        "confidence_level": 0.95,
                        "ci_lower": 537.7184958624899,
                        "ci_upper": 546.2099048966699,
                    },
                    "years": [2025],
                    "activity": {
                        "label": "Sleeping", "include": ["0101"], "exclude": [],
                        "leaf_code_count": 3,
                    },
                    "population": "civilian noninstitutional population age 15+",
                    "n_respondents": 6146,
                    "n_participants": 6139,
                    "weighted_population_per_day": 277984657.483597,
                    "days_in_period": 365,
                    "weight": {
                        "scheme": "multiyear", "bls_variable": "TUFNWGTP",
                        "column": "respondents.final_weight",
                    },
                    "variance_method": "replicate",
                    "analytics_version": "0.2",
                    "spec": {
                        "measure": "average_minutes_per_day",
                        "activity": {"include": ["0101"], "exclude": [], "label": "Sleeping"},
                        "years": [2025], "population": {}, "weights": "multiyear",
                        "variance": "replicate", "confidence_level": 0.95,
                    },
                    "warnings": [
                        "Activity codes use the BLS-harmonized 2003-25 multi-year "
                        "lexicon (cross-year comparable by construction)."
                    ],
                }
            ]
        }
    }


class TrendPointModel(BaseModel):
    year: int
    estimate: EstimateValueModel | None = Field(
        default=None,
        description="null when the year cannot be validly estimated — see "
                    "unavailable_reason. Never serialized as zero.",
    )
    n_respondents: int | None = None
    n_participants: int | None = None
    weighted_population_per_day: float | None = None
    unavailable_reason: str | None = None


class TrendResponse(BaseModel):
    measure: str
    points: list[TrendPointModel]
    activity: ActivityInfoModel
    population: str
    weight: WeightInfoModel
    variance_method: str
    analytics_version: str
    spec: dict
    warnings: list[str]


class CompareResponse(BaseModel):
    measure: str
    label_a: str
    label_b: str
    group_a: EstimateResponse
    group_b: EstimateResponse
    difference: EstimateValueModel = Field(
        description="group_a − group_b; SE from per-replicate differences "
                    "(covariance-correct)."
    )
    analytics_version: str
    warnings: list[str]
