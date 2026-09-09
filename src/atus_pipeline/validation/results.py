"""Shared result type for validation checks."""

from __future__ import annotations

from dataclasses import dataclass


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
