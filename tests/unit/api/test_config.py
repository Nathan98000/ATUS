"""Configuration: every Phase 5 API variable is validated, and production
mode refuses unsafe or missing settings (audit F44)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from atus_pipeline.api.app import create_app
from atus_pipeline.config import (
    DEFAULT_DATABASE_URL,
    ConfigError,
    RateLimit,
    Settings,
    check_api_settings,
    is_local_stack_host,
    load_settings,
)

SECRET_A = "a" * 40
SECRET_B = "b" * 40
PROD_URL = (
    "postgresql://atus_api:s3cr3t-Passw0rd-xyz@db.example.internal:5432/atus"
    "?sslmode=verify-full&sslrootcert=system"
)


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

    def test_dotenv_is_read_and_imported_in_development_only(self, tmp_path):
        """A .env file configures development; production ignores it and never
        imports python-dotenv (kept off the API's start-up path)."""
        import json
        import os
        import subprocess
        import sys

        (tmp_path / ".env").write_text("ATUS_LOG_LEVEL=DEBUG\n")
        code = ("import json, sys; from atus_pipeline.config import load_settings; "
                "s = load_settings(); "
                "print(json.dumps([s.log_level, 'dotenv' in sys.modules]))")
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("ATUS_", "PG")) and k != "PYTHONPATH"}

        def run(**env) -> list:
            result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                                    env={**base, **env}, capture_output=True, text=True,
                                    timeout=60, check=False)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        assert run() == ["DEBUG", True]
        assert run(**prod_env()) == ["INFO", False]

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


STACK_PASSWORD = "Stack-Passw0rd-42"


def stack_env(url: str, **extra) -> dict[str, str]:
    """Production mode with the local stack's escape hatch."""
    return prod_env(ATUS_DATABASE_URL=url, ATUS_DATABASE_TLS="local-stack-only", **extra)


