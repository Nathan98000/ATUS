"""Observability (ADR-014): the access log record, client-IP truncation,
Server-Timing, the JSON log format, statement accounting, the operator
endpoints (/health, /ready, /metrics) and the Prometheus exposition."""

from __future__ import annotations

import io
import json
import logging
import re

import pytest
from fastapi.testclient import TestClient

from atus_pipeline.api.metrics import Counter, Histogram, Metrics
from atus_pipeline.api.observability import (
    UNMATCHED_ROUTE,
    RouteTemplates,
    ip_prefix,
)
from atus_pipeline.api.routes import activities
from atus_pipeline.config import RateLimit
from atus_pipeline.logging_setup import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
    set_redacted_secrets,
)
from tests.unit.api.conftest import FakeDatabase, make_app, make_settings
from tests.unit.api.test_contract import known_estimate_result

SECRET = "x" * 40
OPS_TOKEN = "k" * 40
OPS = {"Authorization": f"Bearer {OPS_TOKEN}"}
ESTIMATE = "/api/v1/analysis/estimate"
BODY = {"activity": {"include": ["0101"], "label": "Visible-Label-7f3a"}, "years": [2023],
        "variance": "none"}
SERVER_TIMING = re.compile(r"^db;dur=(?:0|\d+\.\d), app;dur=\d+\.\d$")


@pytest.fixture
def build(stub_engine, tmp_path):
    clients = []

    def _build(client_kwargs=None, fake_db=None, **overrides):
        stub_engine.estimate_behavior = lambda spec: known_estimate_result()
        fake_db = fake_db or FakeDatabase()
        overrides.setdefault("api_snapshot_ttl_s", 600.0)
        app = make_app(make_settings(tmp_path, **overrides), stub_engine, fake_db)
        client = TestClient(app, raise_server_exceptions=False, **(client_kwargs or {}))
        client.__enter__()
        clients.append(client)
        return client, app

    yield _build
    for client in clients:
        client.__exit__(None, None, None)


def access_records(caplog) -> list[dict]:
    return [r.fields for r in caplog.records
            if r.name == "atus.api.access" and isinstance(getattr(r, "fields", None), dict)]


class TestIpTruncation:
    @pytest.mark.parametrize(
        ("ip", "prefix"),
        [
            ("203.0.113.77", "203.0.113.0/24"),
            ("2001:db8:abcd:12:1:2:3:4", "2001:db8:abcd::/48"),
            ("::ffff:198.51.100.9", "198.51.100.0/24"),
            ("127.0.0.1", "127.0.0.0/24"),
            ("testclient", None),
            ("", None),
            (None, None),
        ],
    )
    def test_prefixes(self, ip, prefix):
        assert ip_prefix(ip) == prefix


class TestRouteTemplates:
    def test_templates_bound_the_label_space(self):
        routes = RouteTemplates()
        routes.add_router(activities.router, "/api/v1")
        assert routes.match("/api/v1/activities") == "/api/v1/activities"
        assert routes.match("/api/v1/activities/presets") == "/api/v1/activities/presets"
        assert routes.match("/api/v1/activities/120303") == "/api/v1/activities/{code}"
        assert routes.match("/api/v1/activities/1/2") == UNMATCHED_ROUTE
        assert routes.match("/wp-login.php") == UNMATCHED_ROUTE


