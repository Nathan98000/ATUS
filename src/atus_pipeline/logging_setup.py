"""Logging configuration for the pipeline CLI and the API.

Two formats (``ATUS_LOG_FORMAT``): ``text`` for people (development) and
``json`` for log processors (production): one JSON object per line with
``ts``, ``level``, ``logger`` and ``message``, plus the structured fields a
record carries (``extra={"fields": {...}}``). The API's access log puts its
fields at the top level this way (see ADR-014 for the schema); tracebacks go
into ``exc``, so a record never spans lines.

Besides the format this module installs two filters the API relies on:

* **Secret redaction.** Configured secrets (the database password, edge
  secrets, the ops token) are replaced by ``***`` in any record emitted by the
  loggers that could carry them (psycopg's pool logs connection errors
  verbatim; audit I07), and again in the formatted output of the handler
  this module installs (which also covers tracebacks). DSNs are also
  validated at startup, so this is defense in depth.
* **One traceback per unhandled error.** The API's catch-all handler logs an
  unexpected exception with its request id and marks it; uvicorn's own
  "Exception in ASGI application" record for the same exception is then
  dropped (audit F50).
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime

# Records from these loggers are scrubbed of configured secrets. Logger-level
# filters only see records created on that exact logger, so each is listed.
_REDACTED_LOGGERS = (
    "",
    "atus",
    "atus.api",
    "atus.api.access",
    "atus.api.metrics",
    "psycopg",
    "psycopg.pool",
    "uvicorn",
    "uvicorn.error",
)
_MIN_REDACTED_LENGTH = 8  # shorter values (e.g. the dev password) would mangle ordinary text

LOGGED_MARKER = "_atus_logged"
"""Attribute set on an exception once the API has logged it with a traceback."""

TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_HANDLER_MARKER = "_atus_handler"
STRUCTURED_FIELDS = "fields"
"""Name of the ``extra`` attribute whose dict the JSON format lifts to the
top level of the line: ``log.info("...", extra={"fields": {...}})``."""


class _RedactingFilter(logging.Filter):
    """Replaces every configured secret in a record's message with ``***``."""

    def __init__(self) -> None:
        super().__init__()
        self.secrets: tuple[str, ...] = ()

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # a malformed record is logging's problem, not ours
            return True
        redacted = self.redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


class _DuplicateTracebackFilter(logging.Filter):
    """Drops uvicorn's record for an exception the API already logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (exc is not None and getattr(exc, LOGGED_MARKER, False))


_REDACTOR = _RedactingFilter()
_DEDUPLICATOR = _DuplicateTracebackFilter()


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT, datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        return _REDACTOR.redact(super().format(record))


class JsonFormatter(logging.Formatter):
    """One JSON object per record (see module docstring)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
                  .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, STRUCTURED_FIELDS, None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                entry.setdefault(key, value)
        if record.exc_info:
            entry["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            entry["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            entry["exc"] = record.exc_text
        return _REDACTOR.redact(json.dumps(entry, default=str, ensure_ascii=False))


def make_formatter(fmt: str) -> logging.Formatter:
    return JsonFormatter() if fmt == "json" else TextFormatter()


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Send records to stderr in ``fmt`` (``text`` or ``json``).

    Like ``logging.basicConfig``, this installs a handler only when the root
    logger has none (a test runner's or host's handlers are left alone); a
    handler installed by an earlier call is reconfigured, so the API can
    switch the CLI's text output to JSON.
    """
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)
        ours = [handler]
    if ours:
        root.setLevel(getattr(logging, level, logging.INFO))
        for handler in ours:
            handler.setFormatter(make_formatter(fmt))
    # curl_cffi / urllib noise is not useful at INFO
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    _install_filters()


def _install_filters() -> None:
    for name in _REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if _REDACTOR not in logger.filters:
            logger.addFilter(_REDACTOR)
    uvicorn_error = logging.getLogger("uvicorn.error")
    if _DEDUPLICATOR not in uvicorn_error.filters:
        uvicorn_error.addFilter(_DEDUPLICATOR)


def set_redacted_secrets(secrets: Iterable[str | None]) -> None:
    """Replace the set of values scrubbed from log records (longest first, so
    a secret that contains another is redacted whole)."""
    values = {s for s in secrets if s and len(s) >= _MIN_REDACTED_LENGTH}
    _REDACTOR.secrets = tuple(sorted(values, key=len, reverse=True))
    _install_filters()


def mark_logged(exc: BaseException) -> None:
    """Record that ``exc`` was logged with a traceback (see module docstring)."""
    try:
        setattr(exc, LOGGED_MARKER, True)
    except Exception:  # exceptions with __slots__ cannot be marked; log twice then
        pass
