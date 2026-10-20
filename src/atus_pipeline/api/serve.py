"""The API server process: one uvicorn process with explicit, safe settings.

    atus api                      # the CLI wrapper
    python -m atus_pipeline.api.serve

Process model (ADR-011): a single process (never ``WEB_CONCURRENCY``
workers: the protection state, result cache and single-flight map are per
process), listening on ``$PORT`` — on all interfaces in production (the
platform routes to the container), on loopback in development.

Hardening (audit F07, F42, F49, F52):

* proxy-header handling is off and no forwarding peer is trusted: the client
  address the app sees is the socket peer, and the only forwarded client IP
  it ever honours is ``X-ATUS-Client-IP`` on edge-authenticated requests;
* no ``server`` header, websockets disabled, the h11 parser with a bounded
  request head;
* idle keep-alive connections close after 5 s; ``limit_concurrency`` sheds
  connections beyond a fixed bound with a 503 before any app code runs,
  which caps what slow or idle clients can pin;
* on SIGTERM, in-flight requests get ``GRACEFUL_SHUTDOWN_S`` (below the
  platform's grace period) before the app shuts down and cancels whatever
  queries are still running.
"""

from __future__ import annotations

import sys

from ..config import ConfigError, Settings, check_api_settings, load_settings
from ..logging_setup import configure_logging

APP_FACTORY = "atus_pipeline.api.app:create_app"
GRACEFUL_SHUTDOWN_S = 20       # Render waits 30 s after SIGTERM before SIGKILL
KEEP_ALIVE_S = 5
MAX_REQUEST_HEAD_BYTES = 16 * 1024


def limit_concurrency(settings: Settings) -> int:
    """Connections + in-flight requests above which uvicorn answers 503
    directly. Four per worker thread leaves room for queued cheap requests
    while bounding what slow clients can hold."""
    return max(32, settings.api_threads * 4)


def default_host(settings: Settings) -> str:
    return "0.0.0.0" if settings.is_production else "127.0.0.1"


def uvicorn_options(
    settings: Settings, *, host: str | None = None, port: int | None = None,
    reload: bool = False,
) -> dict:
    """Keyword arguments for ``uvicorn.run`` (validated, no side effects)."""
    if reload and settings.is_production:
        raise ConfigError("--reload is a development option; refusing it in production")
    return {
        "app": APP_FACTORY,
        "factory": True,
        "host": host or default_host(settings),
        "port": port if port is not None else settings.port,
        "reload": reload,
        "workers": 1,
        "loop": "asyncio",
        "http": "h11",
        "ws": "none",
        "lifespan": "on",
        "proxy_headers": False,
        "forwarded_allow_ips": "",
        "server_header": False,
        "date_header": True,
        "timeout_keep_alive": KEEP_ALIVE_S,
        "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_S,
        "limit_concurrency": limit_concurrency(settings),
        "h11_max_incomplete_event_size": MAX_REQUEST_HEAD_BYTES,
        # The app logs one line per request itself (with its request id);
        # uvicorn's access log would duplicate it with the raw client IP.
        "access_log": False,
        "log_config": None,
    }


def run(
    settings: Settings | None = None, *, host: str | None = None, port: int | None = None,
    reload: bool = False,
) -> None:
    import uvicorn

    settings = settings or load_settings()
    check_api_settings(settings)
    configure_logging(settings.log_level)
    uvicorn.run(**uvicorn_options(settings, host=host, port=port, reload=reload))


def main() -> None:
    try:
        settings = load_settings()
        check_api_settings(settings)
    except ConfigError as exc:
        print(f"atus api: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    run(settings)


if __name__ == "__main__":
    main()
