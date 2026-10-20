"""Start, check and stop the harness's API servers (one process each).

Each server is the API exactly as its code base starts it (``atus api``),
listening on loopback, with a clean environment (no inherited ``ATUS_*`` or
``PG*`` variables) and its working directory in the run's output directory,
so no ``.env`` file can change its configuration (both code bases load one:
the baseline from the package's parent directories, Phase 5 from the working
directory's). Every database connection string gets
``default_transaction_read_only=on`` as a startup option: the full database
is read-only for this harness, and the serving database's API role already
defaults to it.

The Phase 5 servers run in development mode, i.e. without edge
authentication, with limits generous enough for a replay (loopback clients
are exempt from the per-client buckets when edge auth is off; the daily cap,
admission lanes and timeouts are raised so no capacity answer replaces a
result). Nothing here measures time; waits are only for readiness and
shutdown.

Connection strings are never printed or written: messages name databases as
``user@host:port/dbname``, and each server's log is redacted when it stops.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from atus_pipeline.config import DEFAULT_DATABASE_URL
from atus_pipeline.database.dsn import redact

READ_ONLY_OPTION = "-c default_transaction_read_only=on"

# Variables a child process may inherit (everything else is dropped).
INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT")

DEFAULT_MAX_BODY_BYTES = 65_536


class ServerError(RuntimeError):
    """A server could not be started, or died."""


def read_only_dsn(dsn: str) -> str:
    """``dsn`` with ``default_transaction_read_only=on`` added to its
    startup options (existing options are kept)."""
    params = conninfo_to_dict(dsn)
    options = params.get("options") or ""
    if "default_transaction_read_only" not in options:
        options = f"{options} {READ_ONLY_OPTION}".strip()
    return make_conninfo(dsn, options=options)


_PUBLIC_PASSWORD = conninfo_to_dict(DEFAULT_DATABASE_URL).get("password")


def dsn_secrets(dsn: str) -> tuple[str, ...]:
    """The password in ``dsn``, for masking wherever it appears in a log.
    The local development database's well-known password (committed in
    config.py and docker-compose.yml) is not a secret, and masking a word
    as short as it would mangle unrelated text; connection strings are still
    masked by pattern."""
    password = conninfo_to_dict(dsn).get("password")
    return (password,) if password and password != _PUBLIC_PASSWORD else ()


def clean_env(variables: dict[str, str]) -> dict[str, str]:
    env = {name: os.environ[name] for name in INHERITED_ENV if name in os.environ}
    env.update(variables)
    return env


def phase5_env(database_url: str, port: int, *, build_sha: str = "dev",
               log_level: str = "WARNING") -> dict[str, str]:
    """Environment of a Phase 5 API server for the replay.

    Development mode: no ``ATUS_EDGE_SECRETS`` (edge authentication off, so
    loopback peers skip the per-client buckets), no ``ATUS_OPS_TOKEN``, no
    ``ATUS_EXPECTED_DATA_VERSION`` (the harness checks ``X-Data-Version``
    itself). The daily cap and both rate buckets are effectively unlimited;
    four analyses may compute at once and wait up to 120 s for a lane; the
    statement timeout is 10 minutes (the baseline has none, so a timeout
    would be a harness artefact, not a difference). The snapshot TTL is the
    production value. Body limit, cache size, pool size and every other
    setting keep their defaults.
    """
    return {
        "ATUS_ENV": "development",
        "ATUS_DATABASE_URL": database_url,
        "ATUS_LOG_LEVEL": log_level,
        "ATUS_LOG_FORMAT": "text",
        "ATUS_API_DOCS": "off",
        "ATUS_BUILD_SHA": build_sha,
        "PORT": str(port),
        "ATUS_API_DAILY_ANALYSIS_CAP": "1000000",
        "ATUS_API_RATE_ANALYSIS": "1000000/1",
        "ATUS_API_RATE_REQUESTS": "1000000/1",
        "ATUS_API_MAX_INFLIGHT": "4",
        "ATUS_API_MAX_INFLIGHT_HEAVY": "2",
        "ATUS_API_ADMISSION_WAIT_S": "120",
        "ATUS_API_POOL_TIMEOUT_S": "60",
        "ATUS_API_STATEMENT_TIMEOUT_MS": "600000",
        "ATUS_API_SNAPSHOT_TTL_S": "600",
    }


def baseline_env(database_url: str, *, log_level: str = "WARNING") -> dict[str, str]:
    """Environment of the Phase 1-4 API (it reads only these and
    ``ATUS_API_CORS_ORIGINS``/``ATUS_API_CACHE_SIZE``, left at defaults)."""
    return {"ATUS_DATABASE_URL": database_url, "ATUS_LOG_LEVEL": log_level}


def phase5_command(python: str, port: int) -> list[str]:
    """``atus api`` of the Phase 5 code (``python -m atus_pipeline``)."""
    return [python, "-m", "atus_pipeline", "api", "--host", "127.0.0.1", "--port", str(port)]


def baseline_command(python: str, port: int) -> list[str]:
    """``atus api`` of the Phase 1-4 code (it has no ``__main__`` module;
    ``cli.py`` runs the group when executed)."""
    return [python, "-m", "atus_pipeline.cli", "api", "--host", "127.0.0.1",
            "--port", str(port)]


def stray_dotenv(directories: list[Path]) -> list[Path]:
    """``.env`` files in any of ``directories`` or their parents."""
    found: list[Path] = []
    for directory in directories:
        for parent in [directory.resolve(), *directory.resolve().parents]:
            candidate = parent / ".env"
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return found


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def http_get(port: int, path: str, *, timeout: float = 5.0) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Accept-Encoding": "identity"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@dataclass
class ServerConfig:
    key: str                         # a, b or c
    label: str
    command: list[str]
    cwd: Path
    env: dict[str, str]              # the complete environment
    port: int
    log_path: Path
    secrets: tuple[str, ...] = ()    # values to mask in the log
    package_dir: Path | None = None  # also searched for a .env (the baseline's lookup)


@dataclass
class ManagedServer:
    config: ServerConfig
    process: subprocess.Popen | None = None
    _log: object | None = field(default=None, repr=False)

    @property
    def port(self) -> int:
        return self.config.port

    def start(self) -> None:
        config = self.config
        if not port_is_free(config.port):
            raise ServerError(f"server {config.key}: port {config.port} is already in use")
        config.cwd.mkdir(parents=True, exist_ok=True)
        config.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(config.log_path, "wb")  # noqa: SIM115 (closed in stop())
        self.process = subprocess.Popen(
            config.command, cwd=config.cwd, env=config.env, stdin=subprocess.DEVNULL,
            stdout=self._log, stderr=subprocess.STDOUT, start_new_session=True,
        )

    def wait_ready(self, timeout: float = 90.0) -> dict:
        """Poll ``/health`` until it answers 200; returns its body."""
        deadline = time.monotonic() + timeout
        while True:
            if self.process is None or self.process.poll() is not None:
                raise ServerError(f"server {self.config.key} exited during start-up:\n"
                                  + self.log_tail())
            try:
                status, body = http_get(self.port, "/health", timeout=2.0)
                if status == 200:
                    return json.loads(body)
            except (OSError, http.client.HTTPException, ValueError):
                pass
            if time.monotonic() > deadline:
                raise ServerError(f"server {self.config.key} did not become ready on port "
                                  f"{self.port}:\n" + self.log_tail())
            time.sleep(0.25)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self, grace: float = 30.0) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
        if self._log is not None:
            self._log.close()
            self._log = None
        self.redact_log()

    def redact_log(self) -> None:
        path = self.config.log_path
        if path.exists():
            text = path.read_text(errors="replace")
            cleaned = redact(text, *self.config.secrets)
            if cleaned != text:
                path.write_text(cleaned)

    def log_tail(self, lines: int = 30) -> str:
        path = self.config.log_path
        if self._log is not None:
            self._log.flush()
        if not path.exists():
            return "(no log)"
        text = redact(path.read_text(errors="replace"), *self.config.secrets)
        return "\n".join(text.splitlines()[-lines:])


class ServerGroup:
    """Starts the servers together and always stops all of them (also on
    SIGTERM/SIGINT of the harness)."""

    def __init__(self, configs: list[ServerConfig]):
        self.servers = {config.key: ManagedServer(config) for config in configs}
        self._previous: dict[int, object] = {}

    def __enter__(self) -> ServerGroup:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            self._previous[sig] = signal.signal(sig, _raise_interrupt)
        try:
            for server in self.servers.values():
                server.start()
            for server in self.servers.values():
                server.wait_ready()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        try:
            for server in self.servers.values():
                server.stop()
        finally:
            for sig, previous in self._previous.items():
                signal.signal(sig, previous)
            self._previous.clear()


def _raise_interrupt(signum, frame) -> None:  # pragma: no cover - signal path
    raise KeyboardInterrupt(f"signal {signum}")
