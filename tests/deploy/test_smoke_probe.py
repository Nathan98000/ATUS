"""deploy/stack/smoke_probe.py passes a compliant API and fails a non-compliant one.

The probe runs against a small in-process HTTP stub that answers like the
Phase 5 API (edge auth, envelopes, fixture numbers), so the probe's own logic
is tested without Docker.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import REPO_ROOT

SECRET = "stub-edge-secret"


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

        def _send(self, status: int, body: dict, **headers: str) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Request-ID", "stub-request-id")
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            return (
                not behaviour["edge_auth"]
                or self.headers.get("X-ATUS-Edge-Auth") == SECRET
            )

        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path == "/health":
                self._send(200, {"status": "ok", "build": behaviour["build"]})
            elif self.path == "/docs":
                self._send(404 if behaviour["docs_off"] else 200, _envelope("not_found"))
            elif not self._authorized():
                self._send(403, _envelope("forbidden_origin"))
            elif self.path == "/api/v1/meta":
                self._send(200, {"api_version": "v1"}, X_Data_Version="test:1")
            else:
                self._send(404, _envelope("not_found"))

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not self._authorized():
                self._send(403, _envelope("forbidden_origin"))
            elif body["activity"]["include"] == ["999999"]:
                self._send(422, _envelope("unknown_activity"))
            else:
                self._send(200, {"estimate": {"value": behaviour["value"]}}, X_Cache="miss")

    return Handler


@pytest.fixture
def stub_api():
    servers = []

    def start(**overrides) -> str:
        behaviour = {
            "edge_auth": True, "docs_off": True, "build": "abc123", "value": 75.0, **overrides
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _stub_handler(behaviour))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _run(probe, base_url: str, monkeypatch, *extra: str) -> int:
    monkeypatch.setenv("ATUS_SMOKE_EDGE_SECRET", SECRET)
    return probe.main(["--base-url", base_url, "--timeout", "5", *extra])


def test_compliant_api_passes(probe, stub_api, monkeypatch, capsys):
    url = stub_api()
    assert _run(probe, url, monkeypatch, "--fixture", "--expect-build", "abc123") == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out and "smoke: 0 failed" in out
    assert SECRET not in out


@pytest.mark.parametrize(
    ("overrides", "failing_check"),
    [
        ({"edge_auth": False}, "origin_lock"),  # direct hits are not refused
        ({"docs_off": False}, "docs_off"),
        ({"value": 74.0}, "analysis"),  # wrong fixture number
        ({"build": "other"}, "build"),
    ],
)
def test_each_violation_fails_its_check(
    probe, stub_api, monkeypatch, capsys, overrides, failing_check
):
    url = stub_api(**overrides)
    assert _run(probe, url, monkeypatch, "--fixture", "--expect-build", "abc123") == 1
    failed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("[FAIL]")]
    assert len(failed) == 1 and failed[0].split()[1] == failing_check


def test_skip_protection_checks_reports_them_as_skipped(probe, stub_api, monkeypatch, capsys):
    url = stub_api(edge_auth=False, docs_off=False)
    assert _run(probe, url, monkeypatch, "--skip-protection-checks") == 0
    out = capsys.readouterr().out
    assert "[SKIP] origin_lock" in out and "[SKIP] docs_off" in out


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
