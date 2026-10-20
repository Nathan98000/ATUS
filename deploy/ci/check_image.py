"""CI gate for the API image: non-root, no build tools, a health check that never touches the DB.

Brief §8 asks for "a non-root user ... no build tools or test data, and a
healthcheck that does not touch the database". This script proves all three on
the built image itself, with nothing but the Python standard library and the
Docker CLI, so a CI job can run it on the exact digest it is about to push:

    python deploy/ci/check_image.py atus-api:local
    python deploy/ci/check_image.py ghcr.io/nathan98000/atus-api@sha256:<digest>

Prints one line per check and exits 1 if any check fails (2 if the check could
not run at all: no Docker, unknown image, database never ready). Functional
only: the waits below are deadlines, nothing is timed or reported as a number.

Checks:
  user              the image config runs as a numeric, non-zero UID:GID, so a
                    platform can verify "not root" without starting it
  user_runtime      a process in the image and the running API server (PID 1)
                    have no root UID or group, and cannot write the installed
                    code or the working directory
  build_tools       no compiler, linker, build system, VCS or downloader on the
                    PATH; no compiler, ``*-dev`` or build-essential package; no
                    Python build backend; no build-stage leftovers or wheels.
                    pip stays: it is part of the Python base image, not a compiler
  healthcheck       the image declares a Docker HEALTHCHECK that requests /health
                    on loopback and names nothing database-related
  health_no_db      with a database host that does not resolve, the image's
                    health check passes (liveness does not depend on the DB)
  health_db_quiet   against a throwaway Postgres that logs every connection and
                    statement, N runs of the image's HEALTHCHECK command plus N
                    GET /health (what Render probes) produce no log line from the
                    API's role, and Docker's own health status turns healthy
  startup_db_quiet  the API opens no DB connection while starting and passing its
                    first health checks (ADR-011: pool min_size=0, so a Render
                    spin-up never wakes Neon). ``--allow-startup-connection``
                    reports it as SKIP, for images built before that pool change
  detector          control: one edge-authenticated GET /api/v1/meta does appear in
                    the database log, so a quiet log above is evidence, not a
                    blind spot

The database is ``postgres:16`` (pinned by digest) on an ``--internal`` Docker
network with trust authentication: no password and no edge secret ever appears
on a command line or in the output, and nothing can leave that network. Every
container and the network are removed on exit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

POSTGRES_IMAGE = (
    "postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
)
# The role the API connects as, here and in production (ADR-011 "Roles").
API_ROLE = "atus_api"
# A database host that can never resolve (RFC 6761 reserves .invalid).
UNRESOLVABLE_DATABASE_URL = f"postgresql://{API_ROLE}@db.invalid:5432/atus"
EDGE_AUTH_HEADER = "X-ATUS-Edge-Auth"
# Every backend's log lines start with this prefix, so the API role's lines are
# found by `user=atus_api ` whatever application_name the API sets.
LOG_LINE_PREFIX = "%m [%p] user=%u db=%d "

# --- what "no build tools" means ----------------------------------------------------

BUILD_TOOLS = (
    # compilers and toolchains
    "gcc", "cc", "c++", "g++", "clang", "clang++", "cpp", "tcc", "gfortran", "rustc",
    "cargo", "go",
    # binutils: linker, assembler, archiver
    "ld", "ld.bfd", "ld.gold", "as", "ar",
    # build systems and helpers
    "make", "gmake", "cmake", "ninja", "meson", "autoconf", "automake", "libtool",
    "pkg-config", "pkgconf",
    # version control and downloaders (how build steps usually fetch sources)
    "git", "curl", "wget",
)
BUILD_PACKAGES = frozenset({
    "build-essential", "make", "make-guile", "cmake", "ninja-build", "meson", "autoconf",
    "automake", "libtool", "libtool-bin", "pkgconf", "pkgconf-bin", "pkg-config",
    "dpkg-dev", "libc-dev-bin", "binutils", "git", "curl", "wget",
})
# gcc-14, g++-14, cpp-14-x86-64-linux-gnu, clang-19 ... but not gcc-14-base or
# libgcc-s1, which are runtime support files every Debian image carries.
COMPILER_PACKAGE = re.compile(
    r"^(gcc|g\+\+|cpp|clang|gfortran|tcc|llvm)(-\d+(\.\d+)?)?(-[a-z0-9_]+(-[a-z0-9_]+)*-linux-gnu\w*)?$"
)
BUILD_BACKENDS = frozenset({
    "setuptools", "wheel", "cython", "maturin", "skbuild", "scikit_build", "hatchling",
    "flit_core", "poetry", "pdm", "build", "_distutils_hack",
})
# Directories the Dockerfile's build stage uses; none may reach the runtime image.
BUILD_LEFTOVERS = ("/build", "/dist", "/opt/buildenv", "/root/.cache/pip")
# Where a stray wheel or sdist would sit (the venv lives under /opt).
ARCHIVE_ROOTS = ("/opt", "/tmp", "/app", "/root", "/home", "/usr/local/src")

# Runs inside the image (as its default user, with no network) and reports what
# is there as JSON; standard library only, like the rest of this script.
# TOOLS, LEFTOVERS and ARCHIVE_ROOTS are prepended by `probe_code`.
CONTENTS_PROBE = r"""
import glob, importlib.util, json, os, shutil
search = os.pathsep.join([os.environ.get("PATH", ""), "/usr/local/sbin", "/usr/local/bin",
                          "/usr/sbin", "/usr/bin", "/sbin", "/bin"])
