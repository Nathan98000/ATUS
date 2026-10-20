"""The server entry point (process model, proxy trust, limits), the `atus
api` command, and the logging filters (secret redaction, one traceback per
unhandled error)."""

from __future__ import annotations

import logging
import sys
from dataclasses import replace

import anyio
import pytest
import uvicorn
from click.testing import CliRunner
from fastapi.testclient import TestClient

from atus_pipeline import cli
from atus_pipeline.api import serve
from atus_pipeline.api.app import create_app
from atus_pipeline.config import ConfigError, load_settings
from atus_pipeline.logging_setup import mark_logged, set_redacted_secrets
from tests.unit.api.conftest import make_settings

SECRET = "e" * 40
PROD_ENV = {
    "ATUS_ENV": "production",
    "ATUS_DATABASE_URL": "postgresql://atus_api:Prod-Passw0rd-123456@db.internal:5432/atus",
    "ATUS_EDGE_SECRETS": SECRET,
    "PORT": "10000",
}


class TestUvicornOptions:
    def test_development(self, tmp_path):
        options = serve.uvicorn_options(make_settings(tmp_path))
        assert options["host"] == "127.0.0.1" and options["port"] == 8000
        assert options["workers"] == 1 and options["factory"] is True
        assert options["proxy_headers"] is False and options["forwarded_allow_ips"] == ""
        assert options["server_header"] is False
        assert options["timeout_keep_alive"] == 5
        assert options["timeout_graceful_shutdown"] == 20
        assert options["limit_concurrency"] == 64
        assert options["http"] == "h11" and options["ws"] == "none"
        assert options["h11_max_incomplete_event_size"] == 16 * 1024
        assert options["access_log"] is False and options["log_config"] is None

    def test_production_listens_on_port_from_environment(self):
        options = serve.uvicorn_options(load_settings(PROD_ENV))
        assert options["host"] == "0.0.0.0" and options["port"] == 10000
        assert options["reload"] is False

    def test_reload_is_refused_in_production(self):
        with pytest.raises(ConfigError, match="reload"):
            serve.uvicorn_options(load_settings(PROD_ENV), reload=True)

    def test_overrides(self, tmp_path):
        options = serve.uvicorn_options(make_settings(tmp_path), host="0.0.0.0", port=9001)
        assert (options["host"], options["port"]) == ("0.0.0.0", 9001)

    def test_uvicorn_accepts_the_options_and_loads_the_factory(self, monkeypatch):
        for name, value in PROD_ENV.items():
            monkeypatch.setenv(name, value)
        options = serve.uvicorn_options(load_settings())
        config = uvicorn.Config(**options)
        config.load()
        assert config.workers == 1
        assert not config.proxy_headers
        assert config.limit_concurrency == 64


class TestThreadLimiter:
    def test_lifespan_sizes_the_worker_thread_limiter(self, tmp_path):
        app = create_app(make_settings(tmp_path, api_threads=7), open_pool=False)
        with TestClient(app) as client:
            tokens = client.portal.call(
                lambda: anyio.to_thread.current_default_thread_limiter().total_tokens
            )
        assert tokens == 7


