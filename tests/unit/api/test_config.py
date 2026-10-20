"""Configuration: every Phase 5 API variable is validated, and production
mode refuses unsafe or missing settings (audit F44)."""

from __future__ import annotations

import pytest

from atus_pipeline.api.app import create_app
from atus_pipeline.config import (
    DEFAULT_DATABASE_URL,
    ConfigError,
    RateLimit,
    Settings,
    check_api_settings,
    load_settings,
)

SECRET_A = "a" * 40
SECRET_B = "b" * 40
PROD_URL = "postgresql://atus_api:s3cr3t-Passw0rd-xyz@db.example.internal:5432/atus"


def prod_env(**extra) -> dict[str, str]:
    env = {
        "ATUS_ENV": "production",
        "ATUS_DATABASE_URL": PROD_URL,
        "ATUS_EDGE_SECRETS": f"{SECRET_A},{SECRET_B}",
    }
    env.update(extra)
    return env


class TestDefaults:
    def test_development_defaults_match_the_contract(self):
        s = load_settings({})
        assert s.env == "development" and not s.is_production
        assert s.database_url == DEFAULT_DATABASE_URL
        assert s.log_format == "text" and s.api_docs is True
        assert s.edge_secrets == () and not s.edge_auth_enabled
        assert s.api_pool_max_size == 4
        assert s.api_pool_max_idle_s == 240 and s.api_pool_timeout_s == 5
        assert s.api_connect_timeout_s == 10
        assert s.api_statement_timeout_ms == 85_000
        assert s.api_meta_statement_timeout_ms == 10_000
        assert s.api_jit is False and s.api_max_parallel_workers is None
        assert s.api_prepare_threshold == 5
        assert (s.api_max_inflight, s.api_max_inflight_heavy) == (2, 1)
        assert s.api_admission_wait_s == 5
        assert s.api_rate_analysis == RateLimit(6, 60.0)
        assert s.api_rate_requests == RateLimit(120, 60.0)
        assert s.api_daily_analysis_cap == 600
        assert s.api_max_body_bytes == 65_536
        assert s.api_threads == 16
        assert s.api_snapshot_ttl_s == 2
        assert s.build_sha == "dev" and s.port == 8000

    def test_production_defaults(self):
        s = load_settings(prod_env())
        assert s.is_production
        assert s.log_format == "json" and s.api_docs is False
        assert s.api_snapshot_ttl_s == 600
        assert s.edge_secrets == (SECRET_A, SECRET_B)

    def test_dataclass_defaults_equal_environment_defaults(self, tmp_path):
        """Programmatic Settings(...) (tests, scripts) get the same protection
        as the environment path."""
        loaded = load_settings({})
        built = Settings(
            database_url=DEFAULT_DATABASE_URL, data_dir=loaded.data_dir,
            release=loaded.release, log_level=loaded.log_level,
        )
        assert built == loaded


class TestProductionRefusal:
    def test_missing_database_url_is_refused(self):
        env = prod_env()
        del env["ATUS_DATABASE_URL"]
        with pytest.raises(ConfigError, match="ATUS_DATABASE_URL is required"):
            load_settings(env)

    def test_development_url_is_refused_in_production(self):
        with pytest.raises(ConfigError, match="development URL"):
            load_settings(prod_env(ATUS_DATABASE_URL=DEFAULT_DATABASE_URL))

    def test_missing_edge_secrets_refuse_the_api(self, tmp_path):
        env = prod_env()
        del env["ATUS_EDGE_SECRETS"]
        settings = load_settings(env)  # non-API commands may still run
        with pytest.raises(ConfigError, match="ATUS_EDGE_SECRETS is required"):
            check_api_settings(settings)
        with pytest.raises(ConfigError, match="ATUS_EDGE_SECRETS is required"):
            create_app(settings, open_pool=False)

    def test_short_secrets_are_refused_in_production(self):
        with pytest.raises(ConfigError, match="shorter than 32"):
            load_settings(prod_env(ATUS_EDGE_SECRETS="short-secret"))
        with pytest.raises(ConfigError, match="ATUS_OPS_TOKEN is shorter"):
            load_settings(prod_env(ATUS_OPS_TOKEN="tiny"))

    def test_development_accepts_short_secrets(self):
        assert load_settings({"ATUS_EDGE_SECRETS": "dev-secret"}).edge_secrets == ("dev-secret",)

    def test_docs_can_be_switched_back_on_explicitly(self):
        assert load_settings(prod_env(ATUS_API_DOCS="on")).api_docs is True


