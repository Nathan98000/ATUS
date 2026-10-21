"""Checks of a built or restored serving database (``atus verify-serving-db``).

Run after a restore (``scripts/ops/restore_serving_db.sh`` runs them) and
before a release switch. Cheap checks always run: the serving marker, the
data version and row counts against the release manifest, the serving
contract, no large objects (any role can create them, the API role
included: security.md R16), planner statistics (a restore must be followed by ``VACUUM
(ANALYZE)``, because a scale-to-zero compute may suspend before autovacuum
runs; read from ``pg_stats``, which a suspend keeps), and the physical
activity-code order that makes the activity index scan cheap. Two checks are optional:

* ``content_hash`` re-hashes every served row and compares it with the
  manifest. It reads the whole database, which on a metered host counts as
  egress (about the size of the data), so it is meant for local drills. It
  covers row contents only, as read by the connecting role: catalog changes
  that alter what the API role computes without changing a row (a
  row-level security policy the owner bypasses, a ``search_path`` setting
  with a function of the same name) pass it, so it is no check of a
  database whose owner credentials leaked (review r3);
* ``explain`` plans (without running) the worst-case analyses (F37/F66): a
  narrow activity pooled over every year must reach ``activities`` through
  the activity-code index, and every plan's cost is reported.
"""

from __future__ import annotations

import json

import psycopg
from psycopg import sql as pgsql

from ..analytics.activities import ActivityResolver
from ..analytics.population import build_population_sql
from ..analytics.queries import build_query
from ..analytics.spec import ACTIVITY_PRESETS, ActivitySelector, PopulationFilter
from ..analytics.weights import MULTIYEAR
from ..database.versions import read_data_version
from ..validation.results import CheckResult
from .content import HASHED_TABLES, compute_content_hash
from .contract import missing_contract_columns
from .guard import serving_marker

# Correlation between the physical row order and activity_code; 1.0 right
# after a build or a restore of the dump.
MIN_ACTIVITY_CORRELATION = 0.99
# The index-scan expectation applies to a real-sized activities table only.
MIN_ROWS_FOR_INDEX_CHECK = 100_000


def _check(name: str, passed: bool, observed: str, detail: str = "",
           severity: str = "error") -> CheckResult:
    return CheckResult(name=f"serving: {name}", passed=passed, severity=severity,
                       observed=observed, detail=detail)


def worst_case_queries(conn: psycopg.Connection) -> list[tuple[str, bool, str, dict]]:
    """(label, narrow, SQL, params) for the costliest analyses, rendered by the
    engine's own query builder: replicate variance pooled over every year the
    multi-year weight covers."""
    years = [
        year for (year,) in conn.execute(
            "SELECT DISTINCT data_year FROM atus.respondents ORDER BY 1"
        ) if year != 2020
    ]
    resolver = ActivityResolver(conn)
    tier1 = tuple(code for (code,) in conn.execute(
        "SELECT code FROM atus.activity_tier1 ORDER BY code"))
    cases = (
        ("sleep (010101) pooled", True, ActivitySelector(("010101",))),
        ("BLS leisure and sports pooled", False,
         ACTIVITY_PRESETS["leisure_and_sports_bls_table"]),
        ("all tier-1 codes pooled (F66 broad selector)", False, ActivitySelector(tier1)),
    )
    population = build_population_sql(PopulationFilter())
    out = []
    for label, narrow, selector in cases:
        activity = resolver.resolve(selector)
        sql = build_query(activity=activity, population=population, scheme=MULTIYEAR,
                          with_replicates=True)
        params = {"years": years, **activity.sql_params, **population.params}
        out.append((f"{label}, {len(years)} years", narrow, sql, params))
    return out


def tables_without_statistics(conn: psycopg.Connection) -> list[str]:
    """Tables in ``atus`` that hold rows but have no planner statistics.

    Reads the statistics themselves (``pg_stats``, over the ``pg_statistic``
    catalog), which survive a restart and a Neon compute suspend. The
    ``last_analyze`` timestamps of ``pg_stat_user_tables`` are cumulative
    statistics instead: Neon discards them at every suspend, and a read-only
    serving database never gets them back from autovacuum (review r1). A
    table without rows has no statistics after ``ANALYZE`` either, and needs
    none."""
    candidates = [name for (name,) in conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'atus' AND c.relkind = 'r' AND NOT EXISTS ("
        "SELECT FROM pg_stats s WHERE s.schemaname = 'atus' AND s.tablename = c.relname) "
        "ORDER BY c.relname"
    )]
    return [
        name for name in candidates
        if conn.execute(pgsql.SQL("SELECT EXISTS (SELECT FROM atus.{})").format(
            pgsql.Identifier(name))).fetchone()[0]
    ]


