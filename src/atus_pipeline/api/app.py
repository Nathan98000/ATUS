"""FastAPI application factory.

    from atus_pipeline.api.app import create_app
    app = create_app()                 # settings from the environment

or, for uvicorn:

    uvicorn --factory atus_pipeline.api.app:create_app

Lifecycle: startup opens a psycopg connection pool (non-blocking — a database
that is down at startup makes /ready report 503, it does not prevent the
process from starting) and an in-process analysis-result cache; shutdown
closes the pool. Startup performs no data validation and is effectively
instantaneous.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import ConnectionPool

from ..config import Settings, load_settings
from ..logging_setup import configure_logging
from . import API_VERSION
from .cache import InMemoryAnalysisCache
from .errors import register_exception_handlers
from .routes import activities, analysis, health, meta, population

log = logging.getLogger("atus.api")

_DESCRIPTION = """\
Read-only analytical API over the American Time Use Survey (ATUS) 2003-2025
multi-year microdata.

All estimates are **survey-weighted population statistics** computed by the
project's validated analytical engine (ATUS User's Guide ch. 7 estimators;
replicate-weight standard errors per ch. 7.5) — never unweighted sample
means. 2020 has special weighting rules the engine enforces explicitly.
Methodology: see docs/analytics.md and docs/methodology.md in the repository;
the engine reproduces 24 official BLS published values in its benchmark suite.

Responses embed the exact canonical analysis `spec`; resubmitting it (same
`analytics_version` and data release, see GET /api/v1/meta) reproduces the
result.
"""


def create_app(settings: Settings | None = None, *, open_pool: bool = True) -> FastAPI:
    """Build the application. ``open_pool=False`` skips connecting the pool —
    for tests that override the connection dependency entirely."""
    settings = settings or load_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            open=False,
            name="atus-api",
        )
        if open_pool:
            pool.open(wait=False)  # background-connects; readiness reports failures
        app.state.pool = pool
        app.state.cache = InMemoryAnalysisCache(maxsize=settings.api_cache_size)
        app.state.settings = settings
        yield
        pool.close()

    app = FastAPI(
        title="ATUS Analysis API",
        version=f"{API_VERSION} (contract)",
        description=_DESCRIPTION,
        lifespan=lifespan,
    )

    if settings.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.api_cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = uuid.uuid4().hex[:16]
        request.state.request_id = request_id  # read by the outermost 500 handler
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The generic exception handler already produced a sanitized 500
            # response; starlette re-raises so the server can log it. Tag the
            # log with our request id and let it propagate.
            duration_ms = (time.perf_counter() - start) * 1000
            log.error(
                "request_id=%s %s %s -> 500 in %.1fms (unhandled)",
                request_id, request.method, request.url.path, duration_ms,
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        log.info(
            "request_id=%s %s %s -> %d in %.1fms cache=%s",
            request_id, request.method, request.url.path,
            response.status_code, duration_ms,
            response.headers.get("X-Cache", "-"),
        )
        return response

    register_exception_handlers(app)

    prefix = f"/api/{API_VERSION}"
    app.include_router(analysis.router, prefix=prefix)
    app.include_router(activities.router, prefix=prefix)
    app.include_router(population.router, prefix=prefix)
    app.include_router(meta.router, prefix=prefix)
    app.include_router(health.router)

    return app
