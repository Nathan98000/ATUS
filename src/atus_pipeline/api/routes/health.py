"""System endpoints (unversioned): liveness, readiness and metrics.

* ``/health`` is the platform health check and the only monitor target. It
  touches nothing (no database, no edge authentication, no thread) and
  reports the build, which the deploy workflow polls for.
* ``/ready`` and ``/metrics`` are for operators only (audit F16). They need
  ``Authorization: Bearer <ATUS_OPS_TOKEN>``; without a valid token (or with
  no token configured) they answer exactly like a path that does not exist.
  Neither is in the OpenAPI schema, and the edge Worker never routes them.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ...config import Settings
from ..database import Database
from ..dependencies import get_database, get_settings, get_snapshots
from ..errors import DataNotLoadedError
from ..metrics import CONTENT_TYPE
from ..middleware import ops_authorized
from ..schemas.system import HealthResponse
from ..snapshot import SnapshotStore

log = logging.getLogger("atus.api")

router = APIRouter(tags=["system"])

_NO_STORE = {"Cache-Control": "no-store"}


def _require_ops_token(request: Request, settings: Settings) -> None:
    if not ops_authorized(request.headers, settings):
        raise StarletteHTTPException(status_code=404)  # the unknown-route envelope


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness",
    description=(
        "Answers whether the API process is running, and which build it is "
        "(`build`, from ATUS_BUILD_SHA). No dependencies touched (no database, "
        "no edge authentication; HEAD works too). Async on purpose: it "
        "bypasses the worker threadpool, so liveness stays instant even when "
        "every worker thread is busy with long analyses."
    ),
)
async def health(
    response: Response, settings: Settings = Depends(get_settings)
) -> HealthResponse:
    response.headers.update(_NO_STORE)
    return HealthResponse(status="ok", build=settings.build_sha)


@router.get("/ready", include_in_schema=False)
async def ready(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_database),
    snapshots: SnapshotStore = Depends(get_snapshots),
) -> dict:
    """Whether the API can answer analyses: the database is reachable (or
    was within the snapshot TTL), data is loaded, and it is the data version
    the deployment expects. Failures are the ordinary 503 envelopes; the
    remediation hints go to the log only."""
    _require_ops_token(request, settings)
    snapshot = await snapshots.get(db, check_expected=False)
    if not snapshot.is_loaded:
        log.warning("readiness: no succeeded ingestion run; load data with `atus load`")
        raise DataNotLoadedError("readiness: no succeeded ingestion run")
    snapshots.check_expected(snapshot)
    response.headers.update(_NO_STORE)
    age = snapshots.age_s()
    return {
        "status": "ready",
        "database": "ok",
        "data_loaded": True,
        "data_version": snapshot.data_version,
        "expected_data_version": snapshots.expected_version,
        "build": settings.build_sha,
        "snapshot_age_s": round(age, 3) if age is not None else None,
    }


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """Prometheus text format (``metrics.py``)."""
    _require_ops_token(request, settings)
    body = request.app.state.metrics.render(request.app.state)
    return PlainTextResponse(body, media_type=CONTENT_TYPE, headers=_NO_STORE)
