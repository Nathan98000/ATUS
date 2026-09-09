"""Repository layer: one SQL shape that returns sufficient statistics.

Every supported estimator is a ratio (or scaling) of weighted sums, so the
database aggregates exactly three families of sums per year — Σw, Σw·x, Σw·p —
for the full-sample weight and, when variance is requested, for each of the
160 replicate weights. Only aggregates leave the database (a few hundred
numbers per year), never respondent-level rows.

Grain safety is structural: episode minutes are aggregated to one value per
respondent in ``activity_minutes`` *before* joining, and every join
(roster line 1, CPS line 1, replicate weights) is on a key that is unique per
respondent, so a respondent can never contribute twice. The integration tests
assert this with a many-episode fixture.

Precision: full-sample sums are computed in ``numeric`` (exact — the BLS
User's Guide worked example is reproduced to the last digit); replicate sums
use ``float8`` (relative error ~1e-12, far below the 3 significant digits a
standard error carries) to keep 480 aggregate expressions fast.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .activities import ResolvedActivity
from .population import PopulationSQL
from .weights import WeightScheme


@dataclass(frozen=True)
class YearStatistics:
    """Sufficient statistics for one survey year (population-filtered)."""

    year: int
    n_respondents: int
    n_participants: int
    sum_w: float
    sum_wx: float
    sum_wp: float
    rep_sum_w: tuple[float, ...] | None = None
    rep_sum_wx: tuple[float, ...] | None = None
    rep_sum_wp: tuple[float, ...] | None = None


def combine(stats: list[YearStatistics]) -> YearStatistics:
    """Pool years by summing (valid because every estimator is a ratio of
    sums and the weights of a pooled period simply add; User's Guide
    multi-year guidance)."""
    if not stats:
        raise ValueError("cannot combine zero years of statistics")
    has_reps = stats[0].rep_sum_w is not None

    def sum_field(name: str) -> float:
        return sum(getattr(s, name) for s in stats)

    def sum_reps(name: str) -> tuple[float, ...] | None:
        if not has_reps:
            return None
        arrays = [getattr(s, name) for s in stats]
        return tuple(sum(values) for values in zip(*arrays, strict=True))

    return YearStatistics(
        year=0,
        n_respondents=sum(s.n_respondents for s in stats),
        n_participants=sum(s.n_participants for s in stats),
        sum_w=sum_field("sum_w"),
        sum_wx=sum_field("sum_wx"),
        sum_wp=sum_field("sum_wp"),
        rep_sum_w=sum_reps("rep_sum_w"),
        rep_sum_wx=sum_reps("rep_sum_wx"),
        rep_sum_wp=sum_reps("rep_sum_wp"),
    )


def _replicate_select(scheme: WeightScheme) -> str:
    cols = scheme.replicate_columns
    sum_w = ", ".join(f"sum(rw.{c}::float8)" for c in cols)
    sum_wx = ", ".join(f"sum(rw.{c}::float8 * base.x)" for c in cols)
    sum_wp = ", ".join(f"sum(rw.{c}::float8 * base.p)" for c in cols)
    return (
        f", ARRAY[{sum_w}] AS rep_sum_w, ARRAY[{sum_wx}] AS rep_sum_wx"
        f", ARRAY[{sum_wp}] AS rep_sum_wp"
    )


def build_query(
    *,
    activity: ResolvedActivity,
    population: PopulationSQL,
    scheme: WeightScheme,
    with_replicates: bool,
) -> str:
    """Assemble the sufficient-statistics SQL. All dynamic fragments come from
    fixed internal vocabularies (column names, validated code lists); user
    values travel only as bound parameters."""
    joins = []
    if population.needs_roster:
        joins.append(
            "JOIN atus.household_members hm ON hm.tucaseid = r.tucaseid AND hm.lineno = 1"
        )
    if population.needs_cps:
        joins.append("JOIN atus.cps_persons c ON c.tucaseid = r.tucaseid AND c.lineno = 1")
    conditions = ["r.data_year = ANY(%(years)s)", *population.conditions]

    replicate_join = (
        f"JOIN {scheme.replicate_table} rw ON rw.tucaseid = base.tucaseid"
        if with_replicates else ""
    )
    replicate_select = _replicate_select(scheme) if with_replicates else ""

    return f"""
WITH activity_minutes AS (
    SELECT a.tucaseid, sum(a.duration_minutes) AS minutes
    FROM atus.activities a
    WHERE {activity.sql_condition}
    GROUP BY a.tucaseid
),
base AS (
    SELECT r.data_year AS year,
           r.tucaseid,
           COALESCE(m.minutes, 0) AS x,
           (COALESCE(m.minutes, 0) > 0)::int AS p,
           r.{scheme.point_column} AS w
    FROM atus.respondents r
    {' '.join(joins)}
    LEFT JOIN activity_minutes m ON m.tucaseid = r.tucaseid
    WHERE {' AND '.join(conditions)}
)
SELECT base.year,
       count(*)::bigint AS n_respondents,
       sum(base.p)::bigint AS n_participants,
       sum(base.w) AS sum_w,
       sum(base.w * base.x) AS sum_wx,
       sum(base.w * base.p) AS sum_wp
       {replicate_select}
FROM base
{replicate_join}
GROUP BY base.year
ORDER BY base.year
"""


def fetch_year_statistics(
    conn: psycopg.Connection,
    *,
    years: tuple[int, ...],
    activity: ResolvedActivity,
    population: PopulationSQL,
    scheme: WeightScheme,
    with_replicates: bool,
) -> dict[int, YearStatistics]:
    sql = build_query(
        activity=activity, population=population, scheme=scheme,
        with_replicates=with_replicates,
    )
    params: dict[str, object] = {
        "years": list(years),
        **activity.sql_params,
        **population.params,
    }
    out: dict[int, YearStatistics] = {}
    for row in conn.execute(sql, params):
        if with_replicates:
            (year, n, n_part, sum_w, sum_wx, sum_wp, rep_w, rep_wx, rep_wp) = row
            reps = (
                tuple(float(v) for v in rep_w),
                tuple(float(v) for v in rep_wx),
                tuple(float(v) for v in rep_wp),
            )
        else:
            (year, n, n_part, sum_w, sum_wx, sum_wp) = row
            reps = (None, None, None)
        out[year] = YearStatistics(
            year=year,
            n_respondents=int(n),
            n_participants=int(n_part),
            sum_w=float(sum_w),
            sum_wx=float(sum_wx),
            sum_wp=float(sum_wp),
            rep_sum_w=reps[0],
            rep_sum_wx=reps[1],
            rep_sum_wp=reps[2],
        )
    return out
