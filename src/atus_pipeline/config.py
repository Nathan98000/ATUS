"""Environment-based configuration.

All settings come from environment variables so nothing machine-specific is
hard-coded. Outside production a `.env` file in the working directory (or a
parent of it) is loaded first; in production (``ATUS_ENV=production``) only
the real environment counts, as in any 12-factor deployment. Defaults are safe
for local development against the docker-compose PostgreSQL instance.

Every value is validated when it is loaded, so a typo fails at startup with a
message naming the variable rather than at the first request. Messages never
echo the values of secret variables (the database URL, edge secrets, ops
token).

Production mode never falls back to the development database URL: a missing
``ATUS_DATABASE_URL`` is an error for every command, and so is one that does
not verify the server's certificate and host name (``sslmode=verify-full``)
unless ``ATUS_DATABASE_TLS=local-stack-only`` is set for a database on the
local stack (:func:`check_database_tls`). The API
additionally refuses to start in production without ``ATUS_EDGE_SECRETS``
(:func:`check_api_settings`).
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE_URL = "postgresql://atus:atus@localhost:5434/atus"
DEFAULT_DATA_DIR = "data"
DEFAULT_RELEASE = "0325"

ENVIRONMENTS = ("development", "production")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_FORMATS = ("text", "json")

# Accepted edge-secret values are compared against an HTTP header, so they
# must be visible ASCII without commas (the list separator).
_SECRET_PATTERN = re.compile(r"^[!-+\--~]+$")  # 0x21-0x7e except ','
MIN_PRODUCTION_SECRET_LENGTH = 32
MAX_EDGE_SECRETS = 2  # current + next, for rotation
_BUILD_SHA_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# TLS to the database in production (ADR-011 "TLS": never `require` alone).
DATABASE_TLS_VERIFY = "verify"
DATABASE_TLS_LOCAL_STACK = "local-stack-only"
DATABASE_TLS_MODES = (DATABASE_TLS_VERIFY, DATABASE_TLS_LOCAL_STACK)
# Only verify-full checks the host name: verify-ca accepts any certificate the
# root bundle chains to, so with a public bundle (the image's SSL_CERT_FILE)
# any publicly issued certificate for any domain would pass (review r2).
VERIFYING_SSLMODES = ("verify-full",)
_LIBPQ_SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
# Hosts the local production-mode stack uses: compose service names (one DNS
# label that starts with a letter, so it can never be read as a numeric IPv4
# address), loopback, Docker Desktop's alias for the host, Unix sockets.
_LOCAL_STACK_NAMES = frozenset({"localhost", "host.docker.internal"})
_SERVICE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")


class ConfigError(ValueError):
    """A configuration variable is missing or invalid.

    The message names the variable and the expected form; it never contains
    the value of a secret variable.
    """


@dataclass(frozen=True)
class RateLimit:
    """A token-bucket rate: ``limit`` requests per ``period_s`` seconds, with
    a burst of ``limit``. Written ``"<limit>/<seconds>"`` in the environment,
    e.g. ``6/60``."""

    limit: int
    period_s: float

    @property
    def refill_per_second(self) -> float:
        return self.limit / self.period_s

    def __str__(self) -> str:
        return f"{self.limit}/{self.period_s:g}"

    @classmethod
    def parse(cls, text: str, name: str) -> RateLimit:
        limit_text, sep, period_text = text.strip().partition("/")
        try:
            if not sep:
                raise ValueError
            limit = int(limit_text)
            period = float(period_text)
        except ValueError:
            raise ConfigError(
                f"{name}={text!r} must look like '<requests>/<seconds>', e.g. '6/60'"
            ) from None
        if not (1 <= limit <= 1_000_000) or not (math.isfinite(period) and 0 < period <= 86_400):
            raise ConfigError(
                f"{name}={text!r}: requests must be 1-1000000 and seconds in (0, 86400]"
            )
        return cls(limit=limit, period_s=period)


@dataclass(frozen=True)
class Settings:
    # Secret-bearing fields are excluded from repr() so a logged or printed
    # Settings object cannot leak them.
    database_url: str = field(repr=False)
    data_dir: Path
    release: str
    log_level: str
    # Phase 3 API settings
    api_cors_origins: tuple[str, ...] = ()   # allowed frontend origins, e.g. http://localhost:3000
    api_cache_size: int = 256                # analysis-result cache entries (LRU)
    # Phase 5: deployment mode, observability, edge trust
    env: str = "development"
    database_tls: str = DATABASE_TLS_VERIFY  # production: verify the server certificate
    log_format: str = "text"
    api_docs: bool = True                    # /docs, /redoc, /openapi.json
    edge_secrets: tuple[str, ...] = field(default=(), repr=False)
    expected_data_version: str | None = None
    ops_token: str | None = field(default=None, repr=False)
    build_sha: str = "dev"
    port: int = 8000
    # Phase 5: database pool and per-request session limits
    api_pool_max_size: int = 4
    api_pool_max_idle_s: float = 240.0       # below Neon's 300 s scale-to-zero
    api_pool_timeout_s: float = 5.0          # wait for a busy pool -> 503 overloaded
    api_connect_timeout_s: int = 10          # libpq connect_timeout
    api_statement_timeout_ms: int = 85_000   # analyses
    api_meta_statement_timeout_ms: int = 10_000  # metadata/lexicon reads
    api_jit: bool = False
    api_max_parallel_workers: int | None = None  # None = server default
    api_prepare_threshold: int | None = 5    # psycopg default; None behind a transaction pooler
    # Phase 5: load and quota protection (ADR-012)
    api_max_inflight: int = 2                # uncached analyses computing at once
    api_max_inflight_heavy: int = 1          # ... of which heavy
    api_admission_wait_s: float = 5.0
    api_rate_analysis: RateLimit = RateLimit(6, 60.0)
    api_rate_requests: RateLimit = RateLimit(120, 60.0)
    api_daily_analysis_cap: int = 600
    api_max_body_bytes: int = 65_536
    api_threads: int = 16
    api_snapshot_ttl_s: float = 2.0

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def edge_auth_enabled(self) -> bool:
        return bool(self.edge_secrets)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging" / self.release

    @property
    def reference_dir(self) -> Path:
        return self.data_dir / "reference"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"


# --------------------------------------------------------------------------- #
# parsing helpers                                                              #
# --------------------------------------------------------------------------- #


class _Env:
    """Reads and validates variables from one environment mapping."""

    def __init__(self, environ: Mapping[str, str]):
        self._environ = environ

    def raw(self, name: str) -> str | None:
        value = self._environ.get(name)
        if value is None:
            return None
        value = value.strip()
        return value or None

    def choice(self, name: str, default: str, choices: tuple[str, ...], *, upper=False) -> str:
        value = self.raw(name)
        if value is None:
            return default
        value = value.upper() if upper else value.lower()
        if value not in choices:
            raise ConfigError(f"{name}={value!r} must be one of: {', '.join(choices)}")
        return value

    def boolean(self, name: str, default: bool) -> bool:
        value = self.raw(name)
        if value is None:
            return default
        lowered = value.lower()
        if lowered in ("on", "true", "1", "yes"):
            return True
        if lowered in ("off", "false", "0", "no"):
            return False
        raise ConfigError(f"{name}={value!r} must be 'on' or 'off'")

    def integer(self, name: str, default: int, low: int, high: int) -> int:
        value = self.raw(name)
        if value is None:
            return default
        try:
            number = int(value)
        except ValueError:
            raise ConfigError(f"{name}={value!r} must be an integer") from None
        if not low <= number <= high:
            raise ConfigError(f"{name}={number} must be between {low} and {high}")
        return number

    def optional_integer(self, name: str, low: int, high: int, *, none_words=()) -> int | None:
        value = self.raw(name)
        if value is None or value.lower() in none_words:
            return None
        return self.integer(name, 0, low, high)

    def number(self, name: str, default: float, low: float, high: float) -> float:
        value = self.raw(name)
        if value is None:
            return default
        try:
            number = float(value)
        except ValueError:
            raise ConfigError(f"{name}={value!r} must be a number") from None
        if not (math.isfinite(number) and low <= number <= high):
            raise ConfigError(f"{name}={value!r} must be between {low:g} and {high:g}")
        return number

    def rate(self, name: str, default: RateLimit) -> RateLimit:
        value = self.raw(name)
        return default if value is None else RateLimit.parse(value, name)


def _validate_database_url(url: str) -> None:
    """Fail early (and without echoing the URL) on a malformed DSN.

    libpq's own error for, e.g., an un-encoded ``%`` in the password quotes
    the offending token, which is the password; psycopg_pool would then log
    it on every reconnect attempt (audit I07).
    """
    from psycopg.conninfo import conninfo_to_dict

    try:
        conninfo_to_dict(url)
    except Exception:
        raise ConfigError(
            "ATUS_DATABASE_URL is not a valid PostgreSQL connection string "
            "(percent-encode special characters in the user name and password)"
        ) from None


def is_local_stack_host(host: str) -> bool:
    """Whether a libpq ``host`` entry can only be the local stack's database:
    a Unix-socket directory, ``localhost``, a loopback address, Docker
    Desktop's ``host.docker.internal``, or a compose service name (one DNS
    label starting with a letter: no dots, so no public DNS name). An empty
    entry is not: libpq would take the host from ``PGHOST`` instead. A single
    label is still resolved by the container's resolver (whose search domains
    could extend it); on the stack, Docker's embedded DNS answers it."""
    if not host:
        return False
    if host.startswith(("/", "@")):  # a Unix-domain socket directory (or abstract name)
        return True
    if host.lower() in _LOCAL_STACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return bool(_SERVICE_NAME.fullmatch(host))