tools = sorted(t for t in TOOLS if shutil.which(t, path=search))
packages = []
try:
    with open("/var/lib/dpkg/status") as handle:
        for block in handle.read().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in block.splitlines()
                          if ": " in line and not line.startswith(" "))
            if fields.get("Status", "").endswith(" installed") and "Package" in fields:
                packages.append(fields["Package"])
except FileNotFoundError:
    pass
site_entries = []
for pattern in ("/usr/local/lib/python3*/site-packages", "/opt/venv/lib/python3*/site-packages",
                "/usr/lib/python3/dist-packages"):
    for directory in glob.glob(pattern):
        site_entries.extend(os.listdir(directory))
wheels = []
for root in ARCHIVE_ROOTS:
    for dirpath, _, files in os.walk(root):
        wheels.extend(os.path.join(dirpath, f) for f in files if f.endswith((".whl", ".tar.gz")))
spec = importlib.util.find_spec("atus_pipeline")
code_dir = os.path.dirname(spec.origin) if spec and spec.origin else None
writable = [p for p in ("/opt/venv", code_dir, "/app", "/usr/local/lib")
            if p and os.access(p, os.W_OK)]
print(json.dumps({
    "uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(),
    "tools": tools, "packages": packages, "site_entries": site_entries,
    "leftovers": [p for p in LEFTOVERS if os.path.exists(p)],
    "archives": wheels, "writable": writable,
}))
"""


class CheckError(RuntimeError):
    """The check could not run (as opposed to: it ran and found a problem)."""


@dataclass
class Result:
    name: str
    status: str  # PASS, FAIL or SKIP
    detail: str


# --- pure evaluation (unit-tested without Docker) -----------------------------------


def user_problem(user: str | None) -> str | None:
    """Why the image's configured USER is not a verifiable non-root user, or None."""
    if not user:
        return "no USER in the image config: it runs as root"
    name, _, group = user.partition(":")
    if not name.isdigit():
        return f"USER {user!r} is not numeric: a platform cannot verify it is not root"
    if int(name) == 0:
        return f"USER {user!r} is root"
    if group and (not group.isdigit() or int(group) == 0):
        return f"USER {user!r}: the group must be numeric and not root"
    return None


def proc_ids(status_text: str) -> tuple[set[int], set[int]]:
    """Real/effective/saved/filesystem UIDs and GIDs from /proc/<pid>/status."""
    ids: dict[str, set[int]] = {"Uid": set(), "Gid": set()}
    for line in status_text.splitlines():
        key, _, values = line.partition(":")
        if key in ids:
            ids[key] = {int(v) for v in values.split()}
    return ids["Uid"], ids["Gid"]


def build_packages(packages: Iterable[str]) -> list[str]:
    """Installed Debian packages that are compilers, headers or build tooling."""
    found = []
    for name in packages:
        if (
            name in BUILD_PACKAGES
            or name.startswith("binutils-")
            or name.endswith("-dev")
            or COMPILER_PACKAGE.match(name)
        ):
            found.append(name)
    return sorted(found)


