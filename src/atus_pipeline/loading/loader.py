"""Load staged ATUS files into the canonical PostgreSQL schema.

Loading semantics: build, then switch (``loading/switch.py``,
docs/adr/ADR-013-data-releases-and-serving-db.md). The new data is loaded
into a shadow schema (``atus_next``, built from the same migrations), the
shadow can be validated, and one short transaction then swaps the shadow's
tables into ``atus``, marks the run ``succeeded`` and writes its
``data_version``. Readers of the live schema are never blocked by the load
itself; they see either the complete previous data or the complete new data,
and the data version changes in the same commit as the data. A failed load
leaves the live data and its version untouched.

Run metadata (``atus.ingestion_runs``) records the attempt before the data is
built, so failed attempts remain visible; an attempt abandoned by a killed
loader is marked failed by the next load.

Rows stream from the staged CSVs through the pure transformers in
``transformation/tables.py`` into PostgreSQL via the COPY protocol; nothing is
materialized in memory except the set of respondent case IDs (used to filter
the ATUS-CPS file to respondent households).
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

from .. import __version__ as PIPELINE_VERSION
from ..config import Settings
from ..database.migrate import assert_schema_current, default_migrations_dir
from ..database.versions import format_load_version
from ..manifest import Manifest
from ..serving.guard import refuse_if_serving
from ..sources import SourceFile, get_source, lexicon_path
from ..transformation.tables import TABLE_SPECS, TableSpec
from ..validation.results import CheckResult, persist_results
from .errors import LoadError
from .switch import (
    CANONICAL_TABLES,
    SHADOW_SCHEMA,
    SwitchOptions,
    analyze_shadow,
    create_shadow,
    drop_shadow,
    switch,
)

__all__ = ["LoadError", "SwitchOptions", "TableLoadResult", "load_all"]

log = logging.getLogger(__name__)

_PROGRESS_EVERY_ROWS = 500_000

# Session advisory lock held for a whole load: one loader per database.
_LOAD_LOCK_KEY = 0x4154_5553_4C4F_4144  # "ATUSLOAD"

# Validates the shadow before the switch: (connection, schema) -> results.
# Any failed error-severity result refuses the switch.
Validator = Callable[[psycopg.Connection, str], list[CheckResult]]


@dataclass
class TableLoadResult:
    table: str
    source_rows: int
    loaded_rows: int


def _lexicon_path(settings: Settings) -> Path:
    return lexicon_path(settings.reference_dir, settings.release)


def _load_lexicon(
    conn: psycopg.Connection, settings: Settings, schema: str = SHADOW_SCHEMA
) -> dict[str, int]:
    """Load the committed activity lexicon CSV into the three tier tables."""
    path = _lexicon_path(settings)
    if not path.exists():
        raise LoadError(f"Activity lexicon reference file missing: {path}")
    by_level: dict[str, list[dict[str, str]]] = {"1": [], "2": [], "3": []}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            by_level[row["level"]].append(row)

    def copy_into(table: str, columns: str) -> sql.Composed:
        return sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier(schema, table), sql.SQL(columns)
        )

    with conn.cursor() as cur:
        with cur.copy(copy_into("activity_tier1", "code, name")) as copy:
            for row in by_level["1"]:
                copy.write_row((row["code"], row["name"]))
        with cur.copy(copy_into("activity_tier2", "code, tier1_code, name")) as copy:
            for row in by_level["2"]:
                copy.write_row((row["code"], row["code"][:2], row["name"]))
        with cur.copy(
            copy_into("activity_codes", "code, tier2_code, name, harmonization_note")
        ) as copy:
            for row in by_level["3"]:
                copy.write_row(
                    (row["code"], row["code"][:4], row["name"], row["harmonization_note"] or None)
                )
    counts = {level: len(rows) for level, rows in by_level.items()}
    log.info(
        "Loaded activity lexicon: %d tier-1, %d tier-2, %d activity codes",
        counts["1"], counts["2"], counts["3"],
    )
    return counts


def _stream_rows(path: Path):
    with path.open(newline="") as fh:
        yield from csv.DictReader(fh)


def _load_table(
    conn: psycopg.Connection,
    spec: TableSpec,
    staged_path: Path,
    respondent_ids: set[int] | None,
    schema: str = SHADOW_SCHEMA,
) -> tuple[TableLoadResult, set[int] | None]:
    """Stream one staged file through its transformer into its table.

    Returns the load result and, when loading respondents, the set of case IDs
    (used afterwards to filter the ATUS-CPS file).
    """
    columns = ", ".join(spec.columns)
    source_rows = 0
    loaded_rows = 0
    collected_ids: set[int] | None = set() if spec.table == "respondents" else None
    filter_ids = respondent_ids if spec.table == "cps_persons" else None

    log.info("%s: loading from %s", spec.table, staged_path.name)
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(schema, spec.table), sql.SQL(columns)
    )
    with conn.cursor() as cur:
        with cur.copy(statement) as copy:
            for row in _stream_rows(staged_path):
                source_rows += 1
                values = spec.transform(row)
                if filter_ids is not None and values[0] not in filter_ids:
                    continue
                copy.write_row(values)
                loaded_rows += 1
                if collected_ids is not None:
                    collected_ids.add(values[0])
                if loaded_rows % _PROGRESS_EVERY_ROWS == 0:
                    log.info("%s: %d rows loaded...", spec.table, loaded_rows)

    if filter_ids is not None and source_rows != loaded_rows:
        log.info(
            "%s: %d of %d source rows loaded (%d rows for households of "
            "nonrespondents excluded by design)",
            spec.table, loaded_rows, source_rows, source_rows - loaded_rows,
        )
    else:
        log.info("%s: %d rows loaded", spec.table, loaded_rows)
    result = TableLoadResult(spec.table, source_rows, loaded_rows)
    return result, (collected_ids if collected_ids is not None else respondent_ids)


def _check_staged_inputs(
    settings: Settings, manifest: Manifest
) -> list[tuple[TableSpec, SourceFile, Path]]:
    plan = []
    for spec in TABLE_SPECS:
        source = get_source(settings.release, spec.dataset_key)
        staged = settings.staging_dir / source.dat_name
        if not staged.exists():
            raise LoadError(f"Staged file missing for {spec.table}: {staged}. Run `atus extract`.")
        if manifest.get(settings.release, source.key) is None:
            raise LoadError(
                f"No manifest entry for {source.key}. Run `atus download` and `atus extract` "
                "so provenance is recorded before loading."
            )
        plan.append((spec, source, staged))
    if not _lexicon_path(settings).exists():
        raise LoadError(f"Activity lexicon reference file missing: {_lexicon_path(settings)}")
    return plan


def _record_source_file(
    conn: psycopg.Connection, run_id: int, settings: Settings, source: SourceFile,
    manifest: Manifest, result: TableLoadResult,
) -> None:
    entry = manifest.get(settings.release, source.key)
    conn.execute(
        """
        INSERT INTO atus.source_files
            (ingestion_run_id, release, file_key, url, zip_name, zip_sha256,
             zip_size_bytes, downloaded_at, data_file_name, source_row_count,
             loaded_table, loaded_row_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run_id, settings.release, source.key, entry.url, entry.zip_name,
            entry.zip_sha256, entry.zip_size_bytes,
            entry.downloaded_at or None, entry.dat_name, result.source_rows,
            result.table, result.loaded_rows,
        ),
    )


