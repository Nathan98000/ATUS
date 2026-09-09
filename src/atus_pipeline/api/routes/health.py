"""Liveness and readiness endpoints (unversioned; for process supervisors)."""

from __future__ import annotations

import logging

import psycopg
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..dependencies import get_connection
from ..schemas.errors import ErrorResponse

log = logging.getLogger("atus.api")

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    summary="Liveness",
    description=(
        "Answers whether the API process is running. No dependencies touched. "
        "Async on purpose: it bypasses the worker threadpool, so liveness stays "
        "instant even when every worker thread is busy with long analyses."
    ),
)
async def health() -> dict:
    return {"status": "ok"}


@router.get(
    "/ready",
    response_model=None,  # returns either a plain dict or a 503 JSONResponse
    summary="Readiness",
    description=(
        "Verifies the dependencies needed to answer analysis requests: the "
        "database is reachable and ATUS data is loaded. The (optional) result "
        "cache is not a readiness dependency — the service degrades gracefully "
        "without it. Readiness assumes the database passed `atus validate-db` "
        "at load time; it does not re-run data-quality checks."
    ),
    responses={503: {"model": ErrorResponse, "description": "Not ready"}},
)
def ready(conn: psycopg.Connection = Depends(get_connection)) -> dict | JSONResponse:
    try:
        conn.execute("SELECT 1").fetchone()
        loaded = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM atus.respondents)"
        ).fetchone()[0]
    except psycopg.errors.UndefinedTable:
        return _not_ready("schema_not_initialized", "Run `atus migrate` to create the schema.")
    if not loaded:
        return _not_ready("data_not_loaded", "Run the Phase 1 pipeline (`atus load`) first.")
    return {"status": "ready", "database": "ok", "data_loaded": True}


def _not_ready(code: str, message: str) -> JSONResponse:
    log.warning("readiness failed: %s", code)
    return JSONResponse(
        status_code=503,
        content={"error": {"code": code, "message": message, "details": None}},
    )