def build_backends(site_entries: Iterable[str]) -> list[str]:
    """Python build backends among site-packages entries (modules or .dist-info)."""
    found = set()
    for entry in site_entries:
        name = entry.split("-", 1)[0] if entry.endswith((".dist-info", ".egg-info")) else entry
        name = name.removesuffix(".py").removesuffix(".pth").lower().replace("-", "_")
        if name in BUILD_BACKENDS:
            found.add(name)
    return sorted(found)


def build_tool_problems(contents: dict) -> list[str]:
    """Everything in the image's content report that a runtime image must not hold."""
    problems = []
    if contents["tools"]:
        problems.append("on PATH: " + ", ".join(contents["tools"]))
    packages = build_packages(contents["packages"])
    if packages:
        problems.append("packages: " + ", ".join(packages))
    backends = build_backends(contents["site_entries"])
    if backends:
        problems.append("Python build backends: " + ", ".join(backends))
    if contents["leftovers"]:
        problems.append("build-stage leftovers: " + ", ".join(contents["leftovers"]))
    if contents["archives"]:
        problems.append("wheels/sdists: " + ", ".join(contents["archives"][:5]))
    return problems


def runtime_user_problems(contents: dict, pid1_status: str) -> list[str]:
    """Root identities or writable code, from the content report and PID 1's status."""
    problems = []
    if contents["uid"] == 0 or contents["gid"] == 0 or 0 in contents["groups"]:
        problems.append(
            f"a process in the image runs as uid={contents['uid']} gid={contents['gid']} "
            f"groups={contents['groups']}"
        )
    uids, gids = proc_ids(pid1_status)
    if not uids or 0 in uids or 0 in gids:
        problems.append(f"the API server (PID 1) runs with uids={sorted(uids)} gids={sorted(gids)}")
    if contents["writable"]:
        problems.append("writable by the app user: " + ", ".join(contents["writable"]))
    return problems


DB_MARKERS = ("/ready", "/metrics", "/api/", "pg_isready", "psql", "psycopg",
              "DATABASE_URL", "5432")


def healthcheck_problem(health: dict | None) -> str | None:
    """Why the image's HEALTHCHECK is missing or may reach beyond /health, or None."""
    test = (health or {}).get("Test") or []
    if not test:
        return "no HEALTHCHECK in the image"
    if test[0] == "NONE":
        return "HEALTHCHECK NONE disables the health check"
    if test[0] not in ("CMD", "CMD-SHELL") or len(test) < 2:
        return f"unrecognised HEALTHCHECK test {test!r}"
    text = " ".join(test[1:])
    if "/health" not in text:
        return "the HEALTHCHECK does not request /health"
    if "127.0.0.1" not in text and "localhost" not in text:
        return "the HEALTHCHECK does not target the container's loopback address"
    markers = [m for m in DB_MARKERS if m in text]
    if markers:
        return "the HEALTHCHECK mentions " + ", ".join(markers)
    return None


def runnable_healthcheck(health: dict | None) -> bool:
    """True when the HEALTHCHECK is a command this script can run with `docker exec`."""
    test = (health or {}).get("Test") or []
    return len(test) >= 2 and test[0] in ("CMD", "CMD-SHELL")


def healthcheck_command(health: dict) -> list[str]:
    """The HEALTHCHECK test as a `docker exec` argument list."""
    test = health["Test"]
    if test[0] == "CMD-SHELL":
        return ["/bin/sh", "-c", test[1]]
    return list(test[1:])


def role_lines(log_text: str, role: str = API_ROLE) -> list[str]:
    """Postgres log lines written by backends of ``role`` (see LOG_LINE_PREFIX)."""
    marker = f" user={role} "
    return [line for line in log_text.splitlines() if marker in line]


def summarize(lines: Sequence[str], limit: int = 3) -> str:
    """The first few offending log lines, shortened, for a FAIL detail."""
    shown = []
    for line in lines[:limit]:
        _, _, message = line.partition(f" user={API_ROLE} ")
        shown.append(message.strip()[:120])
    more = f" (+{len(lines) - limit} more)" if len(lines) > limit else ""
    return "; ".join(shown) + more


# --- Docker plumbing ------------------------------------------------------------------


def docker(*args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"docker {args[0]} timed out") from exc
    if check and result.returncode != 0:
        raise CheckError(f"docker {args[0]} failed: {result.stderr.strip()[-500:]}")
    return result


