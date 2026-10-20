"""The request guard and context middleware: body size, media type, edge
authentication, client identity, per-client request buckets, control
characters, capped 422s, security headers, HEAD and framework edge cases.

Every rejection here must happen before routing, so the fake database must
record zero transactions and the stub engine must never run."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from atus_pipeline.config import RateLimit
from tests.unit.api.conftest import FakeDatabase, make_app, make_settings
from tests.unit.api.test_contract import known_estimate_result

SECRET_A = "a" * 40
SECRET_B = "b" * 40
ESTIMATE = "/api/v1/analysis/estimate"
BODY = {"activity": {"preset": "sleep"}, "years": [2023], "variance": "none"}
REMOTE = ("203.0.113.5", 40000)


@pytest.fixture
def build(stub_engine, tmp_path):
    """Factory: (settings overrides, client kwargs) -> (client, fake_db)."""
    clients = []

    def _build(client_kwargs=None, **overrides):
        fake_db = FakeDatabase()
        stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        app = make_app(make_settings(tmp_path, **overrides), stub_engine, fake_db)
        client = TestClient(app, raise_server_exceptions=False, **(client_kwargs or {}))
        client.__enter__()
        clients.append(client)
        return client, fake_db

    yield _build
    for client in clients:
        client.__exit__(None, None, None)


class TestBodySize:
    def test_declared_length_over_the_cap_is_413_before_any_work(self, build):
        client, fake_db = build(api_max_body_bytes=1024)
        response = client.post(
            ESTIMATE, content=b"{" + b" " * 2000 + b"}",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
        assert response.headers["connection"] == "close"
        assert fake_db.transactions == []

    def test_streamed_body_over_the_cap_is_413(self, build):
        client, fake_db = build(api_max_body_bytes=1024)

        def chunks():
            for _ in range(10):
                yield b" " * 200

        response = client.post(
            ESTIMATE, content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        assert fake_db.transactions == []

    def test_body_under_the_cap_is_served(self, build):
        client, _ = build(api_max_body_bytes=1024)
        assert client.post(ESTIMATE, json=BODY).status_code == 200


class TestMediaType:
    @pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded",
                                              None])
    def test_non_json_bodies_are_415(self, build, content_type):
        client, fake_db = build()
        headers = {"Content-Type": content_type} if content_type else {}
        response = client.post(ESTIMATE, content=b'{"years": [2023]}', headers=headers)
        assert response.status_code == 415
        assert response.json()["error"]["code"] == "unsupported_media_type"
        assert fake_db.transactions == []

    def test_bodiless_non_post_methods_reach_the_router(self, build):
        client, _ = build()
        response = client.delete("/api/v1/meta")
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "method_not_allowed"

    def test_json_with_charset_is_accepted(self, build):
        client, _ = build()
        response = client.post(
            ESTIMATE, content=b'{"activity": {"preset": "sleep"}, "years": [2023]}',
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        assert response.status_code == 200


class TestEdgeAuth:
    def test_missing_or_wrong_secret_is_403_with_zero_database_work(self, build, stub_engine):
        client, fake_db = build(edge_secrets=(SECRET_A, SECRET_B))
        calls = []
        stub_engine.estimate_behavior = lambda spec: calls.append(spec)
        for headers in ({}, {"X-ATUS-Edge-Auth": "wrong"}, {"X-ATUS-Edge-Auth": SECRET_A[:-1]}):
            for method, path in (("POST", ESTIMATE), ("GET", "/api/v1/meta"),
                                 ("GET", "/ready"), ("GET", "/api/v1/nope")):
                response = client.request(method, path, json=BODY, headers=headers)
                assert response.status_code == 403, (headers, path)
                assert response.json()["error"]["code"] == "forbidden_origin"
        assert fake_db.transactions == [] and calls == []

    def test_both_rotation_values_are_accepted(self, build):
        client, _ = build(edge_secrets=(SECRET_A, SECRET_B))
        for secret in (SECRET_A, SECRET_B):
            response = client.post(ESTIMATE, json=BODY, headers={"X-ATUS-Edge-Auth": secret})
            assert response.status_code == 200

    def test_health_is_exempt(self, build):
        client, fake_db = build(edge_secrets=(SECRET_A,))
        assert client.get("/health").status_code == 200
        assert client.head("/health").status_code == 200
        assert fake_db.transactions == []

    def test_disabled_without_secrets(self, build):
        client, _ = build()
        assert client.post(ESTIMATE, json=BODY).status_code == 200

    def test_incoming_request_id_is_kept_only_when_authenticated(self, build):
        client, _ = build(edge_secrets=(SECRET_A,))
        good = client.get("/health", headers={"X-Request-ID": "edge-req-0001"})
        assert good.headers["X-Request-ID"] != "edge-req-0001"  # unauthenticated
        authed = client.get("/health", headers={
            "X-Request-ID": "edge-req-0001", "X-ATUS-Edge-Auth": SECRET_A,
        })
        assert authed.headers["X-Request-ID"] == "edge-req-0001"
        malformed = client.get("/health", headers={
            "X-Request-ID": "bad id!", "X-ATUS-Edge-Auth": SECRET_A,
        })
        assert malformed.headers["X-Request-ID"] != "bad id!"


class TestRequestRateLimit:
    def test_429_with_retry_after_per_client(self, build):
        client, _ = build(client_kwargs={"client": REMOTE}, api_rate_requests=RateLimit(2, 60.0))
        assert client.get("/api/v1/population/metadata").status_code == 200
        assert client.get("/api/v1/activities/presets").status_code == 200
        limited = client.get("/api/v1/population/metadata")
        assert limited.status_code == 429
        error = limited.json()["error"]
        assert error["code"] == "rate_limited"
        assert error["details"]["scope"] == "client"
        assert error["details"]["retry_after_seconds"] == int(limited.headers["Retry-After"])
        assert 1 <= int(limited.headers["Retry-After"]) <= 30
        assert limited.headers["Cache-Control"] == "no-store"
        assert client.get("/health").status_code == 200  # never limited

    def test_forged_forwarding_headers_do_not_change_the_client(self, build):
        """Without edge auth the socket peer is the client: X-ATUS-Client-IP,
        X-Forwarded-For, Forwarded, CF-Connecting-IP and True-Client-IP are
        all ignored, so rotating them does not mint new buckets."""
        client, _ = build(client_kwargs={"client": REMOTE}, api_rate_requests=RateLimit(2, 60.0))
        statuses = []
        for i in range(4):
            ip = f"198.51.100.{i}"
            statuses.append(client.get("/api/v1/population/metadata", headers={
                "X-ATUS-Client-IP": ip, "X-Forwarded-For": ip, "Forwarded": f"for={ip}",
                "CF-Connecting-IP": ip, "True-Client-IP": ip,
            }).status_code)
        assert statuses == [200, 200, 429, 429]

    def test_forged_client_ip_with_a_wrong_secret_is_ignored(self, build):
        client, _ = build(edge_secrets=(SECRET_A,))
        response = client.get("/api/v1/population/metadata", headers={
            "X-ATUS-Client-IP": "198.51.100.1", "X-ATUS-Edge-Auth": "nope",
        })
        assert response.status_code == 403

    def test_edge_client_ip_selects_the_bucket_when_authenticated(self, build):
        client, _ = build(edge_secrets=(SECRET_A,), api_rate_requests=RateLimit(1, 60.0))

        def get(ip):
            return client.get("/api/v1/population/metadata", headers={
                "X-ATUS-Edge-Auth": SECRET_A, "X-ATUS-Client-IP": ip,
            }).status_code

        assert get("198.51.100.1") == 200
        assert get("198.51.100.2") == 200
        assert get("198.51.100.1") == 429

    def test_in_process_clients_are_not_limited_in_development(self, build):
        client, _ = build(api_rate_requests=RateLimit(1, 60.0))
        assert [client.get("/api/v1/population/metadata").status_code for _ in range(3)] == [
            200, 200, 200,
        ]


class TestValidationOutput:
    def test_control_characters_in_query_are_422_without_database_work(self, build):
        client, fake_db = build()
        response = client.get("/api/v1/activities", params={"search": "ab\x00cd"})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert error["details"]["errors"][0]["location"] == ["query", "search"]
        assert fake_db.transactions == []

    def test_422_lists_at_most_twenty_errors_with_bounded_strings(self, build):
        client, _ = build()
        body = {**BODY, **{f"extra_{i}": i for i in range(500)}, "k" * 5000: 1}
        response = client.post(ESTIMATE, json=body)
        assert response.status_code == 422
        details = response.json()["error"]["details"]
        assert len(details["errors"]) == 20
        assert details["truncated"] is True and details["error_count"] == 501
        for error in details["errors"]:
            assert all(len(part) <= 64 for part in error["location"])
            assert len(error["message"]) <= 200
        assert len(response.content) < 8_000

    def test_preset_length_is_bounded(self, build):
        client, _ = build()
        response = client.post(ESTIMATE, json={**BODY, "activity": {"preset": "x" * 65}})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


class TestHeaders:
    def test_security_headers_on_success_and_errors(self, build):
        client, _ = build()
        for response in (client.get("/health"), client.get("/api/v1/nope"),
                         client.post(ESTIMATE, json=BODY)):
            assert response.headers["Content-Security-Policy"] == (
                "default-src 'none'; frame-ancestors 'none'"
            )
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
            assert "Strict-Transport-Security" not in response.headers  # development
            assert "server" not in response.headers
            assert response.headers["X-Request-ID"]

    def test_errors_are_never_cacheable(self, build):
        client, _ = build()
        assert client.get("/api/v1/nope").headers["Cache-Control"] == "no-store"
        ok = client.get("/api/v1/population/metadata")
        assert ok.headers["Cache-Control"] == "public, max-age=300"

    def test_hsts_in_production(self, build):
        client, _ = build(env="production", edge_secrets=(SECRET_A,), api_docs=False)
        response = client.get("/health")
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
        assert client.get("/docs", headers={"X-ATUS-Edge-Auth": SECRET_A}).status_code == 404
        assert client.get(
            "/openapi.json", headers={"X-ATUS-Edge-Auth": SECRET_A}
        ).status_code == 404

    def test_analysis_responses_carry_version_headers(self, build):
        client, _ = build()
        response = client.post(ESTIMATE, json=BODY)
        assert response.headers["X-Data-Version"] == "0325:run1"
        assert response.headers["X-Analytics-Version"] == "0.2"
        assert response.headers["X-Response-Format"] == "1"


class TestCompression:
    def test_large_responses_are_gzipped_small_ones_are_not(self, build):
        client, _ = build()
        large = client.get("/api/v1/population/metadata", headers={"Accept-Encoding": "gzip"})
        assert large.headers["Content-Encoding"] == "gzip"
        assert large.json()["dimensions"]  # httpx decodes transparently
        small = client.get("/health", headers={"Accept-Encoding": "gzip"})
        assert "Content-Encoding" not in small.headers
        plain = client.get("/api/v1/population/metadata", headers={"Accept-Encoding": "identity"})
        assert "Content-Encoding" not in plain.headers


class TestValidationCostsNothing:
    @pytest.mark.parametrize(
        "payload",
        [
            {"activity": {"preset": "sleep"}, "years": []},
            {"activity": {"preset": "sleep"}, "years": [2023, 2023]},        # domain-invalid
            {"activity": {"preset": "sleep"}, "years": [2023],
             "population": {"age_min": 60, "age_max": 20}},                  # domain-invalid
        ],
    )
    def test_invalid_requests_never_touch_the_database(self, build, payload):
        client, fake_db = build()
        response = client.post(ESTIMATE, json=payload)
        assert response.status_code == 422
        assert fake_db.transactions == []

    def test_malformed_json_never_touches_the_database(self, build):
        client, fake_db = build()
        response = client.post(
            ESTIMATE, content=b"{nope", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert fake_db.transactions == []

    @pytest.mark.parametrize("body", [b"[" * 60_000, b'{"a":' * 10_000 + b"1" + b"}" * 10_000],
                             ids=["arrays", "objects"])
    def test_unparsable_nesting_is_malformed_json(self, build, body):
        """Regression: JSON nested deeper than the parser's recursion limit
        surfaced as FastAPI's generic 400, enveloped as ``http_400`` (the
        docs promise none from documented routes)."""
        client, fake_db = build()
        response = client.post(ESTIMATE, content=body,
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "malformed_json"
        assert fake_db.transactions == []


def raw_request(app, method: str, path: str, headers: list[tuple[bytes, bytes]]):
    """Drive the ASGI app directly with header bytes no HTTP client library
    would send (h11 rejects most of them in production; the guard must hold
    on its own). Returns the status and the response headers."""
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": headers, "client": REMOTE,
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = messages[0]
    return start["status"], {k.decode().lower(): v.decode("latin-1") for k, v in start["headers"]}


class TestRawHeaders:
    """Regressions: the guard parsed Content-Length with ``str.isdigit`` +
    ``int`` (a 500 for ``'²'`` and for values over 4,300 digits), and the
    request-id pattern used ``re.match``, whose ``$`` accepts a trailing
    newline."""

    @pytest.fixture
    def app(self, stub_engine, tmp_path):
        return make_app(make_settings(tmp_path, edge_secrets=(SECRET_A,)), stub_engine,
                        FakeDatabase())

    def edge(self, *extra):
        return [(b"x-atus-edge-auth", SECRET_A.encode()), *extra]

    @pytest.mark.parametrize(
        "value", [b"\xb2", "\N{ARABIC-INDIC DIGIT THREE}".encode(), b"1 2"],
        ids=["superscript-two", "arabic-indic-three", "space"],
    )
    def test_non_ascii_or_odd_content_length_is_not_a_500(self, app, value):
        status, _ = raw_request(app, "GET", "/health", self.edge((b"content-length", value)))
        assert status == 200

    @pytest.mark.parametrize(
        "value", [b"9" * 5000, b"65537", b"0" * 30 + b"65537"],
        ids=["5000-digits", "one-over", "leading-zeros"],
    )
    def test_huge_declared_length_is_413(self, app, value):
        status, headers = raw_request(app, "POST", ESTIMATE, self.edge(
            (b"content-type", b"application/json"), (b"content-length", value),
        ))
        assert status == 413 and headers["connection"] == "close"

    @pytest.mark.parametrize("value", [b"0" * 30 + b"2", b"65536"], ids=["zeros", "at-cap"])
    def test_declared_length_within_the_cap_passes(self, value):
        from atus_pipeline.api.middleware import declares_more_than

        assert not declares_more_than(value.decode(), 65_536)

    def test_request_id_with_a_trailing_newline_is_replaced(self, app):
        incoming = b"abcdefgh12\n"
        status, headers = raw_request(app, "GET", "/health", self.edge(
            (b"x-request-id", incoming),
        ))
        assert status == 200
        assert headers["x-request-id"] != incoming.decode()
        assert "\n" not in headers["x-request-id"]
        status, headers = raw_request(app, "GET", "/health", self.edge(
            (b"x-request-id", b"abcdefgh12"),
        ))
        assert headers["x-request-id"] == "abcdefgh12"


class TestHttpEdgeCases:
    def test_head_on_get_routes(self, build):
        client, _ = build()
        for path in ("/health", "/api/v1/population/metadata", "/api/v1/activities/presets"):
            response = client.head(path)
            assert response.status_code == 200, path
            assert response.content == b""
            assert int(response.headers["content-length"]) > 0

    def test_logged_path_cannot_forge_log_lines(self, build, caplog):
        import logging

        client, _ = build()
        with caplog.at_level(logging.INFO, logger="atus.api"):
            client.get("/api/v1/x%0Aforged-line")
        lines = [r.getMessage() for r in caplog.records if "request_id=" in r.getMessage()]
        assert lines and all("\n" not in line for line in lines)
        assert any("x%0Aforged-line" in line for line in lines)

    def test_405_keeps_the_allow_header(self, build):
        client, _ = build()
        response = client.put("/health", json={})
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "method_not_allowed"
        assert response.headers["Allow"] == "GET"

    def test_trailing_slash_is_not_redirected(self, build):
        client, _ = build()
        response = client.get(
            "/api/v1/population/metadata/", headers={"Host": "evil.example"},
            follow_redirects=False,
        )
        assert response.status_code == 404
        assert "location" not in response.headers

    def test_cors_exposes_the_contract_headers(self, build):
        origin = "http://localhost:5173"
        client, _ = build(api_cors_origins=(origin,))
        response = client.post(ESTIMATE, json=BODY, headers={"Origin": origin})
        exposed = {h.strip() for h in response.headers["Access-Control-Expose-Headers"].split(",")}
        assert {"X-Request-ID", "X-Analysis-Key", "X-Cache", "X-Data-Version",
                "Retry-After"} <= exposed

    def test_cors_preflight_needs_no_edge_secret(self, build):
        origin = "http://localhost:5173"
        client, _ = build(api_cors_origins=(origin,), edge_secrets=(SECRET_A,))
        preflight = client.options(ESTIMATE, headers={
            "Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        })
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
