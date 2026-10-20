"""deploy/stack/smoke_probe.py passes a compliant API and fails a non-compliant one.

The probe runs against a small in-process HTTP stub that answers like the
Phase 5 API contract (edge auth, envelopes, headers, limits, ops gating,
fixture numbers), so the probe's own logic is tested without Docker. Each
behaviour can be switched off to show that exactly one check notices.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import REPO_ROOT

SECRET = "stub-edge-secret"
OPS_TOKEN = "stub-ops-token"
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
COMPLIANT = {
    "edge_auth": True, "docs_off": True, "build": "abc123", "value": 75.0,
    "security_headers": True, "server_header": False, "no_store": True,
    "analysis_headers": True, "echo_request_id": True, "body_limit": True,
    "media_type": True, "ops_gate": True,
    "data_version": "0325:load-7-20261020T120000Z-a1b2c3",
}


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location(
        "smoke_probe", REPO_ROOT / "deploy" / "stack" / "smoke_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _envelope(code: str) -> dict:
    return {"error": {"code": code, "message": code.replace("_", " "), "details": None}}


def _stub_handler(behaviour: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep pytest output clean
            pass

        def send_response(self, code, message=None):
            # BaseHTTPRequestHandler always adds `Server: BaseHTTP/...`; the
            # contract says no server header, so it is opt-in here.
            self.send_response_only(code, message)
            if behaviour["server_header"]:
                self.send_header("Server", "stub")

        def _authorized(self) -> bool:
            return (
                not behaviour["edge_auth"]
                or self.headers.get("X-ATUS-Edge-Auth") == SECRET
            )

        def _send(self, status: int, body: dict, **headers: str) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            sent_id = self.headers.get("X-Request-ID", "")
            if behaviour["echo_request_id"] and self._authorized() and REQUEST_ID.match(sent_id):
                self.send_header("X-Request-ID", sent_id)
            else:
                self.send_header("X-Request-ID", f"api-{uuid.uuid4().hex}")
            if behaviour["security_headers"]:
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; frame-ancestors 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            if status >= 400 and behaviour["no_store"]:
                self.send_header("Cache-Control", "no-store")
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(raw)

        def _ops(self) -> None:
            bearer = self.headers.get("Authorization") == f"Bearer {OPS_TOKEN}"
            if behaviour["ops_gate"] and not bearer:
                self._send(404, _envelope("not_found"))
            else:
                self._send(200, {"status": "ready"})

        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path == "/health":
                self._send(200, {"status": "ok", "build": behaviour["build"]})
            elif self.path == "/docs":
                self._send(404 if behaviour["docs_off"] else 200, _envelope("not_found"))
            elif not self._authorized():
                self._send(403, _envelope("forbidden_origin"))
            elif self.path in ("/ready", "/metrics"):
                self._ops()
            elif self.path == "/api/v1/meta":
                self._send(200, {"api_version": "v1"}, X_Data_Version=behaviour["data_version"])
            else:
                self._send(404, _envelope("not_found"))

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if not self._authorized():
                self._send(403, _envelope("forbidden_origin"))
            elif behaviour["body_limit"] and len(raw) > 65536:
                self._send(413, _envelope("payload_too_large"))
            elif (behaviour["media_type"]
                  and self.headers.get("Content-Type") != "application/json"):
                self._send(415, _envelope("unsupported_media_type"))
            elif json.loads(raw)["activity"]["include"] == ["999999"]:
                self._send(422, _envelope("unknown_activity"))
            else:
                headers = {"X_Cache": "miss"}
                if behaviour["analysis_headers"]:
                    headers.update(
                        X_Response_Format="1", X_Analytics_Version="2.1.0",
                        X_Analysis_Key="k" * 16, X_Data_Version=behaviour["data_version"],
                        Server_Timing="db;dur=12.5, app;dur=3.1",
                    )
                self._send(200, {"estimate": {"value": behaviour["value"]}}, **headers)

    return Handler


@pytest.fixture
def stub_api():
    servers = []

    def start(**overrides) -> str:
        behaviour = {**COMPLIANT, **overrides}
        server = ThreadingHTTPServer(("127.0.0.1", 0), _stub_handler(behaviour))
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _run(probe, base_url: str, monkeypatch, *extra: str, ops_token: bool = True) -> int:
    monkeypatch.setenv("ATUS_SMOKE_EDGE_SECRET", SECRET)
    if ops_token:
        monkeypatch.setenv("ATUS_SMOKE_OPS_TOKEN", OPS_TOKEN)
    else:
        monkeypatch.delenv("ATUS_SMOKE_OPS_TOKEN", raising=False)
    return probe.main(["--base-url", base_url, "--timeout", "5", *extra])


def _failed(capsys) -> list[str]:
    lines = capsys.readouterr().out.splitlines()
    return [line.split()[1] for line in lines if line.startswith("[FAIL]")]


def test_compliant_api_passes(probe, stub_api, monkeypatch, capsys):
    url = stub_api()
    assert _run(probe, url, monkeypatch, "--fixture", "--expect-build", "abc123") == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out and "[SKIP]" not in out and "smoke: 0 failed" in out
    assert SECRET not in out and OPS_TOKEN not in out
    for name in ("security_headers", "analysis_headers", "request_id", "body_limit",
                 "media_type", "ops_gate", "data_version"):
        assert f"[PASS] {name}" in out


@pytest.mark.parametrize(
    ("overrides", "failing_check"),
    [
        # Direct hits are not refused, so nothing tells them apart from the edge.
        ({"edge_auth": False}, ["origin_lock", "request_id"]),
        ({"docs_off": False}, "docs_off"),
        ({"value": 74.0}, "analysis"),  # wrong fixture number
        ({"build": "other"}, "build"),
        ({"security_headers": False}, "security_headers"),
        ({"server_header": True}, "security_headers"),
        ({"no_store": False}, "error_no_store"),
        ({"analysis_headers": False}, "analysis_headers"),
        ({"echo_request_id": False}, "request_id"),
        ({"body_limit": False}, "body_limit"),
        ({"media_type": False}, "media_type"),
        ({"ops_gate": False}, "ops_gate"),  # /ready and /metrics open to anyone
        ({"data_version": "0325:run1"}, "data_version"),  # the pre-Phase-5 format (F06)
    ],
)
def test_each_violation_fails_its_check(
    probe, stub_api, monkeypatch, capsys, overrides, failing_check
):
    url = stub_api(**overrides)
    assert _run(probe, url, monkeypatch, "--fixture", "--expect-build", "abc123") == 1
    expected = failing_check if isinstance(failing_check, list) else [failing_check]
    assert _failed(capsys) == expected


def test_ops_gate_without_a_token_checks_only_the_404(probe, stub_api, monkeypatch, capsys):
    url = stub_api()
    assert _run(probe, url, monkeypatch, ops_token=False) == 0
    out = capsys.readouterr().out
    assert "/ready without bearer=404" in out and "with bearer" not in out


def test_skip_protection_checks_reports_them_as_skipped(probe, stub_api, monkeypatch, capsys):
    url = stub_api(edge_auth=False, docs_off=False, security_headers=False, server_header=True,
                   no_store=False, analysis_headers=False, echo_request_id=False,
                   body_limit=False, media_type=False, ops_gate=False, data_version="0325:run1")
    assert _run(probe, url, monkeypatch, "--skip-protection-checks") == 0
    out = capsys.readouterr().out
    for name in ("origin_lock", "docs_off", "security_headers", "error_no_store",
                 "analysis_headers", "request_id", "body_limit", "media_type", "ops_gate",
                 "data_version"):
        assert f"[SKIP] {name}" in out


def test_unreachable_api_fails(probe, monkeypatch):
    with pytest.raises(OSError):
        _run(probe, "http://127.0.0.1:9", monkeypatch)


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        (_envelope("unknown_activity"), True),
        ({"error": {"code": "x", "message": "m"}}, False),  # details missing
        ({"error": {"code": "x", "message": "m", "details": None}, "extra": 1}, False),
        ({"detail": "Not Found"}, False),  # FastAPI's default shape
        (None, False),
    ],
)
def test_error_envelope_shape(probe, payload, ok):
    assert probe.is_error_envelope(payload) is ok


@pytest.mark.parametrize(
    ("version", "ok"),
    [
        ("0325:load-7-20261020T120000Z-a1b2c3", True),
        ("0325:serving-20261020T120000Z-0a1b2c3d:0123456789ab", True),
        ("0325:run1", False),  # Phase 1-4 format: the same on every fresh DB
        ("0325:load-7-20261020T120000Z", False),  # no random suffix
        ("0325:serving-20261020T120000Z-0a1b2c3d", False),  # no content hash
        ("", False),
    ],
)
def test_data_version_format(probe, version, ok):
    assert bool(probe.DATA_VERSION.match(version)) is ok
