"""Shared result type for validation checks, and its persistence."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    severity: str          # "error" | "warning"
    observed: str          # what was actually measured
    detail: str = ""       # explanation / expectation


def summarize(results: list[CheckResult]) -> tuple[int, int, int]:
    """Return (passed, failed_errors, failed_warnings)."""
    passed = sum(1 for r in results if r.passed)
    failed_errors = sum(1 for r in results if not r.passed and r.severity == "error")
    failed_warnings = sum(1 for r in results if not r.passed and r.severity == "warning")
    return passed, failed_errors, failed_warnings


def persist_results(conn: psycopg.Connection, results: list[CheckResult]) -> None:
    """Record check outcomes in ``atus.validation_results`` (one transaction,
    or a savepoint inside the caller's). Needs a role that can write there:
    the owner/loader role, never the read-only API role."""
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