def _is_loopback_address(text: str) -> bool:
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def check_database_tls(
    database_url: str, tls_mode: str, environ: Mapping[str, str] | None = None
) -> None:
    """Production's rule for the database connection (ADR-011 "TLS").

    ``ATUS_DATABASE_URL`` itself must set ``sslmode=verify-full``: libpq's
    default (``prefer``) and ``require`` encrypt without checking whom they
    talk to, ``verify-ca`` checks the certificate chain but not the host name
    (with a public CA bundle, any public certificate for any domain passes),
    and a ``PGSSLMODE`` in the environment is not proof of anything the URL
    does not say.

    ``tls_mode`` ``local-stack-only`` (``ATUS_DATABASE_TLS``) is the escape
    hatch for the local production-mode stack, whose Postgres has no TLS. It
    is accepted only when the URL names its hosts itself and every ``host``
    is a local-stack host (:func:`is_local_stack_host`), every ``hostaddr``
    a loopback address, and no ``service`` (whose file could name any host)
    is used. libpq also takes ``hostaddr`` and ``service`` from
    ``PGHOSTADDR`` and ``PGSERVICE`` when the URL does not set them, so those
    in ``environ`` (default: the process environment, which libpq reads) are
    held to the same rule. The hatch thus never applies to a host the URL
    does not name as local (a single-label name is still resolved by the
    container's resolver: :func:`is_local_stack_host`). Messages never echo
    the URL.
    """
    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(database_url)  # validated by _validate_database_url
    if tls_mode == DATABASE_TLS_LOCAL_STACK:
        environ = os.environ if environ is None else environ
        hosts = str(params.get("host") or "").split(",")
        hostaddr = params.get("hostaddr") or environ.get("PGHOSTADDR") or ""
        addresses = [a for a in str(hostaddr).split(",") if a]
        if (
            not (params.get("service") or environ.get("PGSERVICE"))
            and all(is_local_stack_host(h.strip()) for h in hosts)
            and all(_is_loopback_address(a.strip()) for a in addresses)
        ):
            return
        raise ConfigError(
            "ATUS_DATABASE_TLS=local-stack-only is accepted only for a database on the "
            "local stack, named in ATUS_DATABASE_URL itself (a compose service name "
            "without dots, localhost or a loopback address, host.docker.internal, or a "
            "Unix socket; no service file, and no PGSERVICE or non-loopback PGHOSTADDR "
            "in the environment); ATUS_DATABASE_URL names another or no host. "
            "Remove ATUS_DATABASE_TLS and use sslmode=verify-full"
        )
    sslmode = str(params.get("sslmode") or "")  # libpq compares it case-sensitively
    if sslmode in VERIFYING_SSLMODES:
        return
    shown = f"sslmode={sslmode}" if sslmode in _LIBPQ_SSLMODES else (
        "no sslmode" if not sslmode else "an unrecognised sslmode"
    )
    why = (" (verify-ca checks the certificate chain but not the server's host name)"
           if sslmode == "verify-ca" else "")
    raise ConfigError(
        f"ATUS_DATABASE_URL has {shown}{why}; when ATUS_ENV=production it must verify the "
        "server's certificate and host name: use sslmode=verify-full (with "
        "sslrootcert=system for a public CA) in the URL"
    )


