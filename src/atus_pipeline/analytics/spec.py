"""The analysis model: how an ATUS analysis is described as data.

Every analysis is a serializable :class:`AnalysisSpec` (or
:class:`ComparisonSpec`) built from three orthogonal parts:

* **data selection** — which respondent-days qualify (:class:`PopulationFilter`,
  ``years``, and the weight scheme's validity rules);
* **measurement** — what per-respondent quantity is measured
  (:class:`ActivitySelector` + :class:`Measure`);
* **estimation** — how respondent measurements become a population estimate
  (weight scheme, variance method — resolved in ``weights.py`` /
  ``estimators.py`` / ``variance.py``).

Specs are plain frozen dataclasses with ``to_dict``/``from_dict`` so the same
analysis can be expressed by tests, the CLI, and a future API, and stored as
JSON for reproducibility.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field, fields
from datetime import date

from .errors import InvalidSpecError


class Measure(enum.StrEnum):
    """Supported measures, matching ATUS User's Guide ch. 7.4 estimators."""

    AVERAGE_MINUTES_PER_DAY = "average_minutes_per_day"
    PARTICIPATION_RATE = "participation_rate"
    AVERAGE_MINUTES_PER_PARTICIPANT = "average_minutes_per_participant"
    PARTICIPANTS_PER_DAY = "participants_per_day"


VALID_WEIGHT_SCHEMES = ("multiyear", "pandemic")
VALID_VARIANCE_METHODS = ("replicate", "none")
VALID_SEX = ("male", "female")
VALID_EMPLOYMENT = ("employed", "unemployed", "not_in_labor_force")
VALID_EDUCATION = (
    "less_than_high_school", "high_school", "some_college_or_associate",
    "bachelor_or_higher",
)
VALID_DAY_TYPE = ("weekday", "weekend")


@dataclass(frozen=True)
class ActivitySelector:
    """Selects activity codes from the harmonized 2003-25 lexicon.

    ``include``/``exclude`` are tuples of 2-, 4-, or 6-digit codes. A 2- or
    4-digit code means *that tier and all of its descendants* (the lexicon is
    strictly prefix-hierarchical, so tier membership equals code prefix). An
    episode is selected when its 6-digit code falls under any ``include``
    entry and under no ``exclude`` entry. Codes are validated against the
    loaded ``atus.activity_*`` lexicon tables before any query runs.
    """

    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.include:
            raise InvalidSpecError("ActivitySelector.include must not be empty")
        for code in (*self.include, *self.exclude):
            if not (isinstance(code, str) and code.isdigit() and len(code) in (2, 4, 6)):
                raise InvalidSpecError(
                    f"Activity code {code!r} must be a 2-, 4-, or 6-digit code string"
                )

    @classmethod
    def preset(cls, name: str) -> ActivitySelector:
        try:
            return ACTIVITY_PRESETS[name]
        except KeyError:
            known = ", ".join(sorted(ACTIVITY_PRESETS))
            raise InvalidSpecError(
                f"Unknown activity preset {name!r}. Known presets: {known}"
            ) from None


# Named selectors with documented definitions. The BLS news-release presets
# reproduce published table categories exactly (verified in the benchmark
# suite); see docs/analytics.md for sources.
ACTIVITY_PRESETS: dict[str, ActivitySelector] = {
    # ATUS User's Guide ch. 7.4: TV watching = 120303 + 120304.
    "watching_tv": ActivitySelector(("120303", "120304"), label="Watching TV"),
    "sleep": ActivitySelector(("0101",), label="Sleeping"),
    "work_main_and_other_jobs": ActivitySelector(
        ("050101", "050102", "050189"), label="Working (main and other jobs)"
    ),
    "housework": ActivitySelector(("0201",), label="Housework"),
    "eating_and_drinking": ActivitySelector(("11",), label="Eating and drinking"),
    # News-release major categories "include related travel time"
    # (www.bls.gov/news.release/atus.tn.htm); travel tiers are 18xx.
    "household_activities_bls_table": ActivitySelector(
        include=("02", "1802"),
        exclude=("020903", "020904"),  # A-1 groups household mail/e-mail under
        label="Household activities (BLS table A-1 definition)",
        # "Telephone calls, mail, and e-mail" instead of household activities
    ),
    "eating_and_drinking_bls_table": ActivitySelector(
        ("11", "1811"), label="Eating and drinking (BLS table A-1 definition)"
    ),
    "leisure_and_sports_bls_table": ActivitySelector(
        ("12", "13", "1812", "1813"), label="Leisure and sports (BLS table A-1 definition)"
    ),
    "work_related_bls_table": ActivitySelector(
        ("05", "1805"), label="Working and work-related activities (BLS table A-1 definition)"
    ),
}


