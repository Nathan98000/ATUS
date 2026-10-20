"""Build a serving database from the validated full database (brief §2, §9).

``atus build-serving-db`` derives the small database the hosted API reads
from the full local database, without changing any number the engine
computes (docs/adr/ADR-013-data-releases-and-serving-db.md, "Serving
database"):

* ``activities``: one row per (respondent, activity code) with the summed
  minutes, kept ``smallint`` so ``sum()`` still returns ``bigint`` (I01), and
  stored in activity-code order behind the ``activity_code`` index;
* replicate weights as ``float8`` (the value the engine casts to), without the
  all-NULL 2020 rows and the all-zero pandemic rows that every estimate
  already excludes through its ``weight > 0`` filter (I02);
* the respondent's own (line 1) roster and CPS rows, and the respondents
  columns the engine and API read, with their NULL rules and CHECK
  constraints (``serving/shape.py``);
* the lexicon, one ``succeeded`` ingestion run carrying the serving
  ``data_version``, the source run's file provenance, an empty
  ``validation_results`` (``atus validate-analytics`` writes there), and no
  ``activity_companions``;
* the serving marker (F57), so ``atus migrate``/``load``/``validate-db``
  refuse to touch it.

The source is read in one ``REPEATABLE READ READ ONLY`` transaction (a
consistent snapshot; nothing is ever written there). The target is written
in one transaction, migrations included: a failed build leaves it exactly as
it was (empty, or freshly migrated). Before committing, the build proves its invariants: the
source assertions (:data:`ASSERTIONS`) are all zero, every served table's
rows re-read from the target hash to exactly the rows streamed from the
source transformation, row counts reconcile with the omitted-row counts, the
serving contract and every kept CHECK constraint are present, and the
activities are physically in activity-code order.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql

from .. import __version__ as PIPELINE_VERSION
from ..database.dsn import same_database
from ..database.migrate import apply_migrations, default_migrations_dir
from ..database.versions import (
    format_serving_version,
    new_build_id,
    read_data_version,
    version_from_row,
)
from .content import (
    HASHED_TABLES,
    StreamDigest,
    TableDigest,
    combine,
    compute_content_hash,
    pin_text_output,
    table_columns,
)
from .contract import READ, missing_contract_columns
from .guard import SERVING_MARKER, serving_marker
from .shape import DROPPED_TABLES, missing_checks, post_load_sql, read_shapes, reshape_sql

log = logging.getLogger(__name__)

# Version of the serving design (shape, omitted rows, content-hash definition).
SERVING_FORMAT = 1

_REP = READ["replicate_weights"][1:]
_PAN = READ["pandemic_replicate_weights"][1:]


def _all_null(columns: tuple[str, ...], alias: str = "") -> str:
    return " AND ".join(f"{alias}{c} IS NULL" for c in columns)


def _all_zero(columns: tuple[str, ...], alias: str = "") -> str:
    # coalesce: a row with a NULL cell is not "all zero" (it is kept as is).
    return "coalesce(" + " AND ".join(f"{alias}{c} = 0" for c in columns) + ", false)"


def _round_trip_changes(columns: tuple[str, ...]) -> str:
    return "coalesce(sum(" + " + ".join(
        f"({c}::float8::numeric <> {c})::int" for c in columns
    ) + "), 0)"


# Rows a serving table omits, as a predicate on the full-DB table (I02).
OMITTED: dict[str, str] = {
    "replicate_weights": _all_null(_REP),
    "pandemic_replicate_weights": _all_zero(_PAN),
    "household_members": "lineno <> 1",
    "cps_persons": "lineno <> 1",
}

# Build-time assertions on the source snapshot: each must count 0, or the
# build stops before writing anything.
ASSERTIONS: dict[str, str] = {
    "replicate rows omitted although the respondent has final_weight > 0":
        "SELECT count(*) FROM atus.replicate_weights w "
        "JOIN atus.respondents r USING (tucaseid) "
        f"WHERE {_all_null(_REP, 'w.')} AND r.final_weight > 0",
    "pandemic replicate rows omitted although the respondent has pandemic_weight > 0":
        "SELECT count(*) FROM atus.pandemic_replicate_weights w "
        "JOIN atus.respondents r USING (tucaseid) "
        f"WHERE {_all_zero(_PAN, 'w.')} AND r.pandemic_weight > 0",
    "(respondent, activity code) minute sums outside 0-1440 (smallint, CHECK)":
        "SELECT count(*) FROM (SELECT sum(duration_minutes) AS m FROM atus.activities "
        "GROUP BY tucaseid, activity_code) g WHERE m NOT BETWEEN 0 AND 1440",
    "replicate weights changed by the numeric -> float8 -> numeric round trip":
        f"SELECT {_round_trip_changes(_REP)} FROM atus.replicate_weights",
    "pandemic replicate weights changed by the numeric -> float8 -> numeric round trip":
        f"SELECT {_round_trip_changes(_PAN)} FROM atus.pandemic_replicate_weights",
}

# Source counts reported in the manifest and reconciled with the served rows.
COUNTS: dict[str, str] = {
    "activities.episodes": "SELECT count(*) FROM atus.activities",
    "activities.groups":
        "SELECT count(*) FROM (SELECT 1 FROM atus.activities "
        "GROUP BY tucaseid, activity_code) g",
    "activity_companions.rows": "SELECT count(*) FROM atus.activity_companions",
    **{f"{table}.rows": f"SELECT count(*) FROM atus.{table}" for table in OMITTED},
    **{f"{table}.omitted": f"SELECT count(*) FROM atus.{table} WHERE {predicate}"
       for table, predicate in OMITTED.items()},
}


class ServingBuildError(RuntimeError):
    """The serving database could not be built; the target is unchanged."""


@dataclass
class BuildReport:
    """What a build produced; the release manifest is built from it."""

    build_id: str
    release: str = ""
    data_version: str = ""
    content_hash: str = ""
    serving_format: int = SERVING_FORMAT
    built_at: str = ""
    source: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    assertions: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    tables: dict[str, dict] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)
    server_version: str = ""
    seconds: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _source_query(table: str, columns: tuple[str, ...], key: tuple[str, ...]) -> sql.Composed:
    """The full-DB query whose rows are exactly ``table``'s serving rows, in
    the order they are stored and hashed."""
    ident = sql.Identifier
    if table == "activities":
        expected = ("tucaseid", "activity_code", "duration_minutes")
        if columns != expected:
            raise ServingBuildError(f"unexpected serving activities columns {columns}")
        return sql.SQL(
            "SELECT tucaseid, activity_code, sum(duration_minutes)::smallint "
            "FROM atus.activities GROUP BY tucaseid, activity_code "
            "ORDER BY activity_code, tucaseid"
        )
    if table in ("replicate_weights", "pandemic_replicate_weights"):
        select = sql.SQL(", ").join(
            ident(c) if c == "tucaseid" else sql.SQL("{}::float8").format(ident(c))
            for c in columns
        )
    else:
        select = sql.SQL(", ").join(ident(c) for c in columns)
    where = (
        sql.SQL(" WHERE NOT ({})").format(sql.SQL(OMITTED[table])) if table in OMITTED
        else sql.SQL("")
    )
    return sql.SQL("SELECT {select} FROM atus.{table}{where} ORDER BY {key}").format(
        select=select, table=ident(table), where=where,
        key=sql.SQL(", ").join(ident(c) for c in key),
    )


def _copy_table(src: psycopg.Connection, tgt: psycopg.Connection, table: str,
                key: tuple[str, ...]) -> TableDigest:
    """Stream one table's serving rows from the source into the target (COPY
    text, so the hash of the stream is directly comparable with the target's)."""
    columns = table_columns(tgt, table)
    names = tuple(c for c, _ in columns)
    stream = StreamDigest()
    target_copy = sql.SQL("COPY atus.{} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(", ").join(sql.Identifier(c) for c in names)
    )
    with src.cursor() as scur, tgt.cursor() as tcur:
        with scur.copy(sql.SQL("COPY ({}) TO STDOUT").format(
                _source_query(table, names, key))) as out, tcur.copy(target_copy) as inp:
            for chunk in out:
                stream.update(chunk)
                inp.write(chunk)
    log.info("  copied %-28s %12s rows", table, f"{stream.rows:,}")
    return TableDigest(table, columns, stream.rows, stream.hexdigest())


