"""HTTP error model: one consistent envelope, explicit exception mapping.

Every non-2xx response has the shape

    {"error": {"code": "...", "message": "...", "details": {...} | null}}

with a stable machine-readable ``code``. Phase 2 domain errors map to 422
(the request is well-formed JSON but analytically invalid), malformed JSON to
400, and anything unexpected is sanitized to a 500 whose details are logged
server-side only (once, with the request id).

Phase 5 adds the protection and capacity codes (ADR-012). Each is an
:class:`ApiError` subclass carrying its status, code and public message;
temporary conditions also carry ``Retry-After`` (header, and
``details.retry_after_seconds``) and a ``details.scope`` naming whose limit
was hit (``client``, ``global``, ``daily`` or ``monthly``). Database errors
are classified by what the client should do about them, not by driver class:

* the query hit the statement timeout or the request deadline -> 503
  ``analysis_timeout`` (never cached);
* lock waits, deadlocks, out-of-resources (SQLSTATE class 53, e.g. a full
  ``/dev/shm``), a pool with every connection busy -> 503 ``overloaded``;
* Neon's quota/suspension refusals -> 503 ``database_paused``;
* everything else about reaching the server -> 503 ``database_unavailable``.

Data-state codes: 503 ``data_not_loaded`` (an empty database) and 503
``data_version_mismatch`` (the database serves another data version than
``ATUS_EXPECTED_DATA_VERSION``). Every handler records the code it answered
in the request state, for the access log and the metrics.
"""

from __future__ import annotations

import logging
import math

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
from ..logging_setup import mark_logged

log = logging.getLogger("atus.api")


class ActivityNotFoundError(Exception):
    """A GET /activities/{code} identifier that does not exist (404).

    ``lexicon`` names the loaded lexicon (e.g. ``"2003-25"``, derived from
    the loaded release; audit F23); without it the message is
    release-neutral.
    """

    def __init__(self, code: str, lexicon: str | None = None):
        self.code = code
        where = f"the {lexicon} activity lexicon" if lexicon else "the activity lexicon"
        super().__init__(f"Activity code {code!r} does not exist in {where}.")


# --------------------------------------------------------------------------- #
# API errors with a fixed status, code and public message                      #
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    """An error with a fixed HTTP status, code and client-facing message.

    The constructor's positional ``detail`` is for the server log only; the
    client always sees ``message`` (the class default unless overridden).
    """

    status: int = 500
    code: str = "internal_error"
    message: str = "An unexpected internal error occurred."
    retry_after: int | None = None
    scope: str | None = None

    def __init__(
        self,
        detail: str | None = None,
        *,
        message: str | None = None,
        retry_after: float | None = None,
        scope: str | None = None,
        details: dict | None = None,
    ):
        super().__init__(detail or message or self.message)
        if message is not None:
            self.message = message
        if retry_after is not None:
            self.retry_after = max(1, math.ceil(retry_after))
        if scope is not None:
            self.scope = scope
        self.extra_details = details

    def response_details(self) -> dict | None:
        details = dict(self.extra_details or {})
        if self.retry_after is not None:
            details["retry_after_seconds"] = self.retry_after
        if self.scope is not None:
            details["scope"] = self.scope
        return details or None

    def response_headers(self) -> dict[str, str] | None:
        if self.retry_after is None:
            return None
        return {"Retry-After": str(self.retry_after)}

    def to_response(self, headers: dict[str, str] | None = None) -> JSONResponse:
        merged = {**(self.response_headers() or {}), **(headers or {})}
        return error_response(
            self.status, self.code, self.message, self.response_details(), merged or None
        )


class ForbiddenOriginError(ApiError):
    """A request that did not come through the edge (missing/wrong secret)."""

    status, code = 403, "forbidden_origin"
    message = "This API is only served through its public site."


class PayloadTooLargeError(ApiError):
    status, code = 413, "payload_too_large"
    message = "The request body is too large."


class UnsupportedMediaTypeError(ApiError):
    status, code = 415, "unsupported_media_type"
    message = "Request bodies must be JSON (Content-Type: application/json)."


class RateLimitedError(ApiError):
    status, code, scope = 429, "rate_limited", "client"
    message = "Too many requests from this client. Retry after the indicated delay."


class OverloadedError(ApiError):
    status, code, scope, retry_after = 503, "overloaded", "global", 10
    message = "The service is busy with other analyses. Retry shortly."


class AnalysisTimeoutError(ApiError):
    status, code, scope, retry_after = 503, "analysis_timeout", "global", 60
    message = (
        "The analysis took too long and was stopped. Retry later, or narrow the "
        "request (fewer years, or variance 'none')."
    )


class DailyCapacityReachedError(ApiError):
    status, code, scope = 503, "daily_capacity_reached", "daily"
    message = (
        "Today's capacity for new analyses has been used. Previously computed "
        "results remain available; new analyses resume at 00:00 UTC."
    )