def _edge_secrets(env: _Env, production: bool) -> tuple[str, ...]:
    raw = env.raw("ATUS_EDGE_SECRETS")
    if raw is None:
        return ()
    secrets = tuple(part.strip() for part in raw.split(",") if part.strip())
    if len(secrets) > MAX_EDGE_SECRETS:
        raise ConfigError(
            f"ATUS_EDGE_SECRETS accepts at most {MAX_EDGE_SECRETS} values (current,next)"
        )
    for position, secret in enumerate(secrets, start=1):
        if not _SECRET_PATTERN.match(secret):
            raise ConfigError(
                f"ATUS_EDGE_SECRETS value #{position} must be visible ASCII "
                "without spaces or commas"
            )
        if production and len(secret) < MIN_PRODUCTION_SECRET_LENGTH:
            raise ConfigError(
                f"ATUS_EDGE_SECRETS value #{position} is shorter than "
                f"{MIN_PRODUCTION_SECRET_LENGTH} characters (required in production)"
            )
    return secrets


def _ops_token(env: _Env, production: bool) -> str | None:
    token = env.raw("ATUS_OPS_TOKEN")
    if token is None:
        return None
    if not _SECRET_PATTERN.match(token):
        raise ConfigError("ATUS_OPS_TOKEN must be visible ASCII without spaces or commas")
    if production and len(token) < MIN_PRODUCTION_SECRET_LENGTH:
        raise ConfigError(
            f"ATUS_OPS_TOKEN is shorter than {MIN_PRODUCTION_SECRET_LENGTH} characters "
            "(required in production)"
        )
    return token