@contextmanager
def _load_session(conn: psycopg.Connection) -> Iterator[None]:
    """Autocommit (every step below opens its own transaction) and the
    one-loader-per-database advisory lock, both undone on exit."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise LoadError(
            "load_all needs a connection with no open transaction (it commits the new "
            "data in its own transactions)"
        )
    previous_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (_LOAD_LOCK_KEY,)).fetchone()[0]:
            raise LoadError("another `atus load` is running against this database")
        try:
            yield
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_LOAD_LOCK_KEY,))
            except psycopg.Error:  # a broken connection releases the lock anyway
                log.warning("Could not release the load lock explicitly")
    finally:
        if not conn.closed:
            conn.autocommit = previous_autocommit


def _abandon_stale_runs(conn: psycopg.Connection) -> None:
    """Runs still 'running' belong to loaders that died (the advisory lock
    proves none is active): record them as failed."""
    with conn.transaction():
        stale = conn.execute(
            "UPDATE atus.ingestion_runs SET status = 'failed', finished_at = now(), "
            "notes = 'abandoned: the loader exited before finishing' "
            "WHERE status = 'running' RETURNING id"
        ).fetchall()
    if stale:
        log.warning("Marked abandoned ingestion run(s) %s as failed", [r[0] for r in stale])


def _mark_failed(conn: psycopg.Connection, run_id: int, exc: BaseException) -> None:
    try:
        with conn.transaction():
            conn.execute(
                "UPDATE atus.ingestion_runs SET status = 'failed', finished_at = now(), "
                "notes = %s WHERE id = %s AND status = 'running'",
                (f"{type(exc).__name__}: {exc}"[:2000], run_id),
            )
    except Exception:  # the original error matters more than the bookkeeping
        log.exception("Could not record ingestion run %d as failed", run_id)


def load_all(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    validate: Validator | None = None,
    options: SwitchOptions | None = None,
    migrations_dir: Path | None = None,
) -> list[TableLoadResult]:
    """Rebuild the canonical data from staged files and switch to it atomically.

    ``validate`` (optional) checks the shadow schema before the switch; any
    failed error-severity check refuses the switch. Its results are recorded
    in ``atus.validation_results`` together with the switch. Raises on
    failure, with the live data and data version unchanged.
    """
    options = options or SwitchOptions()
    migrations_dir = migrations_dir or default_migrations_dir()
    manifest = Manifest(settings.manifest_path)
    plan = _check_staged_inputs(settings, manifest)

    with _load_session(conn):
        refuse_if_serving(conn, "load")
        assert_schema_current(conn, migrations_dir)
        _abandon_stale_runs(conn)
        with conn.transaction():
            run_id = conn.execute(
                "INSERT INTO atus.ingestion_runs (release, pipeline_version) "
                "VALUES (%s, %s) RETURNING id",
                (settings.release, PIPELINE_VERSION),
            ).fetchone()[0]
        log.info("Ingestion run %d started (release %s)", run_id, settings.release)

        results: list[TableLoadResult] = []
        loaded: list[tuple[SourceFile, TableLoadResult]] = []
        try:
            with conn.transaction():
                create_shadow(conn, migrations_dir)
                _load_lexicon(conn, settings)
                respondent_ids: set[int] | None = None
                for spec, source, staged in plan:
                    result, respondent_ids = _load_table(conn, spec, staged, respondent_ids)
                    loaded.append((source, result))
                    results.append(result)
            # Server time after the commit: transactions that began earlier may
            # hold snapshots that cannot see the shadow rows (see switch.py).
            cutoff = conn.execute("SELECT clock_timestamp()").fetchone()[0]
            log.info("New data built in schema %s; analyzing", SHADOW_SCHEMA)
            analyze_shadow(conn)

            checks: list[CheckResult] = []
            if validate is not None:
                checks = validate(conn, SHADOW_SCHEMA)
                failed = [c for c in checks if not c.passed and c.severity == "error"]
                if failed:
                    raise LoadError(
                        f"{len(failed)} validation check(s) failed on the new data "
                        f"(first: {failed[0].name}); the live data was not switched"
                    )

            version = format_load_version(settings.release, run_id)

            def publish(tx: psycopg.Connection) -> None:
                for source, result in loaded:
                    _record_source_file(tx, run_id, settings, source, manifest, result)
                if checks:
                    persist_results(tx, checks)
                updated = tx.execute(
                    "UPDATE atus.ingestion_runs SET status = 'succeeded', "
                    "finished_at = now(), data_version = %s "
                    "WHERE id = %s AND status = 'running'",
                    (version, run_id),
                ).rowcount
                if updated != 1:
                    raise LoadError(f"ingestion run {run_id} is no longer 'running'")

            switch(conn, publish=publish, cutoff=cutoff, options=options)
        except BaseException as exc:
            _mark_failed(conn, run_id, exc)
            log.error("Ingestion run %d failed; live data unchanged. %s", run_id, exc)
            _drop_shadow_quietly(conn)
            raise

        _drop_shadow_quietly(conn)  # only the shadow's unused metadata tables remain
        log.info(
            "Ingestion run %d succeeded; data version %s (%d canonical tables switched)",
            run_id, version, len(CANONICAL_TABLES),
        )
    return results


def _drop_shadow_quietly(conn: psycopg.Connection) -> None:
    try:
        drop_shadow(conn)
    except Exception:  # leftovers are dropped by the next load
        log.warning("Could not drop schema %s; the next load drops it", SHADOW_SCHEMA)