class DataVersionMismatchError(ApiError):
    """The database serves another data version than the deployment expects
    (``ATUS_EXPECTED_DATA_VERSION``; ADR-011 release switch)."""

    status, code, scope, retry_after = 503, "data_version_mismatch", "global", 60
    message = (
        "The service is switching data releases and cannot answer from the "
        "expected data right now. Retry shortly."
    )


class DataNotLoadedError(ApiError):
    status, code, retry_after = 503, "data_not_loaded", 300
    message = "The analytical data is not available right now."


class DatabasePausedError(ApiError):
    status, code, scope, retry_after = 503, "database_paused", "monthly", 3600
    message = (
        "The analytical database is paused until its free usage quota resets. "
        "Previously computed results remain available."
    )


class DatabaseUnavailableError(ApiError):
    """The analytical database cannot be reached (503)."""

    status, code, retry_after = 503, "database_unavailable", 5
    message = "The analytical database is currently unavailable. Try again shortly."


class SchemaNotInitializedError(ApiError):
    status, code, retry_after = 503, "schema_not_initialized", 300
    message = "The analytical database schema has not been initialized."


# --------------------------------------------------------------------------- #
# database error classification                                                #
# --------------------------------------------------------------------------- #

# Neon's refusals when a project exhausts a quota or its endpoint is disabled.
# Neon does not document these strings; they come from its proxy's
# user-facing messages (neondatabase/neon, proxy/src/control_plane/messages.rs:
# "endpoint is temporarily unavailable. Check your quotas and/or contact our
# support.", "endpoint is disabled") and from client reports of the control
# plane's text ("Your account or project has exceeded the compute time
# quota", "Your project has exceeded the data transfer quota"). Matching is
# deliberately narrow and case-insensitive: an unmatched refusal is still a
# 503 (database_unavailable), only with a less specific message.
NEON_PAUSED_MARKERS = (
    "exceeded the compute time quota",
    "exceeded the data transfer quota",
    "check your quotas",
    "endpoint is disabled",
)

# SQLSTATEs meaning "the server is too busy for this right now".
_OVERLOAD_SQLSTATES = ("55P03", "40P01")  # lock_not_available, deadlock_detected
_OVERLOAD_SQLSTATE_CLASSES = ("53",)      # insufficient resources (disk/shm, memory, slots)


def is_neon_paused(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in NEON_PAUSED_MARKERS)


def classify_connect_failure(exc: BaseException) -> ApiError:
    """The client-facing error for a failure to open a connection."""
    if is_neon_paused(str(exc)):
        return DatabasePausedError(f"connect refused by provider: {type(exc).__name__}")
    sqlstate = getattr(exc, "sqlstate", None) or ""
    if sqlstate.startswith(_OVERLOAD_SQLSTATE_CLASSES):  # e.g. too many connections
        return OverloadedError(f"connect refused: {sqlstate}")
    return DatabaseUnavailableError(f"connect failed: {type(exc).__name__}")


def classify_database_error(exc: BaseException, *, analysis: bool) -> ApiError | None:
    """Map a driver/server error to an :class:`ApiError`; ``None`` means it is
    not an availability or capacity problem (a bug: answer 500)."""
    if isinstance(exc, PoolTimeout):
        return OverloadedError("pool timeout")
    if isinstance(exc, psycopg.errors.QueryCanceled):
        # statement_timeout, the request deadline, or a shutdown cancel
        return AnalysisTimeoutError("query canceled") if analysis else OverloadedError(
            "metadata query canceled"
        )
    sqlstate = getattr(exc, "sqlstate", None) or ""
    if sqlstate in _OVERLOAD_SQLSTATES or sqlstate.startswith(_OVERLOAD_SQLSTATE_CLASSES):
        return OverloadedError(f"database refused for capacity: {sqlstate}")
    if is_neon_paused(str(exc)):
        return DatabasePausedError(f"provider refusal: {type(exc).__name__}")
    if isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError):
        return DatabaseUnavailableError(f"{type(exc).__name__} {sqlstate}".strip())
    return None


# --------------------------------------------------------------------------- #
# rendering                                                                    #
# --------------------------------------------------------------------------- #


def error_response(
    status: int, code: str, message: str, details: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details}},
        headers=headers,
    )


# AnalyticsError subclasses -> (HTTP status, machine-readable code). All are
# client-fixable analytical problems, hence 422 (well-formed but unprocessable).
_DOMAIN_MAP: dict[type[AnalyticsError], tuple[int, str]] = {
    InvalidSpecError: (422, "invalid_spec"),
    UnknownActivityError: (422, "unknown_activity"),
    UnsupportedAnalysisError: (422, "unsupported_analysis"),
    InsufficientDataError: (422, "insufficient_data"),
}

