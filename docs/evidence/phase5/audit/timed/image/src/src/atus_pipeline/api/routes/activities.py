"""Read-only activity-lexicon endpoints (the harmonized 2003-25 coding tree).

Backed directly by the Phase 1 lexicon tables (``atus.activity_tier1/2``,
``atus.activity_codes``). The whole lexicon is 556 entries, so listings are
bounded and unpaginated. Responses are version-stable between data loads and
carry a modest public Cache-Control.
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, Query, Response

from ...analytics.spec import ACTIVITY_PRESETS
from ..dependencies import get_connection
from ..errors import ActivityNotFoundError
from ..schemas.activities import (
    ActivityDetailResponse,
    ActivityEntry,
    ActivityListResponse,
    ActivityPreset,
    ActivityPresetsResponse,
)
from ..schemas.errors import ErrorResponse

router = APIRouter(prefix="/activities", tags=["activities"])

_CACHE_HEADER = "public, max-age=300"

# One flat view over the three lexicon tables. level: 1 = major category (2
# digits), 2 = subcategory (4), 3 = leaf activity (6).
_LEXICON_SQL = """
SELECT code, name, level, parent_code, harmonization_note FROM (
    SELECT code, name, 1 AS level, NULL AS parent_code,
           NULL AS harmonization_note
    FROM atus.activity_tier1
    UNION ALL
    SELECT code, name, 2, left(code, 2), NULL FROM atus.activity_tier2
    UNION ALL
    SELECT code, name, 3, tier2_code, harmonization_note FROM atus.activity_codes
) lexicon
"""


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get(
    "",
    response_model=ActivityListResponse,
    summary="List/search the activity lexicon",
    description=(
        "Flat listing of the harmonized 2003-25 activity coding lexicon "
        "(18 major categories, 107 subcategories, 431 leaf activities), "
        "filterable by level, direct parent, and name search. In analysis "
        "requests, a 2- or 4-digit code selects itself plus all descendants."
    ),
)
def list_activities(
    response: Response,
    level: int | None = Query(default=None, ge=1, le=3),
    parent: str | None = Query(
        default=None, pattern=r"^[0-9]{2}([0-9]{2})?$",
        description="Return direct children of this 2- or 4-digit code.",
    ),
    search: str | None = Query(default=None, min_length=2, max_length=80),
    conn: psycopg.Connection = Depends(get_connection),
) -> ActivityListResponse:
    conditions, params = [], {}
    if level is not None:
        conditions.append("level = %(level)s")
        params["level"] = level
    if parent is not None:
        conditions.append("parent_code = %(parent)s")
        params["parent"] = parent
    if search is not None:
        conditions.append(r"name ILIKE '%%' || %(search)s || '%%' ESCAPE '\'")
        params["search"] = _escape_like(search)
    sql = _LEXICON_SQL
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY code, level"
    rows = conn.execute(sql, params).fetchall()
    entries = [
        ActivityEntry(
            code=code, name=name, level=lvl, parent_code=parent_code,
            harmonization_note=note,
        )
        for code, name, lvl, parent_code, note in rows
    ]
    response.headers["Cache-Control"] = _CACHE_HEADER
    return ActivityListResponse(activities=entries, total=len(entries))


@router.get(
    "/presets",
    response_model=ActivityPresetsResponse,
    summary="Named activity selections",
    description=(
        "Curated selections usable as {'preset': name} in analysis requests, "
        "including the BLS news-release table category definitions "
        "(verified against published Table A-1 values)."
    ),
)
def list_presets(response: Response) -> ActivityPresetsResponse:
    presets = [
        ActivityPreset(
            name=name, label=selector.label or name,
            include=list(selector.include), exclude=list(selector.exclude),
        )
        for name, selector in sorted(ACTIVITY_PRESETS.items())
    ]
    response.headers["Cache-Control"] = _CACHE_HEADER
    return ActivityPresetsResponse(presets=presets)


@router.get(
    "/{code}",
    response_model=ActivityDetailResponse,
    responses={404: {"model": ErrorResponse, "description": "Unknown activity code"}},
    summary="One lexicon code with its ancestry and children",
)
def activity_detail(
    code: str,
    response: Response,
    conn: psycopg.Connection = Depends(get_connection),
) -> ActivityDetailResponse:
    if not (code.isdigit() and len(code) in (2, 4, 6)):
        raise ActivityNotFoundError(code)

    def fetch(one_code: str) -> tuple | None:
        return conn.execute(
            _LEXICON_SQL + " WHERE code = %(code)s", {"code": one_code}
        ).fetchone()

    row = fetch(code)
    if row is None:
        raise ActivityNotFoundError(code)
    _, name, level, _, note = row

    parents = []
    for prefix_len in (2, 4):
        if prefix_len < len(code):
            parent_row = fetch(code[:prefix_len])
            if parent_row:
                parents.append(_entry(parent_row))

    child_rows = conn.execute(
        _LEXICON_SQL + " WHERE parent_code = %(code)s ORDER BY code", {"code": code}
    ).fetchall()

    leaf_count = conn.execute(
        "SELECT count(*) FROM atus.activity_codes WHERE code LIKE %(prefix)s",
        {"prefix": code + "%"},
    ).fetchone()[0]

    response.headers["Cache-Control"] = _CACHE_HEADER
    return ActivityDetailResponse(
        code=code, name=name, level=level, parents=parents,
        children=[_entry(r) for r in child_rows],
        harmonization_note=note, descendant_leaf_count=leaf_count,
    )


def _entry(row: tuple) -> ActivityEntry:
    code, name, level, parent_code, note = row
    return ActivityEntry(
        code=code, name=name, level=level, parent_code=parent_code,
        harmonization_note=note,
    )