def inspect_image(image: str) -> dict:
    result = docker("image", "inspect", image, check=False)
    if result.returncode != 0:
        raise CheckError(f"image {image!r} not found locally (build or pull it first)")
    return json.loads(result.stdout)[0]["Config"]


def probe_code(archive_roots: Sequence[str] = ARCHIVE_ROOTS) -> str:
    """CONTENTS_PROBE with its inputs defined, ready for ``python -c``."""
    return (
        f"TOOLS = {BUILD_TOOLS!r}\nLEFTOVERS = {BUILD_LEFTOVERS!r}\n"
        f"ARCHIVE_ROOTS = {tuple(archive_roots)!r}\n" + CONTENTS_PROBE
    )


def read_contents(image: str) -> dict:
    result = docker("run", "--rm", "--network", "none", "--entrypoint", "python", image,
                    "-c", probe_code(), check=False)
    if result.returncode != 0:
        raise CheckError(f"could not inspect the image's contents: {result.stderr[-500:]}")
    return json.loads(result.stdout)


def http_get_snippet(path: str, *, edge_auth: bool = False, strict: bool = False) -> str:
    """Python run inside an API container: GET 127.0.0.1:$PORT<path> and print the status.

    ``strict`` exits non-zero unless the status is 2xx (a health probe). The
    edge secret is read from the container's own environment, so it never
    appears on a command line."""
    headers = (
        f"{{{EDGE_AUTH_HEADER!r}: os.environ['ATUS_EDGE_SECRETS'].split(',')[0]}}"
        if edge_auth else "{}"
    )
    on_error = "    raise\n" if strict else "    print(err.code)\n"
    return (
        "import os, urllib.error, urllib.request\n"
        f"req = urllib.request.Request('http://127.0.0.1:%s{path}' % os.environ.get('PORT', "
        f"'10000'), headers={headers})\n"
        "try:\n"
        "    print(urllib.request.urlopen(req, timeout=60).status)\n"
        "except urllib.error.HTTPError as err:\n"
        + on_error
    )