_HTTP_STATUS_CODES = {404: "not_found", 405: "method_not_allowed"}

# A 422 lists at most this many problems, each with bounded strings: request
# keys are attacker-chosen, and an uncapped list turned a 930 KB body into a
# 9.7 MB response (audit F04).
MAX_VALIDATION_ERRORS = 20
_MAX_LOCATION_PART = 64
_MAX_MESSAGE = 200


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validation_details(raw_errors: list[dict]) -> dict:
    errors = [
        {
            "location": [truncate(str(part), _MAX_LOCATION_PART) for part in e.get("loc", ())],
            "message": truncate(str(e.get("msg", "")), _MAX_MESSAGE),
            "type": truncate(str(e.get("type", "")), _MAX_LOCATION_PART),
        }
        for e in raw_errors[:MAX_VALIDATION_ERRORS]
    ]
    details: dict = {"errors": errors}
    if len(raw_errors) > MAX_VALIDATION_ERRORS:
        details["truncated"] = True
        details["error_count"] = len(raw_errors)
    return details


ERROR_CODE_KEY = "error_code"
"""Key under which the error code of a response is left in the ASGI scope's
``state`` for the access log and the metrics."""


def note_error_code(state: dict, code: str) -> None:
    state[ERROR_CODE_KEY] = code


def _is_analysis_request(request: Request) -> bool:
    return request.url.path.startswith("/api/v1/analysis/")


def register_exception_handlers(app: FastAPI, outermost_headers) -> None:
    """Install the envelope handlers. ``outermost_headers(request)`` supplies
    the headers a 500 needs, because the catch-all runs outside every
    middleware. Every handler records its error code in the request state."""

    def respond(
        request: Request, status: int, code: str, message: str,
        details: dict | None = None, headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        request.state.error_code = code
        return error_response(status, code, message, details, headers)

    def respond_api_error(request: Request, exc: ApiError) -> JSONResponse:
        request.state.error_code = exc.code
        return exc.to_response()

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        level = logging.WARNING if exc.status >= 500 else logging.INFO
        log.log(level, "%s on %s %s: %s", exc.code, request.method, request.url.path, exc)
        return respond_api_error(request, exc)

    @app.exception_handler(AnalyticsError)
    async def handle_domain_error(request: Request, exc: AnalyticsError) -> JSONResponse:
        for exc_type, (status, code) in _DOMAIN_MAP.items():
            if isinstance(exc, exc_type):
                return respond(request, status, code, str(exc))
        # Unmapped AnalyticsError subclass: still a domain rejection.
        return respond(request, 422, "invalid_analysis", str(exc))

    @app.exception_handler(ActivityNotFoundError)
    async def handle_activity_not_found(
        request: Request, exc: ActivityNotFoundError
    ) -> JSONResponse:
        return respond(request, 404, "activity_not_found", str(exc), details={"code": exc.code})

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        raw = list(exc.errors())
        details = validation_details(raw)
        if any(str(e.get("type", "")).startswith("json_") for e in raw):
            return respond(
                request, 400, "malformed_json", "The request body is not valid JSON.",
                details=details,
            )
        return respond(
            request, 422, "validation_error", "The request does not match the API schema.",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Framework-generated 404/405/... use the same envelope as everything
        # else, keeping the framework's headers (e.g. Allow on a 405).
        code = _HTTP_STATUS_CODES.get(exc.status_code, f"http_{exc.status_code}")
        return respond(
            request, exc.status_code, code, str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(psycopg.errors.UndefinedTable)
    async def handle_missing_schema(request: Request, exc: Exception) -> JSONResponse:
        log.error("schema not initialized: %s", exc)
        return respond_api_error(request, SchemaNotInitializedError())

    @app.exception_handler(PoolTimeout)
    @app.exception_handler(psycopg.Error)
    async def handle_database_error(request: Request, exc: Exception) -> JSONResponse:
        mapped = classify_database_error(exc, analysis=_is_analysis_request(request))
        if mapped is None:
            return await handle_unexpected(request, exc)
        first_line = (str(exc).splitlines() or [""])[0][:200]
        log.warning(
            "%s on %s %s: %s %s: %s", mapped.code, request.method, request.url.path,
            type(exc).__name__, getattr(exc, "sqlstate", None) or "", first_line,
        )
        return respond_api_error(request, mapped)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Full detail server-side, logged once; nothing internal leaks to the
        # client. This handler runs at the outermost middleware layer, so it
        # re-attaches the request-ID/CORS/security headers itself.
        request_id = getattr(request.state, "request_id", "-")
        log.exception(
            "request_id=%s unhandled error on %s %s", request_id, request.method,
            request.url.path,
            extra={"fields": {"event": "unhandled_error", "request_id": request_id}},
        )
        mark_logged(exc)
        return respond(
            request, 500, "internal_error", "An unexpected internal error occurred.",
            headers=outermost_headers(request),
        )