class TestDatabaseTls:
    """Production refuses a database URL that does not verify the server's
    certificate (docs/security.md R11); ATUS_DATABASE_TLS=local-stack-only is
    the escape hatch for the local production-mode stack, and only there."""

    @pytest.mark.parametrize(
        ("query", "shown"),
        [
            ("", "no sslmode"),
            ("?sslmode=disable", "sslmode=disable"),
            ("?sslmode=allow", "sslmode=allow"),
            ("?sslmode=prefer", "sslmode=prefer"),
            ("?sslmode=require", "sslmode=require"),  # encrypted, but to anyone
            ("?sslmode=require&sslrootcert=system", "sslmode=require"),
            ("?sslmode=VERIFY-FULL", "an unrecognised sslmode"),  # libpq: case-sensitive
            ("?sslmode=Very-S3cret-Thing", "an unrecognised sslmode"),
        ],
    )
    def test_production_refuses_urls_that_do_not_verify(self, query, shown):
        url = f"postgresql://atus_api:{STACK_PASSWORD}@ep-1.us-east-1.aws.neon.tech/atus{query}"
        with pytest.raises(ConfigError, match="sslmode") as info:
            load_settings(prod_env(ATUS_DATABASE_URL=url))
        message = str(info.value)
        assert shown in message and "sslmode=verify-full" in message
        assert STACK_PASSWORD not in message and "neon.tech" not in message
        assert "Very-S3cret" not in message  # an odd value is never echoed

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://atus_api:pw@ep-1.us-east-1.aws.neon.tech:5432/atus"
            "?sslmode=verify-full&sslrootcert=system",
            "postgresql://atus_api:pw@db.internal/atus?sslmode=verify-ca&sslrootcert=/ca.pem",
            "host=ep-1.neon.tech dbname=atus user=atus_api sslmode=verify-full",
        ],
    )
    def test_production_accepts_verifying_urls(self, url):
        settings = load_settings(prod_env(ATUS_DATABASE_URL=url))
        check_api_settings(settings)
        assert settings.database_tls == "verify"

    def test_pgsslmode_in_the_environment_does_not_count(self):
        """The rule reads the URL: libpq's PGSSLMODE fallback is not proof."""
        url = "postgresql://atus_api:pw@ep-1.neon.tech/atus"
        with pytest.raises(ConfigError, match="no sslmode"):
            load_settings(prod_env(ATUS_DATABASE_URL=url, PGSSLMODE="verify-full"))

    def test_development_does_not_require_tls(self):
        settings = load_settings({"ATUS_DATABASE_URL": "postgresql://u:p@h.example/db"})
        check_api_settings(settings)
        assert settings.database_tls == "verify"

    @pytest.mark.parametrize(
        "url",
        [
            f"postgresql://atus_api:{STACK_PASSWORD}@db:5432/atus",  # the compose service
            f"postgresql://atus_api:{STACK_PASSWORD}@pgbouncer:6432/atus",
            f"postgresql://atus:{STACK_PASSWORD}@localhost:5434/atus_test_x",
            f"postgresql://atus:{STACK_PASSWORD}@127.0.0.1:5434/atus",
            f"postgresql://atus:{STACK_PASSWORD}@[::1]:5434/atus",
            f"postgresql://atus:{STACK_PASSWORD}@host.docker.internal:5434/atus",
            "postgresql:///atus?host=/var/run/postgresql",  # Unix socket
            "host=db,pgbouncer port=5432,6432 dbname=atus user=atus_api",
            "host=db hostaddr=127.0.0.1 dbname=atus user=atus_api",
            # with verification as well: still the local stack
            "postgresql://atus_api:pw@db/atus?sslmode=verify-full",
        ],
    )
    def test_local_stack_hatch_accepts_local_hosts(self, url):
        settings = load_settings(stack_env(url))
        check_api_settings(settings)
        assert settings.database_tls == "local-stack-only"

    @pytest.mark.parametrize(
        "url",
        [
            f"postgresql://atus_api:{STACK_PASSWORD}@ep-1.us-east-1.aws.neon.tech/atus",
            # even with verification: the hatch has no business outside the stack
            "postgresql://atus_api:pw@ep-1.neon.tech/atus?sslmode=verify-full"
            "&sslrootcert=system",
            "postgresql://atus_api:pw@db.internal:5432/atus",  # dotted: a DNS name
            "postgresql://atus_api:pw@10.0.0.5:5432/atus",  # private, not loopback
            "postgresql://atus_api:pw@3627734734:5432/atus",  # a numeric IPv4 address
            "postgresql://atus_api:pw@0xd83ad6ce:5432/atus",  # ... in hex
            "host=db hostaddr=203.0.113.5 dbname=atus",  # libpq dials hostaddr
            "host=db,ep-1.neon.tech dbname=atus",  # one remote host is enough
            "dbname=atus user=atus_api",  # no host: libpq would read PGHOST
            "host=db,, dbname=atus",  # ... for an empty entry too
            "host=db service=prod dbname=atus",  # a service file can name any host
        ],
    )
    def test_local_stack_hatch_refuses_other_hosts(self, url):
        with pytest.raises(ConfigError, match="local-stack-only is accepted only") as info:
            load_settings(stack_env(url))
        assert STACK_PASSWORD not in str(info.value) and "neon.tech" not in str(info.value)

    @pytest.mark.parametrize(
        ("host", "local"),
        [
            ("db", True), ("pgbouncer", True), ("atus-stack_db", True), ("", False),
            ("/tmp", True), ("@abstract", True), ("LOCALHOST", True), ("127.0.0.2", True),
            ("::1", True), ("host.docker.internal", True),
            ("db.example.com", False), ("example.com.", False), ("10.1.2.3", False),
            ("192.168.1.1", False), ("8.8.8.8", False), ("2001:db8::1", False),
            ("1234", False), ("0x7f000001", False), ("-db", False), ("d" * 64, False),
        ],
    )
    def test_local_stack_hosts(self, host, local):
        assert is_local_stack_host(host) is local

    def test_programmatic_settings_are_checked_again(self, tmp_path):
        settings = load_settings(prod_env())
        plain = replace(settings, database_url="postgresql://atus_api:pw@ep-1.neon.tech/atus")
        with pytest.raises(ConfigError, match="no sslmode"):
            check_api_settings(plain)
        with pytest.raises(ConfigError, match="no sslmode"):
            create_app(plain, open_pool=False)
        hatch_elsewhere = replace(settings, database_tls="local-stack-only")
        with pytest.raises(ConfigError, match="local-stack-only is accepted only"):
            check_api_settings(hatch_elsewhere)
        with pytest.raises(ConfigError, match="ATUS_DATABASE_TLS must be one of"):
            check_api_settings(replace(settings, database_tls="off"))


class TestValidation:
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("ATUS_ENV", "staging"),
            ("ATUS_DATABASE_TLS", "off"),
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
