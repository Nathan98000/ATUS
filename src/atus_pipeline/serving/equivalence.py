"""The §9 equivalence criterion, as code, and the randomized diff it drives.

Two databases (the full one and a serving one), or two query renderings on
one database, are equivalent for a specification when their engine outputs
compare as follows (docs/operations.md, "Equivalence criterion (§9)"):

* **exactly equal**: every result field except standard errors and
  confidence bounds (point estimates, ``n_respondents``, ``n_participants``,
  weighted populations, days, labels, years, the echoed spec, warnings), and
  the type and text of every error;
* **standard errors and confidence bounds**: ``|a - b| <= REL_TOL * max(|a|,
  |b|, S)`` with ``REL_TOL = 1e-12`` and ``S`` the largest absolute point
  value of the estimate they belong to (for a comparison's difference, also
  of both groups). The floor ``S`` is what makes zero-variance estimates
  comparable: their "standard error" is floating-point noise around 0
  (F40/I12: 7.83e-12 vs 7.93e-12 on an estimate of 1,440 minutes), which a
  purely relative bound rejects.

Replicate sums are ``float8`` and their summation order follows the query
plan; measured noise is up to 6.24e-13 relative on standard errors and
8.57e-14 between two runs on the same database (audit I04), so 1e-12 bounds it
while catching any real difference (a real change moves results by many
orders of magnitude more).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..analytics import (
    ActivitySelector,
    AnalysisSpec,
    AnalyticsError,
    ComparisonSpec,
    Measure,
    PopulationFilter,
)
from ..analytics.spec import ACTIVITY_PRESETS, population_to_dict

REL_TOL = 1e-12
TOLERANT_FIELDS = frozenset({"standard_error", "ci_lower", "ci_upper"})


@dataclass
class Comparison:
    """Outcome of comparing two results (or two sets of query rows)."""

    mismatches: list[str] = field(default_factory=list)
    compared: int = 0                # leaf values compared
    max_relative: float = 0.0        # largest |a-b| / max(|a|, |b|) on tolerant values
    max_scaled: float = 0.0          # largest |a-b| / max(|a|, |b|, S)

    @property
    def equivalent(self) -> bool:
        return not self.mismatches

    def merge(self, other: Comparison) -> None:
        self.mismatches += other.mismatches
        self.compared += other.compared
        self.max_relative = max(self.max_relative, other.max_relative)
        self.max_scaled = max(self.max_scaled, other.max_scaled)


def _number(value) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _close(a: float, b: float, scale: float, path: str, out: Comparison) -> None:
    out.compared += 1
    diff = abs(float(a) - float(b))
    if diff == 0:
        return
    magnitude = max(abs(float(a)), abs(float(b)))
    bound = max(magnitude, scale)
    out.max_relative = max(out.max_relative, diff / magnitude if magnitude else float("inf"))
    out.max_scaled = max(out.max_scaled, diff / bound if bound else float("inf"))
    if diff > REL_TOL * bound:
        out.mismatches.append(f"{path}: {a!r} vs {b!r} (|diff| {diff:.3g} > {REL_TOL:g} x "
                              f"{bound:.6g})")


def _estimate_value(d) -> float:
    if isinstance(d, dict) and _number(d.get("value")):
        return abs(float(d["value"]))
    return 0.0


def _walk(a, b, path: str, scale: float, out: Comparison) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            out.mismatches.append(f"{path}: keys {sorted(set(a) ^ set(b))} differ")
        local = max(scale, _estimate_value(a)) if "standard_error" in a else scale
        groups = local
        if "group_a" in a and "group_b" in a:
            groups = max(local, *(
                _estimate_value(a[g].get("estimate")) for g in ("group_a", "group_b")
                if isinstance(a[g], dict)
            ))
        for key in sorted(set(a) & set(b)):
            sub = f"{path}.{key}" if path else key
            child = groups if key == "difference" else local
            if key in TOLERANT_FIELDS and _number(a[key]) and _number(b[key]):
                _close(a[key], b[key], child, sub, out)
            else:
                _walk(a[key], b[key], sub, child, out)
    elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            out.mismatches.append(f"{path}: {len(a)} vs {len(b)} items")
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            _walk(x, y, f"{path}[{i}]", scale, out)
    else:
        out.compared += 1
        if type(a) is not type(b) or a != b:
            out.mismatches.append(f"{path}: {a!r} vs {b!r}")


def compare_results(a, b) -> Comparison:
    """Compare two engine outputs: ``to_dict()`` payloads, or error texts."""
    out = Comparison()
    _walk(a, b, "", 0.0, out)
    return out


def compare_statistics(rows_a: list[tuple], rows_b: list[tuple]) -> Comparison:
    """Compare raw sufficient-statistics rows (``queries.build_query``):
    year, counts and the ``numeric`` sums exactly; each replicate ``float8``
    sum within ``REL_TOL`` of the largest value in its array."""
    out = Comparison()
    if len(rows_a) != len(rows_b):
        out.mismatches.append(f"{len(rows_a)} vs {len(rows_b)} rows")
    for ra, rb in zip(rows_a, rows_b, strict=False):
        year = ra[0]
        for i in range(min(6, len(ra))):
            out.compared += 1
            if type(ra[i]) is not type(rb[i]) or ra[i] != rb[i]:
                out.mismatches.append(f"year {year} column {i}: {ra[i]!r} vs {rb[i]!r}")
        for i in range(6, len(ra)):
            scale = max((abs(v) for v in ra[i]), default=0.0)
            for j, (x, y) in enumerate(zip(ra[i], rb[i], strict=True)):
                _close(x, y, scale, f"year {year} column {i}[{j}]", out)
    return out


def rendered_queries(conn, resolver, spec, *, years_loaded: tuple[int, ...]
                     ) -> list[tuple[str, str, dict]]:
    """For each sufficient-statistics query the engine runs for ``spec`` (two
    for a comparison): the Phase 2-4 SQL, the current SQL (year pushdown),
    and their parameters. Raises :class:`AnalyticsError` for a spec the engine
    refuses (mirrors ``AnalysisEngine._prepare``)."""
    from ..analytics.errors import InvalidSpecError
    from ..analytics.population import build_population_sql
    from ..analytics.queries import build_query
    from ..analytics.weights import get_scheme, validate_scheme_for_years

    analyses = ([spec.spec_for("a"), spec.spec_for("b")] if isinstance(spec, ComparisonSpec)
                else [spec])
    out = []
    for analysis in analyses:
        if any(year not in years_loaded for year in analysis.years):
            raise InvalidSpecError("year not loaded")
        scheme = get_scheme(analysis.weights)
        validate_scheme_for_years(scheme, analysis.years)
        activity = resolver.resolve(analysis.activity)
        population = build_population_sql(analysis.population)
        params = {"years": list(analysis.years), **activity.sql_params, **population.params}
        rendered = [
            build_query(activity=activity, population=population, scheme=scheme,
                        with_replicates=analysis.variance == "replicate",
                        push_down_years=push_down)
            for push_down in (False, True)
        ]
        out.append((rendered[0], rendered[1], params))
    return out


def spec_dict(spec) -> dict:
    """JSON-ready form of an analysis or comparison specification."""
    if isinstance(spec, ComparisonSpec):
        return {"base": spec.base.to_dict(), "group_a": population_to_dict(spec.group_a),
                "group_b": population_to_dict(spec.group_b)}
    return spec.to_dict()


def run(engine, operation: str, spec) -> dict | str:
    """An engine result as a payload, or an expected refusal as its text."""
    try:
        return getattr(engine, operation)(spec).to_dict()
    except AnalyticsError as exc:
        return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Specifications: the audit's extremes (F40, F66) plus a seeded random sample #
# --------------------------------------------------------------------------- #

def seed_specs(years: list[int], tier1: list[str]) -> list[tuple[str, object]]:
    """Specs every diff includes: zero-variance, near-universal, broad, pandemic."""
    multiyear = [y for y in years if y != 2020]
    latest = multiyear[-1]
    everything = ActivitySelector(tuple(tier1))
    sleep = ActivitySelector(("010101",))
    specs: list[tuple[str, object]] = [
        # zero variance: every minute of the day (F40/I12)
        ("estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, everything, (latest,))),
        # near-universal participation, pooled over every year
        ("estimate", AnalysisSpec(Measure.PARTICIPATION_RATE, sleep, tuple(multiyear))),
        ("estimate", AnalysisSpec(Measure.PARTICIPATION_RATE, ActivitySelector(("01",)),
                                  tuple(multiyear))),
        # the costliest selectors (F66), pooled
        ("estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, everything,
                                  tuple(multiyear))),
        ("estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY,
                                  ACTIVITY_PRESETS["leisure_and_sports_bls_table"],
                                  tuple(multiyear))),
        ("trend", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, tuple(multiyear))),
        ("compare", ComparisonSpec(
            base=AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (latest,)),
            group_a=PopulationFilter(sex="male"), group_b=PopulationFilter(sex="female"),
        )),
        # an empty population and an unknown code (identical refusals)
        ("estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (latest,),
                                  PopulationFilter(state_fips="99"))),
        ("estimate", AnalysisSpec(Measure.AVERAGE_MINUTES_PER_DAY,
                                  ActivitySelector(("010198",)), (latest,))),
    ]
    if 2019 in years and 2020 in years:
        specs.append(("estimate", AnalysisSpec(
            Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2019, 2020), weights="pandemic")))
        specs.append(("trend", AnalysisSpec(
            Measure.PARTICIPATION_RATE, ActivitySelector(("12",)), (2019, 2020),
            weights="pandemic")))
    return specs


def _population(rng: random.Random, year: int) -> PopulationFilter:
    kwargs: dict = {}
    for _ in range(rng.choice((0, 0, 1, 1, 2, 3))):
        dimension = rng.choice(("age", "sex", "employment", "children", "region", "state",
                                "education", "day", "dates"))
        if dimension == "age":
            low = rng.choice((15, 25, 35, 55, 65))
            kwargs.update(age_min=low, age_max=rng.choice((None, low + 10, low + 30)))
        elif dimension == "sex":
            kwargs["sex"] = rng.choice(("male", "female"))
        elif dimension == "employment":
            kwargs["employment_status"] = rng.choice(
                ("employed", "unemployed", "not_in_labor_force"))
        elif dimension == "children":
            kwargs["has_household_children"] = rng.choice((True, False))
        elif dimension == "region":
            kwargs["region"] = rng.randint(1, 4)
        elif dimension == "state":
            kwargs["state_fips"] = rng.choice(("06", "36", "48", "12", "01"))
        elif dimension == "education":
            kwargs["education_level"] = rng.choice((
                "less_than_high_school", "high_school", "some_college_or_associate",
                "bachelor_or_higher"))
        elif dimension == "day":
            kwargs["day_type"] = rng.choice(("weekday", "weekend"))
        else:
            start = rng.randint(1, 300)
            first = date(year, 1, 1).toordinal() + start
            kwargs.update(diary_date_min=date.fromordinal(first),
                          diary_date_max=date.fromordinal(first + rng.randint(20, 60)))
    return PopulationFilter(**kwargs)


def random_specs(rng: random.Random, *, years: list[int], tier1: list[str], leaves: list[str],
                 n: int) -> list[tuple[str, object]]:
    """``n`` random specifications over the loaded years and lexicon."""
    multiyear = [y for y in years if y != 2020]
    presets = list(ACTIVITY_PRESETS.values())
    specs: list[tuple[str, object]] = []
    while len(specs) < n:
        kind = rng.random()
        if kind < 0.35:
            selector = ActivitySelector((rng.choice(leaves),))
        elif kind < 0.55:
            selector = ActivitySelector((rng.choice(tier1),))
        elif kind < 0.7:
            selector = ActivitySelector((rng.choice(leaves)[:4],))
        elif kind < 0.85:
            selector = rng.choice(presets)
        else:
            include = rng.choice(tier1)
            excluded = [c for c in leaves if c.startswith(include)]
            selector = ActivitySelector(
                (include, *rng.sample(tier1, 1)),
                exclude=tuple(rng.sample(excluded, min(2, len(excluded)))),
            )
        pandemic = 2019 in years and 2020 in years and rng.random() < 0.12
        if pandemic:
            chosen = rng.choice(((2019,), (2020,), (2019, 2020)))
        else:
            shape = rng.random()
            if shape < 0.45:
                chosen = (rng.choice(multiyear),)
            elif shape < 0.8:
                chosen = tuple(sorted(rng.sample(multiyear, min(len(multiyear),
                                                                rng.randint(2, 5)))))
            else:
                chosen = tuple(multiyear)
        population = _population(rng, chosen[0])
        if population.diary_date_min and len(chosen) > 1:
            population = PopulationFilter()      # a date window applies to one year
        operation = rng.choices(("estimate", "trend", "compare"), weights=(6, 2, 2))[0]
        try:
            spec = AnalysisSpec(
                measure=rng.choice(list(Measure)), activity=selector, years=chosen,
                population=PopulationFilter() if operation == "compare" else population,
                weights="pandemic" if pandemic else "multiyear",
                variance="none" if rng.random() < 0.1 else "replicate",
            )
            if operation == "compare":
                spec = ComparisonSpec(base=spec, group_a=population,
                                      group_b=_population(rng, chosen[0]))
        except AnalyticsError:
            continue                     # an invalid combination; draw again
        specs.append((operation, spec))
    return specs