class TestAccessLog:
    def test_one_structured_line_per_request(self, build, caplog):
        client, _ = build(edge_secrets=(SECRET,))
        edge = {"X-ATUS-Edge-Auth": SECRET, "X-ATUS-Client-IP": "198.51.100.77"}
        with caplog.at_level(logging.INFO, logger="atus.api"):
            miss = client.post(ESTIMATE, json=BODY, headers=edge)
            client.post(ESTIMATE, json=BODY, headers=edge)
            client.get("/api/v1/activities/120303", headers=edge)
            client.get("/api/v1/meta")  # no edge secret: 403
        records = access_records(caplog)
        assert len(records) == 4
        first, hit, detail, refused = records
        assert first == {
            "event": "access",
            "request_id": miss.headers["X-Request-ID"],
            "method": "POST",
            "route": ESTIMATE,
            "path": ESTIMATE,
            "status": 200,
            "latency_ms": first["latency_ms"],
            "db_ms": first["db_ms"],
            "db_statements": 2,  # the snapshot read and the version recheck (fake DB)
            "cache": "miss",
            "analysis_key": miss.headers["X-Analysis-Key"],
            "error": None,
            "client_ip_prefix": "198.51.100.0/24",
            "edge_authenticated": True,
            "disconnected": False,
        }
        assert hit["cache"] == "hit" and hit["db_statements"] == 0 and hit["db_ms"] == 0
        assert detail["route"] == "/api/v1/activities/{code}"
        assert detail["path"] == "/api/v1/activities/120303"
        assert refused["status"] == 403 and refused["error"] == "forbidden_origin"
        assert refused["route"] == "/api/v1/meta" and not refused["edge_authenticated"]
        assert refused["client_ip_prefix"] is None  # the in-process peer is not an address

    def test_no_full_address_body_or_spec_is_logged(self, build, caplog):
        client, _ = build(edge_secrets=(SECRET,))
        edge = {"X-ATUS-Edge-Auth": SECRET, "X-ATUS-Client-IP": "198.51.100.77"}
        with caplog.at_level(logging.DEBUG):
            client.post(ESTIMATE, json=BODY, headers=edge)
            client.post(ESTIMATE, json={**BODY, "years": []}, headers=edge)  # 422
            client.get("/api/v1/activities", params={"search": "Query-Term-9c1"}, headers=edge)
        text = "\n".join(  # the API's own records (not the test client's)
            JsonFormatter().format(r) + TextFormatter().format(r)
            for r in caplog.records if r.name.startswith(("atus", "uvicorn", "psycopg"))
        )
        assert "198.51.100.77" not in text
        assert "Visible-Label-7f3a" not in text and "Query-Term-9c1" not in text
        assert "198.51.100.0/24" in text

    def test_errors_carry_their_code(self, build, caplog):
        client, _ = build(api_max_body_bytes=1024)
        with caplog.at_level(logging.INFO, logger="atus.api"):
            client.post(ESTIMATE, content=b"x" * 2000, headers={"Content-Type": "application/json"})
            client.get("/api/v1/nope")
            client.get("/api/v1/activities", params={"search": "a\x01b"})
            client.post(ESTIMATE, json={**BODY, "years": []})
        assert [(r["status"], r["error"]) for r in access_records(caplog)] == [
            (413, "payload_too_large"), (404, "not_found"), (422, "validation_error"),
            (422, "validation_error"),
        ]

    def test_unhandled_error_has_one_traceback_and_an_access_line(
        self, build, stub_engine, caplog
    ):
        client, _ = build()
        stub_engine.estimate_behavior = lambda spec: (_ for _ in ()).throw(RuntimeError("boom"))
        with caplog.at_level(logging.INFO, logger="atus.api"):
            response = client.post(ESTIMATE, json=BODY)
        assert response.status_code == 500
        tracebacks = [r for r in caplog.records if r.exc_info]
        assert len(tracebacks) == 1
        assert tracebacks[0].fields["request_id"] == response.headers["X-Request-ID"]
        [access] = access_records(caplog)
        assert access["status"] == 500 and access["error"] == "internal_error"

    def test_uvicorn_access_log_is_replaced(self, build):
        build()
        assert logging.getLogger("uvicorn.access").disabled


class TestServerTiming:
    def test_on_successes_and_errors(self, build):
        client, _ = build()
        for response in (client.post(ESTIMATE, json=BODY), client.get("/health"),
                         client.get("/api/v1/nope")):
            assert SERVER_TIMING.match(response.headers["Server-Timing"]), response.headers


