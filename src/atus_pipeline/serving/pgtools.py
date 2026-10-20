"""Running the PostgreSQL client tools (``pg_dump``, ``pg_restore``, ``psql``).

A tool runs from the local installation when its major version is at least
the server's (brief §9: restore with a client at least as new as the
server), and otherwise from the official ``postgres:<server major>`` image
through ``docker run``. Either way the connection, password included,
travels in libpq environment variables (``PGHOST``, ``PGPASSWORD``, ...): it
never appears in a command line, where ``ps`` would show it, and the
docker invocation names the variables without their values.

A server on this machine (``localhost``) is reached from the container
through ``host.docker.internal`` (Docker Desktop) or the host network
(Linux).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from ..database.dsn import libpq_env, redact

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TOOLS = ("pg_dump", "pg_restore", "psql")
IMAGE_ENV = "ATUS_PG_CLIENT_IMAGE"

_VERSION = re.compile(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?")


class PgToolError(RuntimeError):
    """A client tool is unavailable or failed (its output is redacted)."""


@dataclass(frozen=True)
class PgTool:
    name: str
    argv: tuple[str, ...]        # command up to and including the tool
    env: dict[str, str]          # full environment for the subprocess
    origin: str                  # "local /usr/bin/pg_dump" or "docker postgres:16"
    secret_values: tuple[str, ...] = ()   # redacted from the tool's output

    def run(self, args: list[str], *, stdin=None, stdout=None, input_bytes: bytes | None = None,
            secrets: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        """Run the tool; raise :class:`PgToolError` with redacted stderr on failure.

        Captured stdout/stderr are returned undecoded (stdout) and redacted
        (stderr, as text)."""
        if stdin is None and input_bytes is None:
            stdin = subprocess.DEVNULL
        result = subprocess.run(
            [*self.argv, *args],
            env=self.env,
            stdin=stdin,
            input=input_bytes,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        stderr = redact(result.stderr.decode(errors="replace"), *self._secrets(secrets))
        result.stderr = stderr
        if result.returncode != 0:
            raise PgToolError(
                f"{self.name} failed (exit {result.returncode}, {self.origin}):\n{stderr.strip()}"
            )
        return result

    def version(self) -> str:
        out = subprocess.run([*self.argv, "--version"], env=self.env, capture_output=True,
                             check=False)
        return out.stdout.decode(errors="replace").strip()

    def _secrets(self, extra: tuple[str, ...]) -> tuple[str, ...]:
        return (self.env.get("PGPASSWORD", ""), *self.secret_values, *extra)


def parse_major(version_text: str) -> int | None:
    """``pg_dump (PostgreSQL) 16.4`` -> 16."""
    match = _VERSION.search(version_text)
    return int(match.group(1)) if match else None


def local_major(tool: str) -> int | None:
    path = shutil.which(tool)
    if not path:
        return None
    out = subprocess.run([path, "--version"], capture_output=True, check=False)
    return parse_major(out.stdout.decode(errors="replace"))


def docker_argv(tool: str, env: dict[str, str], image: str, *,
                platform: str = sys.platform) -> tuple[tuple[str, ...], dict[str, str]]:
    """The ``docker run`` command for ``tool`` and the libpq environment it
    needs (``PGHOST`` rewritten for a server on this machine)."""
    env = dict(env)
    argv = ["docker", "run", "--rm", "-i"]
    host = env.get("PGHOST", "localhost")
    if host.startswith("/"):
        raise PgToolError("a Unix-socket connection cannot be used from a container")
    if host in LOCAL_HOSTS or "PGHOST" not in env:
        if platform.startswith("linux"):
            argv += ["--network", "host"]
        else:
            env["PGHOST"] = "host.docker.internal"
    for name in sorted(env):
        argv += ["-e", name]     # the value comes from docker's own environment
    argv += [image, tool]
    return tuple(argv), env


def resolve(tool: str, conninfo: str, *, server_major: int, image: str | None = None,
            prefer_docker: bool = False, extra_env: dict[str, str] | None = None) -> PgTool:
    """``tool`` connected to ``conninfo``: local if new enough, else docker.

    ``extra_env`` adds environment variables the tool needs besides the
    connection (passed to a container by name, like the connection)."""
    if tool not in TOOLS:
        raise ValueError(f"unknown tool {tool!r}")
    env = {**libpq_env(conninfo), **(extra_env or {})}
    major = None if prefer_docker else local_major(tool)
    if major is not None and major >= server_major:
        path = shutil.which(tool)
        return PgTool(tool, (path,), {**os.environ, **env}, f"local {path} ({major})",
                      tuple((extra_env or {}).values()))
    if not shutil.which("docker"):
        found = f"found version {major}" if major else "none found"
        raise PgToolError(
            f"{tool} {server_major} or newer is needed ({found}) and docker is not "
            "available to run it from the postgres image"
        )
    image = image or os.environ.get(IMAGE_ENV) or f"postgres:{server_major}"
    argv, env = docker_argv(tool, env, image)
    return PgTool(tool, argv, {**os.environ, **env}, f"docker {image}",
                  tuple((extra_env or {}).values()))
