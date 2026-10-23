"""Shared helpers for building synthetic ATUS source rows in tests.

Rows are dicts keyed by the official BLS column names, defaulting every field
to "-1" (the ATUS 'blank' sentinel) so tests only spell out the values they
care about — the same shape ``csv.DictReader`` produces from staged files.
"""

from __future__ import annotations

from atus_pipeline.sources import (
    ACTIVITY_COLUMNS,
    CPS_REQUIRED_COLUMNS,
    PANDEMIC_WEIGHT_COLUMNS,
    REPLICATE_WEIGHT_COLUMNS,
    RESPONDENT_COLUMNS,
    ROSTER_COLUMNS,
    WHO_COLUMNS,
)


def make_row(columns: tuple[str, ...], **overrides: str) -> dict[str, str]:
    row = dict.fromkeys(columns, "-1")
    unknown = set(overrides) - set(columns)
    if unknown:
        raise KeyError(f"Unknown columns for this file: {sorted(unknown)}")
    row.update(overrides)
    return row


def respondent_row(**overrides: str) -> dict[str, str]:
    defaults = {
        "TUCASEID": "20230101230001",
        "TULINENO": "1",
        "TUYEAR": "2023",
        "TUDIARYDATE": "20230115",
        "TUDIARYDAY": "1",
        "TRHOLIDAY": "0",
        "TELFS": "1",
        "TUFNWGTP": "5000000.123456",
        "TU20FWGT": "-1.000000",
    }
    defaults.update(overrides)
    return make_row(RESPONDENT_COLUMNS, **defaults)


def roster_row(**overrides: str) -> dict[str, str]:
    defaults = {
        "TUCASEID": "20230101230001",
        "TULINENO": "1",
        "TERRP": "18",
        "TEAGE": "40",
        "TESEX": "2",
    }
    defaults.update(overrides)
    return make_row(ROSTER_COLUMNS, **defaults)


def activity_row(**overrides: str) -> dict[str, str]:
    defaults = {
        "TUCASEID": "20230101230001",
        "TUACTIVITY_N": "1",
        "TRCODEP": "010101",
        "TRTIER1P": "01",
        "TRTIER2P": "0101",
        "TUSTARTTIM": "04:00:00",
        "TUSTOPTIME": "12:00:00",
        "TUACTDUR24": "480",
        "TUACTDUR": "480",
        "TUCUMDUR24": "480",
        "TEWHERE": "-1",
    }
    defaults.update(overrides)
    return make_row(ACTIVITY_COLUMNS, **defaults)


def who_row(**overrides: str) -> dict[str, str]:
    defaults = {
        "TUCASEID": "20230101230001",
        "TULINENO": "-1",
        "TUACTIVITY_N": "1",
        "TRWHONA": "1",
        "TUWHO_CODE": "-1",
    }
    defaults.update(overrides)
    return make_row(WHO_COLUMNS, **defaults)


def cps_row(**overrides: str) -> dict[str, str]:
    defaults = {
        "TUCASEID": "20230101230001",
        "TULINENO": "1",
        "HRYEAR4": "2022",
        "HRMONTH": "11",
        "GESTFIPS": "6",
        "PRTAGE": "40",
        "PESEX": "2",
    }
    defaults.update(overrides)
    return make_row(CPS_REQUIRED_COLUMNS, **defaults)


def replicate_weights_row(**overrides: str) -> dict[str, str]:
    defaults = {"TUCASEID": "20230101230001"}
    defaults.update({f"TUFNWGTP{i:03d}": f"{1000000 + i}.000001" for i in range(1, 161)})
    defaults.update(overrides)
    return make_row(REPLICATE_WEIGHT_COLUMNS, **defaults)


def pandemic_weights_row(**overrides: str) -> dict[str, str]:
    defaults = {"TUCASEID": "20190101190001"}
    defaults.update({f"TU20FWGT{i:03d}": f"{2000000 + i}.000002" for i in range(1, 161)})
    defaults.update(overrides)
    return make_row(PANDEMIC_WEIGHT_COLUMNS, **defaults)
