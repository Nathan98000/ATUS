"""Row transformers: staged BLS CSV rows -> canonical table rows.

Each canonical table has a ``TableSpec`` pairing the target column order with a
pure function ``dict[str, str] -> tuple`` that converts one source CSV row.
The functions are deliberately explicit (one line per column) so that the
source-variable -> canonical-column mapping is readable here and auditable
against docs/data-lineage.md.

Naming policy: canonical columns get descriptive snake_case names. Exceptions
that keep their BLS names (lowercased):

* replicate weight columns (``tufnwgtp001``..., ``tu20fwgt001``...), which are
  positional by construction; and
* era-specific CPS pairs (``gemetsta``/``gtmetsta``, ``hufaminc``/``hefaminc``)
  whose category definitions changed mid-survey — keeping the BLS names signals
  "consult the ATUS-CPS data dictionary before comparing across years".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .common import (
    TransformError,
    activity_code,
    flag_bool_zero_one,
    hhmmss,
    implied_decimal_2,
    parse_int,
    sentinel_bool_yes_no,
    sentinel_int,
    usual_hours,
    weight,
    yyyymmdd,
)


@dataclass(frozen=True)
class TableSpec:
    table: str                              # canonical table name (in schema "atus")
    dataset_key: str                        # sources.py key of the staged file
    columns: tuple[str, ...]                # target columns, in transformer output order
    transform: Callable[[dict[str, str]], tuple]


# --------------------------------------------------------------------------- #
# respondents (source: Respondent file)                                       #
# --------------------------------------------------------------------------- #

RESPONDENT_TARGET_COLUMNS: tuple[str, ...] = (
    "tucaseid", "data_year", "diary_date", "diary_day_of_week", "is_holiday",
    "labor_force_status", "has_multiple_jobs", "full_or_part_time",
    "usual_weekly_hours", "usual_hours_vary", "class_of_worker",
    "major_industry", "major_occupation", "weekly_earnings", "hourly_earnings",
    "school_enrolled", "school_level", "spouse_or_partner_present",
    "spouse_or_partner_employed", "household_size", "household_children",
    "youngest_child_age", "provided_eldercare_on_diary_day", "eldercare_minutes",
    "secondary_childcare_minutes", "time_alone_minutes", "time_with_family_minutes",
    "final_weight", "pandemic_weight",
)


def _eldercare_yesterday(value: str) -> bool | None:
    # The 2003-25 respondent file contains exactly one undocumented TUECYTD=0
    # (a single 2024 case; the data dictionary lists only 1/2 and sentinels).
    # Treated as missing — see docs/data-lineage.md.
    if value == "0":
        return None
    return sentinel_bool_yes_no(value, field="TUECYTD")


def transform_respondent(row: dict[str, str]) -> tuple:
    hours, hours_vary = usual_hours(row["TEHRUSLT"], field="TEHRUSLT")
    return (
        parse_int(row["TUCASEID"], field="TUCASEID"),
        parse_int(row["TUYEAR"], field="TUYEAR"),
        yyyymmdd(row["TUDIARYDATE"], field="TUDIARYDATE"),
        parse_int(row["TUDIARYDAY"], field="TUDIARYDAY"),
        flag_bool_zero_one(row["TRHOLIDAY"], field="TRHOLIDAY"),
        parse_int(row["TELFS"], field="TELFS"),
        sentinel_bool_yes_no(row["TEMJOT"], field="TEMJOT"),
        sentinel_int(row["TRDPFTPT"], field="TRDPFTPT"),
        hours,
        hours_vary,
        sentinel_int(row["TEIO1COW"], field="TEIO1COW"),
        sentinel_int(row["TRMJIND1"], field="TRMJIND1"),
        sentinel_int(row["TRMJOCC1"], field="TRMJOCC1"),
        implied_decimal_2(row["TRERNWA"], field="TRERNWA"),
        implied_decimal_2(row["TRERNHLY"], field="TRERNHLY"),
        sentinel_bool_yes_no(row["TESCHENR"], field="TESCHENR"),
        sentinel_int(row["TESCHLVL"], field="TESCHLVL"),
        sentinel_int(row["TRSPPRES"], field="TRSPPRES"),
        sentinel_bool_yes_no(row["TESPEMPNOT"], field="TESPEMPNOT"),
        sentinel_int(row["TRNUMHOU"], field="TRNUMHOU"),
        sentinel_int(row["TRCHILDNUM"], field="TRCHILDNUM"),
        sentinel_int(row["TRYHHCHILD"], field="TRYHHCHILD"),
        _eldercare_yesterday(row["TUECYTD"]),
        sentinel_int(row["TRTEC"], field="TRTEC"),
        sentinel_int(row["TRTCCTOT"], field="TRTCCTOT"),
        sentinel_int(row["TRTALONE"], field="TRTALONE"),
        sentinel_int(row["TRTFAMILY"], field="TRTFAMILY"),
        weight(row["TUFNWGTP"], field="TUFNWGTP"),
        weight(row["TU20FWGT"], field="TU20FWGT"),
    )


# --------------------------------------------------------------------------- #
# household_members (source: Roster file)                                     #
# --------------------------------------------------------------------------- #

HOUSEHOLD_MEMBER_TARGET_COLUMNS: tuple[str, ...] = (
    "tucaseid", "lineno", "relationship", "age", "sex",
)


def transform_household_member(row: dict[str, str]) -> tuple:
    return (
        parse_int(row["TUCASEID"], field="TUCASEID"),
        parse_int(row["TULINENO"], field="TULINENO"),
        sentinel_int(row["TERRP"], field="TERRP"),
        sentinel_int(row["TEAGE"], field="TEAGE"),
        parse_int(row["TESEX"], field="TESEX"),
    )


# --------------------------------------------------------------------------- #
# activities (source: Activity file)                                          #
# --------------------------------------------------------------------------- #

ACTIVITY_TARGET_COLUMNS: tuple[str, ...] = (
    "tucaseid", "activity_number", "activity_code", "tier1_code", "tier2_code",
    "start_time", "stop_time", "duration_minutes", "duration_uncapped_minutes",
    "cumulative_minutes", "location_code", "secondary_childcare_minutes",
    "eldercare_minutes",
)


def transform_activity(row: dict[str, str]) -> tuple:
    return (
        parse_int(row["TUCASEID"], field="TUCASEID"),
        parse_int(row["TUACTIVITY_N"], field="TUACTIVITY_N"),
        activity_code(row["TRCODEP"], field="TRCODEP", width=6),
        activity_code(row["TRTIER1P"], field="TRTIER1P", width=2),
        activity_code(row["TRTIER2P"], field="TRTIER2P", width=4),
        hhmmss(row["TUSTARTTIM"], field="TUSTARTTIM"),
        hhmmss(row["TUSTOPTIME"], field="TUSTOPTIME"),
        parse_int(row["TUACTDUR24"], field="TUACTDUR24"),
        parse_int(row["TUACTDUR"], field="TUACTDUR"),
        parse_int(row["TUCUMDUR24"], field="TUCUMDUR24"),
        sentinel_int(row["TEWHERE"], field="TEWHERE"),
        sentinel_int(row["TRTCCTOT_LN"], field="TRTCCTOT_LN"),
        sentinel_int(row["TRTEC_LN"], field="TRTEC_LN"),
    )


# --------------------------------------------------------------------------- #
# activity_companions (source: Who file)                                      #
# --------------------------------------------------------------------------- #
# TULINENO and TUWHO_CODE keep the source value -1: for TULINENO it is
# structural ("not a household roster member" — nonhousehold companions and
# not-asked placeholders), and both participate in the primary key.

COMPANION_TARGET_COLUMNS: tuple[str, ...] = (
    "tucaseid", "activity_number", "who_lineno", "who_code", "who_not_asked",
)


def transform_companion(row: dict[str, str]) -> tuple:
    return (
        parse_int(row["TUCASEID"], field="TUCASEID"),
        parse_int(row["TUACTIVITY_N"], field="TUACTIVITY_N"),
        parse_int(row["TULINENO"], field="TULINENO"),
        parse_int(row["TUWHO_CODE"], field="TUWHO_CODE"),
        flag_bool_zero_one(row["TRWHONA"], field="TRWHONA"),
    )


# --------------------------------------------------------------------------- #
# cps_persons (source: ATUS-CPS file, curated column subset)                  #
# --------------------------------------------------------------------------- #

CPS_PERSON_TARGET_COLUMNS: tuple[str, ...] = (
    "tucaseid", "lineno", "cps_year", "cps_month", "region", "division",
    "state_fips", "gemetsta", "gtmetsta", "household_size", "household_type",
    "tenure", "hufaminc", "hefaminc", "relationship", "age", "sex",
    "education", "race", "is_hispanic", "marital_status", "citizenship",
    "cps_labor_force_status",
)


def _state_fips(value: str, *, field: str) -> str | None:
    number = parse_int(value, field=field)
    if number < 0:
        return None
    if not 1 <= number <= 78:  # 50 states, DC, and outlying-area FIPS range
        raise TransformError(f"{field}: implausible state FIPS {value!r}")
    return f"{number:02d}"


def transform_cps_person(row: dict[str, str]) -> tuple:
    return (
        parse_int(row["TUCASEID"], field="TUCASEID"),
        parse_int(row["TULINENO"], field="TULINENO"),
        sentinel_int(row["HRYEAR4"], field="HRYEAR4"),
        sentinel_int(row["HRMONTH"], field="HRMONTH"),
        sentinel_int(row["GEREG"], field="GEREG"),
        sentinel_int(row["GEDIV"], field="GEDIV"),
        _state_fips(row["GESTFIPS"], field="GESTFIPS"),
        sentinel_int(row["GEMETSTA"], field="GEMETSTA"),
        sentinel_int(row["GTMETSTA"], field="GTMETSTA"),
        sentinel_int(row["HRNUMHOU"], field="HRNUMHOU"),
        sentinel_int(row["HRHTYPE"], field="HRHTYPE"),
        sentinel_int(row["HETENURE"], field="HETENURE"),
        sentinel_int(row["HUFAMINC"], field="HUFAMINC"),
        sentinel_int(row["HEFAMINC"], field="HEFAMINC"),
        sentinel_int(row["PERRP"], field="PERRP"),
        sentinel_int(row["PRTAGE"], field="PRTAGE"),
        sentinel_int(row["PESEX"], field="PESEX"),
        sentinel_int(row["PEEDUCA"], field="PEEDUCA"),
        sentinel_int(row["PTDTRACE"], field="PTDTRACE"),
        sentinel_bool_yes_no(row["PEHSPNON"], field="PEHSPNON"),
        sentinel_int(row["PEMARITL"], field="PEMARITL"),
        sentinel_int(row["PRCITSHP"], field="PRCITSHP"),
        sentinel_int(row["PEMLR"], field="PEMLR"),
    )


# --------------------------------------------------------------------------- #
# replicate weights (sources: Replicate weights / Pandemic replicate weights) #
# --------------------------------------------------------------------------- #

REPLICATE_WEIGHT_TARGET_COLUMNS: tuple[str, ...] = (
    "tucaseid", *[f"tufnwgtp{i:03d}" for i in range(1, 161)],
)

PANDEMIC_WEIGHT_TARGET_COLUMNS: tuple[str, ...] = (
    "tucaseid", *[f"tu20fwgt{i:03d}" for i in range(1, 161)],
)


def _transform_weight_row(row: dict[str, str], prefix: str) -> tuple:
    values: list[object] = [parse_int(row["TUCASEID"], field="TUCASEID")]
    for i in range(1, 161):
        name = f"{prefix}{i:03d}"
        values.append(weight(row[name], field=name))
    return tuple(values)


def transform_replicate_weights(row: dict[str, str]) -> tuple:
    return _transform_weight_row(row, "TUFNWGTP")


def transform_pandemic_weights(row: dict[str, str]) -> tuple:
    return _transform_weight_row(row, "TU20FWGT")


# --------------------------------------------------------------------------- #
# Registry (in load order: parents before children)                           #
# --------------------------------------------------------------------------- #

TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec("respondents", "respondent", RESPONDENT_TARGET_COLUMNS, transform_respondent),
    TableSpec(
        "household_members", "roster", HOUSEHOLD_MEMBER_TARGET_COLUMNS,
        transform_household_member,
    ),
    TableSpec("activities", "activity", ACTIVITY_TARGET_COLUMNS, transform_activity),
    TableSpec("activity_companions", "who", COMPANION_TARGET_COLUMNS, transform_companion),
    TableSpec("cps_persons", "cps", CPS_PERSON_TARGET_COLUMNS, transform_cps_person),
    TableSpec(
        "replicate_weights", "replicate_weights", REPLICATE_WEIGHT_TARGET_COLUMNS,
        transform_replicate_weights,
    ),
    TableSpec(
        "pandemic_replicate_weights", "pandemic_weights", PANDEMIC_WEIGHT_TARGET_COLUMNS,
        transform_pandemic_weights,
    ),
)
