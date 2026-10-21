"""A local fake HTTP server and script loaders for the ops-script tests.

The scripts under scripts/ops/ and scripts/replay_deployed.py check the
deployed site; these tests run them against an in-process server on an
ephemeral loopback port (plain HTTP is allowed only to loopback), so nothing
ever reaches a provider.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS_DIR = REPO_ROOT / "scripts" / "ops"

Reply = tuple[int, dict[str, str], bytes]
Route = Reply | Callable[[dict], Reply]

#: What the Worker sends on every /api response (edge/src/http.ts).
API_SECURITY = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cache-Control": "no-store",
}
#: What frontend/public/_headers gives the static site.
SITE_SECURITY = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=()",
}
SHELL_HTML = (b'<!doctype html><html><head><meta http-equiv="Content-Security-Policy" '
              b'content="default-src \'none\'"><title>ATUS Explorer</title></head>'
              b'<body><div id="root"></div></body></html>')


def json_reply(status: int, payload: Any, headers: dict[str, str] | None = None,
               *, api: bool = True) -> Reply:
    base = dict(API_SECURITY) if api else {}
    base["Content-Type"] = "application/json"
    base.update(headers or {})
    return status, base, json.dumps(payload).encode()


def envelope(status: int, code: str, headers: dict[str, str] | None = None) -> Reply:
    return json_reply(status, {"error": {"code": code, "message": code, "details": {}}},
                      headers)


def html_reply(body: bytes = SHELL_HTML, headers: dict[str, str] | None = None) -> Reply:
    base = {**SITE_SECURITY, "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-cache"}
    base.update(headers or {})
    return 200, base, body


@dataclass
class FakeServer:
    """Routes map (method, path) to a reply or a callable; unknown paths get the
    SPA fallback for GET (as Workers static assets do) and a 404 envelope otherwise."""

    routes: dict[tuple[str, str], Route] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    base: str = ""
    spa_fallback: bool = True
    gzip_json: bool = False

    def reply(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> Reply:
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        route = self.routes.get((method, path))
        if route is None:
            if method == "GET" and self.spa_fallback and not path.startswith("/api"):
                return html_reply()
            return envelope(404, "not_found")
        return route({"body": body, "headers": headers, "path": path}) if callable(route) \
            else route

    def count(self, method: str | None = None, path: str | None = None) -> int:
        return sum(1 for c in self.calls if (method is None or c["method"] == method)
                   and (path is None or c["path"] == path))


@pytest.fixture
def fake_server() -> Iterator[FakeServer]:
    server_state = FakeServer()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, reply_headers, payload = server_state.reply(
                self.command, self.path, body, headers)
            if server_state.gzip_json and "json" in reply_headers.get("Content-Type", ""):
                payload = gzip.compress(payload)
                reply_headers = {**reply_headers, "Content-Encoding": "gzip"}
            self.send_response(status)
            for name, value in reply_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_DELETE = _handle

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_state.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    try:
        yield server_state
    finally:
        httpd.shutdown()
        httpd.server_close()


def load_script(path: Path, name: str) -> ModuleType:
    """Import a script file as a module (its own sys.path setup runs too)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Retries and pacing sleep through httpcheck/time; record instead of waiting."""
    slept: list[float] = []
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture(autouse=True)
def _clean_ops_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real deployment secret or URL from the developer's shell reaches a test."""
    for name in ("RENDER_ORIGIN", "RENDER_DEPLOY_HOOK_URL", "EDGE_OPS_TOKEN", "PUBLIC_BASE_URL",
                 "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY",
                 "GITHUB_REPOSITORY"):
        monkeypatch.delenv(name, raising=False)
