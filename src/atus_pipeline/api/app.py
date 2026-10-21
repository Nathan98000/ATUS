"""FastAPI application factory.

    from atus_pipeline.api.app import create_app
    app = create_app()                 # settings from the environment

The production process is started by ``atus api`` (see
``atus_pipeline.api.serve``), which runs exactly this factory under one
uvicorn process.

Lifecycle: startup builds the database pool without connecting (``min_size``
0: an API cold start never wakes the database), the analysis-result cache,
the (empty) in-process data snapshot and the per-process protection state
(rate buckets, daily cap, admission lanes); it sizes the worker-thread
limiter and starts the periodic metrics log event (``metrics.py``; it
touches no database). Nothing reads the database until a request needs
data. Shutdown closes the admission lanes and the database to new work,
cancels queries still running, waits briefly for detached computations,
cancels again whatever still runs, and closes the pool. The lifespan startup performs no data
validation and no I/O (a cold start's cost is importing the code); a
production configuration without edge secrets, or with a database URL that
does not verify the server's certificate, is refused before anything starts
(``config.check_api_settings``).

Logging is configured here too (``ATUS_LOG_FORMAT``), and uvicorn's own
access log is switched off: the request-context middleware writes one access
line per request, with truncated client addresses (ADR-014).
"""

from __future__ import annotations

import asyncio
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
from .metrics import Metrics, emit_metrics_events
from .middleware import (
    TIMING_STATE_KEY,
    GuardMiddleware,
    RequestContextMiddleware,
    request_server_timing,
    security_headers,
)
from .observability import RouteTemplates
from .protection import Protection
from .routes import activities, analysis, health, meta, population
from .snapshot import SnapshotStore

log = logging.getLogger("atus.api")

GZIP_MINIMUM_BYTES = 1024

# Response headers a cross-origin browser client may read (audit F45).
CORS_EXPOSE_HEADERS = (
    "X-Request-ID", "X-Analysis-Key", "X-Cache", "X-Data-Version",
    "X-Analytics-Version", "X-Response-Format", "Retry-After", "Server-Timing",
)

# Release-neutral on purpose (audit F23): the loaded release and its years
# are data, reported by GET /api/v1/meta.
_DESCRIPTION = """\
Read-only analytical API over the American Time Use Survey (ATUS) multi-year
microdata. The loaded BLS release and its survey years are reported by
GET /api/v1/meta.

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
    configure_logging(settings.log_level, settings.log_format)
    set_redacted_secrets(_secrets_to_redact(settings))
    if settings.is_production and settings.expected_data_version is None:
        log.warning(
            "ATUS_EXPECTED_DATA_VERSION is not set: the data snapshot is re-read every "
            "%.0f s when requests need it, and each read wakes the database (ADR-012)",
            settings.api_snapshot_ttl_s,
        )
    # Replaced by the access line of RequestContextMiddleware, which never
    # logs a full client address (uvicorn's would).
    logging.getLogger("uvicorn.access").disabled = True
    metrics = Metrics(build=settings.build_sha)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Every blocking DB call shares this limiter (audit F02).
        anyio.to_thread.current_default_thread_limiter().total_tokens = settings.api_threads
        database = Database(settings, open=open_pool)
        protection = Protection.from_settings(settings)
        snapshots = SnapshotStore(settings.api_snapshot_ttl_s, settings.expected_data_version)
        app.state.settings = settings
        app.state.database = database
        app.state.protection = protection
        app.state.snapshots = snapshots
        app.state.runner = AnalysisRunner(protection, snapshots)
        app.state.cache = InMemoryAnalysisCache(maxsize=settings.api_cache_size)
        metrics_events = asyncio.ensure_future(emit_metrics_events(metrics, app.state))
        try:
            yield
        finally:
            metrics_events.cancel()
            # Nothing new starts from here on: an analysis waiting for a lane
            # must not take the lane a cancelled query frees, and no
            # transaction may begin after the cancel below.
            protection.lanes.close()
            cancelled = await anyio.to_thread.run_sync(database.begin_shutdown)
            if cancelled:
                log.warning("shutdown: cancelled %d running queries", cancelled)
            await app.state.runner.shutdown()
            # A cancel that reached a transaction between two statements was
            # ignored by the server; cancel whatever is still running.
            late = await anyio.to_thread.run_sync(database.cancel_active)
            if late:
                log.warning("shutdown: cancelled %d queries still running", late)
            await anyio.to_thread.run_sync(database.close)

    app = FastAPI(
        title="ATUS Analysis API",
        version=f"{API_VERSION} (contract)",
        description=_DESCRIPTION,
        lifespan=lifespan,
        # ATUS_API_DOCS=off (the production default) removes /docs,
        # /docs/oauth2-redirect, /redoc and /openapi.json from the API; the
        # same schema is published as a static asset of the site at
        # /openapi.json instead (audit F43; generated without any docs route
        # by scripts/export_openapi.py).
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
    routes = RouteTemplates()
    app.add_middleware(
        RequestContextMiddleware, settings=settings, metrics=metrics, routes=routes
    )
    app.state.settings = settings
    app.state.metrics = metrics

    def outermost_headers(request: Request) -> dict[str, str]:
        """Headers for responses produced at the outermost error layer, which
        the request-context and CORS middleware never see (unexpected 500s):
        the request ID, security headers, no-store, ``Server-Timing`` (the
        governor sizes a computing request's charge from its ``db`` time,
        ADR-014) and, for allowed origins, CORS, so browser clients can still
        read the error."""
        headers = security_headers(settings, request.url.path)
        headers["Cache-Control"] = "no-store"
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            headers["X-Request-ID"] = request_id
        timing = getattr(request.state, TIMING_STATE_KEY, None)
        if timing is not None:
            headers["Server-Timing"] = request_server_timing(*timing)
        origin = request.headers.get("origin")
        if origin and origin in settings.api_cors_origins:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Expose-Headers"] = ", ".join(CORS_EXPOSE_HEADERS)
        return headers

    register_exception_handlers(app, outermost_headers)

    prefix = f"/api/{API_VERSION}"
    for router in (analysis.router, activities.router, population.router, meta.router):
        app.include_router(router, prefix=prefix)
        routes.add_router(router, prefix)
    app.include_router(health.router)
    routes.add_router(health.router)
    if app.openapi_url:  # the same conditions under which FastAPI adds them
        routes.add(app.openapi_url)
        if app.docs_url:
            routes.add(app.docs_url)
            if app.swagger_ui_oauth2_redirect_url:
                routes.add(app.swagger_ui_oauth2_redirect_url)
        if app.redoc_url:
            routes.add(app.redoc_url)

    return app
