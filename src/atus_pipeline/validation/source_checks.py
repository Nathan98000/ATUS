"""File-level validation of staged source data, before anything touches the DB.

For every staged file this verifies:

* the file exists and parses as CSV;
* the header exactly matches the layout the transformers were written against
  (or, for very wide files, the column count plus required columns);
* the data row count equals the official record count BLS publishes in the
  ``*_info.txt`` inside each archive;
* for the Respondent file, that every survey year of the release is present.

A failure here means either a corrupted download or a BLS layout change, and
in both cases loading must not proceed.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from ..sources import SourceFile
from .results import CheckResult

log = logging.getLogger(__name__)


def expected_years(release: str) -> set[int]:
    """Survey years covered by a multi-year release code like '0325'."""
    start, end = 2000 + int(release[:2]), 2000 + int(release[2:])
    return set(range(start, end + 1))


def _read_header(path: Path) -> list[str]:
    with path.open(newline="") as fh:
        return next(csv.reader(fh))


def _check_header(source: SourceFile, header: list[str]) -> list[CheckResult]:
    results = []
    if source.expected_columns is not None:
        matches = tuple(header) == source.expected_columns
        results.append(
            CheckResult(
                name=f"{source.key}: header layout",
                passed=matches,
                severity="error",
                observed=f"{len(header)} columns",
                detail="exact match against registered layout"
                if matches else "header differs from registered layout — inspect before loading",
            )
        )
    if source.expected_column_count is not None:
        results.append(
            CheckResult(
                name=f"{source.key}: column count",
                passed=len(header) == source.expected_column_count,
                severity="error",
                observed=str(len(header)),
                detail=f"expected {source.expected_column_count}",
            )
        )
    if source.required_columns:
        missing = [c for c in source.required_columns if c not in header]
        results.append(
            CheckResult(
                name=f"{source.key}: required columns present",
                passed=not missing,
                severity="error",
                observed="all present" if not missing else f"missing: {missing}",
                detail=f"{len(source.required_columns)} columns needed by the transformer",
            )
        )
    return results


def _check_row_count_and_years(
    source: SourceFile, path: Path, release: str
) -> list[CheckResult]:
    results = []
    rows = 0
    years: set[int] = set()
    collect_years = source.key == "respondent"
    if collect_years:
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                rows += 1
                years.add(int(row["TUYEAR"]))
    else:
        # plain line count is enough (and much faster on multi-GB files)
        with path.open("rb") as fh:
            first = True
            for line in fh:
                if first:
                    first = False
                    continue
                if line.strip():
                    rows += 1
    results.append(
        CheckResult(
            name=f"{source.key}: row count matches official BLS count",
            passed=rows == source.expected_rows,
            severity="error",
            observed=f"{rows:,}",
            detail=f"BLS {source.zip_name} info file documents {source.expected_rows:,} records",
        )
    )
    if collect_years:
        expected = expected_years(release)
        results.append(
            CheckResult(
                name=f"{source.key}: all survey years present",
                passed=years == expected,
                severity="error",
                observed=f"{min(years)}-{max(years)} ({len(years)} years)" if years else "none",
                detail=f"expected {min(expected)}-{max(expected)}",
            )
        )
    return results


def validate_source(source: SourceFile, staged_path: Path, release: str) -> list[CheckResult]:
    if not staged_path.exists():
        return [
            CheckResult(
                name=f"{source.key}: staged file exists",
                passed=False,
                severity="error",
                observed=f"{staged_path} missing",
                detail="run `atus extract` first",
            )
        ]
    header = _read_header(staged_path)
    results = _check_header(source, header)
    if all(r.passed for r in results):
        results.extend(_check_row_count_and_years(source, staged_path, release))
    else:
        log.warning("%s: skipping row-count check because the header is wrong", source.key)
    return results


def validate_all_sources(
    sources: tuple[SourceFile, ...], staging_dir: Path, release: str
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for source in sources:
        log.info("Validating staged %s (%s)", source.key, source.dat_name)
        results.extend(validate_source(source, staging_dir / source.dat_name, release))
    return results
