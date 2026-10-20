"""Pure-ASGI request middleware: context, security headers, and the guard.

Order of the stack (outermost first)::

    ServerErrorMiddleware (Starlette; the 500 catch-all)
    RequestContextMiddleware  request id, edge-auth status, client identity,
                              HEAD as GET, security headers, Server-Timing,
                              disconnect detection, database accounting, the
                              access log line and the request metrics
    CORSMiddleware            only when ATUS_API_CORS_ORIGINS is set
    GZipMiddleware            responses >= 1 KB, when the client accepts gzip
    GuardMiddleware           declared body size, edge auth, per-client
                              request bucket, media type, streamed body size,
                              control characters in the query string
    ExceptionMiddleware -> routes

The guard rejects before routing, so a rejected request never reaches a
dependency, a pool connection or the database (ADR-011 request flow). Both
classes are plain ASGI (not ``BaseHTTPMiddleware``): no extra task per
request, and the downstream app is never cancelled when a client disconnects.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
import unicodedata
import uuid
from urllib.parse import parse_qsl

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings
from . import observability
from .errors import (
    ERROR_CODE_KEY,
    ApiError,
    ForbiddenOriginError,
    PayloadTooLargeError,
    RateLimitedError,
    UnsupportedMediaTypeError,
    error_response,
    note_error_code,
)
from .metrics import Metrics
from .protection import client_identity

log = logging.getLogger("atus.api")
access_log = logging.getLogger("atus.api.access")

EDGE_AUTH_HEADER = "x-atus-edge-auth"
EDGE_CLIENT_IP_HEADER = "x-atus-client-ip"
REQUEST_ID_HEADER = "x-request-id"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

EDGE_AUTH_EXEMPT_PATHS = frozenset({"/health"})
"""Answered without edge auth: the platform health check reaches the
container directly, and liveness touches nothing."""
OPS_PATHS = frozenset({"/ready", "/metrics"})
"""Operator endpoints: reachable with a valid ops bearer token, with or
without edge auth (the Worker never routes them). Without a valid token
they behave exactly like an unknown path."""
RATE_LIMIT_EXEMPT_PATHS = frozenset({"/health"})
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"
HSTS = "max-age=31536000"


def edge_secret_matches(presented: str | None, secrets: tuple[str, ...]) -> bool:
    """Constant-time comparison against every accepted value (all of them
    are compared, so timing does not reveal which slot matched)."""
    if not presented or not secrets:
        return False
    given = presented.encode("utf-8", "surrogateescape")
    results = [hmac.compare_digest(given, secret.encode()) for secret in secrets]
    return any(results)


def ops_authorized(headers, settings: Settings) -> bool:
    """``Authorization: Bearer <ATUS_OPS_TOKEN>``, compared in constant
    time; always false when no ops token is configured."""
    if not settings.ops_token:
        return False
    scheme, _, presented = (headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(
        presented.strip().encode("utf-8", "surrogateescape"), settings.ops_token.encode()
    )


def server_timing(db_ms: float, total_ms: float) -> str:
    """``Server-Timing``: time in database transactions and everything else."""
    return f"db;dur={db_ms:.1f}, app;dur={max(0.0, total_ms - db_ms):.1f}"


def security_headers(settings: Settings, path: str) -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    # The interactive docs pages (development only) load their own scripts.
    if not (settings.api_docs and path in DOCS_PATHS):
        headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    if settings.is_production:
        headers["Strict-Transport-Security"] = HSTS
    return headers


class _DisconnectWatcher:
    """Notices a client that goes away while its request is being served.

    Once the request body has been read, nothing downstream calls
    ``receive`` again, so a background task waits on it: an
    ``http.disconnect`` before the response started means the client left.
    Later downstream ``receive`` calls get the watcher's message instead of
    racing it.
    """

    def __init__(self, receive: Receive, request_id: str):
        self._receive = receive
        self._request_id = request_id
        self._task: asyncio.Task | None = None
        self.response_started = False
        self.disconnected = False

    async def receive(self) -> Message:
        if self._task is not None:
            return await asyncio.shield(self._task)
        message = await self._receive()
        if message["type"] == "http.disconnect":
            self._note_disconnect()
        elif message["type"] == "http.request" and not message.get("more_body", False):
            self._task = asyncio.ensure_future(self._watch())
            self._task.add_done_callback(_consume_result)
        return message

    async def _watch(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.disconnect":
            self._note_disconnect()
        return message

    def _note_disconnect(self) -> None:
        if not self.response_started and not self.disconnected:
            self.disconnected = True
            log.info(
                "request_id=%s client disconnected before the response; "
                "an admitted analysis still completes and is cached", self._request_id,
                extra={"fields": {"event": "client_disconnected",
                                  "request_id": self._request_id}},
            )

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()


def _consume_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()  # retrieved: a failed watch is not worth a warning


def loggable_path(scope: Scope) -> str:
    """The request path as received (still percent-encoded, so a decoded
    newline cannot forge a log line), bounded in length."""
    raw = scope.get("raw_path") or scope["path"].encode("utf-8", "replace")
    return raw.decode("ascii", "backslashreplace")[:200]


class RequestContextMiddleware:
    def __init__(
        self, app: ASGIApp, settings: Settings, metrics: Metrics | None = None,
        routes: observability.RouteTemplates | None = None,
    ):
        self.app = app
        self.settings = settings
        self.metrics = metrics
        self.routes = routes or observability.RouteTemplates()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        settings = self.settings
        headers = Headers(scope=scope)
        authenticated = edge_secret_matches(headers.get(EDGE_AUTH_HEADER), settings.edge_secrets)

        incoming_id = headers.get(REQUEST_ID_HEADER)
        if authenticated and incoming_id and _REQUEST_ID_PATTERN.match(incoming_id):
            request_id = incoming_id
        else:
            request_id = uuid.uuid4().hex[:16]

        peer = scope["client"][0] if scope.get("client") else None
        identity = client_identity(
            peer=peer,
            edge_client_ip=headers.get(EDGE_CLIENT_IP_HEADER),
            authenticated=authenticated,
            edge_auth_enabled=settings.edge_auth_enabled,
        )
        state = scope.setdefault("state", {})
        state.update(
            request_id=request_id,
            edge_authenticated=authenticated,
            client_ip=identity.ip,
            rate_limit_key=identity.rate_key,
        )

        method = scope["method"]
        path = scope["path"]
        head = method == "HEAD"
        if head:  # HEAD is GET without the body, on every GET route
            scope = {**scope, "method": "GET"}

        watcher = _DisconnectWatcher(receive, request_id) if method in BODY_METHODS else None
        stats, stats_token = observability.start_request()
        response: dict = {"status": None, "cache": None, "analysis_key": None}

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = message["status"]
                response["status"] = status
                if watcher is not None:
                    watcher.response_started = True
                response_headers = MutableHeaders(scope=message)
                for name, value in security_headers(settings, path).items():
                    response_headers[name] = value
                response_headers["X-Request-ID"] = request_id
                response_headers["Server-Timing"] = server_timing(
                    stats.db_ms, (time.perf_counter() - started) * 1000
                )
                if status >= 400:
                    response_headers["Cache-Control"] = "no-store"  # never cache errors
                response["cache"] = response_headers.get("x-cache")
                response["analysis_key"] = response_headers.get("x-analysis-key")
            elif message["type"] == "http.response.body" and head:
                message = {**message, "body": b""}
            await send(message)

        unhandled = False
        try:
            await self.app(scope, watcher.receive if watcher else receive, send_with_headers)
        except Exception:
            # The catch-all handler logs the traceback (once, with the request
            # id) and answers 500 outside this middleware.
            unhandled = True
            raise
        finally:
            if watcher is not None:
                watcher.stop()
            observability.end_request(stats_token)
            self._record(
                scope, method, request_id, started, stats, response, unhandled,
                disconnected=watcher is not None and watcher.disconnected,
            )

    def _record(
        self, scope: Scope, method: str, request_id: str, started: float,
        stats: observability.RequestStats, response: dict, unhandled: bool,
        *, disconnected: bool,
    ) -> None:
        """One access line and the request metrics; never raises."""
        try:
            state = scope.get("state", {})
            status = 500 if unhandled else response["status"]
            error = "internal_error" if unhandled else state.get(ERROR_CODE_KEY)
            latency_ms = (time.perf_counter() - started) * 1000
            route = self.routes.match(scope["path"])
            fields = {
                "event": "access",
                "request_id": request_id,
                "method": method,
                "route": route,
                "path": loggable_path(scope),
                "status": status,
                "latency_ms": round(latency_ms, 1),
                "db_ms": round(stats.db_ms, 1),
                "db_statements": stats.db_statements,
                "cache": response["cache"],
                "analysis_key": response["analysis_key"],
                "error": error,
                "client_ip_prefix": observability.ip_prefix(state.get("client_ip")),
                "edge_authenticated": bool(state.get("edge_authenticated")),
                "disconnected": disconnected,
            }
            access_log.log(
                logging.ERROR if unhandled else logging.INFO,
                "request_id=%s %s %s -> %s in %.1fms db=%.1fms/%d cache=%s%s%s",
                request_id, method, fields["path"], status if status is not None else "-",
                latency_ms, stats.db_ms, stats.db_statements, response["cache"] or "-",
                f" error={error}" if error else "",
                " client_disconnected=1" if disconnected else "",
                extra={"fields": fields},
            )
            if self.metrics is not None:
                self.metrics.observe_request(
                    route=route, method=method, status=status, seconds=latency_ms / 1000,
                    db_statements=stats.db_statements, cache=response["cache"], error=error,
                )
        except Exception:  # observability must never break a response
            log.exception("request_id=%s access record failed", request_id)


class GuardMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = self.settings
        state = scope.get("state", {})
        headers = Headers(scope=scope)
        path = scope["path"]
        method = scope["method"]

        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.api_max_body_bytes:
            await self._reject(PayloadTooLargeError(), scope, receive, send, close=True)
            return

        if (
            settings.edge_auth_enabled
            and path not in EDGE_AUTH_EXEMPT_PATHS
            and not state.get("edge_authenticated", False)
            and not (path in OPS_PATHS and ops_authorized(headers, settings))
        ):
            await self._reject(ForbiddenOriginError(), scope, receive, send)
            return

        rate_key = state.get("rate_limit_key")
        if rate_key is not None and path not in RATE_LIMIT_EXEMPT_PATHS:
            protection = getattr(scope["app"].state, "protection", None)
            if protection is not None:
                wait = protection.request_limiter.acquire(rate_key)
                if wait is not None:
                    await self._reject(
                        RateLimitedError("request rate limit", retry_after=wait),
                        scope, receive, send,
                    )
                    return

        has_body = declared not in ("", "0") or "transfer-encoding" in headers
        if method == "POST" or (method in BODY_METHODS and has_body):
            media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                await self._reject(UnsupportedMediaTypeError(), scope, receive, send)
                return
            body = await self._read_body(receive, settings.api_max_body_bytes)
            if body is None:  # client went away mid-body
                return
            if body is _TOO_LARGE:
                await self._reject(PayloadTooLargeError(), scope, receive, send, close=True)
                return
            receive = _replay(body, receive)

        bad_parameter = _control_character_parameter(scope.get("query_string", b""))
        if bad_parameter is not None:
            note_error_code(scope.setdefault("state", {}), "validation_error")
            response = error_response(
                422, "validation_error", "The request does not match the API schema.",
                details={"errors": [{
                    "location": ["query", bad_parameter[:64]],
                    "message": "Control characters are not allowed.",
                    "type": "value_error",
                }]},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(
        error: ApiError, scope: Scope, receive: Receive, send: Send, *, close: bool = False
    ) -> None:
        log.info("rejected %s %s: %s", scope["method"], loggable_path(scope), error.code)
        note_error_code(scope.setdefault("state", {}), error.code)
        response = error.to_response({"Connection": "close"} if close else None)
        await response(scope, receive, send)

    @staticmethod
    async def _read_body(receive: Receive, limit: int):
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                return _TOO_LARGE
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)


_TOO_LARGE = object()


def _replay(body: bytes, receive: Receive) -> Receive:
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


def _control_character_parameter(query_string: bytes) -> str | None:
    """The first query parameter whose name or value contains a control
    character (e.g. NUL, which PostgreSQL text cannot hold; audit F50)."""
    if not query_string:
        return None
    pairs = parse_qsl(query_string.decode("latin-1"), keep_blank_values=True, errors="replace")
    for name, value in pairs:
        if any(unicodedata.category(ch) == "Cc" for ch in name + value):
            return name
    return None
