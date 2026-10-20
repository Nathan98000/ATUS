"""Statements per request as PostgreSQL itself logs them (audit F14).

Starts a throwaway ``postgres:16`` container (``atus-wsa-stmt``, host port
5461) with ``log_statement=all``, loads the analytics fixture dataset, and
serves the API in-process against it. Between requests a separate
connection logs a marker statement; the API's statements for one request are
the ``app=atus-api`` statement lines between two markers. Each backend
writes its log lines to the server's stderr as it executes, and the next
marker is sent only after the response (the API ends its transactions
before responding), so the count is exact.

The server-side count is the API's own count (``db_statements``: the pool's
connection check, the limits, the queries) plus the ``BEGIN`` and ``COMMIT``
psycopg sends around each transaction. Skipped when Docker is unavailable.
"""

from __future__ import annotations

import logging
import re
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

from atus_pipeline.api.app import create_app
from atus_pipeline.database.migrate import apply_migrations
from atus_pipeline.loading.loader import load_all
from tests.integration.analytics_dataset import analytics_settings
from tests.integration.conftest import MIGRATIONS_DIR

pytestmark = pytest.mark.integration

CONTAINER = "atus-wsa-stmt"
PORT = 5461
IMAGE = "postgres:16"
URL = f"postgresql://atus:atus@127.0.0.1:{PORT}/atus"
ESTIMATE = "/api/v1/analysis/estimate"
# One logged statement of the API (not the DETAIL/continuation lines).
_API_STATEMENT = re.compile(r" app=atus-api LOG:  (statement|execute [^:]*): ?(.*)$")


def _docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _port_free(port: int) -> bool:
    """No listener on the port (connections in TIME_WAIT do not count)."""
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@pytest.fixture(scope="module")
def statement_db(tmp_path_factory):
    if not _docker_available():
        pytest.skip("Docker is not available")
    _docker("rm", "-f", CONTAINER)  # a leftover of an interrupted run (ours by name)
    if not _port_free(PORT):
        pytest.skip(f"port {PORT} is in use")
    started = _docker(
        "run", "-d", "--rm", "--name", CONTAINER, "-p", f"127.0.0.1:{PORT}:5432",
        "-e", "POSTGRES_USER=atus", "-e", "POSTGRES_PASSWORD=atus", "-e", "POSTGRES_DB=atus",
        IMAGE, "-c", "log_statement=all", "-c", "log_line_prefix=%m [%p] app=%a ",
        timeout=300,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start {IMAGE}: {started.stderr.strip()[:200]}")
    try:
        deadline = time.monotonic() + 90
        while True:
            try:
                with psycopg.connect(URL, connect_timeout=2):
                    break
            except psycopg.OperationalError:
                assert time.monotonic() < deadline, "postgres:16 container did not start"
                time.sleep(0.5)
        settings = analytics_settings(tmp_path_factory.mktemp("stmt"), URL)
        with psycopg.connect(URL) as conn:
            apply_migrations(conn, MIGRATIONS_DIR)
            load_all(conn, settings)
        yield settings
    finally:
        _docker("rm", "-f", CONTAINER)


class StatementLog:
    """Reads the container's log between markers."""

    def __init__(self) -> None:
        self._marker_conn = psycopg.connect(
            URL, autocommit=True, application_name="atus-test-marker"
        )

    def mark(self) -> str:
        marker = f"marker-{uuid.uuid4().hex}"
        self._marker_conn.execute(f"SELECT '{marker}'")
        deadline = time.monotonic() + 30
        while marker not in _docker("logs", CONTAINER).stderr:
            assert time.monotonic() < deadline, "marker never reached the log"
            time.sleep(0.1)
        return marker

    def api_statements(self, start: str, end: str) -> list[str]:
        log = _docker("logs", CONTAINER).stderr
        between = log[log.index(start):log.index(end)]
        return [m.group(2) for line in between.splitlines()
                if (m := _API_STATEMENT.search(line))]

    def close(self) -> None:
        self._marker_conn.close()


@contextmanager
def serving(settings):
    app = create_app(replace(settings, api_snapshot_ttl_s=600.0))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


SLEEP = {"activity": {"include": ["010101"]}, "years": [2023]}
COMPARE = {**SLEEP, "group_a": {"sex": "male"}, "group_b": {"sex": "female"}}

# (name, request, expected statements in PostgreSQL's log)
DOCUMENTED = [
    # the first data request reads the snapshot: BEGIN, check, limits,
    # version, run, years, respondents, lexicon, lowering probe, the
    # resolver's 3 lexicon reads, COMMIT
    ("meta, snapshot read", lambda c: c.get("/api/v1/meta"), 13),
    ("meta, fresh snapshot", lambda c: c.get("/api/v1/meta"), 0),
    ("activities", lambda c: c.get("/api/v1/activities", params={"search": "sleep"}), 0),
    ("activity detail", lambda c: c.get("/api/v1/activities/0101"), 0),
    # BEGIN, check, limits, statistics query, data-version recheck, COMMIT
    ("estimate, uncached", lambda c: c.post(ESTIMATE, json=SLEEP), 6),
    ("estimate, cache hit", lambda c: c.post(ESTIMATE, json=SLEEP), 0),
    ("trend, uncached", lambda c: c.post("/api/v1/analysis/trend",
                                         json={**SLEEP, "years": [2019, 2023]}), 6),
    ("compare, uncached", lambda c: c.post("/api/v1/analysis/compare", json=COMPARE), 7),
    ("compare, cache hit", lambda c: c.post("/api/v1/analysis/compare", json=COMPARE), 0),
    ("422 validation", lambda c: c.post(ESTIMATE, json={**SLEEP, "years": []}), 0),
    ("422 unknown activity", lambda c: c.post(
        ESTIMATE, json={**SLEEP, "activity": {"include": ["999999"]}}), 0),
    ("422 2020 multiyear", lambda c: c.post(ESTIMATE, json={**SLEEP, "years": [2020]}), 0),
    ("400 malformed", lambda c: c.post(
        ESTIMATE, content=b"{", headers={"Content-Type": "application/json"}), 0),
    ("population metadata", lambda c: c.get("/api/v1/population/metadata"), 0),
    ("health", lambda c: c.get("/health"), 0),
]


class TestServerSideStatementCounts:
    def test_documented_counts(self, statement_db, caplog):
        log = StatementLog()
        observed, mismatched_accounting = {}, []
        try:
            with serving(statement_db) as client:
                for name, send, _ in DOCUMENTED:
                    start = log.mark()
                    caplog.clear()
                    with caplog.at_level(logging.INFO, logger="atus.api"):
                        response = send(client)
                    assert response.status_code < 500, (name, response.text)
                    statements = log.api_statements(start, log.mark())
                    observed[name] = len(statements)
                    [access] = [r for r in caplog.records if r.name == "atus.api.access"]
                    begins = sum(s.startswith("BEGIN") for s in statements)
                    ends = sum(s in ("COMMIT", "ROLLBACK") for s in statements)
                    assert begins == ends, (name, statements)
                    # every transaction is read-only (I05)
                    assert all("READ ONLY" in s for s in statements if s.startswith("BEGIN"))
                    # the API's own count + BEGIN/COMMIT per transaction
                    if access.fields["db_statements"] + begins + ends != len(statements):
                        mismatched_accounting.append((name, statements))
        finally:
            log.close()
        assert observed == {name: expected for name, _, expected in DOCUMENTED}
        assert mismatched_accounting == []
