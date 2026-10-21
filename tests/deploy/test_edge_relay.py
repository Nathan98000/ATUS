"""deploy/edge/relay.mjs: the loopback relay that lets the local Worker reach the API container.

The Worker accepts plain HTTP only to a loopback origin (edge/src/upstream.ts),
so the stack's edge container relays 127.0.0.1:10000 to api:10000. Here the
relay forwards to a local HTTP server; it needs Node (skipped without it).
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .conftest import REPO_ROOT

RELAY = REPO_ROOT / "deploy" / "edge" / "relay.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def upstream():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = f"upstream saw {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                     daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_the_relay_passes_requests_through(upstream):
    port = free_port()
    relay = subprocess.Popen(["node", str(RELAY), f"127.0.0.1:{port}", f"127.0.0.1:{upstream}"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert relay.stdout.readline().startswith(f"relay: 127.0.0.1:{port} -> 127.0.0.1:")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as reply:
            assert reply.read() == b"upstream saw /health"
    finally:
        relay.terminate()
        relay.wait(timeout=10)


@pytest.mark.parametrize("args", [["0.0.0.0:10000", "api:10000"], ["127.0.0.1", "api:10000"],
                                  ["127.0.0.1:10000", "api"]])
def test_the_relay_listens_on_loopback_only_and_validates_its_arguments(args):
    result = subprocess.run(["node", str(RELAY), *args], capture_output=True, text=True,
                            timeout=30, check=False)
    assert result.returncode == 2, result.stderr


def test_the_stack_wires_the_relay():
    entrypoint = (REPO_ROOT / "deploy" / "edge" / "entrypoint.sh").read_text()
    dockerfile = (REPO_ROOT / "deploy" / "edge" / "Dockerfile").read_text()
    assert "atus-edge-relay.mjs" in entrypoint and "ATUS_EDGE_UPSTREAM" in entrypoint
    assert "deploy/edge/relay.mjs /usr/local/lib/atus-edge-relay.mjs" in dockerfile
