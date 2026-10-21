"""deploy/stack/edge_probe.py passes a compliant edge and fails a non-compliant one.

The probe runs against a small in-process HTTP stub that answers like the
public origin under `wrangler dev`: the static site with frontend/public/
_headers, the static snapshot, and the Worker's /api routes (allowlist,
security headers, limits, the edge cache, the governor's per-network
denial, the ops gate), plus a stub of the API itself for the origin lock.
Each behaviour can be switched off to show that exactly its check notices,
as tests/deploy/test_smoke_probe.py does for the API probe.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import REPO_ROOT

DATA_VERSION = "0325:serving-20261020T000000Z-0123abcd:abc"
SNAPSHOT_ID = "s-0123abcd"
OPS_TOKEN = "stub-ops-token"
ESTIMATE = "/api/v1/analysis/estimate"
META = json.dumps({"api_version": "v1", "data": {"years": [2023]}}).encode()
COMPLIANT = {
    "spa_fallback": True, "drop_static": None, "static_override": {}, "index_no_cache": True,
    "asset_immutable": True, "pointer_no_cache": True, "api_meta": META,
    "edge_snapshot_id": SNAPSHOT_ID, "allowlist": True, "api_no_store": True, "limits": True,
    "edge_cache": True, "wake": True, "ops_gate": True, "origin_lock": True, "governor": True,
}
API_SECURITY = {
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "strict-transport-security": "max-age=31536000",
    "permissions-policy": "camera=()",
}
INDEX = b'<!doctype html><html><body><div id="root"></div>' \
        b'<script type="module" src="/assets/index-abc123.js"></script></body></html>'


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location(
        "edge_probe", REPO_ROOT / "deploy" / "stack" / "edge_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _site_headers() -> dict[str, str]:
    """The `/*` rule of frontend/public/_headers, parsed independently."""
    headers, inside = {}, False
    for line in (REPO_ROOT / "frontend" / "public" / "_headers").read_text().splitlines():
        if line.startswith("/"):
            inside = line.strip() == "/*"
        elif inside and line.startswith("  ") and ":" in line:
            name, _, value = line.strip().partition(":")
            headers[name] = value.strip()
    return headers


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    """A published site: _headers, index.html, one hashed bundle, a snapshot."""
    root = tmp_path_factory.mktemp("assets")
    shutil.copy(REPO_ROOT / "frontend" / "public" / "_headers", root / "_headers")
    (root / "index.html").write_bytes(INDEX)
    (root / "assets").mkdir()
    (root / "assets" / "index-abc123.js").write_bytes(b"console.log(1)\n")
    snap = root / "snap" / SNAPSHOT_ID
    (snap / "get").mkdir(parents=True)
    (snap / "post").mkdir()
    (snap / "get" / "meta.json").write_bytes(META)
    (snap / "post" / "k1.json").write_bytes(b'{"estimate":1}')
    manifest = {"snapshot_id": SNAPSHOT_ID, "data_version": DATA_VERSION,
                "files": {"get/meta.json": {"bytes": len(META)},
                          "post/k1.json": {"bytes": 14}},
                "post": {"k1": {"file": "post/k1.json"}}}
    (snap / "manifest.json").write_text(json.dumps(manifest))
    (root / "snap" / "current.json").write_text(json.dumps(
        {"snapshot_id": SNAPSHOT_ID, "data_version": DATA_VERSION}))
    return root


def _envelope(code: str, details: dict | None = None) -> bytes:
    return json.dumps({"error": {"code": code, "message": code, "details": details}}).encode()


def _edge_handler(behaviour: dict, assets):
    site = {**_site_headers(), **behaviour["static_override"]}
    if behaviour["drop_static"]:
        site.pop(behaviour["drop_static"])
    cache: dict[bytes, bytes] = {}
    admitted: dict[str, int] = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep pytest output clean
            pass

        def send_response(self, code, message=None):
            self.send_response_only(code, message)   # no `Server` header

        def _send(self, status: int, body: bytes, content_type: str,
                  headers: dict[str, str]) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        # -- static assets (no Worker) ---------------------------------------
        def _static(self, path: str) -> None:
            headers = dict(site)
            if path.startswith("/assets/"):
                file = assets / path.lstrip("/")
                if behaviour["asset_immutable"]:
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return self._send(200, file.read_bytes(), "text/javascript", headers)
            if path.startswith("/snap/"):
                file = assets / path.lstrip("/")
                if path == "/snap/current.json":
                    if behaviour["pointer_no_cache"]:
                        headers["Cache-Control"] = "no-cache"
                else:
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return self._send(200, file.read_bytes(), "application/json", headers)
            known = path in ("/", "/explore", "/about") or path.startswith("/analysis/")
            if not known and not behaviour["spa_fallback"]:
                return self._send(404, b"not found", "text/plain", headers)
            if path in ("/", "/explore", "/about") or path.startswith("/analysis/"):
                if behaviour["index_no_cache"]:
                    headers["Cache-Control"] = "no-cache"
            return self._send(200, INDEX, "text/html", headers)

        # -- the Worker -------------------------------------------------------
        def _api(self, status: int, body: bytes, **extra: str) -> None:
            headers = dict(API_SECURITY)
            if behaviour["api_no_store"]:
                headers["Cache-Control"] = "no-store"
            headers["X-Request-Id"] = "stub-request-0001"
            headers.update(extra)
            self._send(status, body, "application/json", headers)

        def _meta(self, query: bool) -> None:
            # As the Worker does (edge/src/snapshot.ts): a plain /api/v1/meta is the
            # deployed snapshot's file; with a query string, or without that file,
            # the request reaches the API.
            file = assets / "snap" / behaviour["edge_snapshot_id"] / "get" / "meta.json"
            body = behaviour["api_meta"] if query or not file.is_file() else file.read_bytes()
            self._api(200, body, **{"X-Data-Version": DATA_VERSION})

        def do_GET(self):
            path = self.path
            if not path.startswith("/api"):
                return self._static(path.split("?")[0])
            if path.split("?", 1)[0] == "/api/v1/meta":
                return self._meta("?" in path)
            if path == "/api/_edge/health":
                return self._api(200, json.dumps({
                    "status": "ok", "cache_scope": "stub", "data_version": DATA_VERSION,
                    "snapshot_id": behaviour["edge_snapshot_id"]}).encode())
            if path == "/api/v1/wake":
                if not behaviour["wake"]:
                    return self._api(502, _envelope("origin_unavailable"))
                return self._api(200, b'{"origin":"warm"}')
            if path == "/api/_ops/budget":
                auth = self.headers.get("Authorization")
                if auth == f"Bearer {OPS_TOKEN}" or (auth and not behaviour["ops_gate"]):
                    return self._api(200, b'{"governor":{"mode":"conservative"}}')
                return self._api(404, _envelope("not_found"))
            if path == "/api/docs" and not behaviour["allowlist"]:
                return self._api(200, b"{}")
            if path == ESTIMATE:
                return self._api(405, _envelope("method_not_allowed"))
            return self._api(404, _envelope("not_found"))

        def do_DELETE(self):
            self._api(405, _envelope("method_not_allowed"))

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path != ESTIMATE:
                return self._api(405, _envelope("method_not_allowed"))
            if behaviour["limits"]:
                if len(body) > 64 * 1024:
                    return self._api(413, _envelope("payload_too_large"))
                if self.headers.get("Content-Type") != "application/json":
                    return self._api(415, _envelope("unsupported_media_type"))
            try:
                spec = json.loads(body or b"{}")
            except ValueError:
                return self._api(400, _envelope("invalid_json"))
            if spec.get("years") == []:
                return self._api(422, _envelope("validation_error"))
            if behaviour["edge_cache"] and body in cache:
                return self._api(200, cache[body], **{"X-Cache": "edge",
                                                      "X-Data-Version": DATA_VERSION})
            network = self.headers.get("CF-Connecting-IP", "127.0.0.1").rsplit(".", 1)[0]
            with lock:
                if behaviour["governor"] and admitted.get(network, 0) >= 60:
                    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
                    return self._api(429, _envelope("rate_limited", {
                        "scope": "client", "budget": "prefix",
                        "resumes_at": f"{tomorrow}T00:00:00Z"}), **{"Retry-After": "3600"})
                admitted[network] = admitted.get(network, 0) + 1
            result = json.dumps({"estimate": {"value": 1.0}}).encode()
            cache[body] = result
            return self._api(200, result, **{"X-Cache": "miss", "X-Data-Version": DATA_VERSION})

    return Handler


def _origin_handler(behaviour: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            status, body = (403, _envelope("forbidden_origin")) if behaviour["origin_lock"] \
                else (200, META)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture
def stub_edge(assets):
    servers = []

    def serve(handler) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def start(**overrides) -> tuple[str, str]:
        behaviour = {**COMPLIANT, **overrides}
        return serve(_edge_handler(behaviour, assets)), serve(_origin_handler(behaviour))

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _run(probe, stub_edge, assets, monkeypatch, capsys, **overrides) -> tuple[int, list[str]]:
    edge, origin = stub_edge(**overrides)
    monkeypatch.setenv("ATUS_PROBE_EDGE_OPS_TOKEN", OPS_TOKEN)
    monkeypatch.setenv("ATUS_PROBE_DATA_VERSION", DATA_VERSION)
    code = probe.main(["--base", edge, "--assets", str(assets), "--direct-api", origin])
    lines = capsys.readouterr().out.splitlines()
    return code, [line.split()[1] for line in lines if line.startswith("[FAIL]")]


def test_a_compliant_edge_passes_every_check(probe, stub_edge, assets, monkeypatch, capsys):
    code, failed = _run(probe, stub_edge, assets, monkeypatch, capsys)
    assert failed == [] and code == 0


@pytest.mark.parametrize("overrides, failing_check", [
    # The two headers the old check could not see (an empty needle matched
    # anything): missing, or with another value.
    ({"drop_static": "Referrer-Policy"}, "static_headers"),
    ({"static_override": {"Referrer-Policy": "unsafe-url"}}, "static_headers"),
    ({"drop_static": "Permissions-Policy"}, "static_headers"),
    ({"static_override": {"Permissions-Policy": "camera=*"}}, "static_headers"),
    ({"drop_static": "Strict-Transport-Security"}, "static_headers"),
    ({"drop_static": "Cross-Origin-Opener-Policy"}, "static_headers"),
    ({"static_override": {"Content-Security-Policy": "default-src 'self'"}},
     "static_headers"),
    ({"index_no_cache": False}, "static_headers"),
    ({"asset_immutable": False}, "static_headers"),
    ({"spa_fallback": False}, "spa_fallback"),
    ({"pointer_no_cache": False}, "snapshot_static"),
    ({"api_meta": b"{\"api_version\":\"v0\"}"}, "snapshot_matches"),
    ({"edge_snapshot_id": "s-other"}, "edge_health"),
    ({"allowlist": False}, "api_allowlist"),
    ({"api_no_store": False}, "api_headers"),
    ({"limits": False}, "limits"),
    ({"wake": False}, "wake"),
    ({"ops_gate": False}, "ops_budget"),
    ({"origin_lock": False}, "origin_lock"),
    ({"governor": False}, "governor_denial"),
])
def test_each_violation_fails_its_check(probe, stub_edge, assets, monkeypatch, capsys,
                                        overrides, failing_check):
    code, failed = _run(probe, stub_edge, assets, monkeypatch, capsys, **overrides)
    assert failed == [failing_check] and code == 1


def test_a_missing_edge_cache_fails_its_check(probe, stub_edge, assets, monkeypatch, capsys):
    """Separate: the probe waits up to 5 s for the edge cache to answer."""
    code, failed = _run(probe, stub_edge, assets, monkeypatch, capsys, edge_cache=False)
    assert failed == ["kv_cache"] and code == 1


def test_the_expected_static_headers_are_the_files_star_rule(probe, tmp_path):
    expected = probe.site_headers()
    assert expected == {name.lower(): value for name, value in _site_headers().items()}
    assert expected["referrer-policy"] == "no-referrer"
    bad = tmp_path / "_headers"
    bad.write_text("/*\n  X-Content-Type-Options: nosniff\n")
    with pytest.raises(SystemExit, match="referrer-policy"):
        probe.site_headers(bad)