class TestValidation:
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("ATUS_ENV", "staging"),
            ("ATUS_LOG_LEVEL", "LOUD"),
            ("ATUS_LOG_FORMAT", "xml"),
            ("ATUS_API_DOCS", "maybe"),
            ("ATUS_API_JIT", "sometimes"),
            ("ATUS_API_POOL_MAX_SIZE", "0"),
            ("ATUS_API_POOL_MAX_SIZE", "four"),
            ("ATUS_API_POOL_TIMEOUT_S", "nan"),
            ("ATUS_API_POOL_MAX_IDLE_S", "-1"),
            ("ATUS_API_CONNECT_TIMEOUT_S", "1"),
            ("ATUS_API_STATEMENT_TIMEOUT_MS", "10"),
            ("ATUS_API_META_STATEMENT_TIMEOUT_MS", "abc"),
            ("ATUS_API_MAX_PARALLEL_WORKERS", "-1"),
            ("ATUS_API_PREPARE_THRESHOLD", "sometimes"),
            ("ATUS_API_MAX_INFLIGHT", "0"),
            ("ATUS_API_ADMISSION_WAIT_S", "1000"),
            ("ATUS_API_RATE_ANALYSIS", "6"),
            ("ATUS_API_RATE_ANALYSIS", "0/60"),
            ("ATUS_API_RATE_REQUESTS", "10/0"),
            ("ATUS_API_DAILY_ANALYSIS_CAP", "0"),
            ("ATUS_API_MAX_BODY_BYTES", "10"),
            ("ATUS_API_THREADS", "0"),
            ("ATUS_API_SNAPSHOT_TTL_S", "inf"),
            ("ATUS_API_CACHE_SIZE", "0"),
            ("ATUS_BUILD_SHA", "not a sha!"),
            ("PORT", "70000"),
            ("ATUS_API_CORS_ORIGINS", "*"),
            ("ATUS_API_CORS_ORIGINS", "http://localhost:5173/app"),
            ("ATUS_EDGE_SECRETS", "a,b,c"),
            ("ATUS_EDGE_SECRETS", "has space"),
        ],
    )
    def test_invalid_values_fail_naming_the_variable(self, name, value):
        with pytest.raises(ConfigError, match=name):
            load_settings({name: value})

    def test_heavy_lanes_cannot_exceed_total_lanes(self):
        with pytest.raises(ConfigError, match="MAX_INFLIGHT_HEAVY"):
            load_settings({"ATUS_API_MAX_INFLIGHT": "1", "ATUS_API_MAX_INFLIGHT_HEAVY": "2"})

    def test_values_are_parsed(self):
        s = load_settings({
            "ATUS_API_RATE_ANALYSIS": "10/30",
            "ATUS_API_JIT": "on",
            "ATUS_API_MAX_PARALLEL_WORKERS": "0",
            "ATUS_API_PREPARE_THRESHOLD": "none",
            "ATUS_API_POOL_TIMEOUT_S": "2.5",
            "ATUS_LOG_LEVEL": "debug",
            "ATUS_EXPECTED_DATA_VERSION": "0325:serving-x:abc",
            "ATUS_BUILD_SHA": "0123abcd",
            "PORT": "10000",
        })
        assert s.api_rate_analysis == RateLimit(10, 30.0)
        assert s.api_rate_analysis.refill_per_second == pytest.approx(1 / 3)
        assert str(s.api_rate_analysis) == "10/30"
        assert s.api_jit is True
        assert s.api_max_parallel_workers == 0
        assert s.api_prepare_threshold is None
        assert s.api_pool_timeout_s == 2.5
        assert s.log_level == "DEBUG"
        assert s.expected_data_version == "0325:serving-x:abc"
        assert (s.build_sha, s.port) == ("0123abcd", 10000)

    def test_malformed_database_url_never_echoes_the_password(self):
        with pytest.raises(ConfigError) as info:
            load_settings({"ATUS_DATABASE_URL": "postgresql://u:pa%zzword@h/db"})
        assert "pa%zz" not in str(info.value)
        assert info.value.__cause__ is None and info.value.__suppress_context__

    def test_secrets_are_not_echoed_or_repr(self):
        settings = load_settings({
            "ATUS_EDGE_SECRETS": SECRET_A, "ATUS_OPS_TOKEN": SECRET_B,
            "ATUS_DATABASE_URL": PROD_URL,
        })
        text = repr(settings)
        assert SECRET_A not in text and SECRET_B not in text and "s3cr3t" not in text
        with pytest.raises(ConfigError) as info:
            load_settings({"ATUS_EDGE_SECRETS": "ok-value,bad value with spaces"})
        assert "bad value" not in str(info.value)
