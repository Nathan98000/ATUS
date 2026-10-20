"""Validation of the analytical engine against official BLS estimates.

Every benchmark reproduces a value BLS itself published, through the full
engine path (spec -> SQL -> estimators -> variance). Tolerances reflect the
precision of the published number (half a unit in its last published digit,
plus a small epsilon), never a fudge factor; discrepancies beyond tolerance
mean either an implementation bug or a definitional mismatch, and both demand
investigation rather than adjustment.

Sources:

* **UG** — ATUS User's Guide (June 2026), www.bls.gov/tus/atususersguide.pdf:
  ch. 7.4 worked example (2007 TV watching: exact weighted mean) and ch. 7.5
  (its replicate-weight standard error, 0.0293 hours); Appendix J (persons in
  the South doing housework on an average 2006 day: 29,517,003 from a
  1,861-respondent subpopulation).
* **A1-2025** — Table A-1, ATUS 2025 annual averages,
  www.bls.gov/tus/tables/a1-2025.pdf (also news release Table 1). Category
  definitions follow the published table structure: major categories include
  related travel (tier 18 counterparts), and household mail/e-mail
  (020903/020904) is tabulated under "Telephone calls, mail, and e-mail"
  rather than household activities.
* **NR-2020** — "American Time Use Survey — 2020 Results" (July 22, 2021),
  www.bls.gov/news.release/archives/atus_07222021.htm: estimates for
  May 10 - Dec 31, 2020 using the pandemic weight TU20FWGT.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import psycopg

from ..validation.results import CheckResult
from .engine import AnalysisEngine
from .results import EstimateResult
from .spec import ActivitySelector, AnalysisSpec, Measure, PopulationFilter

_HOURS = 1.0 / 60.0


@dataclass(frozen=True)
class Benchmark:
    name: str
    source: str
    spec: AnalysisSpec
    extract: Callable[[EstimateResult], float]
    expected: float
    tolerance: float
    detail: str = ""


def _mean_hours(result: EstimateResult) -> float:
    return result.estimate.value * _HOURS


def _se_hours(result: EstimateResult) -> float:
    return result.estimate.standard_error * _HOURS


def _participation_pct(result: EstimateResult) -> float:
    return result.estimate.value * 100.0


def _value(result: EstimateResult) -> float:
    return result.estimate.value


def _sample_participants(result: EstimateResult) -> float:
    return float(result.n_participants)


def _spec(
    measure: Measure, activity: ActivitySelector, years: tuple[int, ...],
    population: PopulationFilter | None = None, weights: str = "multiyear",
    variance: str = "replicate",
) -> AnalysisSpec:
    return AnalysisSpec(
        measure=measure, activity=activity, years=years,
        population=population if population is not None else PopulationFilter(),
        weights=weights, variance=variance,
    )


def build_benchmarks() -> tuple[Benchmark, ...]:
    tv = ActivitySelector.preset("watching_tv")
    sleep = ActivitySelector.preset("sleep")
    housework = ActivitySelector.preset("housework")
    household_a1 = ActivitySelector.preset("household_activities_bls_table")
    leisure_a1 = ActivitySelector.preset("leisure_and_sports_bls_table")
    eating_a1 = ActivitySelector.preset("eating_and_drinking_bls_table")
    work_a1 = ActivitySelector.preset("work_related_bls_table")
    window_2020 = PopulationFilter(diary_date_min=date(2020, 5, 10))

    return (
        # ---- User's Guide worked examples (exact methodology anchors) ---- #
        Benchmark(
            name="UG ch7.4: TV watching 2007, mean minutes/day",
            source="ATUS User's Guide ch. 7.4 (157.395 minutes)",
            spec=_spec(Measure.AVERAGE_MINUTES_PER_DAY, tv, (2007,)),
            extract=_value, expected=157.395, tolerance=0.001,
        ),
        Benchmark(
            name="UG ch7.5: TV watching 2007, replicate-weight SE (hours)",
            source="ATUS User's Guide ch. 7.5 (SE = 0.0293 hours)",
            spec=_spec(Measure.AVERAGE_MINUTES_PER_DAY, tv, (2007,)),
            extract=_se_hours, expected=0.0293, tolerance=0.00005,
        ),
        Benchmark(
            name="UG App J: persons in the South doing housework per day, 2006",
            source="ATUS User's Guide Appendix J (29,517,003 persons)",
            spec=_spec(
                Measure.PARTICIPANTS_PER_DAY, housework, (2006,),
                PopulationFilter(region=3),
            ),
            extract=_value, expected=29_517_003, tolerance=1.0,
        ),
        Benchmark(
            name="UG App J: unweighted participant count in that subpopulation",
            source="ATUS User's Guide Appendix J (1,861 respondents)",
            spec=_spec(
                Measure.PARTICIPANTS_PER_DAY, housework, (2006,),
                PopulationFilter(region=3), variance="none",
            ),
            extract=_sample_participants, expected=1861, tolerance=0.0,
        ),
        # ---- Published 2025 annual averages (Table A-1) ------------------ #
        Benchmark(
            name="A1-2025: sleeping, hours/day",
            source="BLS Table A-1 2025 (9.03)",
            spec=_spec(Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2025,), variance="none"),
            extract=_mean_hours, expected=9.03, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: sleeping, percent participating",
            source="BLS Table A-1 2025 (99.9)",
            spec=_spec(Measure.PARTICIPATION_RATE, sleep, (2025,), variance="none"),
            extract=_participation_pct, expected=99.9, tolerance=0.06,
        ),
        Benchmark(
            name="A1-2025: sleeping, hours/day of participants",
            source="BLS Table A-1 2025 (9.04)",
            spec=_spec(
                Measure.AVERAGE_MINUTES_PER_PARTICIPANT, sleep, (2025,), variance="none"
            ),
            extract=_mean_hours, expected=9.04, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: sleeping, men, hours/day",
            source="BLS Table A-1 2025 (8.93)",
            spec=_spec(
                Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2025,),
                PopulationFilter(sex="male"), variance="none",
            ),
            extract=_mean_hours, expected=8.93, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: sleeping, women, hours/day",
            source="BLS Table A-1 2025 (9.13)",
            spec=_spec(
                Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2025,),
                PopulationFilter(sex="female"), variance="none",
            ),
            extract=_mean_hours, expected=9.13, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: watching TV, hours/day",
            source="BLS Table A-1 2025 (2.61)",
            spec=_spec(Measure.AVERAGE_MINUTES_PER_DAY, tv, (2025,), variance="none"),
            extract=_mean_hours, expected=2.61, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: watching TV, percent participating",
            source="BLS Table A-1 2025 (74.3)",
            spec=_spec(Measure.PARTICIPATION_RATE, tv, (2025,), variance="none"),
            extract=_participation_pct, expected=74.3, tolerance=0.06,
        ),
        Benchmark(
            name="A1-2025: household activities (A-1 definition), hours/day",
            source="BLS Table A-1 2025 (1.99); see module docstring on the A-1 mapping",
            spec=_spec(
                Measure.AVERAGE_MINUTES_PER_DAY, household_a1, (2025,), variance="none"
            ),
            extract=_mean_hours, expected=1.99, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: household activities (A-1 definition), percent participating",
            source="BLS Table A-1 2025 (80.9)",
            spec=_spec(Measure.PARTICIPATION_RATE, household_a1, (2025,), variance="none"),
            extract=_participation_pct, expected=80.9, tolerance=0.06,
        ),
        Benchmark(
            name="A1-2025: household activities (A-1 definition), women, hours/day",
            source="BLS Table A-1 2025 (2.38)",
            spec=_spec(
                Measure.AVERAGE_MINUTES_PER_DAY, household_a1, (2025,),
                PopulationFilter(sex="female"), variance="none",
            ),
            extract=_mean_hours, expected=2.38, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: leisure and sports, hours/day",
            source="BLS Table A-1 2025 (5.16)",
            spec=_spec(Measure.AVERAGE_MINUTES_PER_DAY, leisure_a1, (2025,), variance="none"),
            extract=_mean_hours, expected=5.16, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: leisure and sports, percent participating",
            source="BLS Table A-1 2025 (95.1)",
            spec=_spec(Measure.PARTICIPATION_RATE, leisure_a1, (2025,), variance="none"),
            extract=_participation_pct, expected=95.1, tolerance=0.06,
        ),
        Benchmark(
            name="A1-2025: leisure and sports, hours/day of participants",
            source="BLS Table A-1 2025 (5.43)",
            spec=_spec(
                Measure.AVERAGE_MINUTES_PER_PARTICIPANT, leisure_a1, (2025,),
                variance="none",
            ),
            extract=_mean_hours, expected=5.43, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: eating and drinking, hours/day",
            source="BLS Table A-1 2025 (1.21)",
            spec=_spec(Measure.AVERAGE_MINUTES_PER_DAY, eating_a1, (2025,), variance="none"),
            extract=_mean_hours, expected=1.21, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: eating and drinking, percent participating",
            source="BLS Table A-1 2025 (96.9)",
            spec=_spec(Measure.PARTICIPATION_RATE, eating_a1, (2025,), variance="none"),
            extract=_participation_pct, expected=96.9, tolerance=0.06,
        ),
        Benchmark(
            name="A1-2025: working and work-related, hours/day",
            source="BLS Table A-1 2025 (3.32)",
            spec=_spec(Measure.AVERAGE_MINUTES_PER_DAY, work_a1, (2025,), variance="none"),
            extract=_mean_hours, expected=3.32, tolerance=0.006,
        ),
        Benchmark(
            name="A1-2025: working and work-related, hours/day of participants",
            source="BLS Table A-1 2025 (7.98)",
            spec=_spec(
                Measure.AVERAGE_MINUTES_PER_PARTICIPANT, work_a1, (2025,), variance="none"
            ),
            extract=_mean_hours, expected=7.98, tolerance=0.006,
        ),
        # ---- 2020 pandemic weights (May 10 - Dec 31 reference period) ---- #
        Benchmark(
            name="NR-2020: sleeping, hours/day (May 10 - Dec 31, TU20FWGT)",
            source="ATUS 2020 Results news release Table 1 (9.01)",
            spec=_spec(
                Measure.AVERAGE_MINUTES_PER_DAY, sleep, (2020,), window_2020,
                weights="pandemic", variance="none",
            ),
            extract=_mean_hours, expected=9.01, tolerance=0.006,
        ),
        Benchmark(
            name="NR-2020: watching TV, hours/day (May 10 - Dec 31, TU20FWGT)",
            source="ATUS 2020 Results news release Table 1 (3.05)",
            spec=_spec(
                Measure.AVERAGE_MINUTES_PER_DAY, tv, (2020,), window_2020,
                weights="pandemic", variance="none",
            ),
            extract=_mean_hours, expected=3.05, tolerance=0.006,
        ),
        Benchmark(
            name="NR-2020: watching TV, percent participating (May 10 - Dec 31)",
            source="ATUS 2020 Results news release Table 1 (78.8)",
            spec=_spec(
                Measure.PARTICIPATION_RATE, tv, (2020,), window_2020,
                weights="pandemic", variance="none",
            ),
            extract=_participation_pct, expected=78.8, tolerance=0.06,
        ),
    )


def run_benchmarks(conn: psycopg.Connection) -> list[CheckResult]:
    engine = AnalysisEngine(conn)
    results: list[CheckResult] = []
    for benchmark in build_benchmarks():
        try:
            observed = benchmark.extract(engine.estimate(benchmark.spec))
            delta = abs(observed - benchmark.expected)
            passed = delta <= benchmark.tolerance
            observed_text = (
                f"{observed:,.4f} (published {benchmark.expected:,.4f}, |Δ|={delta:.5f})"
            )
        except Exception as exc:  # a crash is a failed benchmark, loudly
            passed = False
            observed_text = f"{type(exc).__name__}: {exc}"
        results.append(
            CheckResult(
                name=f"analytics: {benchmark.name}",
                passed=passed,
                severity="error",
                observed=observed_text,
                detail=benchmark.source,
            )
        )
    _persist(conn, results)
    return results


def _persist(conn: psycopg.Connection, results: list[CheckResult]) -> None:
    with conn.transaction():
        for result in results:
            conn.execute(
                """
                INSERT INTO atus.validation_results
                    (check_name, severity, passed, observed, detail)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (result.name, result.severity, result.passed, result.observed, result.detail),
            )