def _check_target(tgt: psycopg.Connection) -> None:
    """The target must be a new database: empty, or freshly migrated and empty."""
    with tgt.transaction():
        if tgt.execute("SELECT to_regnamespace('atus') IS NULL").fetchone()[0]:
            return
        reason = serving_marker(tgt)
        if reason:
            raise ServingBuildError(
                f"the target is already a serving database ({reason}); build into a new database"
            )
        tables = [r[0] for r in tgt.execute(
            "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'atus' AND c.relkind = 'r'"
        )]
        for table in tables:
            if tgt.execute(
                sql.SQL("SELECT EXISTS (SELECT FROM atus.{})").format(sql.Identifier(table))
            ).fetchone()[0]:
                raise ServingBuildError(
                    f"the target database is not empty (atus.{table} has rows); "
                    "build into a new database"
                )


def _source_run(src: psycopg.Connection) -> dict:
    row = src.execute(
        "SELECT to_jsonb(r) FROM atus.ingestion_runs r "
        "WHERE r.status = 'succeeded' ORDER BY r.id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ServingBuildError("the source database has no succeeded ingestion run")
    run = row[0]
    return {
        "run_id": run["id"],
        "release": run["release"],
        "pipeline_version": run["pipeline_version"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "data_version": version_from_row(run),
    }


def validation_evidence(src: psycopg.Connection, run_started_at: str) -> dict:
    """The latest ``atus validate-db`` and ``atus validate-analytics`` results
    recorded for the source's current data (``None`` when there are none)."""
    batches = src.execute(
        """
        SELECT run_at, check_name LIKE 'analytics: %%' AS analytics, count(*),
               count(*) FILTER (WHERE NOT passed AND severity = 'error')
        FROM atus.validation_results
        WHERE run_at >= %s::timestamptz
        GROUP BY run_at, analytics
        ORDER BY run_at
        """,
        (run_started_at,),
    ).fetchall()
    evidence: dict[str, dict | None] = {"validate_db": None, "validate_analytics": None}
    for run_at, analytics, checks, failed in batches:
        evidence["validate_analytics" if analytics else "validate_db"] = {
            "run_at": run_at.isoformat(), "checks": checks, "failed_errors": failed,
        }
    return evidence


def _require_validated(evidence: dict) -> None:
    problems = []
    for name, command in (("validate_db", "atus validate-db"),
                          ("validate_analytics", "atus validate-analytics")):
        latest = evidence[name]
        if latest is None:
            problems.append(f"no `{command}` results recorded for the loaded data")
        elif latest["failed_errors"]:
            problems.append(
                f"the latest `{command}` run ({latest['run_at']}) has "
                f"{latest['failed_errors']} failed check(s)"
            )
    if problems:
        raise ServingBuildError(
            "the source database is not validated: " + "; ".join(problems)
            + ". Run the release procedure's validation first (or pass "
            "--allow-unvalidated for a test fixture)."
        )


def _count(conn: psycopg.Connection, query: str) -> int:
    return int(conn.execute(query).fetchone()[0])


def _reconcile(counts: dict[str, int], served: dict[str, TableDigest]) -> list[str]:
    """Served rows must equal source rows minus omitted rows."""
    problems = []
    for table in OMITTED:
        expected = counts[f"{table}.rows"] - counts[f"{table}.omitted"]
        if served[table].rows != expected:
            problems.append(f"{table}: served {served[table].rows}, expected {expected}")
    if served["activities"].rows != counts["activities.groups"]:
        problems.append(
            f"activities: served {served['activities'].rows}, "
            f"expected {counts['activities.groups']} (respondent, code) groups"
        )
    return problems


def _activities_out_of_order(tgt: psycopg.Connection) -> int:
    return _count(
        tgt,
        "SELECT count(*) FROM (SELECT activity_code, "
        "lag(activity_code) OVER (ORDER BY ctid) AS previous FROM atus.activities) s "
        "WHERE previous > activity_code",
    )


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _write_run(tgt: psycopg.Connection, src: psycopg.Connection, source: dict,
               report: BuildReport) -> None:
    """The one succeeded ingestion run (the source run's id and timestamps, so
    ``/meta`` describes the same ingested data), its file provenance, and the
    serving marker."""
    notes = json.dumps({
        "kind": "serving",
        "serving_format": SERVING_FORMAT,
        "build_id": report.build_id,
        "content_hash": report.content_hash,
        "source_data_version": source["data_version"],
        "builder": f"atus-pipeline {PIPELINE_VERSION}",
    }, sort_keys=True)
    tgt.execute(
        """
        INSERT INTO atus.ingestion_runs
            (id, release, pipeline_version, started_at, finished_at, status, notes, data_version)
        OVERRIDING SYSTEM VALUE
        VALUES (%s, %s, %s, %s, %s, 'succeeded', %s, %s)
        """,
        (source["run_id"], source["release"], source["pipeline_version"],
         _timestamp(source["started_at"]), _timestamp(source["finished_at"]), notes,
         report.data_version),
    )
    tgt.execute(
        "SELECT setval(pg_get_serial_sequence('atus.ingestion_runs', 'id'), %s)",
        (source["run_id"],),
    )
    names = [c for c, _ in table_columns(tgt, "source_files")]
    rows = src.execute(
        sql.SQL("SELECT {} FROM atus.source_files WHERE ingestion_run_id = %s ORDER BY id")
        .format(sql.SQL(", ").join(sql.Identifier(c) for c in names)),
        (source["run_id"],),
    ).fetchall()
    insert = sql.SQL("INSERT INTO atus.source_files ({}) OVERRIDING SYSTEM VALUE VALUES ({})")
    with tgt.cursor() as cur:
        cur.executemany(
            insert.format(sql.SQL(", ").join(sql.Identifier(c) for c in names),
                          sql.SQL(", ").join(sql.Placeholder() * len(names))),
            rows,
        )
    report.counts["source_files.rows"] = len(rows)
    marker = (
        f"{SERVING_MARKER} build {report.build_id}; release {report.release}; "
        f"data version {report.data_version}; built from {source['data_version']}. "
        "A serving database: never run atus migrate, load or validate-db here."
    )
    tgt.execute(sql.SQL("COMMENT ON SCHEMA atus IS {}").format(sql.Literal(marker)))


def _sizes(conn: psycopg.Connection) -> tuple[dict[str, int], dict[str, dict]]:
    tables = {
        name: {"total_bytes": total, "heap_bytes": heap, "index_bytes": index}
        for name, total, heap, index in conn.execute(
            "SELECT c.relname, pg_total_relation_size(c.oid), pg_relation_size(c.oid), "
            "pg_indexes_size(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'atus' AND c.relkind = 'r'"
        )
    }
    database, cluster = conn.execute(
        "SELECT pg_database_size(current_database()), "
        "(SELECT sum(pg_database_size(datname))::bigint FROM pg_database "
        " WHERE has_database_privilege(datname, 'CONNECT'))"
    ).fetchone()
    sizes = {
        "database_bytes": int(database),
        "cluster_bytes": int(cluster),
        "atus_tables_bytes": sum(t["total_bytes"] for t in tables.values()),
    }
    return sizes, tables


def build_serving_database(
    source_url: str,
    target_url: str,
    *,
    build_id: str | None = None,
    require_validated: bool = True,
    migrations_dir: Path | None = None,
) -> BuildReport:
    """Build a serving database in ``target_url`` (a new database: empty, or
    freshly migrated and empty) from the full database at ``source_url``
    (read only). Raises :class:`ServingBuildError` when an invariant does not
    hold; the target is then exactly as it was."""
    if same_database(source_url, target_url):
        raise ServingBuildError("the source and the target are the same database")
    migrations_dir = migrations_dir or default_migrations_dir()
    report = BuildReport(build_id=build_id or new_build_id())
    clock = time.perf_counter()

    def step(name: str, started: float) -> None:
        report.seconds[name] = round(time.perf_counter() - started, 2)
        log.info("%s: %.2f s", name, report.seconds[name])

    # Autocommit sessions: every transaction below is explicit.
    with psycopg.connect(target_url, autocommit=True) as tgt, psycopg.connect(
        source_url, autocommit=True, options="-c default_transaction_read_only=on"
    ) as src:
        _check_target(tgt)
        src.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        src.read_only = True
        src.execute("SET jit = off")
        src.execute("SET work_mem = '128MB'")

        # One transaction on the target, migrations included: a failed build
        # leaves it exactly as it was. One consistent, read-only snapshot of
        # the full database.
        with tgt.transaction(), src.transaction():
            started = time.perf_counter()
            apply_migrations(tgt, migrations_dir)
            report.server_version = tgt.execute("SHOW server_version").fetchone()[0]
            step("migrate target", started)

            if src.execute("SHOW transaction_read_only").fetchone()[0] != "on":
                raise ServingBuildError("could not open a read-only source transaction")
            reason = serving_marker(src)
            if reason:
                raise ServingBuildError(f"the source is a serving database ({reason})")
            source = _source_run(src)
            report.source = source
            report.release = source["release"]
            report.validation = validation_evidence(src, source["started_at"])
            if require_validated:
                _require_validated(report.validation)

            started = time.perf_counter()
            report.assertions = {label: _count(src, q) for label, q in ASSERTIONS.items()}
            failed = {label: n for label, n in report.assertions.items() if n}
            if failed:
                raise ServingBuildError(f"source assertions failed: {failed}")
            report.counts = {label: _count(src, q) for label, q in COUNTS.items()}
            step("source assertions and counts", started)

            pin_text_output(src)
            tgt.execute("SET LOCAL maintenance_work_mem = '256MB'")
            canonical = read_shapes(tgt)
            for statement in reshape_sql(canonical):
                tgt.execute(statement)
            pin_text_output(tgt)

            started = time.perf_counter()
            served = {table: _copy_table(src, tgt, table, key)
                      for table, key in HASHED_TABLES}
            step("copy", started)

            started = time.perf_counter()
            for statement in post_load_sql(canonical):
                tgt.execute(statement)
            step("keys, indexes and foreign keys", started)

            problems = _reconcile(report.counts, served)
            missing = missing_contract_columns(tgt)
            if missing:
                problems.append(f"serving contract columns missing: {missing}")
            lost = missing_checks(canonical, read_shapes(tgt))
            if lost:
                problems.append(f"CHECK constraints lost: {lost}")
            leftover = [t for t in DROPPED_TABLES if tgt.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"atus.{t}",)).fetchone()[0]]
            if leftover:
                problems.append(f"tables that should be dropped remain: {leftover}")
            out_of_order = _activities_out_of_order(tgt)
            if out_of_order:
                problems.append(f"activities not in activity_code order ({out_of_order})")
            if problems:
                raise ServingBuildError("serving invariants failed: " + "; ".join(problems))

            # Re-read every served table from the target: it must hash to
            # exactly the rows streamed from the source transformation.
            started = time.perf_counter()
            target_hash = compute_content_hash(tgt)
            source_hash = combine([served[table] for table, _ in HASHED_TABLES])
            step("content hash", started)
            if target_hash != source_hash:
                mismatched = [
                    t.table for t, s in zip(target_hash.tables, source_hash.tables,
                                            strict=True) if t != s
                ]
                raise ServingBuildError(
                    f"content hash mismatch between source and target: {mismatched}"
                )
            report.content_hash = target_hash.content_hash
            report.tables = {
                t.table: {"rows": t.rows, "sha256": t.sha256} for t in target_hash.tables
            }
            report.data_version = format_serving_version(
                report.release, report.build_id, report.content_hash
            )
            _write_run(tgt, src, source, report)
            if _count(tgt, "SELECT count(*) FROM atus.validation_results"):
                raise ServingBuildError("validation_results must be empty")
            if read_data_version(tgt) != report.data_version:
                raise ServingBuildError("the target does not report the new data version")
        report.built_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    with psycopg.connect(target_url, autocommit=True) as tgt:
        started = time.perf_counter()
        tgt.execute("VACUUM (ANALYZE)")
        step("VACUUM (ANALYZE)", started)
        report.sizes, sizes = _sizes(tgt)
        for table, measured in sizes.items():
            report.tables.setdefault(table, {}).update(measured)
    report.seconds["total"] = round(time.perf_counter() - clock, 2)
    log.info("Serving database built: %s", report.data_version)
    return report
