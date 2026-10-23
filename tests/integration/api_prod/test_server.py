"""The API as it runs in production: a real uvicorn server started with the
production options (``atus_pipeline.api.serve``), over real sockets.

Covers what an in-process client cannot: the ``server`` header, proxy-header
handling, chunked request bodies, and a client that disconnects while its
analysis is computing (the computation finishes and is cached; the
disconnect is logged).
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import httpx
import psycopg
import pytest
import uvicorn

from atus_pipeline.analytics import AnalysisEngine
from atus_pipeline.api import serve
from atus_pipeline.api.app import create_app
from atus_pipeline.api.dependencies import get_engine_factory
from atus_pipeline.config import load_settings
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings

pytestmark = pytest.mark.integration

SECRET = "p" * 48
PORTS = range(8501, 8520)  # this workstream's reserved range
BODY = {"activity": {"include": ["010101"]}, "years": [2023]}


def _free_port() -> int:
    for port in PORTS:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 8501-8519")


@contextmanager
def running(app, settings, servers: list | None = None):
    """Serve ``app`` with the production uvicorn options on a loopback port
    (the ``uvicorn.Server`` is appended to ``servers`` when given)."""
    port = _free_port()
    options = serve.uvicorn_options(settings, host="127.0.0.1", port=port)
    options.update(app=app, factory=False)
    server = uvicorn.Server(uvicorn.Config(**options))
    if servers is not None:
        servers.append(server)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.should_exit = True
        thread.join(30)


@pytest.fixture(scope="module")
def data_settings(tmp_path_factory, migrated_db):
    settings = analytics_settings(tmp_path_factory.mktemp("api_prod"), migrated_db)
    with psycopg.connect(settings.database_url) as conn:
        load_all(conn, settings)
    return settings


@pytest.fixture
def production_settings(data_settings):
    return load_settings({
        "ATUS_ENV": "production",
        "ATUS_DATABASE_URL": data_settings.database_url,
        # the local test server has no TLS: the local-stack escape hatch
        "ATUS_DATABASE_TLS": "local-stack-only",
        "ATUS_EDGE_SECRETS": SECRET,
        "ATUS_LOG_LEVEL": "INFO",
    })


def raw_request(port: int, data: bytes, *, read: bool = True) -> bytes:
    """Send ``data`` and read the reply until the server closes the connection.

    A server that answers before reading the whole request (the 413 for an
    over-cap body) closes with the rest of the body unread, so its kernel
    resets the connection rather than closing it cleanly: the reset can cut
    the send short or arrive after the reply. Once the whole reply is in, the
    reset is that close; before, it is an error. (Taking it for an error
    either way made the chunked 413 below flaky in CI, 2026-10-23.)"""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        try:
            sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            if not read:
                raise
        if not read:
            return b""
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                if not _whole_reply(b"".join(chunks)):
                    raise
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _whole_reply(reply: bytes) -> bool:
    """Whether ``reply`` holds a complete response: its head and as many body
    bytes as its Content-Length."""
    head, separator, body = reply.partition(b"\r\n\r\n")
    length = re.search(rb"(?im)^content-length:[ \t]*(\d+)[ \t]*\r?$", head)
    return bool(separator) and length is not None and len(body) >= int(length[1])


class TestProductionServer:
    def test_edge_auth_headers_and_docs(self, production_settings):
        app = create_app(production_settings)
        with running(app, production_settings) as (base, _):
            health = httpx.get(f"{base}/health")
            assert health.status_code == 200
            assert "server" not in health.headers
            assert health.headers["Strict-Transport-Security"] == "max-age=31536000"
            assert httpx.head(f"{base}/health").status_code == 200

            direct = httpx.get(f"{base}/api/v1/meta")
            assert direct.status_code == 403
            assert direct.json()["error"]["code"] == "forbidden_origin"

            edge = {"X-ATUS-Edge-Auth": SECRET, "X-ATUS-Client-IP": "198.51.100.20"}
            meta = httpx.get(f"{base}/api/v1/meta", headers=edge)
            assert meta.status_code == 200
            assert meta.json()["data"]["release"] == "0325"
            for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
                assert httpx.get(f"{base}{path}", headers=edge).status_code == 404, path

    def test_forwarded_headers_never_change_the_client(self, production_settings):
        """Proxy headers are off: with a valid secret the client is
        X-ATUS-Client-IP, and X-Forwarded-For cannot override it."""
        from dataclasses import replace

        from atus_pipeline.config import RateLimit

        settings = replace(production_settings, api_rate_requests=RateLimit(2, 60.0))
        app = create_app(settings)
        with running(app, settings) as (base, _):
            statuses = []
            for i in range(4):
                statuses.append(httpx.get(f"{base}/api/v1/population/metadata", headers={
                    "X-ATUS-Edge-Auth": SECRET, "X-ATUS-Client-IP": "198.51.100.30",
                    "X-Forwarded-For": f"203.0.113.{i}", "CF-Connecting-IP": f"203.0.113.{i}",
                }).status_code)
        assert statuses == [200, 200, 429, 429]

    def test_body_caps_over_real_sockets(self, production_settings):
        app = create_app(production_settings)
        head = (
            "POST /api/v1/analysis/estimate HTTP/1.1\r\nHost: localhost\r\n"
            f"X-ATUS-Edge-Auth: {SECRET}\r\nContent-Type: application/json\r\n"
        )
        with running(app, production_settings) as (_, port):
            declared = raw_request(
                port, (head + "Content-Length: 70000\r\nConnection: close\r\n\r\n").encode()
            )
            assert declared.startswith(b"HTTP/1.1 413")
            assert b'"payload_too_large"' in declared

            chunk = b"8000\r\n" + b" " * 0x8000 + b"\r\n"
            chunked = (head + "Transfer-Encoding: chunked\r\n\r\n").encode() + chunk * 3
            streamed = raw_request(port, chunked + b"0\r\n\r\n")
            assert streamed.startswith(b"HTTP/1.1 413")
            assert b'"payload_too_large"' in streamed


class TestEdgeContractOverSockets:
    def test_odd_request_targets_get_envelopes_never_redirects(self, production_settings):
        """What reaches the socket unnormalized (dot segments, double and
        trailing slashes, an absolute-form target, bad percent-escapes): the
        answer is a JSON envelope or the route itself, never a 3xx, which the
        Worker would turn into a 502."""
        app = create_app(production_settings)
        targets = [
            "/api/v1/activities/../meta", "/api/v1/./meta", "//api/v1/meta", "/api/v1/meta/",
            "/api/v1/meta?%ZZ=1", "/api/v1/activities/%2e%2e", "/api", "/api/",
            "http://localhost/api/v1/meta", "/api/v1/analysis/estimate/",
        ]
        with running(app, production_settings) as (_, port):
            for target in targets:
                reply = raw_request(port, (
                    f"GET {target} HTTP/1.1\r\nHost: localhost\r\n"
                    f"X-ATUS-Edge-Auth: {SECRET}\r\nConnection: close\r\n\r\n").encode())
                head, _, body = reply.partition(b"\r\n\r\n")
                status = int(head.split()[1])
                assert not 300 <= status < 400, (target, head)
                headers = {line.split(b":", 1)[0].strip().lower(): line.split(b":", 1)[1].strip()
                           for line in head.split(b"\r\n")[1:]}
                assert headers[b"content-type"] == b"application/json", (target, head)
                if status >= 400:
                    assert json.loads(body)["error"]["code"], target


class TestConnectionBound:
    def test_connections_beyond_the_limit_are_shed_before_the_app(self, production_settings):
        """``limit_concurrency`` bounds what idle or slow clients can pin
        (audit F49): with the limit reached, uvicorn answers 503 itself."""
        from dataclasses import replace

        settings = replace(production_settings, api_threads=8)  # limit = 32
        limit = serve.limit_concurrency(settings)
        app = create_app(settings)
        with running(app, settings) as (base, port):
            idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(limit)]
            try:
                time.sleep(0.2)
                shed = httpx.get(f"{base}/health")
                assert shed.status_code == 503
                # Exactly what the edge Worker recognizes and maps to the
                # `overloaded` envelope (edge/src/proxy.ts): uvicorn's own
                # plain-text answer, no envelope, no Retry-After.
                assert shed.headers["content-type"] == "text/plain; charset=utf-8"
                assert shed.text == "Service Unavailable"
                assert "retry-after" not in shed.headers
            finally:
                for sock in idle:
                    sock.close()
            time.sleep(0.2)
            assert httpx.get(f"{base}/health").status_code == 200


class SlowEngine:
    def __init__(self, conn, **snapshot_parts):
        self._conn = conn
        self._engine = AnalysisEngine(conn, **snapshot_parts)

    def estimate(self, spec):
        self._conn.execute("SELECT pg_sleep(1.0)")
        return self._engine.estimate(spec)


class TestClientDisconnect:
    def test_abandoned_analysis_completes_is_cached_and_logged(self, data_settings, caplog):
        app = create_app(data_settings)
        app.dependency_overrides[get_engine_factory] = lambda: SlowEngine
        payload = json.dumps(BODY).encode()
        request = (
            "POST /api/v1/analysis/estimate HTTP/1.1\r\nHost: localhost\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload
        with caplog.at_level(logging.INFO, logger="atus.api"):
            with running(app, data_settings) as (base, port):
                with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                    sock.sendall(request)
                    time.sleep(0.3)  # the computation is now running
                # client gone; wait for the detached computation to finish
                deadline = time.monotonic() + 10
                runner = app.state.runner
                while runner.computations < 1 or runner._flights:
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                retry = httpx.post(f"{base}/api/v1/analysis/estimate", json=BODY)
        assert retry.status_code == 200
        assert retry.headers["X-Cache"] == "hit"
        assert retry.json()["estimate"]["value"] == pytest.approx(75.0)
        assert runner.computations == 1
        messages = [r.getMessage() for r in caplog.records]
        assert any("client disconnected before the response" in m for m in messages)
        assert any("client_disconnected=1" in m and "-> 200" in m for m in messages)
        # ... and counted on /metrics (F53); the retry was answered, not abandoned.
        assert app.state.metrics.disconnects.value("/api/v1/analysis/estimate") == 1


class HeavyEngine(SlowEngine):
    """Stands in for a heavy analysis (21-71 s at 0.25 CU, ADR-012)."""

    def estimate(self, spec):
        self._conn.execute("SELECT pg_sleep(4.0)")
        return self._engine.estimate(spec)


def _wait_for(condition, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


class TestHealthWhileRequestsWait:
    def test_health_answers_while_requests_wait_on_one_analysis(self, data_settings):
        """Review r3 (health-503-under-joiners): requests that wait on one
        running analysis (single-flight joiners) took no lane, no token and
        had no cap, so they filled uvicorn's ``limit_concurrency`` and the
        platform's health check on /health got uvicorn's plain 503 for as
        long as the analysis ran (Render stops routing after 15 s of failed
        checks and restarts the instance after 60 s). The app now serves at
        most ``max_inflight_requests`` requests besides /health at once and
        answers the rest 503 ``overloaded`` itself; uvicorn's bound lies
        above that, so /health always gets through."""
        from concurrent.futures import ThreadPoolExecutor
        from dataclasses import replace

        settings = replace(data_settings, api_threads=8)   # uvicorn's bound was 32
        sent = 40
        app = create_app(settings)
        app.dependency_overrides[get_engine_factory] = lambda: HeavyEngine
        with running(app, settings) as (base, _):
            assert httpx.get(f"{base}/health").status_code == 200
            with ThreadPoolExecutor(sent) as pool:
                replies = [pool.submit(httpx.post, f"{base}/api/v1/analysis/estimate",
                                       json=BODY, timeout=30) for _ in range(sent)]
                runner = app.state.runner
                _wait_for(lambda: runner.computations == 1 and runner.joins >= 25)
                time.sleep(0.5)                   # every request has arrived
                during = httpx.get(f"{base}/health")
                assert runner._flights, "the analysis already ended: no evidence"
                replies = [reply.result() for reply in replies]
            after = httpx.get(f"{base}/health")
        assert during.status_code == 200, (during.status_code, during.text)
        assert during.json()["status"] == "ok"
        assert after.status_code == 200
        served = [r for r in replies if r.status_code == 200]
        shed = [r for r in replies if r.status_code != 200]
        assert {r.headers["X-Cache"] for r in served} == {"miss", "join"}
        # Beyond the app's bound: its own envelope (Retry-After, request id),
        # which the edge refunds like any other shed request.
        assert shed, "no request was over the bound"
        for reply in shed:
            assert reply.status_code == 503
            assert reply.headers["content-type"] == "application/json"
            assert reply.json()["error"]["code"] == "overloaded"
            assert reply.headers["Retry-After"] == "10"
            assert reply.headers["X-Request-ID"]
            assert reply.headers["Server-Timing"].startswith("db;dur=0,")
        assert len(served) + len(shed) == sent


class TestShutdownDuringAnAnalysis:
    def test_requests_cut_off_by_the_graceful_window_get_the_overloaded_envelope(
            self, data_settings, monkeypatch, caplog):
        """Review r3 (sigterm-plain-500): on SIGTERM uvicorn waits the
        graceful window, then cancels every request task still running, and
        only then runs the app's lifespan shutdown. An analysis still
        computing, and every request joined to it, got uvicorn's plain-text
        500 (no envelope, no Retry-After, no request id; status "-" in the
        access line), which the edge took for a platform error. They now get
        503 ``overloaded`` ("server shutting down") from the app."""
        from concurrent.futures import ThreadPoolExecutor

        monkeypatch.setattr(serve, "GRACEFUL_SHUTDOWN_S", 1)   # 20 s in production
        app = create_app(data_settings)
        app.dependency_overrides[get_engine_factory] = lambda: HeavyEngine
        servers: list = []
        with caplog.at_level(logging.INFO, logger="atus.api"):
            with running(app, data_settings, servers) as (base, _):
                with ThreadPoolExecutor(2) as pool:
                    url = f"{base}/api/v1/analysis/estimate"
                    leader = pool.submit(httpx.post, url, json=BODY, timeout=30)
                    runner = app.state.runner
                    _wait_for(lambda: runner.computations == 1)
                    joiner = pool.submit(httpx.post, url, json=BODY, timeout=30)
                    _wait_for(lambda: runner.joins == 1)
                    servers[0].should_exit = True        # what uvicorn's SIGTERM handler does
                    replies = [leader.result(), joiner.result()]
        for reply in replies:
            assert reply.status_code == 503, (reply.status_code, reply.text)
            assert reply.headers["content-type"] == "application/json"
            error = reply.json()["error"]
            assert error["code"] == "overloaded"
            assert error["message"] == "The service is busy with other analyses. Retry shortly."
            assert reply.headers["Retry-After"] == "10"
            assert reply.headers["X-Request-ID"]
            assert reply.headers["Cache-Control"] == "no-store"
            assert reply.headers["X-Content-Type-Options"] == "nosniff"
        # The edge charges the leader what its computation spent before the
        # cut (about the 1 s window), the joiner nothing.
        leader_db = float(re.match(r"db;dur=([0-9.]+),", replies[0].headers["Server-Timing"])[1])
        assert leader_db >= 500, replies[0].headers["Server-Timing"]
        assert replies[1].headers["Server-Timing"].startswith("db;dur=0,")
        access = [r.getMessage() for r in caplog.records if r.name == "atus.api.access"]
        cut_off = [m for m in access if "/api/v1/analysis/estimate -> 503" in m]
        assert len(cut_off) == 2 and all("error=overloaded" in m for m in cut_off), access


OPS_TOKEN = "q" * 48


class TestProductionProcess:
    """``atus api`` with a production environment (on loopback, not the
    production 0.0.0.0): JSON logs on stderr (one access line per request,
    truncated client addresses, no uvicorn access line), the build on
    /health, operator endpoints behind the ops token, and Server-Timing."""

    def test_json_access_log_and_operator_endpoints(self, data_settings):
        port = _free_port()
        env = {
            **os.environ,
            "ATUS_ENV": "production",
            "ATUS_DATABASE_URL": data_settings.database_url,
            "ATUS_DATABASE_TLS": "local-stack-only",
            "ATUS_EDGE_SECRETS": SECRET,
            "ATUS_OPS_TOKEN": OPS_TOKEN,
            "ATUS_BUILD_SHA": "0123abcd",
            "ATUS_DATA_DIR": str(data_settings.data_dir),
            "PORT": str(port),
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "atus_pipeline", "api", "--host", "127.0.0.1"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    health = httpx.get(f"{base}/health")
                    break
                except httpx.TransportError:
                    assert process.poll() is None, process.stderr.read()
                    assert time.monotonic() < deadline, "server did not start"
                    time.sleep(0.1)
            edge = {"X-ATUS-Edge-Auth": SECRET, "X-ATUS-Client-IP": "198.51.100.77",
                    "X-Request-ID": "edge-request-0001"}
            meta = httpx.get(f"{base}/api/v1/meta", headers=edge)
            estimate = httpx.post(f"{base}/api/v1/analysis/estimate", json=BODY, headers=edge)
            hidden = httpx.get(f"{base}/metrics", headers=edge)
            metrics = httpx.get(f"{base}/metrics",
                                headers={"Authorization": f"Bearer {OPS_TOKEN}"})
            ready = httpx.get(f"{base}/ready", headers={"Authorization": f"Bearer {OPS_TOKEN}"})
        finally:
            process.terminate()
            _, stderr = process.communicate(timeout=30)
        assert health.json() == {"status": "ok", "build": "0123abcd",
                                 "expected_data_version": None}
        assert meta.status_code == 200 and estimate.status_code == 200
        assert re.match(r"^db;dur=[0-9.]+, app;dur=[0-9.]+$", estimate.headers["Server-Timing"])
        assert hidden.status_code == 404
        assert metrics.status_code == 200 and 'atus_build_info{build="0123abcd"} 1' in metrics.text
        assert ready.status_code == 200 and ready.json()["build"] == "0123abcd"

        entries = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
        assert entries, stderr[-2000:]
        assert all(set(e) >= {"ts", "level", "logger", "message"} for e in entries)
        access = [e for e in entries if e.get("event") == "access"]
        by_route = {e["route"]: e for e in access}
        assert by_route["/api/v1/analysis/estimate"]["request_id"] == "edge-request-0001"
        assert by_route["/api/v1/analysis/estimate"]["client_ip_prefix"] == "198.51.100.0/24"
        assert by_route["/api/v1/analysis/estimate"]["cache"] == "miss"
        assert by_route["/api/v1/meta"]["db_statements"] == 11  # the snapshot read
        assert {e["route"] for e in access} >= {"/health", "/metrics", "/ready"}
        assert "198.51.100.77" not in stderr
        assert not [e for e in entries if e["logger"] == "uvicorn.access"]
        assert all(not line or line.startswith("{") for line in stderr.splitlines()), stderr
