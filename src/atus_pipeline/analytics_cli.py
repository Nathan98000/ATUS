"""CLI for the analytical engine (Phase 2).

This module is deliberately thin: it parses options into
:class:`~atus_pipeline.analytics.AnalysisSpec` objects, calls the engine, and
formats results. No statistical logic lives here — the same engine call is
what a future API will make.

    atus analyze estimate --activity sleep --year 2025
    atus analyze trend --activity leisure_and_sports_bls_table \\
        --start-year 2003 --end-year 2025
    atus analyze compare --activity household_activities_bls_table \\
        --year 2025 --group-a sex=male --group-b sex=female
    atus analyze run --spec my_analysis.json [--trend]
    atus validate-analytics
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import date

import click

from .analytics import (
    ActivitySelector,
    AnalysisEngine,
    AnalysisSpec,
    AnalyticsError,
    ComparisonSpec,
    Measure,
    PopulationFilter,
)
from .analytics.results import ComparisonResult, EstimateResult, EstimateValue, TrendResult
from .analytics.spec import ACTIVITY_PRESETS
from .database.connection import connect

_MEASURES = [m.value for m in Measure]

_UNIT_LABELS = {
    "minutes_per_day": "min/day",
    "minutes_per_day_of_participants": "min/day among participants",
    "proportion_of_population": "of the population",
    "persons_per_day": "persons/day",
}


def _parse_activity(activity: str, exclude: str | None, label: str | None) -> ActivitySelector:
    """`--activity` accepts a preset name or comma-separated lexicon codes."""
    if activity in ACTIVITY_PRESETS:
        if exclude or label:
            raise click.UsageError("--exclude/--label cannot modify a preset activity")
        return ActivitySelector.preset(activity)
    include = tuple(code.strip() for code in activity.split(",") if code.strip())
    excludes = tuple(code.strip() for code in (exclude or "").split(",") if code.strip())
    return ActivitySelector(include=include, exclude=excludes, label=label)


_POPULATION_FIELDS = {f.name: f for f in dataclasses.fields(PopulationFilter)}


def _parse_population_kv(pairs: str) -> PopulationFilter:
    """Parse 'key=value,key=value' into a PopulationFilter (for --group-a/b)."""
    values: dict[str, object] = {}
    for pair in (p for p in pairs.split(",") if p.strip()):
        if "=" not in pair:
            raise click.UsageError(f"Expected key=value, got {pair!r}")
        key, raw = (s.strip() for s in pair.split("=", 1))
        if key not in _POPULATION_FIELDS:
            raise click.UsageError(
                f"Unknown population field {key!r}. Fields: {', '.join(_POPULATION_FIELDS)}"
            )
        if key in ("age_min", "age_max", "region"):
            values[key] = int(raw)
        elif key == "has_household_children":
            values[key] = raw.lower() in ("1", "true", "yes")
        elif key in ("diary_date_min", "diary_date_max"):
            values[key] = date.fromisoformat(raw)
        else:
            values[key] = raw
    return PopulationFilter(**values)


def _population_options(fn):
    options = [
        click.option("--age-min", type=int, default=None),
        click.option("--age-max", type=int, default=None),
        click.option("--sex", type=click.Choice(["male", "female"]), default=None),
        click.option(
            "--employment",
            type=click.Choice(["employed", "unemployed", "not_in_labor_force"]),
            default=None,
        ),
        click.option(
            "--has-children/--no-children", "has_children", default=None,
            help="Household children under 18 present / absent.",
        ),
        click.option("--region", type=click.IntRange(1, 4), default=None,
                     help="Census region: 1 NE, 2 MW, 3 South, 4 West."),
        click.option("--state-fips", default=None, help="2-digit state FIPS, e.g. 06."),
        click.option(
            "--education",
            type=click.Choice([
                "less_than_high_school", "high_school",
                "some_college_or_associate", "bachelor_or_higher",
            ]),
            default=None,
        ),
        click.option("--day-type", type=click.Choice(["weekday", "weekend"]), default=None),
        click.option("--from-date", type=click.DateTime(["%Y-%m-%d"]), default=None,
                     help="Restrict diary dates (inclusive lower bound)."),
        click.option("--to-date", type=click.DateTime(["%Y-%m-%d"]), default=None,
                     help="Restrict diary dates (inclusive upper bound)."),
    ]
    for option in reversed(options):
        fn = option(fn)
    return fn


def _build_population(
    age_min, age_max, sex, employment, has_children, region, state_fips,
    education, day_type, from_date, to_date,
) -> PopulationFilter:
    return PopulationFilter(
        age_min=age_min,
        age_max=age_max,
        sex=sex,
        employment_status=employment,
        has_household_children=has_children,
        region=region,
        state_fips=state_fips,
        education_level=education,
        day_type=day_type,
        diary_date_min=from_date.date() if from_date else None,
        diary_date_max=to_date.date() if to_date else None,
    )


def _shared_options(fn):
    fn = click.option(
        "--activity", required=True,
        help=f"Preset ({', '.join(sorted(ACTIVITY_PRESETS))}) or comma-separated "
             "2/4/6-digit lexicon codes.",
    )(fn)
    fn = click.option("--exclude", default=None, help="Comma-separated codes to exclude.")(fn)
    fn = click.option("--label", default=None, help="Label for a custom activity selection.")(fn)
    fn = click.option(
        "--measure", type=click.Choice(_MEASURES), default=Measure.AVERAGE_MINUTES_PER_DAY.value,
    )(fn)
    fn = click.option(
        "--weights", type=click.Choice(["multiyear", "pandemic"]), default="multiyear",
        help="Weight scheme: multiyear TUFNWGTP (refuses 2020) or pandemic TU20FWGT "
             "(2019/2020 only).",
    )(fn)
    fn = click.option(
        "--no-variance", is_flag=True,
        help="Point estimate only (skip replicate-weight SE and CI).",
    )(fn)
    fn = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")(fn)
    return fn


def _format_value(value: EstimateValue) -> str:
    unit = _UNIT_LABELS.get(value.unit, value.unit)
    if value.unit == "proportion_of_population":
        text = f"{value.value * 100:.2f}% {unit}"
        if value.standard_error is not None:
            text += (
                f"   SE {value.standard_error * 100:.3f}pp"
                f"   {value.confidence_level:.0%} CI"
                f" [{value.ci_lower * 100:.2f}%, {value.ci_upper * 100:.2f}%]"
            )
        return text
    if value.unit == "persons_per_day":
        text = f"{value.value:,.0f} {unit}"
        if value.standard_error is not None:
            text += (
                f"   SE {value.standard_error:,.0f}"
                f"   {value.confidence_level:.0%} CI"
                f" [{value.ci_lower:,.0f}, {value.ci_upper:,.0f}]"
            )
        return text
    text = f"{value.value:.2f} {unit} ({value.value / 60:.2f} h)"
    if value.standard_error is not None:
        text += (
            f"   SE {value.standard_error:.3f}"
            f"   {value.confidence_level:.0%} CI [{value.ci_lower:.2f}, {value.ci_upper:.2f}]"
        )
    return text


def _print_estimate(result: EstimateResult) -> None:
    click.echo(f"{result.measure} — {result.activity.label}")
    years = ", ".join(str(y) for y in result.years)
    click.echo(
        f"Population: {result.population} | Years: {years} | "
        f"Weight: {result.weight.bls_variable} ({result.weight.scheme})"
    )
    click.echo(f"Estimate: {_format_value(result.estimate)}")
    click.echo(
        f"Sample: {result.n_respondents:,} respondents "
        f"({result.n_participants:,} participants) · represents "
        f"{result.weighted_population_per_day:,.0f} persons on an average day"
    )
    for warning in result.warnings:
        click.echo(f"note: {warning}")


def _print_trend(result: TrendResult) -> None:
    click.echo(f"{result.measure} — {result.activity.label} (by year)")
    click.echo(
        f"Population: {result.population} | Weight: {result.weight.bls_variable}"
    )
    for point in result.points:
        if point.estimate is None:
            click.echo(f"  {point.year}: unavailable — {point.unavailable_reason}")
            continue
        value = point.estimate
        if value.unit == "proportion_of_population":
            text = f"{value.value * 100:7.2f}%"
        elif value.unit == "persons_per_day":
            text = f"{value.value:15,.0f}"
        else:
            text = f"{value.value:7.1f} min ({value.value / 60:5.2f} h)"
        se = f"  SE {value.standard_error:.3f}" if value.standard_error is not None else ""
        click.echo(f"  {point.year}: {text}{se}   n={point.n_respondents:,}")
    for warning in result.warnings:
        click.echo(f"note: {warning}")


def _print_comparison(result: ComparisonResult) -> None:
    click.echo(f"{result.measure} — comparison")
    for label, group in ((result.label_a, result.group_a), (result.label_b, result.group_b)):
        click.echo(f"\n[{label}] {group.population}")
        click.echo(f"  {_format_value(group.estimate)}   n={group.n_respondents:,}")
    click.echo(f"\nDifference ({result.label_a} − {result.label_b}):")
    click.echo(f"  {_format_value(result.difference)}")
    for warning in result.warnings:
        click.echo(f"note: {warning}")


def _emit(payload: dict, as_json: bool, printer, result) -> None:
    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
    else:
        printer(result)


def _run_with_engine(fn):
    """Open a connection, run fn(engine), translate AnalyticsError to exit 2."""
    from .cli import _settings  # late import to avoid a cycle at module load

    settings = _settings()
    try:
        with connect(settings.database_url) as conn:
            return fn(AnalysisEngine(conn))
    except AnalyticsError as exc:
        click.echo(f"{type(exc).__name__}: {exc}", err=True)
        sys.exit(2)


@click.group(help="Run analyses against the loaded ATUS database (Phase 2 engine).")
def analyze() -> None:
    pass


@analyze.command(help="Single (or pooled multi-year) estimate.")
@_shared_options
@click.option(
    "--year", "years", type=int, multiple=True, required=True,
    help="Survey year; repeat to pool years.",
)
@_population_options
def estimate(
    activity, exclude, label, measure, weights, no_variance, as_json, years, **pop
) -> None:
    spec = AnalysisSpec(
        measure=Measure(measure),
        activity=_parse_activity(activity, exclude, label),
        years=tuple(sorted(years)),
        population=_build_population(**pop),
        weights=weights,
        variance="none" if no_variance else "replicate",
    )
    result = _run_with_engine(lambda engine: engine.estimate(spec))
    _emit(result.to_dict(), as_json, _print_estimate, result)


@analyze.command(help="Per-year trend across a range of years.")
@_shared_options
@click.option("--start-year", type=int, required=True)
@click.option("--end-year", type=int, required=True)
@_population_options
def trend(activity, exclude, label, measure, weights, no_variance, as_json,
          start_year, end_year, **pop) -> None:
    if end_year < start_year:
        raise click.UsageError("--end-year must be >= --start-year")
    spec = AnalysisSpec(
        measure=Measure(measure),
        activity=_parse_activity(activity, exclude, label),
        years=tuple(range(start_year, end_year + 1)),
        population=_build_population(**pop),
        weights=weights,
        variance="none" if no_variance else "replicate",
    )
    result = _run_with_engine(lambda engine: engine.trend(spec))
    _emit(result.to_dict(), as_json, _print_trend, result)


@analyze.command(help="Compare two populations (difference with covariance-correct SE).")
@_shared_options
@click.option("--year", "years", type=int, multiple=True, required=True)
@click.option("--group-a", required=True, help="e.g. sex=male or age_min=25,age_max=34")
@click.option("--group-b", required=True, help="e.g. sex=female")
@click.option("--label-a", default=None)
@click.option("--label-b", default=None)
@_population_options
def compare(activity, exclude, label, measure, weights, no_variance, as_json,
            years, group_a, group_b, label_a, label_b, **pop) -> None:
    base = AnalysisSpec(
        measure=Measure(measure),
        activity=_parse_activity(activity, exclude, label),
        years=tuple(sorted(years)),
        population=_build_population(**pop),
        weights=weights,
        variance="none" if no_variance else "replicate",
    )
    comparison = ComparisonSpec(
        base=base,
        group_a=_parse_population_kv(group_a),
        group_b=_parse_population_kv(group_b),
        label_a=label_a or group_a,
        label_b=label_b or group_b,
    )
    result = _run_with_engine(lambda engine: engine.compare(comparison))
    _emit(result.to_dict(), as_json, _print_comparison, result)


@analyze.command(help="Run an analysis from a serialized JSON spec file.")
@click.option("--spec", "spec_path", type=click.Path(exists=True, dir_okay=False),
              required=True)
@click.option("--trend", "as_trend", is_flag=True, help="Run as a per-year trend.")
@click.option("--json", "as_json", is_flag=True)
def run(spec_path: str, as_trend: bool, as_json: bool) -> None:
    with open(spec_path) as fh:
        spec = AnalysisSpec.from_dict(json.load(fh))
    if as_trend:
        result = _run_with_engine(lambda engine: engine.trend(spec))
        _emit(result.to_dict(), as_json, _print_trend, result)
    else:
        result = _run_with_engine(lambda engine: engine.estimate(spec))
        _emit(result.to_dict(), as_json, _print_estimate, result)


@click.command("validate-analytics",
               help="Reproduce official BLS estimates through the engine (benchmark suite).")
def validate_analytics() -> None:
    from .analytics.benchmarks import run_benchmarks
    from .cli import _print_results, _settings

    settings = _settings()
    with connect(settings.database_url) as conn:
        results = run_benchmarks(conn)
    if not _print_results(results):
        sys.exit(1)