class TestJsonFormat:
    def record(self, **kwargs) -> logging.LogRecord:
        record = logging.LogRecord("atus.api.access", logging.INFO, __file__, 1,
                                   "request_id=%s done", ("abc",), None)
        for key, value in kwargs.items():
            setattr(record, key, value)
        return record

    def test_fields_are_lifted_to_the_top_level(self):
        line = JsonFormatter().format(self.record(fields={"event": "access", "status": 200}))
        entry = json.loads(line)
        assert entry["message"] == "request_id=abc done"
        assert entry["logger"] == "atus.api.access" and entry["level"] == "INFO"
        assert entry["event"] == "access" and entry["status"] == 200
        assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", entry["ts"])

    def test_tracebacks_stay_on_one_line_and_secrets_are_redacted(self):
        set_redacted_secrets(["Sup3r-Secret-Value"])
        try:
            try:
                raise RuntimeError("connect failed: password Sup3r-Secret-Value")
            except RuntimeError:
                import sys
                record = self.record(exc_info=sys.exc_info())
            line = JsonFormatter().format(record)
        finally:
            set_redacted_secrets([])
        assert "\n" not in line
        entry = json.loads(line)
        assert entry["exc_type"] == "RuntimeError" and "Traceback" in entry["exc"]
        assert "Sup3r-Secret-Value" not in line and "***" in line

    def test_secrets_that_json_escapes_are_redacted(self):
        """Regression: redaction ran only on the serialized line, where a
        password containing a quote or a backslash appears escaped
        (``\\"``, ``\\\\``) and no longer matched."""
        secret = 'Pa"ss\\w0rd-xyz'
        set_redacted_secrets([secret])
        try:
            try:
                raise RuntimeError(f"connect failed for password {secret}")
            except RuntimeError:
                import sys
                record = self.record(exc_info=sys.exc_info(), fields={"detail": secret})
            line = JsonFormatter().format(record)
        finally:
            set_redacted_secrets([])
        entry = json.loads(line)
        assert secret not in entry["exc"] and secret not in entry["detail"]
        assert json.dumps(secret)[1:-1] not in line
        assert "***" in entry["exc"] and entry["detail"] == "***"

    def test_configure_logging_switches_the_installed_handler(self):
        root = logging.getLogger()
        saved, saved_level = root.handlers[:], root.level
        root.handlers = []
        try:
            configure_logging("INFO", "text")
            [handler] = root.handlers
            assert isinstance(handler.formatter, TextFormatter)
            configure_logging("WARNING", "json")
            assert root.handlers == [handler]
            assert isinstance(handler.formatter, JsonFormatter)
            assert root.level == logging.WARNING
            stream = io.StringIO()
            handler.setStream(stream)
            logging.getLogger("atus.api").warning("hello", extra={"fields": {"k": 1}})
            entry = json.loads(stream.getvalue())
            assert entry["message"] == "hello" and entry["k"] == 1
        finally:
            root.handlers = saved
            root.setLevel(saved_level)

    def test_foreign_handlers_are_left_alone(self):
        root = logging.getLogger()
        saved = root.handlers[:]
        foreign = logging.StreamHandler(io.StringIO())
        root.handlers = [foreign]
        try:
            configure_logging("INFO", "json")
            assert root.handlers == [foreign] and foreign.formatter is None
        finally:
            root.handlers = saved

    def test_production_settings_log_json(self, tmp_path):
        from atus_pipeline.config import load_settings

        settings = load_settings({
            "ATUS_ENV": "production",
            "ATUS_DATABASE_URL": "postgresql://u:p@db.example/atus",
            "ATUS_EDGE_SECRETS": SECRET,
        })
        assert settings.log_format == "json"


class TestHealth:
    def test_build_and_no_dependencies(self, build):
        client, app = build(build_sha="0123abcd", edge_secrets=(SECRET,))
        response = client.get("/health")
        assert response.json() == {"status": "ok", "build": "0123abcd"}
        assert response.headers["Cache-Control"] == "no-store"
        head = client.head("/health")
        assert head.status_code == 200 and head.content == b""
        assert app.state.snapshots.refreshes == 0


