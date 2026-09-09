"""HTTP error model: one consistent envelope, explicit domain-exception mapping.

Every non-2xx response has the shape

    {"error": {"code": "...", "message": "...", "details": {...} | null}}

with a stable machine-readable ``code``. Phase 2 domain errors map to 422
(the request is well-formed JSON but analytically invalid), database
unavailability maps to 503, malformed JSON to 400, and anything unexpected is
sanitized to a 500 whose details are logged server-side only.
"""

from __future__ import annotations

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..analytics.errors import (
    AnalyticsError,
    InsufficientDataError,
    InvalidSpecError,
    UnknownActivityError,
    UnsupportedAnalysisError,
)

log = logging.getLogger("atus.api")


class ActivityNotFoundError(Exception):
    """A GET /activities/{code} identifier that does not exist (404)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Activity code {code!r} does not exist in the 2003-25 lexicon.")


class DatabaseUnavailableError(Exception):
    """The analytical database cannot be reached (503)."""


# AnalyticsError subclasses -> (HTTP status, machine-readable code). All are
# client-fixable analytical problems, hence 422 (well-formed but unprocessable).
_DOMAIN_MAP: dict[type[AnalyticsError], tuple[int, str]] = {
    InvalidSpecError: (422, "invalid_spec"),
    UnknownActivityError: (422, "unknown_activity"),
    UnsupportedAnalysisError: (422, "unsupported_analysis"),
    InsufficientDataError: (422, "insufficient_data"),
}


def error_response(
    status: int, code: str, message: str, details: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details}},
        headers=headers,
    )


_HTTP_STATUS_CODES = {404: "not_found", 405: "method_not_allowed"}


def _outermost_headers(request: Request) -> dict[str, str]:
    """Headers for responses produced at the outermost error layer, which the
    request-ID and CORS middleware never see (unexpected 500s): re-attach the
    request ID and, for allowed origins, the CORS header so browser clients
    can still read the error."""
    headers = {}
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        headers["X-Request-ID"] = request_id
    origin = request.headers.get("origin")
    settings = getattr(request.app.state, "settings", None)
    if origin and settings and origin in settings.api_cors_origins:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AnalyticsError)
    async def handle_domain_error(request: Request, exc: AnalyticsError) -> JSONResponse:
        for exc_type, (status, code) in _DOMAIN_MAP.items():
            if isinstance(exc, exc_type):
                return error_response(status, code, str(exc))
        # Unmapped AnalyticsError subclass: still a domain rejection.
        return error_response(422, "invalid_analysis", str(exc))

    @app.exception_handler(ActivityNotFoundError)
    async def handle_activity_not_found(
        request: Request, exc: ActivityNotFoundError
    ) -> JSONResponse:
        return error_response(
            404, "activity_not_found", str(exc), details={"code": exc.code}
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "location": [str(part) for part in e.get("loc", ())],
                "message": e.get("msg", ""),
                "type": e.get("type", ""),
            }
            for e in exc.errors()
        ]
        if any(e["type"].startswith("json_") for e in errors):
            return error_response(
                400, "malformed_json", "The request body is not valid JSON.",
                details={"errors": errors},
            )
        return error_response(
            422, "validation_error", "The request does not match the API schema.",
            details={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Framework-generated 404/405/... use the same envelope as everything else.
        code = _HTTP_STATUS_CODES.get(exc.status_code, f"http_{exc.status_code}")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(psycopg.errors.UndefinedTable)
    async def handle_missing_schema(request: Request, exc: Exception) -> JSONResponse:
        log.error("schema not initialized: %s", exc)
        return error_response(
            503, "schema_not_initialized",
            "The analytical database schema has not been initialized.",
        )

    @app.exception_handler(DatabaseUnavailableError)
    @app.exception_handler(PoolTimeout)
    @app.exception_handler(psycopg.OperationalError)
    async def handle_database_unavailable(request: Request, exc: Exception) -> JSONResponse:
        log.error("database unavailable: %s: %s", type(exc).__name__, exc)
        return error_response(
            503, "database_unavailable",
            "The analytical database is currently unavailable. Try again shortly.",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Full detail server-side; nothing internal leaks to the client. This
        # handler runs at the outermost middleware layer, so it re-attaches the
        # request-ID/CORS headers itself.
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return error_response(
            500, "internal_error", "An unexpected internal error occurred.",
            headers=_outermost_headers(request),
        )
