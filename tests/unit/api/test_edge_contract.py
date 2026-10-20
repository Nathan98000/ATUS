"""The API's half of the edge contract (D2's wave-1 notes), over every kind of
answer the API itself gives:

* a 32-hex ``X-Request-ID`` from an authenticated edge (the Worker's
  ``crypto.randomUUID()`` without dashes, ``edge/src/http.ts``) is used as is;
* edge requests without a usable ``X-ATUS-Client-IP`` share one bucket;
* every error is the JSON envelope, ``Content-Type: application/json`` and
  ``Cache-Control: no-store`` (the Worker passes only JSON envelopes through
  and turns anything else into ``upstream_unavailable``);
* no ``/api`` path is ever answered with a redirect (the Worker never follows
  one, and turns it into a 502);
* ``Server-Timing`` reports ``db;dur=0`` exactly when the database did no
  work (cache hits, single-flight joins, every 4xx, shed requests), parsed
  as the governor parses it (``edge/src/admission.ts``), so a settled
  analysis is charged compute only when there was some.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

from atus_pipeline.config import RateLimit
from tests.unit.api.conftest import FakeDatabase, make_app, make_settings
from tests.unit.api.test_contract import known_estimate_result

SECRET = "e" * 40
EDGE = {"X-ATUS-Edge-Auth": SECRET}
ESTIMATE = "/api/v1/analysis/estimate"
BODY = {"activity": {"preset": "sleep"}, "years": [2023], "variance": "none"}

# edge/src/admission.ts serverTimingDbMs: the governor's own parse.
_DB_METRIC = re.compile(r"^dur=([0-9]+(?:\.[0-9]+)?)$", re.IGNORECASE)


def governor_db_ms(header: str | None) -> float | None:
    if header is None:
        return None
    for metric in header.split(","):
        name, *params = [part.strip() for part in metric.split(";")]
        if name.lower() != "db":
            continue
        for param in params:
            match = _DB_METRIC.match(param)
            if match:
                return float(match.group(1))
        return None
    return None


@pytest.fixture
def build(stub_engine, tmp_path):
    clients = []

    def _build(fake_db=None, **overrides):
        fake_db = fake_db or FakeDatabase()
        if stub_engine.estimate_behavior is None:
            stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        overrides.setdefault("edge_secrets", (SECRET,))
        overrides.setdefault("api_snapshot_ttl_s", 600.0)
        app = make_app(make_settings(tmp_path, **overrides), stub_engine, fake_db)
        client = TestClient(app, raise_server_exceptions=False, client=("203.0.113.7", 4000))
        client.__enter__()
        clients.append(client)
        return client, app

    yield _build
    for client in clients:
        client.__exit__(None, None, None)


def assert_envelope(response, status: int | None = None) -> str:
    assert not 300 <= response.status_code < 400, response.status_code
    if status is not None:
        assert response.status_code == status, (response.status_code, response.text[:200])
    assert response.headers["content-type"] == "application/json", response.headers
    assert response.headers["cache-control"] == "no-store"
    if response.request.method == "HEAD":
        return "(head)"
    error = response.json()["error"]
    assert isinstance(error["code"], str) and isinstance(error["message"], str)
    assert "details" in error
    return error["code"]


class TestRequestIds:
    def test_the_edges_32_hex_ids_are_used_as_is(self, build):
        client, _ = build()
        for _ in range(5):
            edge_id = uuid.uuid4().hex  # what newRequestId() produces
            assert re.fullmatch(r"[0-9a-f]{32}", edge_id)
            response = client.get("/api/v1/population/metadata",
                                  headers={**EDGE, "X-Request-ID": edge_id})
            assert response.headers["X-Request-ID"] == edge_id
            error = client.get("/api/v1/nope", headers={**EDGE, "X-Request-ID": edge_id})
            assert error.headers["X-Request-ID"] == edge_id

    def test_unauthenticated_ids_are_replaced(self, build):
        client, _ = build()
        edge_id = uuid.uuid4().hex
        response = client.get("/api/v1/meta", headers={"X-Request-ID": edge_id})
        assert response.status_code == 403
        assert response.headers["X-Request-ID"] != edge_id


class TestMissingClientIp:
    def test_edge_requests_without_an_address_share_one_bucket(self, build):
        client, _ = build(api_rate_requests=RateLimit(2, 3600.0))
        path = "/api/v1/population/metadata"
        statuses = [client.get(path, headers=EDGE).status_code for _ in range(2)]
        statuses.append(client.get(path, headers={**EDGE, "X-ATUS-Client-IP": "bogus"})
                        .status_code)
        # a client with an address has its own bucket
        statuses.append(client.get(path, headers={**EDGE, "X-ATUS-Client-IP": "198.51.100.4"})
                        .status_code)
        assert statuses == [200, 200, 429, 200]

    def test_the_access_line_carries_no_address(self, build, caplog):
        client, _ = build()
        with caplog.at_level("INFO", logger="atus.api.access"):
            client.get("/api/v1/population/metadata", headers=EDGE)
        [record] = [r for r in caplog.records if r.name == "atus.api.access"]
        assert record.fields["client_ip_prefix"] is None
        assert record.fields["edge_authenticated"] is True


def _error_requests(client) -> dict[str, object]:
    """Every error the API produces at its own layer, through the edge."""
    json_headers = {**EDGE, "Content-Type": "application/json"}
    return {
        "unknown path": lambda: client.get("/api/v1/nope", headers=EDGE),
        "trailing slash": lambda: client.get("/api/v1/meta/", headers=EDGE),
        "trailing slash on a POST route": lambda: client.post(
            ESTIMATE + "/", json=BODY, headers=EDGE),
        "double slash": lambda: client.get("/api/v1//meta", headers=EDGE),
        "bare /api": lambda: client.get("/api", headers=EDGE),
        "/api/": lambda: client.get("/api/", headers=EDGE),
        "/api/v1": lambda: client.get("/api/v1", headers=EDGE),
        "case": lambda: client.get("/API/v1/meta", headers=EDGE),
        "POST on a GET route": lambda: client.post("/api/v1/meta", json={}, headers=EDGE),
        "GET on a POST route": lambda: client.get(ESTIMATE, headers=EDGE),
        "HEAD on a POST route": lambda: client.head(ESTIMATE, headers=EDGE),
        "PUT": lambda: client.put(ESTIMATE, json=BODY, headers=EDGE),
        "DELETE": lambda: client.delete("/api/v1/meta", headers=EDGE),
        "OPTIONS": lambda: client.options("/api/v1/meta", headers=EDGE),
        "PATCH": lambda: client.patch(ESTIMATE, json=BODY, headers=EDGE),
        "415": lambda: client.post(ESTIMATE, content=b"{}",
                                   headers={**EDGE, "Content-Type": "text/plain"}),
        "413": lambda: client.post(ESTIMATE, content=b"[" + b"0," * 40_000 + b"0]",
                                   headers=json_headers),
        "400 malformed": lambda: client.post(ESTIMATE, content=b"{", headers=json_headers),
        "422 body": lambda: client.post(ESTIMATE, json={**BODY, "years": []}, headers=EDGE),
        "422 query": lambda: client.get("/api/v1/activities", params={"level": 9},
                                        headers=EDGE),
        "422 control character": lambda: client.get(
            "/api/v1/activities", params={"search": "a\x00b"}, headers=EDGE),
        "404 unknown code": lambda: client.get("/api/v1/activities/999999", headers=EDGE),
        "404 malformed code": lambda: client.get("/api/v1/activities/abc", headers=EDGE),
        "403 no edge secret": lambda: client.get("/api/v1/meta"),
        "403 wrong edge secret": lambda: client.get(
            "/api/v1/meta", headers={"X-ATUS-Edge-Auth": "x" * 40}),
        "404 ops path": lambda: client.get("/ready", headers=EDGE),
        "404 metrics": lambda: client.get("/metrics", headers=EDGE),
    }


class TestEnvelopesAndNoRedirects:
    def test_every_api_error_is_a_json_envelope(self, build):
        client, _ = build()
        codes = {name: assert_envelope(send()) for name, send in _error_requests(client).items()}
        assert codes["415"] == "unsupported_media_type"
        assert codes["413"] == "payload_too_large"
        assert codes["400 malformed"] == "malformed_json"
        assert codes["403 no edge secret"] == "forbidden_origin"
        assert {codes[k] for k in ("unknown path", "trailing slash", "bare /api", "case")} == {
            "not_found"}
        assert codes["POST on a GET route"] == "method_not_allowed"

    def test_successes_on_api_paths_are_never_redirects(self, build):
        client, _ = build()
        for path in ("/api/v1/meta", "/api/v1/activities", "/api/v1/activities/presets",
                     "/api/v1/activities/0101", "/api/v1/population/metadata"):
            for send in (client.get, client.head):
                response = send(path, headers=EDGE)
                assert response.status_code == 200, (path, send)
        assert client.post(ESTIMATE, json=BODY, headers=EDGE).status_code == 200

    def test_rate_limit_envelopes(self, build):
        client, _ = build(api_rate_requests=RateLimit(1, 3600.0))
        client.get("/api/v1/population/metadata", headers=EDGE)
        response = client.get("/api/v1/population/metadata", headers=EDGE)
        assert assert_envelope(response, 429) == "rate_limited"
        assert int(response.headers["Retry-After"]) >= 1

    def test_operational_failures_are_envelopes(self, build, stub_engine):
        unavailable = FakeDatabase()
        unavailable.snapshot_error = psycopg.OperationalError("connection refused")
        client, _ = build(fake_db=unavailable)
        response = client.get("/api/v1/meta", headers=EDGE)
        assert assert_envelope(response, 503) == "database_unavailable"
        assert response.headers["Retry-After"]

        client, _ = build(expected_data_version="0325:another")
        assert assert_envelope(client.get("/api/v1/meta", headers=EDGE),
                               503) == "data_version_mismatch"

        stub_engine.estimate_behavior = lambda spec: (_ for _ in ()).throw(RuntimeError("x"))
        client, _ = build()
        response = client.post(ESTIMATE, json=BODY, headers=EDGE)
        assert assert_envelope(response, 500) == "internal_error"
        assert "RuntimeError" not in response.text


class TestServerTimingForTheGovernor:
    def test_db_dur_is_zero_exactly_when_the_database_did_nothing(self, build):
        client, _ = build()
        miss = client.post(ESTIMATE, json=BODY, headers=EDGE)
        assert miss.headers["X-Cache"] == "miss"
        assert governor_db_ms(miss.headers["Server-Timing"]) > 0
        no_work = {
            "hit": client.post(ESTIMATE, json=BODY, headers=EDGE),
            "422": client.post(ESTIMATE, json={**BODY, "years": []}, headers=EDGE),
            "domain 422 (dry run)": client.post(
                ESTIMATE, json={**BODY, "years": [2099]}, headers=EDGE),
            "400": client.post(ESTIMATE, content=b"{",
                               headers={**EDGE, "Content-Type": "application/json"}),
            "415": client.post(ESTIMATE, content=b"{}",
                               headers={**EDGE, "Content-Type": "text/plain"}),
            "403": client.post(ESTIMATE, json=BODY),
            "404": client.get("/api/v1/nope", headers=EDGE),
            "metadata from the snapshot": client.get("/api/v1/meta", headers=EDGE),
        }
        assert no_work["hit"].headers["X-Cache"] == "hit"
        for name, response in no_work.items():
            assert response.headers["Server-Timing"].startswith("db;dur=0, "), name
            assert governor_db_ms(response.headers["Server-Timing"]) == 0, name

    def test_a_shed_analysis_reports_no_database_time(self, build):
        client, _ = build(api_rate_analysis=RateLimit(1, 3600.0))
        assert client.post(ESTIMATE, json=BODY, headers=EDGE).status_code == 200
        shed = client.post(ESTIMATE, json={**BODY, "years": [2022]}, headers=EDGE)
        assert shed.status_code == 429
        assert governor_db_ms(shed.headers["Server-Timing"]) == 0

    def test_a_single_flight_join_reports_no_database_time(self, build, stub_engine):
        entered, release = threading.Event(), threading.Event()

        def held(spec):
            entered.set()
            assert release.wait(10)
            return known_estimate_result()

        stub_engine.estimate_behavior = held
        client, app = build()
        with ThreadPoolExecutor(2) as pool:
            leader = pool.submit(client.post, ESTIMATE, json=BODY, headers=EDGE)
            assert entered.wait(5)
            joiner = pool.submit(client.post, ESTIMATE, json=BODY, headers=EDGE)
            deadline = time.monotonic() + 5
            while app.state.runner.joins < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            release.set()
            first, second = leader.result(10), joiner.result(10)
        by_cache = {r.headers["X-Cache"]: r for r in (first, second)}
        assert set(by_cache) == {"miss", "join"}
        assert governor_db_ms(by_cache["join"].headers["Server-Timing"]) == 0
        assert governor_db_ms(by_cache["miss"].headers["Server-Timing"]) > 0

    def test_fast_real_work_is_never_rounded_to_zero(self):
        from atus_pipeline.api.middleware import server_timing

        assert server_timing(0.01, 2.0).startswith("db;dur=0.1, ")
        assert server_timing(0.0, 2.0, touched_db=True).startswith("db;dur=0.1, ")
        assert server_timing(12.34, 20.0) == "db;dur=12.3, app;dur=7.7"
        assert server_timing(0.0, 3.0, touched_db=False) == "db;dur=0, app;dur=3.0"
        assert json.dumps(governor_db_ms("db;dur=0.1, app;dur=1.0")) == "0.1"