class TestOperatorEndpoints:
    def not_found_body(self, client) -> bytes:
        return client.get("/api/v1/definitely-not-a-route").content

    @pytest.mark.parametrize("path", ["/ready", "/metrics"])
    def test_hidden_without_a_configured_token(self, build, path):
        client, _ = build()
        for headers in ({}, {"Authorization": "Bearer "}, OPS):
            response = client.get(path, headers=headers)
            assert response.status_code == 404
            assert response.content == self.not_found_body(client)

    @pytest.mark.parametrize("path", ["/ready", "/metrics"])
    def test_wrong_or_missing_bearer_looks_like_an_unknown_path(self, build, path):
        client, _ = build(ops_token=OPS_TOKEN)
        for headers in ({}, {"Authorization": f"Bearer {OPS_TOKEN[:-1]}"},
                        {"Authorization": f"Basic {OPS_TOKEN}"},
                        {"Authorization": OPS_TOKEN}):
            response = client.get(path, headers=headers)
            assert response.status_code == 404, headers
            assert response.content == self.not_found_body(client)
        assert client.get(path, headers=OPS).status_code == 200
        assert client.get(path, headers={"Authorization": f"bearer {OPS_TOKEN}"}).status_code == 200

    @pytest.mark.parametrize("path", ["/ready", "/metrics"])
    @pytest.mark.parametrize("edge", [False, True])
    def test_other_methods_do_not_reveal_the_path_either(self, build, path, edge):
        """Regression: without a valid bearer, POST/PUT/PATCH/DELETE on an
        operator path answered 405 with "Allow: GET" (an unknown path is
        404), revealing that it exists. Every method must look exactly like
        an unknown path; a valid bearer still gets the truthful 405."""
        overrides = {"edge_secrets": (SECRET,)} if edge else {}
        client, _ = build(ops_token=OPS_TOKEN, **overrides)
        base = {"X-ATUS-Edge-Auth": SECRET} if edge else {}
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for headers in ({"Content-Type": "application/json"}, {}):
                sent = {**base, **headers}
                kwargs = {"content": b"{}"} if headers else {}
                hidden = client.request(method, path, headers=sent, **kwargs)
                unknown = client.request(method, "/api/v1/nope", headers=sent, **kwargs)
                assert hidden.status_code == unknown.status_code, (method, headers)
                assert hidden.content == unknown.content
                assert "allow" not in hidden.headers
        allowed = client.post(path, headers={**base, **OPS, "Content-Type": "application/json"},
                              content=b"{}")
        assert allowed.status_code == 405 and allowed.headers["allow"] == "GET"

    def test_edge_auth_interplay(self, build):
        """Behind edge auth, an ops path without a valid bearer is refused
        like every unknown path (403); a valid bearer suffices on its own."""
        client, _ = build(ops_token=OPS_TOKEN, edge_secrets=(SECRET,))
        assert client.get("/metrics").status_code == 403
        assert client.get("/api/v1/nope").status_code == 403
        assert client.get("/metrics", headers=OPS).status_code == 200
        assert client.get("/metrics", headers={"X-ATUS-Edge-Auth": SECRET}).status_code == 404

    def test_ready_reports_the_data(self, build):
        client, _ = build(ops_token=OPS_TOKEN, build_sha="abc123",
                          expected_data_version="0325:run1")
        payload = client.get("/ready", headers=OPS).json()
        assert payload["status"] == "ready" and payload["data_loaded"] is True
        assert payload["data_version"] == payload["expected_data_version"] == "0325:run1"
        assert payload["build"] == "abc123" and payload["snapshot_age_s"] >= 0

    def test_ready_reports_a_mismatch(self, build):
        client, _ = build(ops_token=OPS_TOKEN, expected_data_version="0325:other")
        response = client.get("/ready", headers=OPS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "data_version_mismatch"

    def test_not_in_the_published_schema(self, build):
        client, _ = build(ops_token=OPS_TOKEN)
        paths = client.get("/openapi.json").json()["paths"]
        assert "/ready" not in paths and "/metrics" not in paths and "/health" in paths


SAMPLE = re.compile(
    r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\]|\\.)*",?)*\})? '
    r"(-?[0-9.]+(e[+-]?[0-9]+)?|\+Inf)$"
)


