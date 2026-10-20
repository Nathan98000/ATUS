"""Logging configuration for the pipeline CLI and the API.

Besides the basic format this module installs two filters the API relies on:

* **Secret redaction.** Configured secrets (the database password, edge
  secrets, the ops token) are replaced by ``***`` in any record emitted by the
  loggers that could carry them (psycopg's pool logs connection errors
  verbatim; audit I07). DSNs are also validated at startup, so this is defense
  in depth.
* **One traceback per unhandled error.** The API's catch-all handler logs an
  unexpected exception with its request id and marks it; uvicorn's own
  "Exception in ASGI application" record for the same exception is then
  dropped (audit F50).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

# Records from these loggers are scrubbed of configured secrets. Logger-level
# filters only see records created on that exact logger, so each is listed.
_REDACTED_LOGGERS = (
    "",
    "atus",
    "atus.api",
    "psycopg",
    "psycopg.pool",
    "uvicorn",
    "uvicorn.error",
)
_MIN_REDACTED_LENGTH = 8  # shorter values (e.g. the dev password) would mangle ordinary text

LOGGED_MARKER = "_atus_logged"
"""Attribute set on an exception once the API has logged it with a traceback."""


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # curl_cffi / urllib noise is not useful at INFO
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    _install_filters()


class _RedactingFilter(logging.Filter):
    """Replaces every configured secret in a record's message with ``***``."""

    def __init__(self) -> None:
        super().__init__()
        self.secrets: tuple[str, ...] = ()

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # a malformed record is logging's problem, not ours
            return True
        redacted = message
        for secret in self.secrets:
            redacted = redacted.replace(secret, "***")
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