class Stack:
    """Throwaway containers: API instances and a Postgres that logs everything.

    Every name gets a random suffix; ``remove`` deletes all of them. The edge
    secrets are random per container, written to a mode-600 env file that is
    deleted as soon as ``docker run`` has read it, and redacted from any log
    excerpt this script prints."""

    def __init__(self, image: str, prefix: str, postgres_image: str, timeout: float) -> None:
        suffix = secrets.token_hex(4)
        self.image = image
        self.postgres_image = postgres_image
        self.timeout = timeout
        self.network = f"{prefix}-net-{suffix}"
        self.db = f"{prefix}-db-{suffix}"
        self.api = f"{prefix}-api-{suffix}"
        self.nodb = f"{prefix}-nodb-{suffix}"
        self.containers: list[str] = []
        self.network_created = False
        self.edge_secrets: list[str] = []

    # lifecycle

    def start_database(self) -> None:
        """Postgres 16 on an internal network, trust auth, every statement logged."""
        docker("network", "create", "--internal", self.network)
        self.network_created = True
        self.containers.append(self.db)
        docker(
            "run", "-d", "--name", self.db, "--network", self.network, "--network-alias", "db",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust", self.postgres_image,
            "postgres", "-c", "log_connections=on", "-c", "log_disconnections=on",
            "-c", "log_statement=all", "-c", f"log_line_prefix={LOG_LINE_PREFIX}",
        )
        # TCP, not the socket: during initdb the image's temporary server listens
        # on the socket only, so this succeeds once the real server is up.
        self.wait(
            lambda: docker("exec", self.db, "pg_isready", "-h", "127.0.0.1", "-U", "postgres",
                           check=False).returncode == 0,
            "the throwaway Postgres did not become ready",
        )
        docker("exec", self.db, "psql", "-h", "127.0.0.1", "-U", "postgres", "-v",
               "ON_ERROR_STOP=1", "-qc", f"CREATE ROLE {API_ROLE} LOGIN")

    def start_api(self, name: str, database_url: str, network: str) -> None:
        """Run the image's own command, configured the way Render runs it."""
        self.containers.append(name)
        secret = secrets.token_urlsafe(24)
        self.edge_secrets.append(secret)
        with tempfile.TemporaryDirectory(prefix="atus-image-check-") as tmp:
            env_file = os.path.join(tmp, "api.env")
            with open(os.open(env_file, os.O_WRONLY | os.O_CREAT, 0o600), "w") as handle:
                handle.write(f"ATUS_DATABASE_URL={database_url}\nATUS_EDGE_SECRETS={secret}\n")
            docker("run", "-d", "--name", name, "--network", network, "--env-file", env_file,
                   self.image)

    def remove(self) -> None:
        for name in reversed(self.containers):
            docker("rm", "-f", "-v", name, check=False)
        if self.network_created:
            docker("network", "rm", self.network, check=False)

    # waiting

    def wait(self, condition, failure: str) -> None:
        """Poll ``condition`` once a second until it holds; CheckError at the deadline."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(1)
        raise CheckError(failure)

    def running(self, container: str) -> bool:
        state = docker("inspect", "-f", "{{.State.Running}}", container, check=False)
        return state.stdout.strip() == "true"

    def exec_ok(self, container: str, command: Sequence[str]) -> bool:
        return docker("exec", container, *command, check=False, timeout=90).returncode == 0

    def wait_until_healthy(self, container: str, command: Sequence[str]) -> bool:
        """Run ``command`` in the container until it passes (True) or the deadline (False).

        A container that exits is reported at once, with its last log lines."""
        def passed() -> bool:
            if not self.running(container):
                logs = docker("logs", "--tail", "15", container, check=False)
                raise CheckError(f"container {container} exited:\n"
                                 + self.redact(logs.stdout + logs.stderr))
            return self.exec_ok(container, command)

        try:
            self.wait(passed, "")
        except CheckError as exc:
            if str(exc):
                raise
            return False
        return True

    def docker_health(self, container: str) -> str:
        """Docker's own HEALTHCHECK verdict once it leaves `starting` (or at the deadline)."""
        status = "starting"
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            status = docker("inspect", "-f", "{{.State.Health.Status}}", container,
                            check=False).stdout.strip()
            if status in ("healthy", "unhealthy"):
                break
            time.sleep(1)
        return status

    # the database log

    def db_lines(self) -> list[str]:
        logs = docker("logs", self.db, check=False)
        return role_lines(logs.stdout + logs.stderr)

    def settled_db_lines(self, stable_polls: int = 3) -> list[str]:
        """The API role's log lines once their count stops changing.

        A settle wait, not a measurement: it lets late lines (a pool connecting
        in the background, a deferred query) land before they are attributed to
        the next phase."""
        previous, stable = None, 0
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            lines = self.db_lines()
            stable = stable + 1 if previous is not None and len(lines) == len(previous) else 0
            if stable >= stable_polls - 1:
                return lines
            previous = lines
            time.sleep(1)
        raise CheckError("the database log did not settle")

    def redact(self, text: str) -> str:
        for secret in self.edge_secrets:
            text = text.replace(secret, "***")
        return text


# --- the checks ---------------------------------------------------------------------


def static_checks(config: dict, contents: dict) -> list[Result]:
    """Checks that need only the image config and a look inside the image."""
    results = []
    problem = user_problem(config.get("User"))
    results.append(Result("user", "FAIL" if problem else "PASS",
                          problem or f"USER {config.get('User')}"))
    problems = build_tool_problems(contents)
    results.append(Result(
        "build_tools", "FAIL" if problems else "PASS",
        "; ".join(problems) or f"none of {len(BUILD_TOOLS)} tools; no build packages, "
        "backends, leftovers or wheels",
    ))
    problem = healthcheck_problem(config.get("Healthcheck"))
    results.append(Result("healthcheck", "FAIL" if problem else "PASS",
                          problem or "HEALTHCHECK requests /health on loopback"))
    return results