class TestApiCommand:
    def test_production_without_edge_secrets_refuses_to_start(self, monkeypatch):
        env = {k: v for k, v in PROD_ENV.items() if k != "ATUS_EDGE_SECRETS"}
        for name in ("ATUS_EDGE_SECRETS",):
            monkeypatch.delenv(name, raising=False)
        started = []
        monkeypatch.setattr(serve, "run", lambda *a, **k: started.append(k))
        result = CliRunner().invoke(cli.cli, ["api"], env=env)
        assert result.exit_code != 0
        assert "ATUS_EDGE_SECRETS is required" in result.output
        assert started == []

    def test_production_without_database_url_refuses_to_start(self, monkeypatch):
        monkeypatch.delenv("ATUS_DATABASE_URL", raising=False)
        started = []
        monkeypatch.setattr(serve, "run", lambda *a, **k: started.append(k))
        env = {"ATUS_ENV": "production", "ATUS_EDGE_SECRETS": SECRET}
        result = CliRunner().invoke(cli.cli, ["api"], env=env)
        assert result.exit_code != 0
        assert "ATUS_DATABASE_URL is required" in result.output
        assert started == []

    def test_valid_configuration_starts_the_server(self, monkeypatch):
        started = []
        monkeypatch.setattr(serve, "run", lambda settings, **k: started.append((settings, k)))
        result = CliRunner().invoke(cli.cli, ["api", "--port", "8517"], env=PROD_ENV)
        assert result.exit_code == 0, result.output
        settings, kwargs = started[0]
        assert settings.is_production and kwargs == {"host": None, "port": 8517,
                                                     "reload": False}

    def test_module_entry_point_reports_configuration_errors(self, monkeypatch, capsys):
        monkeypatch.setenv("ATUS_ENV", "production")
        monkeypatch.delenv("ATUS_DATABASE_URL", raising=False)
        monkeypatch.setattr(sys, "argv", ["serve"])
        with pytest.raises(SystemExit) as info:
            serve.main()
        assert info.value.code == 2
        assert "ATUS_DATABASE_URL" in capsys.readouterr().err


class TestLogging:
    def test_configured_secrets_are_redacted(self, caplog):
        set_redacted_secrets(["Prod-Passw0rd-123456", "short", None])
        try:
            with caplog.at_level(logging.WARNING, logger="psycopg.pool"):
                logging.getLogger("psycopg.pool").warning(
                    "error connecting in %r: %s", "atus-api",
                    "password Prod-Passw0rd-123456 rejected (short)",
                )
            message = caplog.records[-1].getMessage()
            assert "Prod-Passw0rd-123456" not in message and "***" in message
            assert "short" in message  # values under 8 characters are left alone
        finally:
            set_redacted_secrets([])

    def test_create_app_redacts_the_database_password(self, tmp_path, caplog):
        url = "postgresql://atus_api:Very%40Secret-123@db.internal/atus"
        create_app(replace(make_settings(tmp_path), database_url=url), open_pool=False)
        try:
            with caplog.at_level(logging.WARNING, logger="atus.api"):
                logging.getLogger("atus.api").warning("dsn %s / %s", url, "Very@Secret-123")
            message = caplog.records[-1].getMessage()
            assert "Very%40Secret-123" not in message and "Very@Secret-123" not in message
        finally:
            set_redacted_secrets([])

    def test_uvicorn_duplicate_traceback_is_dropped(self, tmp_path, caplog):
        logger = logging.getLogger("uvicorn.error")
        create_app(make_settings(tmp_path), open_pool=False)  # installs the filters
        logged, other = RuntimeError("already logged"), RuntimeError("not logged")
        mark_logged(logged)
        with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
            for exc in (logged, other):
                try:
                    raise exc
                except RuntimeError:
                    logger.exception("Exception in ASGI application")
        records = [r for r in caplog.records if r.name == "uvicorn.error"]
        assert [r.exc_info[1] for r in records] == [other]

    def test_unhandled_error_is_logged_once_with_the_request_id(
        self, client, stub_engine, caplog
    ):
        def boom(spec):
            raise RuntimeError("secret internal detail")

        stub_engine.estimate_behavior = boom
        with caplog.at_level(logging.INFO, logger="atus.api"):
            response = client.post(
                "/api/v1/analysis/estimate",
                json={"activity": {"preset": "sleep"}, "years": [2023]},
            )
        assert response.status_code == 500
        request_id = response.headers["X-Request-ID"]
        tracebacks = [r for r in caplog.records if r.exc_info]
        assert len(tracebacks) == 1
        assert request_id in tracebacks[0].getMessage()
        assert getattr(tracebacks[0].exc_info[1], "_atus_logged", False)
        assert response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"

