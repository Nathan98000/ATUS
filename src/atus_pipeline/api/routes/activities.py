"""Read-only activity-lexicon endpoints (the harmonized multi-year coding tree).

Answered from the lexicon in the in-process data snapshot, which is read from
the Phase 1 lexicon tables (``atus.activity_tier1/2``, ``atus.activity_codes``)
with the same SQL these endpoints used to run per request; a fresh snapshot
answers with no database statement (audit F14). The one exception is a
search term the snapshot cannot match exactly as PostgreSQL's ``ILIKE``
would (a non-ASCII term; see ``snapshot.py``), which runs the original
query. The lexicon has a few hundred entries, so listings are bounded and
unpaginated. Responses are version-stable between data loads and carry a
modest public Cache-Control.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from starlette.concurrency import run_in_threadpool

from ...analytics.spec import ACTIVITY_PRESETS
from ..database import Database
from ..dependencies import get_database, get_snapshots
from ..errors import ActivityNotFoundError
from ..schemas.activities import (
    ActivityDetailResponse,
    ActivityEntry,
    ActivityListResponse,
    ActivityPreset,
    ActivityPresetsResponse,
)
from ..schemas.errors import DATA_ROUTE_503, ErrorResponse
from ..snapshot import LEXICON_SQL, LexiconEntry, SnapshotStore

router = APIRouter(prefix="/activities", tags=["activities"])

_CACHE_HEADER = "public, max-age=300"


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def lexicon_query(
    level: int | None, parent: str | None, search: str | None
) -> tuple[str, dict]:
    """The listing's SQL (the search fallback, and the reference the
    snapshot's Python filter is tested against)."""
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
    sql = LEXICON_SQL
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY code, level"
    return sql, params


@router.get(
    "",
    response_model=ActivityListResponse,
    responses={503: DATA_ROUTE_503},
    summary="List/search the activity lexicon",
    description=(
        "Flat listing of the harmonized multi-year activity coding lexicon of "
        "the loaded release (major categories, subcategories and leaf "
        "activities; see GET /api/v1/meta for the release), filterable by "
        "level, direct parent, and name search. In analysis requests, a 2- or "
        "4-digit code selects itself plus all descendants."
    ),
)
async def list_activities(
    response: Response,
    level: int | None = Query(default=None, ge=1, le=3),
    parent: str | None = Query(
        default=None, pattern=r"^[0-9]{2}([0-9]{2})?$",
        description="Return direct children of this 2- or 4-digit code.",
    ),
    search: str | None = Query(default=None, min_length=2, max_length=80),
    db: Database = Depends(get_database),
    snapshots: SnapshotStore = Depends(get_snapshots),
) -> ActivityListResponse:
    snapshot = await snapshots.get(db)
    entries = snapshot.search_lexicon(level=level, parent=parent, search=search)
    if entries is None:
        rows = await run_in_threadpool(_fetch_all, db, *lexicon_query(level, parent, search))
    else:
        rows = [entry.row() for entry in entries]
    activities = [
        ActivityEntry(
            code=code, name=name, level=lvl, parent_code=parent_code,
            harmonization_note=note,
        )
        for code, name, lvl, parent_code, note in rows
    ]
    response.headers["Cache-Control"] = _CACHE_HEADER
    response.headers["X-Data-Version"] = snapshot.data_version
    return ActivityListResponse(activities=activities, total=len(activities))


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
async def list_presets(response: Response) -> ActivityPresetsResponse:
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
    responses={
        404: {"model": ErrorResponse, "description": "Unknown activity code"},
        503: DATA_ROUTE_503,
    },
    summary="One lexicon code with its ancestry and children",
)
async def activity_detail(
    code: str,
    response: Response,
    db: Database = Depends(get_database),
    snapshots: SnapshotStore = Depends(get_snapshots),
) -> ActivityDetailResponse:
    if not (code.isdigit() and len(code) in (2, 4, 6)):
        # Malformed codes never need the data; name the lexicon only when
        # the snapshot already knows it.
        current = snapshots.current
        raise ActivityNotFoundError(code, current.lexicon_label if current else None)
    snapshot = await snapshots.get(db)
    detail = snapshot.detail(code)
    if detail is None:
        raise ActivityNotFoundError(code, snapshot.lexicon_label)
    response.headers["Cache-Control"] = _CACHE_HEADER
    response.headers["X-Data-Version"] = snapshot.data_version
    return ActivityDetailResponse(
        code=code, name=detail.entry.name, level=detail.entry.level,
        parents=[_entry(p) for p in detail.parents],
        children=[_entry(c) for c in detail.children],
        harmonization_note=detail.entry.harmonization_note,
        descendant_leaf_count=detail.descendant_leaf_count,
    )


def _fetch_all(db: Database, sql: str, params: dict) -> list[tuple]:
    with db.transaction("metadata") as conn:
        return conn.execute(sql, params).fetchall()


def _entry(entry: LexiconEntry) -> ActivityEntry:
    return ActivityEntry(
        code=entry.code, name=entry.name, level=entry.level,
        parent_code=entry.parent_code, harmonization_note=entry.harmonization_note,
    )