@dataclass(frozen=True)
class PopulationFilter:
    """Respondent-level population restriction.

    All conditions are defined at the respondent (diary-day) grain. A filter
    on a dimension **excludes respondents whose value is missing/NULL for
    that dimension** (missing is never treated as "no"); leaving a dimension
    unset includes everyone. With no filters, the population is the full ATUS
    target universe: the U.S. civilian noninstitutional population age 15+.

    Sources of each dimension (see docs/analytics.md for codings):
    age/sex — ATUS roster (interview-time); employment — TELFS;
    has_household_children — TRCHILDNUM > 0; region/state/education — CPS
    (measured 2-5 months before the diary day); day_type — diary day of week;
    diary_date_min/max — restricts to a within-year period (estimates then
    represent the average day of that period).
    """

    age_min: int | None = None
    age_max: int | None = None
    sex: str | None = None
    employment_status: str | None = None
    has_household_children: bool | None = None
    region: int | None = None
    state_fips: str | None = None
    education_level: str | None = None
    day_type: str | None = None
    diary_date_min: date | None = None
    diary_date_max: date | None = None

    def __post_init__(self) -> None:
        for bound in (self.age_min, self.age_max):
            if bound is not None and not (0 <= bound <= 130):
                raise InvalidSpecError(f"Age bound {bound} is out of range")
        if (
            self.age_min is not None and self.age_max is not None
            and self.age_min > self.age_max
        ):
            raise InvalidSpecError(f"age_min {self.age_min} > age_max {self.age_max}")
        _check_choice("sex", self.sex, VALID_SEX)
        _check_choice("employment_status", self.employment_status, VALID_EMPLOYMENT)
        _check_choice("education_level", self.education_level, VALID_EDUCATION)
        _check_choice("day_type", self.day_type, VALID_DAY_TYPE)
        if self.region is not None and self.region not in (1, 2, 3, 4):
            raise InvalidSpecError(
                f"region must be a Census region 1-4 (got {self.region})"
            )
        if self.state_fips is not None and not (
            isinstance(self.state_fips, str) and len(self.state_fips) == 2
            and self.state_fips.isdigit()
        ):
            raise InvalidSpecError(
                f"state_fips must be a 2-digit FIPS string, got {self.state_fips!r}"
            )
        if (
            self.diary_date_min is not None and self.diary_date_max is not None
            and self.diary_date_min > self.diary_date_max
        ):
            raise InvalidSpecError("diary_date_min is after diary_date_max")

    def is_unrestricted(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))

    def describe(self) -> str:
        if self.is_unrestricted():
            return "civilian noninstitutional population age 15+"
        parts = []
        if self.age_min is not None or self.age_max is not None:
            lo = self.age_min if self.age_min is not None else "15"
            hi = self.age_max if self.age_max is not None else "85+ (topcoded)"
            parts.append(f"age {lo}-{hi}")
        for name in ("sex", "employment_status", "education_level", "day_type"):
            value = getattr(self, name)
            if value is not None:
                parts.append(f"{name}={value}")
        if self.has_household_children is not None:
            parts.append(
                "household children under 18 present"
                if self.has_household_children else "no household children under 18"
            )
        if self.region is not None:
            parts.append(f"Census region {self.region}")
        if self.state_fips is not None:
            parts.append(f"state FIPS {self.state_fips}")
        if self.diary_date_min or self.diary_date_max:
            parts.append(
                f"diary dates {self.diary_date_min or '(start)'} to "
                f"{self.diary_date_max or '(end of period)'}"
            )
        return ", ".join(parts)


def _check_choice(name: str, value: str | None, valid: tuple[str, ...]) -> None:
    if value is not None and value not in valid:
        raise InvalidSpecError(f"{name} must be one of {valid}, got {value!r}")


