"""The local stack's edge pieces (docker-compose.prod.yml, profile `edge`).

Without Docker:

- deploy/edge/origin-relay.mjs, the loopback relay that lets the Worker reach
  the API container exactly as deployed (plain HTTP only on loopback), moves
  bytes both ways unchanged and refuses to start without a target (needs node);
- deploy/stack/edge_probe.py's visitor addresses and checks of the
  governor's denial shape;
- scripts/smoke_stack.sh refuses --e2e without --edge before touching Docker.

With ATUS_DOCKER_TESTS=1 (and node + the frontend's node_modules), the whole
edge cycle: scripts/smoke_stack.sh --edge on the image under test: the
serving DB, the release binding, the SPA build, the static snapshot, the
Worker under wrangler dev, and every check of deploy/stack/edge_probe.py.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from .conftest import REPO_ROOT
from .test_stack_smoke import image_build_sha

RELAY = REPO_ROOT / "deploy" / "edge" / "origin-relay.mjs"


def _probe_module():
    spec = importlib.util.spec_from_file_location(
        "edge_probe", REPO_ROOT / "deploy" / "stack" / "edge_probe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def node():
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not installed")
    return path


class EchoServer:
    """Answers every connection with b'echo:' + whatever it received, then closes."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                conn.sendall(b"echo:" + data)

    def close(self):
        self.sock.close()


def _connect_retrying(port: int, timeout: float = 10.0) -> socket.socket:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


class TestOriginRelay:
    def test_bytes_pass_both_ways_unchanged(self, node):
        echo = EchoServer()
        listen = _free_port()
        env = dict(os.environ, ATUS_EDGE_RELAY_LISTEN=f"127.0.0.1:{listen}",
                   ATUS_EDGE_RELAY_TARGET=f"127.0.0.1:{echo.port}")
        relay = subprocess.Popen([node, str(RELAY)], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            payload = bytes(range(256)) * 400 + b"\n"  # binary, larger than one chunk
            with _connect_retrying(listen) as sock:
                sock.sendall(payload)
                received = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    received += chunk
            assert received == b"echo:" + payload
        finally:
            relay.terminate()
            out, _ = relay.communicate(timeout=10)
            echo.close()
        assert f"127.0.0.1:{listen} -> 127.0.0.1:{echo.port}" in out

    def test_an_unreachable_api_closes_the_client(self, node):
        listen, dead = _free_port(), _free_port()
        env = dict(os.environ, ATUS_EDGE_RELAY_LISTEN=f"127.0.0.1:{listen}",
                   ATUS_EDGE_RELAY_TARGET=f"127.0.0.1:{dead}")
        relay = subprocess.Popen([node, str(RELAY)], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        try:
            with _connect_retrying(listen) as sock:
                sock.sendall(b"GET /health HTTP/1.1\r\n\r\n")
                assert sock.recv(10) == b""  # closed: the Worker sees a connection failure
            assert relay.poll() is None  # the relay itself keeps running
        finally:
            relay.terminate()
            relay.communicate(timeout=10)

    def test_refuses_to_start_without_a_target(self, node):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ATUS_EDGE_RELAY")}
        result = subprocess.run([node, str(RELAY)], env=env, capture_output=True, text=True,
                                timeout=10, check=False)
        assert result.returncode == 2
        assert "ATUS_EDGE_RELAY_TARGET must be host:port" in result.stderr


class TestEdgeProbeHelpers:
    def test_visitors_are_benchmarking_addresses_with_their_own_networks(self):
        probe = _probe_module()
        benchmarking = ipaddress.ip_network("198.18.0.0/15")
        addresses = [probe.visitor(network, 7)["CF-Connecting-IP"] for network in range(300)]
        assert all(ipaddress.ip_address(a) in benchmarking for a in addresses)
        networks = {ipaddress.ip_network(f"{a}/24", strict=False) for a in addresses}
        assert len(networks) == 300
        # the E2E suite's visitors (e2e/fixtures.ts) live in 198.19.0.0/16, the
        # probe's in 198.18.0.0/16, so the governor check never spends theirs
        assert all(a.startswith("198.18.") for a in addresses[:256])

    def test_midnight_utc(self):
        probe = _probe_module()
        assert probe._midnight_utc("2026-10-22T00:00:00.000Z")
        assert probe._midnight_utc("2026-10-22T00:00:00+00:00")
        assert not probe._midnight_utc("2026-10-22T00:00:01Z")
        assert not probe._midnight_utc("2026-10-22T00:00:00+02:00")
        assert not probe._midnight_utc(None) and not probe._midnight_utc("soon")

    def test_analysis_bodies_are_the_spas_sorted_compact_json(self):
        probe = _probe_module()
        body = probe.analysis_body("x")
        assert body == b'{"activity":{"include":["010101"],"label":"x"},"years":[2023]}'


def test_smoke_stack_refuses_e2e_without_edge():
    result = subprocess.run([str(REPO_ROOT / "scripts" / "smoke_stack.sh"), "--e2e"],
                            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
                            check=False)
    assert result.returncode == 2
    assert "--e2e needs --edge" in result.stderr


def test_edge_cycle(api_image, docker_prefix, node):
    """The public origin, end to end (opt-in: ATUS_DOCKER_TESTS=1), with the
    Playwright projects in E2E_PROJECTS through it (default: `mobile`, the
    shortest; the full stack-mode run is scripts/smoke_stack.sh --edge --e2e)."""
    if not (REPO_ROOT / "frontend" / "node_modules").is_dir():
        pytest.skip("the frontend's node_modules are not installed (npm ci in frontend/)")
    project = f"{docker_prefix}-edge"
    env = dict(os.environ, ATUS_STACK_IMAGE=api_image, ATUS_BUILD_SHA=image_build_sha(api_image),
               E2E_PROJECTS=os.environ.get("E2E_PROJECTS", "mobile"))
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "smoke_stack.sh"), "--project", project,
         "--no-build", "--edge", "--e2e"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=1800, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-6000:]
    assert "smoke: 0 failed" in output and "edge probe: 0 failed" in output
    assert re.search(r"^\s+\d+ passed", output, re.MULTILINE), output[-3000:]
    # everything is removed afterwards (regression: the E2E loop once
    # overwrote the compose project name, so the stack outlived the run)
    leftovers = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
         "--format", "{{.Names}}"], capture_output=True, text=True, check=True).stdout.split()
    assert leftovers == []
    for check in ("spa_fallback", "static_headers", "snapshot_static", "snapshot_matches",
                  "api_allowlist", "api_headers", "kv_cache", "governor_denial",
                  "origin_lock"):
        assert f"[PASS] {check}" in output, check
    # the snapshot builder skipped only what the fixture cannot answer
    skipped = [line for line in output.splitlines() if line.startswith("snapshot: skipped")]
    assert all("HTTP 422" in line for line in skipped)