class TestMetrics:
    def scrape(self, client) -> str:
        response = client.get("/metrics", headers=OPS)
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
        assert response.headers["Cache-Control"] == "no-store"
        return response.text

    def test_exposition_is_well_formed(self, build):
        client, _ = build(ops_token=OPS_TOKEN)
        client.post(ESTIMATE, json=BODY)
        text = self.scrape(client)
        declared = set()
        for line in text.splitlines():
            if line.startswith("# TYPE "):
                declared.add(line.split()[2])
            elif line.startswith("# HELP "):
                continue
            else:
                assert SAMPLE.match(line), line
                name = re.split(r"[{ ]", line, maxsplit=1)[0]
                assert re.sub(r"_(bucket|sum|count)$", "", name) in declared or name in declared

    def test_series_after_traffic(self, build):
        client, _ = build(ops_token=OPS_TOKEN, build_sha="feedbeef",
                          client_kwargs={"client": ("203.0.113.9", 4000)},
                          api_rate_analysis=RateLimit(1, 60.0),
                          api_daily_analysis_cap=5)
        client.post(ESTIMATE, json=BODY)                              # miss
        client.post(ESTIMATE, json=BODY)                              # hit
        client.post(ESTIMATE, json={**BODY, "years": [2024]})         # 429 analysis bucket
        client.get("/api/v1/activities/120303")
        text = self.scrape(client)
        expected = [
            'atus_build_info{build="feedbeef"} 1',
            'atus_http_requests_total{route="/api/v1/analysis/estimate",method="POST",'
            'status="200"} 2',
            'atus_http_requests_total{route="/api/v1/analysis/estimate",method="POST",'
            'status="429"} 1',
            'atus_http_requests_total{route="/api/v1/activities/{code}",method="GET",'
            'status="200"} 1',
            'atus_http_request_duration_seconds_count{route="/api/v1/analysis/estimate"} 3',
            'atus_http_errors_total{code="rate_limited"} 1',
            'atus_analysis_cache_total{result="hit"} 1',
            'atus_analysis_cache_total{result="miss"} 1',
            'atus_db_statements_per_request_bucket{route="/api/v1/analysis/estimate",le="0"} 2',
            'atus_rate_limit_rejections_total{bucket="analysis"} 1',
            'atus_rate_limit_rejections_total{bucket="request"} 0',
            "atus_analysis_computations_total 1",
            "atus_analyses_in_flight 0",
            "atus_daily_cap_used 1",
            "atus_daily_cap_limit 5",
            "atus_daily_cap_rejections_total 0",
            "atus_admission_shed_total 0",
            "atus_query_timeouts_total 0",
            "atus_snapshot_refreshes_total 1",
            "atus_snapshot_stale_serves_total 0",
            "atus_snapshot_pinned 0",
            "atus_analysis_cache_entries 1",
        ]
        lines = set(text.splitlines())
        for line in expected:
            assert line in lines, line

    def test_snapshot_pinning_and_stale_serves(self, build):
        client, _ = build(ops_token=OPS_TOKEN, expected_data_version="0325:run1",
                          api_snapshot_ttl_s=0.0)
        client.get("/api/v1/meta")
        lines = set(self.scrape(client).splitlines())
        assert "atus_snapshot_pinned 1" in lines

        fake_db = FakeDatabase()
        client, _ = build(ops_token=OPS_TOKEN, api_snapshot_ttl_s=0.0, fake_db=fake_db)
        client.get("/api/v1/meta")
        fake_db.snapshot_error = RuntimeError("database down")
        client.get("/api/v1/meta")
        lines = set(self.scrape(client).splitlines())
        assert "atus_snapshot_stale_serves_total 1" in lines
        assert "atus_snapshot_refresh_failures_total 1" in lines

    def test_counter_and_histogram_rendering(self):
        counter = Counter("c_total", "help", ("a",))
        counter.inc('x"y\\z\n')
        assert counter.render()[-1] == 'c_total{a="x\\"y\\\\z\\n"} 1'
        histogram = Histogram("h", "help", (1, 5))
        for value in (0.5, 3, 9):
            histogram.observe(value)
        assert histogram.render()[2:] == [
            'h_bucket{le="1"} 1', 'h_bucket{le="5"} 2', 'h_bucket{le="+Inf"} 3',
            "h_sum 12.5", "h_count 3",
        ]

    def test_metrics_event_only_after_traffic(self, build, caplog):
        import asyncio

        from atus_pipeline.api.metrics import emit_metrics_events

        client, app = build()
        client.post(ESTIMATE, json=BODY)
        client.post(ESTIMATE, json=BODY)
        metrics = app.state.metrics

        async def scenario():
            task = asyncio.ensure_future(emit_metrics_events(metrics, app.state, 0.02))
            await asyncio.sleep(0.15)  # several intervals, traffic only before the first
            task.cancel()

        with caplog.at_level(logging.INFO, logger="atus.api.metrics"):
            asyncio.run(scenario())
        # the requests above are counted once the task starts: no event
        assert [r for r in caplog.records if r.name == "atus.api.metrics"] == []

        async def with_traffic():
            task = asyncio.ensure_future(emit_metrics_events(metrics, app.state, 0.05))
            await asyncio.sleep(0.01)
            metrics.observe_request(route=ESTIMATE, method="POST", status=200, seconds=0.1,
                                    db_statements=0, cache="hit", error=None)
            await asyncio.sleep(0.2)
            task.cancel()

        with caplog.at_level(logging.INFO, logger="atus.api.metrics"):
            asyncio.run(with_traffic())
        events = [r.fields for r in caplog.records if r.name == "atus.api.metrics"]
        assert len(events) == 1  # one event, not one per interval
        event = events[0]
        assert event["event"] == "metrics" and event["requests"] == 3
        assert event["responses"] == {"2xx": 3}
        assert event["cache"] == {"hit": 2, "miss": 1}
        assert event["computations"] == 1 and event["daily_cap_used"] == 1
        assert event["snapshot_age_s"] is not None

    def test_unknown_methods_are_bucketed(self):
        metrics = Metrics(build="dev")
        metrics.observe_request(route="unmatched", method="PROPFIND", status=405, seconds=0.01,
                                db_statements=0, cache=None, error="method_not_allowed")
        assert metrics.requests.value("unmatched", "OTHER", "405") == 1