def dynamic_checks(stack: Stack, config: dict, contents: dict, results: list[Result], *,
                   health_runs: int, allow_startup_connection: bool) -> None:
    """Checks that run the image; appends to ``results`` as each one finishes."""
    declared = runnable_healthcheck(config.get("Healthcheck"))
    # Without a usable HEALTHCHECK (already a FAIL above), probe /health the way
    # Render does, so the remaining checks still say something.
    health = (healthcheck_command(config["Healthcheck"]) if declared
              else ["python", "-c", http_get_snippet("/health", strict=True)])
    label = "the image's health check" if declared else "GET /health"

    # Liveness without any database: no network at all, and a host that does
    # not resolve.
    stack.start_api(stack.nodb, UNRESOLVABLE_DATABASE_URL, network="none")
    ok = stack.wait_until_healthy(stack.nodb, health)
    results.append(Result("health_no_db", "PASS" if ok else "FAIL",
                          f"{label} passes with an unresolvable database host" if ok
                          else f"{label} never passed without a database"))
    docker("rm", "-f", "-v", stack.nodb, check=False)

    # Against a database that logs everything the API role does.
    stack.start_database()
    stack.start_api(stack.api, f"postgresql://{API_ROLE}@db:5432/postgres", stack.network)
    if not stack.wait_until_healthy(stack.api, health):
        raise CheckError(f"{label} never passed against the test database")
    pid1 = docker("exec", stack.api, "cat", "/proc/1/status").stdout
    problems = runtime_user_problems(contents, pid1)
    results.append(Result(
        "user_runtime", "FAIL" if problems else "PASS",
        "; ".join(problems) or f"uid={contents['uid']} gid={contents['gid']}; the API server "
        "(PID 1) is not root; code and /app read-only",
    ))

    startup = stack.settled_db_lines()
    mark = len(startup)
    probes = [health, ["python", "-c", http_get_snippet("/health", strict=True)]]
    for _ in range(health_runs):
        for command in probes:
            stack.exec_ok(stack.api, command)
    docker_status = stack.docker_health(stack.api) if declared else "no HEALTHCHECK"
    window = stack.settled_db_lines()[mark:]
    quiet = not window and docker_status == "healthy"
    detail = (
        f"{health_runs} x ({label} + GET /health): {len(window)} database log lines from "
        f"{API_ROLE}; Docker health: {docker_status}"
    )
    if window:
        detail += f" [{summarize(window)}]"
    results.append(Result("health_db_quiet", "PASS" if quiet else "FAIL", detail))

    if not startup:
        results.append(Result("startup_db_quiet", "PASS",
                              "no database connection while starting"))
    elif allow_startup_connection:
        results.append(Result("startup_db_quiet", "SKIP",
                              f"{len(startup)} log lines at start-up, allowed by "
                              "--allow-startup-connection"))
    else:
        results.append(Result("startup_db_quiet", "FAIL",
                              f"{len(startup)} log lines at start-up [{summarize(startup)}]"))

    # Control: a request that must touch the database shows up in the log.
    before = len(stack.settled_db_lines())
    status = docker("exec", stack.api, "python", "-c",
                    http_get_snippet("/api/v1/meta", edge_auth=True), check=False).stdout.strip()
    seen = len(stack.settled_db_lines()) - before
    results.append(Result(
        "detector", "PASS" if seen > 0 else "FAIL",
        f"GET /api/v1/meta (status {status or '?'}) produced {seen} log lines from {API_ROLE}"
        + ("" if seen else ": the database log does not see the API, so a quiet log above "
           "proves nothing"),
    ))


def report(results: Iterable[Result]) -> int:
    failures = 0
    for result in results:
        print(f"[{result.status}] {result.name:<17} {result.detail}", flush=True)
        failures += result.status == "FAIL"
    print(f"image check: {failures} failed", flush=True)
    return 1 if failures else 0


def run(args: argparse.Namespace) -> int:
    if shutil.which("docker") is None:
        raise CheckError("the docker CLI is not installed")
    config = inspect_image(args.image)
    contents = read_contents(args.image)
    results = static_checks(config, contents)
    if args.static_only:
        return report(results)
    stack = Stack(args.image, args.name_prefix, args.postgres_image, args.timeout)
    try:
        dynamic_checks(stack, config, contents, results, health_runs=args.health_runs,
                       allow_startup_connection=args.allow_startup_connection)
    except CheckError:
        report(results)  # what was established before the run broke off
        raise
    finally:
        stack.remove()
    return report(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="local image reference or digest to check")
    parser.add_argument("--allow-startup-connection", action="store_true",
                        help="report a DB connection at start-up as SKIP, not FAIL "
                        "(images that predate the lazy pool)")
    parser.add_argument("--static-only", action="store_true",
                        help="only inspect the image; start no API or database container")
    parser.add_argument("--health-runs", type=int, default=5,
                        help="health checks to run against the logging database (default 5)")
    parser.add_argument("--name-prefix", default="atus-imagecheck",
                        help="prefix of the throwaway container and network names")
    parser.add_argument("--postgres-image", default=POSTGRES_IMAGE)
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="deadline in seconds for each wait (cold starts are slow)")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except CheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
