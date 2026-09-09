"""Field-level parsing/conversion helpers shared by all table transformers.

ATUS missing-value conventions (2003-25 Interview Data Dictionary, p. 7):

    -1  Blank (not asked / out of the question's universe)
    -2  Don't know
    -3  Refused

plus ``-4`` ("hours vary") on usual-hours variables.

Policy: for analytical columns, the negative sentinels are converted to SQL
NULL. The distinction between "blank", "don't know", and "refused" is not
carried into the canonical tables; it remains recoverable from the immutable
raw/staged files. The one semantically meaningful sentinel, ``-4`` (hours
vary), is preserved as an explicit boolean column instead of being folded into
NULL. Where a sentinel is structural rather than missing (the Who file's
``TULINENO = -1`` meaning "not a household roster member"), the transformer
keeps it and the schema documents it. See docs/data-lineage.md.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal


class TransformError(ValueError):
    """A source value could not be interpreted; raised to fail the load loudly."""


def parse_int(value: str, *, field: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise TransformError(f"{field}: expected integer, got {value!r}") from exc


def sentinel_int(value: str, *, field: str) -> int | None:
    """Integer with ATUS sentinels (-1/-2/-3, and -4) mapped to None."""
    number = parse_int(value, field=field)
    return None if number < 0 else number


def sentinel_bool_yes_no(value: str, *, field: str) -> bool | None:
    """ATUS yes/no coding: 1 = yes, 2 = no; sentinels map to None."""
    number = parse_int(value, field=field)
    if number == 1:
        return True
    if number == 2:
        return False
    if number < 0:
        return None
    raise TransformError(f"{field}: expected 1/2 or sentinel, got {value!r}")


def flag_bool_zero_one(value: str, *, field: str) -> bool:
    """0/1 flag with no missing values allowed (e.g. TRHOLIDAY, TRWHONA)."""
    number = parse_int(value, field=field)
    if number in (0, 1):
        return bool(number)
    raise TransformError(f"{field}: expected 0/1, got {value!r}")


def implied_decimal_2(value: str, *, field: str) -> Decimal | None:
    """ATUS earnings fields carry 2 implied decimals (e.g. 66000 -> 660.00).

    Values are usually integers, but allocated earnings can carry fractional
    cents (e.g. TRERNHLY 7211.525 -> 72.11525), so the raw value is parsed as
    a decimal and shifted exactly.
    """
    try:
        number = Decimal(value)
    except ArithmeticError as exc:
        raise TransformError(f"{field}: expected number, got {value!r}") from exc
    if number < 0:
        return None
    return number.scaleb(-2)


def weight(value: str, *, field: str) -> Decimal | None:
    """Statistical weight: non-negative decimal; -1 means not defined."""
    try:
        number = Decimal(value)
    except ArithmeticError as exc:
        raise TransformError(f"{field}: expected decimal weight, got {value!r}") from exc
    if number == -1:
        return None
    if number < 0:
        raise TransformError(f"{field}: unexpected negative weight {value!r}")
    return number


def yyyymmdd(value: str, *, field: str) -> date:
    """TUDIARYDATE style date, e.g. 20200126."""
    if len(value) != 8 or not value.isdigit():
        raise TransformError(f"{field}: expected YYYYMMDD, got {value!r}")
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError as exc:
        raise TransformError(f"{field}: invalid calendar date {value!r}") from exc


def hhmmss(value: str, *, field: str) -> time:
    """Episode start/stop times, e.g. '04:00:00'."""
    parts = value.split(":")
    if len(parts) != 3:
        raise TransformError(f"{field}: expected HH:MM:SS, got {value!r}")
    try:
        return time(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise TransformError(f"{field}: invalid time {value!r}") from exc


def activity_code(value: str, *, field: str, width: int) -> str:
    """Harmonized activity codes (TRCODEP/TRTIER1P/TRTIER2P) are zero-padded
    digit strings in the multi-year files; validate rather than reformat."""
    if len(value) != width or not value.isdigit():
        raise TransformError(f"{field}: expected {width}-digit code, got {value!r}")
    return value


def usual_hours(value: str, *, field: str) -> tuple[int | None, bool]:
    """Usual weekly hours: returns (hours, hours_vary).

    -4 means "hours vary" (a real answer, kept as a flag); -1/-2/-3 become
    (None, False); 0-999 becomes (n, False).
    """
    number = parse_int(value, field=field)
    if number == -4:
        return None, True
    if number < 0:
        return None, False
    return number, False
