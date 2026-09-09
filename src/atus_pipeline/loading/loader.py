"""Load staged ATUS files into the canonical PostgreSQL schema.

Loading semantics: one atomic rebuild. All canonical tables are truncated and
reloaded inside a single transaction, so the database is always either the
complete previous state or the complete new state — a failed load changes
nothing. Run metadata (``atus.ingestion_runs``) is written outside that
transaction so failed attempts remain visible.

Rows stream from the staged CSVs through the pure transformers in
``transformation/tables.py`` into PostgreSQL via the COPY protocol; nothing is
materialized in memory except the set of respondent case IDs (used to filter
the ATUS-CPS file to respondent households).
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg

from .. import __version__ as PIPELINE_VERSION
from ..config import Settings
from ..manifest import Manifest
from ..sources import SourceFile, get_source
from ..transformation.tables import TABLE_SPECS, TableSpec

log = logging.getLogger(__name__)

_PROGRESS_EVERY_ROWS = 500_000

# Truncation order is irrelevant to TRUNCATE itself (single statement), but the
# list must name every canonical table so a rebuild is complete.
_CANONICAL_TABLES = (
    "activity_companions",
    "activities",
    "cps_persons",
    "household_members",
    "replicate_weights",
    "pandemic_replicate_weights",
    "respondents",
    "activity_codes",
    "activity_tier2",
    "activity_tier1",
)


class LoadError(RuntimeError):
    pass


@dataclass
class TableLoadResult:
    table: str
    source_rows: int
    loaded_rows: int


def _lexicon_path(settings: Settings) -> Path:
    return settings.reference_dir / f"activity_lexicon_{settings.release}.csv"


def _load_lexicon(conn: psycopg.Connection, settings: Settings) -> dict[str, int]:
    """Load the committed activity lexicon CSV into the three tier tables."""
    path = _lexicon_path(settings)
    if not path.exists():
        raise LoadError(f"Activity lexicon reference file missing: {path}")
    by_level: dict[str, list[dict[str, str]]] = {"1": [], "2": [], "3": []}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            by_level[row["level"]].append(row)

    with conn.cursor() as cur:
        with cur.copy("COPY atus.activity_tier1 (code, name) FROM STDIN") as copy:
            for row in by_level["1"]:
                copy.write_row((row["code"], row["name"]))
        with cur.copy("COPY atus.activity_tier2 (code, tier1_code, name) FROM STDIN") as copy:
            for row in by_level["2"]:
                copy.write_row((row["code"], row["code"][:2], row["name"]))
        with cur.copy(
            "COPY atus.activity_codes (code, tier2_code, name, harmonization_note) FROM STDIN"
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
    with conn.cursor() as cur:
        with cur.copy(f"COPY atus.{spec.table} ({columns}) FROM STDIN") as copy:
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


def load_all(conn: psycopg.Connection, settings: Settings) -> list[TableLoadResult]:
    """Rebuild the canonical schema from staged data, atomically."""
    manifest = Manifest(settings.manifest_path)
    plan = _check_staged_inputs(settings, manifest)

    with conn.transaction():
        run_id = conn.execute(
            "INSERT INTO atus.ingestion_runs (release, pipeline_version) VALUES (%s, %s) "
            "RETURNING id",
            (settings.release, PIPELINE_VERSION),
        ).fetchone()[0]
    log.info("Ingestion run %d started (release %s)", run_id, settings.release)

    results: list[TableLoadResult] = []
    try:
        with conn.transaction():
            tables = ", ".join(f"atus.{t}" for t in _CANONICAL_TABLES)
            conn.execute(f"TRUNCATE {tables}")
            log.info("Canonical tables truncated (atomic rebuild)")
            _load_lexicon(conn, settings)
            respondent_ids: set[int] | None = None
            for spec, source, staged in plan:
                result, respondent_ids = _load_table(conn, spec, staged, respondent_ids)
                _record_source_file(conn, run_id, settings, source, manifest, result)
                results.append(result)
    except Exception as exc:
        with conn.transaction():
            conn.execute(
                "UPDATE atus.ingestion_runs SET status = 'failed', finished_at = now(), "
                "notes = %s WHERE id = %s",
                (f"{type(exc).__name__}: {exc}"[:2000], run_id),
            )
        log.error("Ingestion run %d failed; database unchanged. %s", run_id, exc)
        raise

    with conn.transaction():
        conn.execute(
            "UPDATE atus.ingestion_runs SET status = 'succeeded', finished_at = now() "
            "WHERE id = %s",
            (run_id,),
        )
    log.info("Ingestion run %d succeeded", run_id)
    return results