def _plan_nodes(node: dict):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)


def explain_worst_cases(conn: psycopg.Connection) -> list[CheckResult]:
    results = []
    # On a small table (a test fixture) a sequential scan is the right plan.
    rows = conn.execute(
        "SELECT reltuples FROM pg_class WHERE oid = 'atus.activities'::regclass"
    ).fetchone()[0]
    large = rows >= MIN_ROWS_FOR_INDEX_CHECK
    for label, narrow, sql, params in worst_case_queries(conn):
        plan = conn.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()[0]
        if isinstance(plan, str):
            plan = json.loads(plan)
        root = plan[0]["Plan"]
        access = sorted({
            node["Node Type"] for node in _plan_nodes(root)
            if node.get("Relation Name") == "activities"
        })
        observed = f"cost {root['Total Cost']:,.0f}; activities via {', '.join(access)}"
        passed = not (narrow and large and "Seq Scan" in access)
        results.append(_check(
            f"plan: {label}", passed, observed,
            "a narrow activity selector should read activities through "
            "idx_activities_activity_code; a sequential scan means missing statistics "
            "or a lost physical order (run VACUUM (ANALYZE))" if narrow else "",
        ))
    return results


def serving_checks(
    conn: psycopg.Connection,
    manifest: dict | None = None,
    *,
    content_hash: bool = False,
    explain: bool = False,
) -> list[CheckResult]:
    """Check the serving database at ``conn`` (read only)."""
    results: list[CheckResult] = []
    marker = serving_marker(conn)
    results.append(_check("serving marker", marker is not None, marker or "none",
                          "the builder marks the schema comment and the data version"))
    version = read_data_version(conn)
    if manifest:
        results.append(_check("data version", version == manifest["data_version"], version,
                              f"manifest: {manifest['data_version']}"))
    else:
        results.append(_check("data version", True, version))

    for table, _ in HASHED_TABLES:
        rows = conn.execute(f"SELECT count(*) FROM atus.{table}").fetchone()[0]
        if manifest:
            expected = manifest["tables"][table]["rows"]
            results.append(_check(f"rows in {table}", rows == expected, f"{rows:,}",
                                  f"manifest: {expected:,}"))
    missing = missing_contract_columns(conn)
    results.append(_check("serving contract", not missing,
                          "complete" if not missing else ", ".join(missing)))
    large_objects = conn.execute("SELECT count(*) FROM pg_largeobject_metadata").fetchone()[0]
    results.append(_check(
        "no large objects", large_objects == 0, f"{large_objects} large object(s)",
        "a serving database has none, but any role can create them, the API role included "
        "(security.md R16): re-run roles.sql (--roles-only), which removes the API role's "
        "and fails on any other",
    ))
    recorded = conn.execute("SELECT count(*) FROM atus.validation_results").fetchone()[0]
    results.append(_check(
        "validation_results empty", recorded == 0, f"{recorded} row(s)",
        "expected after `atus validate-analytics` ran here; the artifact itself ships empty",
        severity="warning",
    ))
    unanalyzed = tables_without_statistics(conn)
    results.append(_check(
        "planner statistics", not unanalyzed,
        "every table with rows has statistics" if not unanalyzed
        else "no statistics: " + ", ".join(unanalyzed),
        "run VACUUM (ANALYZE) after a restore",
    ))
    correlation = conn.execute(
        "SELECT correlation FROM pg_stats WHERE schemaname = 'atus' "
        "AND tablename = 'activities' AND attname = 'activity_code'"
    ).fetchone()
    value = correlation[0] if correlation else None
    results.append(_check(
        "activities stored in activity_code order",
        value is not None and value >= MIN_ACTIVITY_CORRELATION,
        "no statistics" if value is None else f"correlation {value:.4f}",
        f"pg_stats correlation must be >= {MIN_ACTIVITY_CORRELATION}",
    ))
    if content_hash:
        computed = compute_content_hash(conn)
        expected = manifest["content_hash"] if manifest else None
        suffix_ok = version.endswith(":" + computed.content_hash[:12])
        results.append(_check(
            "content hash", suffix_ok and expected in (None, computed.content_hash),
            computed.content_hash,
            f"manifest: {expected}; the data version must end in its first 12 digits",
        ))
    if explain:
        results.extend(explain_worst_cases(conn))
    return results
