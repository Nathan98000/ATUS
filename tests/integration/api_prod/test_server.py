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
import socket
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
def running(app, settings):
    """Serve ``app`` with the production uvicorn options on a loopback port."""
    port = _free_port()
    options = serve.uvicorn_options(settings, host="127.0.0.1", port=port)
    options.update(app=app, factory=False)
    server = uvicorn.Server(uvicorn.Config(**options))
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
        "ATUS_EDGE_SECRETS": SECRET,
        "ATUS_LOG_LEVEL": "INFO",
    })


def raw_request(port: int, data: bytes, *, read: bool = True) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(data)
        if not read:
            return b""
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


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
            assert httpx.get(f"{base}/docs", headers=edge).status_code == 404
            assert httpx.get(f"{base}/openapi.json", headers=edge).status_code == 404

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
