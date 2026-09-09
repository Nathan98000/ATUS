"""The analysis engine: spec in, structured result out.

Orchestrates the pipeline

    AnalysisSpec -> validation -> activity resolution -> population SQL
        -> sufficient-statistics query -> point estimator
        -> replicate variance -> result object (+ metadata, warnings)

and exposes three operations: ``estimate`` (single, possibly pooled),
``trend`` (per-year series), and ``compare`` (two populations plus a
covariance-correct difference). It is deliberately independent of the CLI and
of HTTP so Phase 3 can call it directly.
"""

from __future__ import annotations

from dataclasses import replace

import psycopg

from .activities import ActivityResolver, ResolvedActivity
from .errors import InsufficientDataError, InvalidSpecError
from .estimators import MEASURE_UNITS, point_estimate, replicate_estimates
from .population import PopulationSQL, build_population_sql
from .queries import YearStatistics, combine, fetch_year_statistics
from .results import (
    ActivityInfo,
    ComparisonResult,
    EstimateResult,
    EstimateValue,
    TrendPoint,
    TrendResult,
    WeightInfo,
)
from .spec import AnalysisSpec, ComparisonSpec, Measure
from .variance import confidence_interval, standard_error
from .weights import (
    MULTIYEAR,
    WeightScheme,
    get_scheme,
    total_days_represented,
    validate_scheme_for_years,
)

_ANALYTICS_VERSION = "0.2"  # kept in sync with atus_pipeline.analytics.ANALYTICS_VERSION

_HARMONIZATION_NOTE = (
    "Activity codes use the BLS-harmonized 2003-25 multi-year lexicon "
    "(cross-year comparable by construction)."
)
_PANDEMIC_NOTE = (
    "Pandemic weights (TU20FWGT): estimates represent only the comparable "
    "collection windows Jan 1 - Mar 17 and May 10 - Dec 31; annual estimates "
    "for 2020 are not possible."
)