@dataclass(frozen=True)
class AnalysisSpec:
    """A complete, reproducible description of one estimate (or trend)."""

    measure: Measure
    activity: ActivitySelector
    years: tuple[int, ...]
    population: PopulationFilter = field(default_factory=PopulationFilter)
    weights: str = "multiyear"
    variance: str = "replicate"
    confidence_level: float = 0.95

    def __post_init__(self) -> None:
        if isinstance(self.measure, str) and not isinstance(self.measure, Measure):
            object.__setattr__(self, "measure", Measure(self.measure))
        if not self.years:
            raise InvalidSpecError("years must not be empty")
        seen = set()
        for year in self.years:
            if not isinstance(year, int) or not 2003 <= year <= 2100:
                raise InvalidSpecError(f"Invalid survey year {year!r} (ATUS begins in 2003)")
            if year in seen:
                raise InvalidSpecError(f"Year {year} appears more than once")
            seen.add(year)
        _check_choice("weights", self.weights, VALID_WEIGHT_SCHEMES)
        _check_choice("variance", self.variance, VALID_VARIANCE_METHODS)
        if not 0.5 < self.confidence_level < 1.0:
            raise InvalidSpecError(
                f"confidence_level must be in (0.5, 1.0), got {self.confidence_level}"
            )

    # -- serialization ----------------------------------------------------- #

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["measure"] = self.measure.value
        payload["activity"] = {
            "include": list(self.activity.include),
            "exclude": list(self.activity.exclude),
            "label": self.activity.label,
        }
        payload["years"] = list(self.years)
        population = {
            k: (v.isoformat() if isinstance(v, date) else v)
            for k, v in asdict(self.population).items() if v is not None
        }
        payload["population"] = population
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> AnalysisSpec:
        try:
            activity_raw = payload["activity"]
            if isinstance(activity_raw, str):
                activity = ActivitySelector.preset(activity_raw)
            else:
                activity = ActivitySelector(
                    include=tuple(activity_raw["include"]),
                    exclude=tuple(activity_raw.get("exclude", ())),
                    label=activity_raw.get("label"),
                )
            population_raw = dict(payload.get("population") or {})
            for key in ("diary_date_min", "diary_date_max"):
                if isinstance(population_raw.get(key), str):
                    population_raw[key] = date.fromisoformat(population_raw[key])
            known = {f.name for f in fields(PopulationFilter)}
            unknown = set(population_raw) - known
            if unknown:
                raise InvalidSpecError(f"Unknown population fields: {sorted(unknown)}")
            return cls(
                measure=Measure(payload["measure"]),
                activity=activity,
                years=tuple(payload["years"]),
                population=PopulationFilter(**population_raw),
                weights=payload.get("weights", "multiyear"),
                variance=payload.get("variance", "replicate"),
                confidence_level=payload.get("confidence_level", 0.95),
            )
        except KeyError as exc:
            raise InvalidSpecError(f"Analysis spec is missing required field {exc}") from exc
        except ValueError as exc:
            raise InvalidSpecError(str(exc)) from exc


def merge_filters(shared: PopulationFilter, group: PopulationFilter) -> PopulationFilter:
    """Combine a shared restriction with a group-defining restriction.

    Age and diary-date bounds combine to the more restrictive interval; any
    other dimension set differently in both filters is a conflict (the intent
    would be ambiguous), which raises :class:`InvalidSpecError`.
    """
    merged: dict[str, object] = {}
    for f in fields(PopulationFilter):
        a, b = getattr(shared, f.name), getattr(group, f.name)
        if f.name in ("age_min", "diary_date_min"):
            candidates = [v for v in (a, b) if v is not None]
            merged[f.name] = max(candidates) if candidates else None
        elif f.name in ("age_max", "diary_date_max"):
            candidates = [v for v in (a, b) if v is not None]
            merged[f.name] = min(candidates) if candidates else None
        elif a is not None and b is not None and a != b:
            raise InvalidSpecError(
                f"Comparison group filter conflicts with the shared population "
                f"filter on {f.name!r}: {a!r} vs {b!r}"
            )
        else:
            merged[f.name] = a if a is not None else b
    return PopulationFilter(**merged)


@dataclass(frozen=True)
class ComparisonSpec:
    """Two populations, one measure: A vs B with a difference estimate.

    Both groups share the measure, activity, years, weighting, and the
    ``base.population`` restriction (each group filter is merged onto it via
    :func:`merge_filters`). Sharing the design means the per-replicate
    difference correctly accounts for the covariance between the
    (overlapping-sample) group estimates.
    """

    base: AnalysisSpec
    group_a: PopulationFilter
    group_b: PopulationFilter
    label_a: str = "group_a"
    label_b: str = "group_b"

    def spec_for(self, which: str) -> AnalysisSpec:
        group = self.group_a if which == "a" else self.group_b
        return AnalysisSpec(
            measure=self.base.measure,
            activity=self.base.activity,
            years=self.base.years,
            population=merge_filters(self.base.population, group),
            weights=self.base.weights,
            variance=self.base.variance,
            confidence_level=self.base.confidence_level,
        )