def _prepare_threshold(env: _Env) -> int | None:
    """psycopg's auto-prepare threshold: unset keeps psycopg's default (5);
    ``none``/``off`` disables server-side prepared statements, which
    transaction-mode poolers may not support (audit F18)."""
    if env.raw("ATUS_API_PREPARE_THRESHOLD") is None:
        return 5
    return env.optional_integer(
        "ATUS_API_PREPARE_THRESHOLD", 0, 1_000_000, none_words=("none", "off")
    )


def _cors_origins(env: _Env) -> tuple[str, ...]:
    raw = env.raw("ATUS_API_CORS_ORIGINS") or ""
    origins = tuple(origin.strip() for origin in raw.split(",") if origin.strip())
    for origin in origins:
        if origin == "*" or not re.match(r"^https?://[^/\s*]+$", origin):
            raise ConfigError(
                f"ATUS_API_CORS_ORIGINS entry {origin!r} must be an exact origin such as "
                "'http://localhost:5173' (no wildcard, no path)"
            )
    return origins


# --------------------------------------------------------------------------- #
# public API                                                                   #
# --------------------------------------------------------------------------- #


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build validated settings from the environment.

    With ``environ=None`` the process environment is used, after loading a
    `.env` file found from the working directory upwards (development only).
    Raises :class:`ConfigError` on any invalid value.
    """
    if environ is None:
        if os.environ.get("ATUS_ENV", "").strip().lower() != "production":
            # Imported here: production never reads a .env file, so the API's
            # start-up does not import python-dotenv (deploy/ci/check_image.py).
            from dotenv import find_dotenv, load_dotenv

            load_dotenv(find_dotenv(usecwd=True))
        environ = os.environ
    env = _Env(environ)

    mode = env.choice("ATUS_ENV", "development", ENVIRONMENTS)
    production = mode == "production"

    database_url = env.raw("ATUS_DATABASE_URL")
    if database_url is None:
        if production:
            raise ConfigError(
                "ATUS_DATABASE_URL is required when ATUS_ENV=production "
                "(the development default is never used in production)"
            )
        database_url = DEFAULT_DATABASE_URL
    elif production and database_url == DEFAULT_DATABASE_URL:
        raise ConfigError(
            "ATUS_DATABASE_URL is the well-known development URL; refusing it "
            "when ATUS_ENV=production"
        )
    _validate_database_url(database_url)
    database_tls = env.choice("ATUS_DATABASE_TLS", DATABASE_TLS_VERIFY, DATABASE_TLS_MODES)
    if production:
        check_database_tls(database_url, database_tls, environ)

    max_inflight = env.integer("ATUS_API_MAX_INFLIGHT", 2, 1, 64)
    max_inflight_heavy = env.integer("ATUS_API_MAX_INFLIGHT_HEAVY", 1, 1, 64)
    if max_inflight_heavy > max_inflight:
        raise ConfigError(
            "ATUS_API_MAX_INFLIGHT_HEAVY must not exceed ATUS_API_MAX_INFLIGHT"
        )
    pool_max_size = env.integer("ATUS_API_POOL_MAX_SIZE", 4, 1, 50)
    if pool_max_size <= max_inflight:
        # Lanes come before pool checkouts (ADR-012) so that metadata reads
        # and cache lookups always find a connection: the pool must hold at
        # least one connection beyond what the computations can take.
        raise ConfigError(
            f"ATUS_API_POOL_MAX_SIZE ({pool_max_size}) must exceed ATUS_API_MAX_INFLIGHT "
            f"({max_inflight}), so metadata reads always find a connection"
        )

    build_sha = env.raw("ATUS_BUILD_SHA") or "dev"
    if not _BUILD_SHA_PATTERN.match(build_sha):
        raise ConfigError("ATUS_BUILD_SHA must be 1-64 characters of [A-Za-z0-9._-]")

    expected_version = env.raw("ATUS_EXPECTED_DATA_VERSION")
    if expected_version is not None and len(expected_version) > 200:
        raise ConfigError("ATUS_EXPECTED_DATA_VERSION must be at most 200 characters")

    return Settings(
        database_url=database_url,
        data_dir=Path(env.raw("ATUS_DATA_DIR") or DEFAULT_DATA_DIR),
        release=env.raw("ATUS_RELEASE") or DEFAULT_RELEASE,
        log_level=env.choice("ATUS_LOG_LEVEL", "INFO", LOG_LEVELS, upper=True),
        api_cors_origins=_cors_origins(env),
        api_cache_size=env.integer("ATUS_API_CACHE_SIZE", 256, 1, 100_000),
        env=mode,
        database_tls=database_tls,
        log_format=env.choice(
            "ATUS_LOG_FORMAT", "json" if production else "text", LOG_FORMATS
        ),
        api_docs=env.boolean("ATUS_API_DOCS", not production),
        edge_secrets=_edge_secrets(env, production),
        expected_data_version=expected_version,
        ops_token=_ops_token(env, production),
        build_sha=build_sha,
        port=env.integer("PORT", 8000, 1, 65_535),
        api_pool_max_size=pool_max_size,
        api_pool_max_idle_s=env.number("ATUS_API_POOL_MAX_IDLE_S", 240.0, 1.0, 86_400.0),
        api_pool_timeout_s=env.number("ATUS_API_POOL_TIMEOUT_S", 5.0, 0.1, 120.0),
        api_connect_timeout_s=env.integer("ATUS_API_CONNECT_TIMEOUT_S", 10, 2, 120),
        api_statement_timeout_ms=env.integer(
            "ATUS_API_STATEMENT_TIMEOUT_MS", 85_000, 100, 600_000
        ),
        api_meta_statement_timeout_ms=env.integer(
            "ATUS_API_META_STATEMENT_TIMEOUT_MS", 10_000, 100, 600_000
        ),
        api_jit=env.boolean("ATUS_API_JIT", False),
        api_max_parallel_workers=env.optional_integer(
            "ATUS_API_MAX_PARALLEL_WORKERS", 0, 64
        ),
        api_prepare_threshold=_prepare_threshold(env),
        api_max_inflight=max_inflight,
        api_max_inflight_heavy=max_inflight_heavy,
        api_admission_wait_s=env.number("ATUS_API_ADMISSION_WAIT_S", 5.0, 0.0, 120.0),
        api_rate_analysis=env.rate("ATUS_API_RATE_ANALYSIS", RateLimit(6, 60.0)),
        api_rate_requests=env.rate("ATUS_API_RATE_REQUESTS", RateLimit(120, 60.0)),
        api_daily_analysis_cap=env.integer("ATUS_API_DAILY_ANALYSIS_CAP", 600, 1, 10_000_000),
        api_max_body_bytes=env.integer("ATUS_API_MAX_BODY_BYTES", 65_536, 1_024, 10_485_760),
        api_threads=env.integer("ATUS_API_THREADS", 16, 1, 256),
        api_snapshot_ttl_s=env.number(
            "ATUS_API_SNAPSHOT_TTL_S", 600.0 if production else 2.0, 0.0, 86_400.0
        ),
    )


def check_api_settings(settings: Settings) -> None:
    """Refuse to serve the API with an unsafe production configuration.

    Production requires an explicit database URL (enforced by
    :func:`load_settings`) and at least one edge secret: without one, the
    provider hostname would serve the API to anyone, bypassing every
    protection that lives at the edge. The database URL rules are checked
    again here (against the process environment, which is what libpq will
    read), so programmatically built settings (``Settings(...)``,
    ``dataclasses.replace``) cannot skip them.
    """
    if not settings.is_production:
        return
    if not settings.edge_secrets:
        raise ConfigError(
            "ATUS_EDGE_SECRETS is required when ATUS_ENV=production "
            "(comma-separated current,next values of the X-ATUS-Edge-Auth header)"
        )
    if settings.database_url == DEFAULT_DATABASE_URL:
        raise ConfigError(
            "ATUS_DATABASE_URL is the well-known development URL; refusing it "
            "when ATUS_ENV=production"
        )
    if settings.database_tls not in DATABASE_TLS_MODES:
        raise ConfigError(f"ATUS_DATABASE_TLS must be one of: {', '.join(DATABASE_TLS_MODES)}")
    _validate_database_url(settings.database_url)
    check_database_tls(settings.database_url, settings.database_tls)
