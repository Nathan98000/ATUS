"""FastAPI application factory.

    from atus_pipeline.api.app import create_app
    app = create_app()                 # settings from the environment

The production process is started by ``atus api`` (see
``atus_pipeline.api.serve``), which runs exactly this factory under one
uvicorn process.

Lifecycle: startup builds the database pool without connecting (``min_size``
0: an API cold start never wakes the database), the analysis-result cache, and
the per-process protection state (rate buckets, daily cap, admission lanes),
and sizes the worker-thread limiter. Shutdown cancels queries still running,
waits briefly for detached computations, and closes the pool. Startup
performs no data validation and is effectively instantaneous; a production
configuration without edge secrets is refused before anything starts.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from psycopg.conninfo import conninfo_to_dict

from ..config import Settings, check_api_settings, load_settings
from ..logging_setup import configure_logging, set_redacted_secrets
from . import API_VERSION
from .analysis_runner import AnalysisRunner
from .cache import InMemoryAnalysisCache
from .database import Database
from .errors import register_exception_handlers
from .middleware import GuardMiddleware, RequestContextMiddleware, security_headers
from .protection import Protection
from .routes import activities, analysis, health, meta, population

log = logging.getLogger("atus.api")

GZIP_MINIMUM_BYTES = 1024

# Response headers a cross-origin browser client may read (audit F45).
CORS_EXPOSE_HEADERS = (
    "X-Request-ID", "X-Analysis-Key", "X-Cache", "X-Data-Version",
    "X-Analytics-Version", "X-Response-Format", "Retry-After",
)

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


def _secrets_to_redact(settings: Settings) -> list[str | None]:
    secrets: list[str | None] = [settings.ops_token, *settings.edge_secrets]
    try:
        secrets.append(conninfo_to_dict(settings.database_url).get("password"))
    except Exception:  # validated at load time; nothing to add if unparsable
        pass
    if "://" in settings.database_url and "@" in settings.database_url:
        # the raw (still percent-encoded) form, as it appears in the URL
        userinfo = settings.database_url.split("://", 1)[1].rsplit("@", 1)[0]
        if ":" in userinfo:
            secrets.append(userinfo.split(":", 1)[1])
    return secrets


def create_app(settings: Settings | None = None, *, open_pool: bool = True) -> FastAPI:
    """Build the application. ``open_pool=False`` never starts the pool — for
    tests that override the database dependency entirely."""
    settings = settings or load_settings()
    check_api_settings(settings)
    configure_logging(settings.log_level)
    set_redacted_secrets(_secrets_to_redact(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Every blocking DB call shares this limiter (audit F02).
        anyio.to_thread.current_default_thread_limiter().total_tokens = settings.api_threads
        database = Database(settings, open=open_pool)
        protection = Protection.from_settings(settings)
        app.state.settings = settings
        app.state.database = database
        app.state.protection = protection
        app.state.runner = AnalysisRunner(protection)
        app.state.cache = InMemoryAnalysisCache(maxsize=settings.api_cache_size)
        try:
            yield
        finally:
            cancelled = database.cancel_active()
            if cancelled:
                log.warning("shutdown: cancelled %d running queries", cancelled)
            await app.state.runner.shutdown()
            await anyio.to_thread.run_sync(database.close)

    app = FastAPI(
        title="ATUS Analysis API",
        version=f"{API_VERSION} (contract)",
        description=_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.api_docs else None,
        redoc_url="/redoc" if settings.api_docs else None,
        openapi_url="/openapi.json" if settings.api_docs else None,
        # A trailing-slash redirect would build its Location from the Host
        # header (audit F48); the JSON API has exactly one spelling per route.
        redirect_slashes=False,
    )

    # add_middleware prepends, so the last one added is the outermost.
    app.add_middleware(GuardMiddleware, settings=settings)
    # Responses of 1 KB or more are gzipped for clients that accept it (the
    # activity lexicon is ~70 KB uncompressed; audit F42).
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_BYTES)
    if settings.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.api_cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            expose_headers=list(CORS_EXPOSE_HEADERS),
        )
    app.add_middleware(RequestContextMiddleware, settings=settings)

    def outermost_headers(request: Request) -> dict[str, str]:
        """Headers for responses produced at the outermost error layer, which
        the request-context and CORS middleware never see (unexpected 500s):
        the request ID, security headers, no-store and, for allowed origins,
        CORS, so browser clients can still read the error."""
        headers = security_headers(settings, request.url.path)
        headers["Cache-Control"] = "no-store"
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            headers["X-Request-ID"] = request_id
        origin = request.headers.get("origin")
        if origin and origin in settings.api_cors_origins:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Expose-Headers"] = ", ".join(CORS_EXPOSE_HEADERS)
        return headers

    register_exception_handlers(app, outermost_headers)

    prefix = f"/api/{API_VERSION}"
    app.include_router(analysis.router, prefix=prefix)
    app.include_router(activities.router, prefix=prefix)
    app.include_router(population.router, prefix=prefix)
    app.include_router(meta.router, prefix=prefix)
    app.include_router(health.router)

    return app