class AnalysisEngine:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn
        self._resolver: ActivityResolver | None = None
        self._loaded_years: tuple[int, ...] | None = None

    # ------------------------------------------------------------------ #
    # public operations
    # ------------------------------------------------------------------ #

    def estimate(self, spec: AnalysisSpec) -> EstimateResult:
        """One estimate for the whole requested period (pooled if several
        years)."""
        prepared = self._prepare(spec)
        stats_by_year = self._fetch(spec, prepared)
        missing = [y for y in spec.years if y not in stats_by_year]
        if missing:
            raise InsufficientDataError(
                f"No respondents match the population filters in year(s) {missing}; "
                "a pooled estimate would silently misrepresent the period. "
                "Narrow `years` or widen the population."
            )
        pooled = combine([stats_by_year[y] for y in spec.years])
        result, _ = self._estimate_from_stats(spec, prepared, pooled, spec.years)
        return result

    def trend(self, spec: AnalysisSpec) -> TrendResult:
        """Per-year estimates. Years that cannot be validly estimated under
        the requested weight scheme (2020 under 'multiyear') or that match no
        respondents appear as explicit unavailable points, never silently
        dropped."""
        scheme = get_scheme(spec.weights)
        supported_years = tuple(
            y for y in spec.years if not (scheme is MULTIYEAR and y == 2020)
        )
        prepared = self._prepare(replace(spec, years=supported_years or spec.years))
        stats_by_year = (
            self._fetch(replace(spec, years=supported_years), prepared)
            if supported_years else {}
        )

        points: list[TrendPoint] = []
        warnings: set[str] = set(prepared.population_sql.warnings)
        for year in spec.years:
            if year not in supported_years:
                points.append(
                    TrendPoint(
                        year=year, estimate=None, n_respondents=None,
                        n_participants=None, weighted_population_per_day=None,
                        unavailable_reason=(
                            "TUFNWGTP is undefined for 2020 (pandemic collection "
                            "suspension); estimate 2020 separately with "
                            "weights='pandemic'."
                        ),
                    )
                )
                continue
            stats = stats_by_year.get(year)
            if stats is None:
                points.append(
                    TrendPoint(
                        year=year, estimate=None, n_respondents=None,
                        n_participants=None, weighted_population_per_day=None,
                        unavailable_reason="no respondents match the population filters",
                    )
                )
                continue
            single = replace(spec, years=(year,))
            result, _ = self._estimate_from_stats(single, prepared, stats, (year,))
            warnings.update(result.warnings)
            points.append(
                TrendPoint(
                    year=year,
                    estimate=result.estimate,
                    n_respondents=result.n_respondents,
                    n_participants=result.n_participants,
                    weighted_population_per_day=result.weighted_population_per_day,
                )
            )

        return TrendResult(
            measure=spec.measure.value,
            points=tuple(points),
            activity=self._activity_info(prepared.activity),
            population=spec.population.describe(),
            weight=self._weight_info(prepared.scheme),
            variance_method=spec.variance,
            analytics_version=_ANALYTICS_VERSION,
            spec=spec.to_dict(),
            warnings=tuple(sorted(warnings | {_HARMONIZATION_NOTE})),
        )

    def compare(self, comparison: ComparisonSpec) -> ComparisonResult:
        """Estimate two populations and their difference (A − B). The
        difference SE comes from per-replicate differences, so the covariance
        between the overlapping-sample group estimates is accounted for."""
        spec_a = comparison.spec_for("a")
        spec_b = comparison.spec_for("b")
        result_a, reps_a = self._run_pooled(spec_a)
        result_b, reps_b = self._run_pooled(spec_b)

        diff_point = result_a.estimate.value - result_b.estimate.value
        unit = result_a.estimate.unit
        if reps_a is not None and reps_b is not None:
            diffs = tuple(a - b for a, b in zip(reps_a, reps_b, strict=True))
            se = standard_error(diff_point, diffs)
            interval = confidence_interval(diff_point, se, comparison.base.confidence_level)
            difference = EstimateValue(
                value=diff_point, unit=unit, standard_error=se,
                confidence_level=comparison.base.confidence_level,
                ci_lower=interval.lower, ci_upper=interval.upper,
            )
        else:
            difference = EstimateValue(value=diff_point, unit=unit)

        warnings = tuple(
            sorted(
                set(result_a.warnings) | set(result_b.warnings)
                | {
                    "The difference's standard error uses per-replicate differences, "
                    "which accounts for covariance between the group estimates."
                }
            )
        )
        return ComparisonResult(
            measure=comparison.base.measure.value,
            label_a=comparison.label_a,
            label_b=comparison.label_b,
            group_a=result_a,
            group_b=result_b,
            difference=difference,
            analytics_version=_ANALYTICS_VERSION,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    class _Prepared:
        def __init__(
            self, scheme: WeightScheme, activity: ResolvedActivity,
            population_sql: PopulationSQL,
        ):
            self.scheme = scheme
            self.activity = activity
            self.population_sql = population_sql

    def _prepare(self, spec: AnalysisSpec) -> _Prepared:
        loaded = self._get_loaded_years()
        unknown = [y for y in spec.years if y not in loaded]
        if unknown:
            raise InvalidSpecError(
                f"Year(s) {unknown} are not in the database "
                f"(loaded: {min(loaded)}-{max(loaded)})."
            )
        scheme = get_scheme(spec.weights)
        validate_scheme_for_years(scheme, spec.years)
        if self._resolver is None:
            self._resolver = ActivityResolver(self._conn)
        activity = self._resolver.resolve(spec.activity)
        population_sql = build_population_sql(spec.population)
        return self._Prepared(scheme, activity, population_sql)

    def _fetch(self, spec: AnalysisSpec, prepared: _Prepared) -> dict[int, YearStatistics]:
        return fetch_year_statistics(
            self._conn,
            years=spec.years,
            activity=prepared.activity,
            population=prepared.population_sql,
            scheme=prepared.scheme,
            with_replicates=spec.variance == "replicate",
        )

    def _run_pooled(self, spec: AnalysisSpec) -> tuple[EstimateResult, tuple[float, ...] | None]:
        prepared = self._prepare(spec)
        stats_by_year = self._fetch(spec, prepared)
        missing = [y for y in spec.years if y not in stats_by_year]
        if missing:
            raise InsufficientDataError(
                f"No respondents match the population filters in year(s) {missing}."
            )
        pooled = combine([stats_by_year[y] for y in spec.years])
        return self._estimate_from_stats(spec, prepared, pooled, spec.years)

    def _estimate_from_stats(
        self,
        spec: AnalysisSpec,
        prepared: _Prepared,
        stats: YearStatistics,
        years: tuple[int, ...],
    ) -> tuple[EstimateResult, tuple[float, ...] | None]:
        days = total_days_represented(prepared.scheme, years, spec.population)
        if days <= 0:
            raise InvalidSpecError(
                "The diary-date window does not overlap the requested years."
            )
        if stats.n_respondents == 0:
            raise InsufficientDataError("No respondents match the population filters.")

        point = point_estimate(spec.measure, stats, days)
        warnings = set(prepared.population_sql.warnings)
        warnings.add(_HARMONIZATION_NOTE)
        if prepared.scheme.name == "pandemic":
            warnings.add(_PANDEMIC_NOTE)
        if len(years) > 1:
            warnings.add(
                f"Pooled estimate: represents the average day across {len(years)} "
                f"survey years ({days:,} days combined)."
            )

        replicates: tuple[float, ...] | None = None
        if spec.variance == "replicate":
            replicates = replicate_estimates(spec.measure, stats, days)
            se = standard_error(point, replicates)
            interval = confidence_interval(point, se, spec.confidence_level)
            lower, upper = interval.lower, interval.upper
            if spec.measure is Measure.PARTICIPATION_RATE:
                clipped_lower, clipped_upper = max(0.0, lower), min(1.0, upper)
                if (clipped_lower, clipped_upper) != (lower, upper):
                    warnings.add(
                        "Confidence interval truncated to [0, 1]; the normal "
                        "approximation is poor for rates this close to a bound."
                    )
                lower, upper = clipped_lower, clipped_upper
            estimate = EstimateValue(
                value=point, unit=MEASURE_UNITS[spec.measure], standard_error=se,
                confidence_level=spec.confidence_level, ci_lower=lower, ci_upper=upper,
            )
        else:
            estimate = EstimateValue(value=point, unit=MEASURE_UNITS[spec.measure])

        result = EstimateResult(
            measure=spec.measure.value,
            estimate=estimate,
            years=years,
            activity=self._activity_info(prepared.activity),
            population=spec.population.describe(),
            n_respondents=stats.n_respondents,
            n_participants=stats.n_participants,
            weighted_population_per_day=stats.sum_w / days,
            days_in_period=days,
            weight=self._weight_info(prepared.scheme),
            variance_method=spec.variance,
            analytics_version=_ANALYTICS_VERSION,
            spec=spec.to_dict() | {"years": list(years)},
            warnings=tuple(sorted(warnings)),
        )
        return result, replicates

    def _get_loaded_years(self) -> tuple[int, ...]:
        if self._loaded_years is None:
            rows = self._conn.execute(
                "SELECT DISTINCT data_year FROM atus.respondents ORDER BY data_year"
            ).fetchall()
            self._loaded_years = tuple(r[0] for r in rows)
            if not self._loaded_years:
                raise InsufficientDataError(
                    "The database contains no respondents — run the Phase 1 "
                    "pipeline (`atus load`) first."
                )
        return self._loaded_years

    @staticmethod
    def _activity_info(activity: ResolvedActivity) -> ActivityInfo:
        return ActivityInfo(
            label=activity.label,
            include=activity.selector.include,
            exclude=activity.selector.exclude,
            leaf_code_count=len(activity.leaf_codes),
        )

    @staticmethod
    def _weight_info(scheme: WeightScheme) -> WeightInfo:
        return WeightInfo(
            scheme=scheme.name,
            bls_variable=scheme.bls_variable,
            column=f"respondents.{scheme.point_column}",
        )
