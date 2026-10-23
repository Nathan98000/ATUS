"""Post-load data-quality validation of the canonical database.

Three families of checks:

* **scalar** — a measured value must equal an expectation that comes from an
  authoritative source (official BLS record counts from the ``*_info.txt``
  files, year counts documented in the BLS "Changes between data files"
  document, the committed lexicon).
* **violations** — a query enumerating rows that break an ATUS invariant
  (diary arithmetic, weight rules, referential structure) must return nothing.
* **cross-file** — loaded episode durations are recomputed and compared against
  the independently produced BLS Activity Summary file for a sample of cases.

Severity "error" means the database should not be trusted until explained;
"warning" flags things worth knowing that have a plausible benign cause.
Results are printed and persisted to ``atus.validation_results``.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass

import psycopg

from ..config import Settings
from ..sources import get_source
from .results import CheckResult
from .source_checks import expected_years

log = logging.getLogger(__name__)

_SUMMARY_SAMPLE_CASES = 2_000

# 50 states + DC (FIPS). Source: U.S. Census Bureau state FIPS codes.
_VALID_STATE_FIPS = (
    "01", "02", "04", "05", "06", "08", "09", "10", "11", "12", "13", "15", "16", "17",
    "18", "19", "20", "21", "22", "23", "24", "25", "26", "27", "28", "29", "30", "31",
    "32", "33", "34", "35", "36", "37", "38", "39", "40", "41", "42", "44", "45", "46",
    "47", "48", "49", "50", "51", "53", "54", "55", "56",
)


@dataclass(frozen=True)
class ScalarCheck:
    name: str
    sql: str
    expected: object
    detail: str
    severity: str = "error"


@dataclass(frozen=True)
class ViolationsCheck:
    name: str
    sql: str          # must SELECT a count(*) of violating rows
    detail: str
    severity: str = "error"


def _scalar_checks(settings: Settings) -> list[ScalarCheck]:
    years = expected_years(settings.release)
    counts = {
        "respondents": get_source(settings.release, "respondent").expected_rows,
        "household_members": get_source(settings.release, "roster").expected_rows,
        "activities": get_source(settings.release, "activity").expected_rows,
        "activity_companions": get_source(settings.release, "who").expected_rows,
        "replicate_weights": get_source(settings.release, "replicate_weights").expected_rows,
        "pandemic_replicate_weights": get_source(
            settings.release, "pandemic_weights"
        ).expected_rows,
    }
    checks = [
        ScalarCheck(
            name=f"row count: {table}",
            sql=f"SELECT count(*) FROM atus.{table}",
            expected=expected,
            detail="official BLS record count for the source file",
        )
        for table, expected in counts.items()
    ]
    checks += [
        ScalarCheck(
            name="survey years present",
            sql="SELECT count(DISTINCT data_year) FROM atus.respondents",
            expected=len(years),
            detail=f"release {settings.release} covers {min(years)}-{max(years)}",
        ),
        ScalarCheck(
            name="earliest survey year",
            sql="SELECT min(data_year)::int FROM atus.respondents",
            expected=min(years),
            detail="ATUS began in 2003",
        ),
        ScalarCheck(
            name="latest survey year",
            sql="SELECT max(data_year)::int FROM atus.respondents",
            expected=max(years),
            detail=f"release {settings.release}",
        ),
        # Documented in "Changes between 2003-2022 Data Files" (bls.gov/tus/lexicons/changes.pdf):
        # 20,720 completed interviews in 2003 and 13,973 in 2004.
        ScalarCheck(
            name="2003 respondent count (BLS-documented)",
            sql="SELECT count(*) FROM atus.respondents WHERE data_year = 2003",
            expected=20_720,
            detail="BLS documents 20,720 completed interviews in 2003",
        ),
        ScalarCheck(
            name="2004 respondent count (BLS-documented)",
            sql="SELECT count(*) FROM atus.respondents WHERE data_year = 2004",
            expected=13_973,
            detail="BLS documents 13,973 completed interviews in 2004",
        ),
        ScalarCheck(
            name="pandemic-weight years respondent count",
            sql="SELECT count(*) FROM atus.respondents WHERE data_year IN (2019, 2020)",
            expected=get_source(settings.release, "pandemic_weights").expected_rows,
            detail="2019+2020 respondents must equal the pandemic replicate weights file's records",
        ),
    ]
    return checks


_VIOLATION_CHECKS: list[ViolationsCheck] = [
    # ---- diary arithmetic -------------------------------------------------
    ViolationsCheck(
        name="diary day sums to 1440 minutes",
        sql="""
            SELECT count(*) FROM (
                SELECT tucaseid FROM atus.activities
                GROUP BY tucaseid HAVING sum(duration_minutes) <> 1440
            ) v
        """,
        detail="TUACTDUR24 is defined so each diary day totals exactly 24h",
    ),
    ViolationsCheck(
        name="first episode starts at 04:00",
        sql="""
            SELECT count(*) FROM atus.activities
            WHERE activity_number = 1 AND start_time <> time '04:00:00'
        """,
        detail="the ATUS diary day runs 04:00 to 04:00",
    ),
    ViolationsCheck(
        name="cumulative minutes equal running total of durations",
        sql="""
            SELECT count(*) FROM (
                SELECT tucaseid, activity_number,
                       cumulative_minutes,
                       sum(duration_minutes) OVER (
                           PARTITION BY tucaseid ORDER BY activity_number
                       ) AS running
                FROM atus.activities
            ) t WHERE cumulative_minutes <> running
        """,
        detail="TUCUMDUR24 must be the running sum of TUACTDUR24 within each case",
    ),
    ViolationsCheck(
        name="episode clock time matches uncapped duration",
        sql="""
            SELECT count(*) FROM atus.activities
            WHERE ((86400 + extract(epoch FROM stop_time)::int
                          - extract(epoch FROM start_time)::int) / 60) % 1440
                  <> duration_uncapped_minutes % 1440
        """,
        detail=(
            "stop - start (mod 24h) should equal TUACTDUR (mod 24h). Known source "
            "anomaly: 3 episodes in the 2003-25 release carry internally inconsistent "
            "clock times (cases 20161008161668, 20231009230928, 20240806241220). The "
            "duration fields are authoritative — they reconcile to 1440/day, to running "
            "totals, and to the BLS activity summary file — so growth in this count, "
            "not its mere presence, is what warrants investigation"
        ),
        severity="warning",
    ),
    ViolationsCheck(
        name="capped duration never exceeds uncapped duration",
        sql="""
            SELECT count(*) FROM atus.activities
            WHERE duration_minutes > duration_uncapped_minutes
        """,
        detail="TUACTDUR24 truncates the final episode at 04:00",
    ),
    # ---- 2020 pandemic collection gap ------------------------------------
    ViolationsCheck(
        name="no diary days in the 2020 collection gap",
        sql="""
            SELECT count(*) FROM atus.respondents
            WHERE diary_date BETWEEN date '2020-03-18' AND date '2020-05-09'
        """,
        detail="BLS collected no data about 2020-03-18..2020-05-09 (COVID-19 suspension)",
    ),
    # ---- weight rules -----------------------------------------------------
    ViolationsCheck(
        name="pandemic weight present for all 2019-2020 respondents",
        sql="""
            SELECT count(*) FROM atus.respondents
            WHERE data_year IN (2019, 2020) AND pandemic_weight IS NULL
        """,
        detail="TU20FWGT is defined for every 2019 and 2020 respondent",
    ),
    ViolationsCheck(
        name="every respondent has a replicate-weight row",
        sql="""
            SELECT count(*) FROM atus.respondents r
            WHERE NOT EXISTS (
                SELECT 1 FROM atus.replicate_weights w WHERE w.tucaseid = r.tucaseid
            )
        """,
        detail="the replicate weights file covers all respondents",
    ),
    ViolationsCheck(
        name="replicate weights NULL exactly for 2020",
        sql="""
            SELECT count(*) FROM atus.replicate_weights w
            JOIN atus.respondents r USING (tucaseid)
            WHERE (r.data_year = 2020) <> (w.tufnwgtp001 IS NULL)
               OR (r.data_year = 2020) <> (w.tufnwgtp160 IS NULL)
        """,
        detail="TUFNWGTP (and its replicates) are undefined only for 2020",
    ),
    ViolationsCheck(
        name="pandemic replicate rows are exactly the 2019-2020 respondents",
        sql="""
            SELECT
              (SELECT count(*) FROM atus.respondents r
               WHERE r.data_year IN (2019, 2020) AND NOT EXISTS (
                   SELECT 1 FROM atus.pandemic_replicate_weights p WHERE p.tucaseid = r.tucaseid))
            + (SELECT count(*) FROM atus.pandemic_replicate_weights p
               JOIN atus.respondents r USING (tucaseid)
               WHERE r.data_year NOT IN (2019, 2020))
        """,
        detail="the pandemic replicate weights file covers 2019-2020 respondents only",
    ),
    # ---- structural / referential ----------------------------------------
    ViolationsCheck(
        name="every respondent appears on the roster as line 1",
        sql="""
            SELECT count(*) FROM atus.respondents r
            WHERE NOT EXISTS (
                SELECT 1 FROM atus.household_members m
                WHERE m.tucaseid = r.tucaseid AND m.lineno = 1
            )
        """,
        detail="the respondent always has TULINENO = 1",
    ),
    ViolationsCheck(
        name="roster line 1 is coded 'self'",
        sql="""
            SELECT count(*) FROM atus.household_members
            WHERE lineno = 1 AND relationship NOT IN (18, 19)
        """,
        detail="TERRP 18/19 both mean 'self'",
    ),
    ViolationsCheck(
        name="every activity episode has who-file coverage",
        sql="""
            SELECT count(*) FROM atus.activities a
            WHERE NOT EXISTS (
                SELECT 1 FROM atus.activity_companions c
                WHERE c.tucaseid = a.tucaseid AND c.activity_number = a.activity_number
            )
        """,
        detail="the Who file carries a placeholder row when who info was not collected",
    ),
    ViolationsCheck(
        name="who-not-asked placeholder rows are exclusive",
        sql="""
            SELECT count(*) FROM (
                SELECT tucaseid, activity_number FROM atus.activity_companions
                GROUP BY tucaseid, activity_number
                HAVING bool_or(who_not_asked) AND count(*) > 1
            ) v
        """,
        detail="an episode either has real who codes or one 'not asked' placeholder",
    ),
    ViolationsCheck(
        name="household-member companions resolve to the roster",
        sql="""
            SELECT count(*) FROM atus.activity_companions c
            WHERE c.who_lineno >= 1 AND NOT EXISTS (
                SELECT 1 FROM atus.household_members m
                WHERE m.tucaseid = c.tucaseid AND m.lineno = c.who_lineno
            )
        """,
        detail="positive who_lineno values are roster line numbers",
    ),
    ViolationsCheck(
        name="every respondent has a CPS person record (line 1)",
        sql="""
            SELECT count(*) FROM atus.respondents r
            WHERE NOT EXISTS (
                SELECT 1 FROM atus.cps_persons p
                WHERE p.tucaseid = r.tucaseid AND p.lineno = 1
            )
        """,
        detail="every ATUS respondent was sampled from a CPS household",
    ),
    # ---- documented variable rules ----------------------------------------
    ViolationsCheck(
        name="eldercare fields empty before 2011",
        sql="""
            SELECT count(*) FROM atus.respondents
            WHERE data_year < 2011
              AND (eldercare_minutes IS NOT NULL
                   OR provided_eldercare_on_diary_day IS NOT NULL)
        """,
        detail="eldercare questions were introduced in January 2011",
    ),
    ViolationsCheck(
        name="weekly earnings respect the BLS topcode (through 2023)",
        sql="""
            SELECT count(*) FROM atus.respondents
            WHERE data_year <= 2023 AND weekly_earnings > 2884.61
        """,
        detail=(
            "TRERNWA was topcoded at 2884.61 until June 2024, when BLS switched to "
            "monthly top-3%% topcodes; later years legitimately exceed the old cap"
        ),
    ),
    ViolationsCheck(
        name="weekly earnings within plausible bounds",
        sql="""
            SELECT count(*) FROM atus.respondents
            WHERE weekly_earnings > 30000
        """,
        detail="broad plausibility bound above the post-2024 monthly topcodes",
    ),
    ViolationsCheck(
        name="CPS state codes are valid state FIPS",
        sql=f"""
            SELECT count(*) FROM atus.cps_persons
            WHERE state_fips IS NOT NULL
              AND state_fips NOT IN ({", ".join(f"'{f}'" for f in _VALID_STATE_FIPS)})
        """,
        detail="GESTFIPS must be one of the 50 states or DC",
    ),
    # ---- plausibility (warnings) ------------------------------------------
    ViolationsCheck(
        name="diary date year matches survey year",
        sql="""
            SELECT count(*) FROM atus.respondents
            WHERE extract(year FROM diary_date)::int <> data_year
        """,
        detail=(
            "TUYEAR is expected to match the diary date's year; a small number of "
            "year-boundary interviews would be a benign explanation"
        ),
        severity="warning",
    ),
    ViolationsCheck(
        name="diary day-of-week matches diary date",
        sql="""
            SELECT count(*) FROM atus.respondents
            WHERE extract(dow FROM diary_date)::int + 1 <> diary_day_of_week
        """,
        detail="TUDIARYDAY uses 1=Sunday..7=Saturday; PostgreSQL dow uses 0=Sunday",
    ),
]


def _summary_cross_check(conn: psycopg.Connection, settings: Settings) -> CheckResult:
    """Compare loaded episode durations against the BLS Activity Summary file.

    The Activity Summary file is produced by BLS independently of the Activity
    file's episode records; agreement for a sample of cases is strong evidence
    that episodes were loaded and typed correctly.
    """
    source = get_source(settings.release, "activity_summary")
    path = settings.staging_dir / source.dat_name
    if not path.exists():
        return CheckResult(
            name="episode totals match BLS activity summary (sample)",
            passed=False,
            severity="error",
            observed=f"{path} missing",
            detail="stage the activity summary file first (`atus extract`)",
        )

    expected: dict[int, dict[str, int]] = {}
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        tcols = [c for c in reader.fieldnames if len(c) == 7 and c[0] == "t" and c[1:].isdigit()]
        for row in reader:
            case = int(row["TUCASEID"])
            expected[case] = {
                col[1:]: int(row[col]) for col in tcols if int(row[col]) != 0
            }
            if len(expected) >= _SUMMARY_SAMPLE_CASES:
                break

    rows = conn.execute(
        """
        SELECT tucaseid, activity_code, sum(duration_minutes)::int
        FROM atus.activities
        WHERE tucaseid = ANY(%s)
        GROUP BY tucaseid, activity_code
        """,
        (list(expected),),
    ).fetchall()
    observed: dict[int, dict[str, int]] = {case: {} for case in expected}
    for case, code, minutes in rows:
        observed[case][code] = minutes

    mismatches = sum(1 for case in expected if expected[case] != observed[case])
    return CheckResult(
        name="episode totals match BLS activity summary (sample)",
        passed=mismatches == 0,
        severity="error",
        observed=f"{mismatches} of {len(expected)} sampled cases disagree",
        detail="per-case, per-code minute totals recomputed from episodes",
    )


def _lexicon_checks(conn: psycopg.Connection, settings: Settings) -> list[CheckResult]:
    path = settings.reference_dir / f"activity_lexicon_{settings.release}.csv"
    results = []
    with path.open(newline="") as fh:
        levels = [row["level"] for row in csv.DictReader(fh)]
    for level, table in (("1", "activity_tier1"), ("2", "activity_tier2"), ("3", "activity_codes")):
        db_count = conn.execute(f"SELECT count(*) FROM atus.{table}").fetchone()[0]
        csv_count = levels.count(level)
        results.append(
            CheckResult(
                name=f"lexicon loaded completely: {table}",
                passed=db_count == csv_count,
                severity="error",
                observed=str(db_count),
                detail=f"reference CSV has {csv_count} level-{level} entries",
            )
        )
    return results


def run_db_checks(conn: psycopg.Connection, settings: Settings) -> list[CheckResult]:
    results: list[CheckResult] = []

    for check in _scalar_checks(settings):
        value = conn.execute(check.sql).fetchone()[0]
        results.append(
            CheckResult(
                name=check.name,
                passed=value == check.expected,
                severity=check.severity,
                observed=f"{value:,}" if isinstance(value, int) else str(value),
                detail=f"expected {check.expected:,} — {check.detail}"
                if isinstance(check.expected, int) else check.detail,
            )
        )
        log.info("check %-55s %s", check.name, "PASS" if results[-1].passed else "FAIL")

    for check in _VIOLATION_CHECKS:
        count = conn.execute(check.sql).fetchone()[0]
        results.append(
            CheckResult(
                name=check.name,
                passed=count == 0,
                severity=check.severity,
                observed=f"{count:,} violating rows",
                detail=check.detail,
            )
        )
        log.info("check %-55s %s", check.name, "PASS" if results[-1].passed else "FAIL")

    results.extend(_lexicon_checks(conn, settings))
    results.append(_summary_cross_check(conn, settings))
    log.info(
        "check %-55s %s", results[-1].name, "PASS" if results[-1].passed else "FAIL"
    )

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
    return results
