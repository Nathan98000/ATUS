"""scripts/e2e_in_container.sh: the Playwright E2E suite with its browsers in
the official Playwright Linux image (Firefox cannot launch on macOS 27).

Without Docker (needs node): the script runs in a copy of its repository
layout with a fake ``docker`` and ``npx`` on PATH, so every rule is checked
without starting anything — the image is the lockfile's exact version, a
node_modules or image of another version is refused before Docker is touched,
the browser server is published on loopback only and runs the mounted
lockfile Playwright read-only, the test runner gets ``E2E_BROWSER_SERVER``
and every argument, its exit status is the script's, and the container is
removed whatever happens.

With ATUS_DOCKER_TESTS=1 and ATUS_E2E_DATABASE_URL (a disposable atus_e2e_*
database, as for ``npm run test:e2e``): one real Firefox journey through the
container, on free ports (or E2E_BROWSER_SERVER_PORT, E2E_FRONTEND_PORT and
E2E_BACKEND_PORT when set); the container is ``<ATUS_DOCKER_TEST_PREFIX>-e2e-browsers``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from .conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "e2e_in_container.sh"
LOCK_VERSION = "1.63.0"

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Records every call; answers like the Docker CLI would for this script.
printf 'docker %s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  container)
    if [ "$2" = inspect ] && [ "$3" = --format ]; then
      if [ "${FAKE_NEVER_READY:-}" = 1 ]; then echo false; else echo true; fi
      exit 0
    fi
    [ "${FAKE_EXISTS:-}" = 1 ] && exit 0
    exit 1 ;;
  image) [ "${FAKE_NO_IMAGE:-}" = 1 ] && exit 1; exit 0 ;;
  pull) exit 0 ;;
  run) echo 0123456789ab; exit 0 ;;
  logs)
    if [ "${FAKE_NEVER_READY:-}" = 1 ]; then echo "browserType.launch: something broke"; exit 0; fi
    echo "Listening on ws://0.0.0.0:3000/"; exit 0 ;;
  rm) exit 0 ;;
esac
exit 3
"""

FAKE_NPX = r"""#!/usr/bin/env bash
printf 'npx %s\n' "$*" >> "$FAKE_LOG"
printf 'npx-cwd %s\n' "$PWD" >> "$FAKE_LOG"
printf 'npx-env E2E_BROWSER_SERVER=%s\n' "${E2E_BROWSER_SERVER:-}" >> "$FAKE_LOG"
exit "${FAKE_NPX_STATUS:-0}"
"""


@pytest.fixture
def node():
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not installed")
    return path


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class FakeRepo:
    """A copy of the script in a minimal repository layout, with fake tools."""

    def __init__(self, root: Path, node: str):
        self.root = root
        self.frontend = root / "frontend"
        self.log = root / "calls.log"
        (root / "scripts").mkdir(parents=True)
        shutil.copy2(SCRIPT, root / "scripts" / "e2e_in_container.sh")
        _write_json(self.frontend / "package-lock.json", {
            "lockfileVersion": 3,
            "packages": {"node_modules/@playwright/test": {"version": LOCK_VERSION}},
        })
        for package in ("@playwright/test", "playwright", "playwright-core"):
            self.install(package, LOCK_VERSION)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _write_executable(bin_dir / "docker", FAKE_DOCKER)
        _write_executable(bin_dir / "npx", FAKE_NPX)
        self.path = os.pathsep.join([str(bin_dir), str(Path(node).parent), "/usr/bin", "/bin"])

    def install(self, package: str, version: str) -> None:
        _write_json(self.frontend / "node_modules" / package / "package.json",
                    {"name": package, "version": version})

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        self.log.write_text("")
        clean = {"PATH": self.path, "HOME": str(self.root), "FAKE_LOG": str(self.log)}
        clean.update(env)
        return subprocess.run(["bash", str(self.root / "scripts" / "e2e_in_container.sh"), *args],
                              cwd=self.root, env=clean, capture_output=True, text=True,
                              timeout=60, check=False)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def docker_calls(self) -> list[str]:
        return [line for line in self.calls() if line.startswith("docker ")]


@pytest.fixture
def repo(tmp_path, node) -> FakeRepo:
    return FakeRepo(tmp_path / "repo", node)


IMAGE = f"mcr.microsoft.com/playwright:v{LOCK_VERSION}-noble"


def test_the_image_is_the_lockfiles_exact_playwright_version(node):
    lock = json.loads((REPO_ROOT / "frontend" / "package-lock.json").read_text())
    versions = {name: lock["packages"][f"node_modules/{name}"]["version"]
                for name in ("@playwright/test", "playwright", "playwright-core")}
    assert len(set(versions.values())) == 1, versions  # client and server: one version
    result = subprocess.run([str(SCRIPT), "--print-image"], cwd=REPO_ROOT, capture_output=True,
                            text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"mcr.microsoft.com/playwright:v{versions['playwright']}-noble"


def test_runs_the_local_suite_against_the_containers_browsers(repo):
    result = repo.run("--project", "firefox", "--grep", "two words",
                      E2E_BROWSER_SERVER_PORT="8899", E2E_BROWSER_CONTAINER="atus-t-browsers")
    assert result.returncode == 0, result.stderr
    calls = repo.calls()
    run = next(line for line in calls if line.startswith("docker run "))
    # loopback only, unprivileged, the lockfile's Playwright mounted read-only
    assert "--publish 127.0.0.1:8899:3000" in run
    assert "--user pwuser" in run and "--rm" in run and "--init" in run
    assert f"{repo.frontend}/node_modules:/opt/atus-e2e/node_modules:ro" in run
    assert re.search(
        rf" {re.escape(IMAGE)} node /opt/atus-e2e/node_modules/playwright-core/cli\.js "
        r"run-server --port 3000 --host 0\.0\.0\.0$", run), run
    assert "--name atus-t-browsers" in run
    # the image is present: nothing pulled
    assert not any(line.startswith("docker pull") for line in calls)
    # the test runner: here, in frontend/, with every argument and the endpoint
    assert "npx --no-install playwright test --project firefox --grep two words" in calls
    assert f"npx-cwd {repo.frontend}" in calls
    assert "npx-env E2E_BROWSER_SERVER=ws://127.0.0.1:8899/" in calls
    # and the container is gone afterwards
    assert calls[-1] == "docker rm -f atus-t-browsers"
    assert calls.index("npx-env E2E_BROWSER_SERVER=ws://127.0.0.1:8899/") < len(calls) - 1


def test_defaults(repo):
    result = repo.run()
    assert result.returncode == 0, result.stderr
    calls = repo.calls()
    run = next(line for line in calls if line.startswith("docker run "))
    assert "--publish 127.0.0.1:8139:3000" in run and "--name atus-e2e-browsers" in run
    assert "npx --no-install playwright test" in calls  # no filter: every project
    assert calls[-1] == "docker rm -f atus-e2e-browsers"


def test_a_failing_suite_fails_the_script_and_still_removes_the_container(repo):
    result = repo.run("--project", "firefox", FAKE_NPX_STATUS="1")
    assert result.returncode == 1
    assert repo.calls()[-1] == "docker rm -f atus-e2e-browsers"


def test_pulls_the_image_only_when_missing(repo):
    result = repo.run(FAKE_NO_IMAGE="1")
    assert result.returncode == 0, result.stderr
    assert f"docker pull {IMAGE}" in repo.calls()


def test_a_digest_pinned_reference_to_the_same_image_is_used(repo):
    pinned = f"{IMAGE}@sha256:{'ab' * 32}"
    result = repo.run(E2E_PLAYWRIGHT_IMAGE=pinned)
    assert result.returncode == 0, result.stderr
    run = next(line for line in repo.calls() if line.startswith("docker run "))
    assert f" {pinned} node " in run


@pytest.mark.parametrize("image", [
    "mcr.microsoft.com/playwright:v1.62.0-noble",
    "mcr.microsoft.com/playwright:v1.63.0-jammy",
    "mcr.microsoft.com/playwright:latest",
    "example.test/playwright:v1.63.0-noble",
])
def test_refuses_an_image_of_another_version_or_source(repo, image):
    result = repo.run(E2E_PLAYWRIGHT_IMAGE=image)
    assert result.returncode == 2
    assert "must match the lockfile's Playwright" in result.stderr
    assert repo.docker_calls() == []


@pytest.mark.parametrize("package", ["@playwright/test", "playwright", "playwright-core"])
def test_refuses_installed_packages_that_differ_from_the_lockfile(repo, package):
    repo.install(package, "1.62.0")
    result = repo.run()
    assert result.returncode == 2
    assert f"frontend/node_modules/{package} is 1.62.0, the lockfile pins 1.63.0" in result.stderr
    assert repo.docker_calls() == []


def test_refuses_a_checkout_without_node_modules(repo):
    shutil.rmtree(repo.frontend / "node_modules")
    result = repo.run()
    assert result.returncode == 2
    assert "run `npm ci` in frontend/ first" in result.stderr
    assert repo.docker_calls() == []


def test_never_touches_a_container_it_did_not_start(repo):
    result = repo.run(FAKE_EXISTS="1", E2E_BROWSER_CONTAINER="atus-someone-else")
    assert result.returncode == 2
    assert "a container named atus-someone-else already exists" in result.stderr
    calls = repo.docker_calls()
    assert not any(line.startswith(("docker run", "docker rm")) for line in calls)


@pytest.mark.parametrize("env", [
    {"E2E_BROWSER_SERVER_PORT": "88a"},
    {"E2E_BROWSER_SERVER_PORT": "0"},
    {"E2E_BROWSER_SERVER_PORT": "70000"},
    {"E2E_BROWSER_CONTAINER": "-rf"},
    {"E2E_BROWSER_CONTAINER": "a b"},
])
def test_refuses_bad_settings_before_docker(repo, env):
    result = repo.run(**env)
    assert result.returncode == 2
    assert repo.docker_calls() == []


def test_a_server_that_never_starts_is_reported_and_removed(repo):
    result = repo.run(FAKE_NEVER_READY="1")
    assert result.returncode == 1
    assert "the browser server did not start" in result.stderr
    assert "something broke" in result.stderr  # its log
    calls = repo.calls()
    assert not any(line.startswith("npx ") for line in calls)
    assert calls[-1] == "docker rm -f atus-e2e-browsers"


def test_print_image_takes_no_other_arguments(repo):
    result = repo.run("--print-image", "--project", "firefox")
    assert result.returncode == 2
    assert repo.docker_calls() == []


# ---------------------------------------------------------------------------
# The real thing (opt-in)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_firefox_journey_through_the_container(docker_daemon, docker_prefix, node):
    """One @core journey in the image's Firefox, against the real API on the
    fixture database (opt-in: ATUS_DOCKER_TESTS=1 and ATUS_E2E_DATABASE_URL)."""
    if not os.environ.get("ATUS_E2E_DATABASE_URL"):
        pytest.skip("set ATUS_E2E_DATABASE_URL to a disposable atus_e2e_* database")
    if not (REPO_ROOT / "frontend" / "node_modules").is_dir():
        pytest.skip("the frontend's node_modules are not installed (npm ci in frontend/)")
    container = f"{docker_prefix}-e2e-browsers"
    # free ports unless the environment assigns them (parallel checkouts)
    ports = {name: os.environ.get(name) or str(_free_port())
             for name in ("E2E_BROWSER_SERVER_PORT", "E2E_FRONTEND_PORT", "E2E_BACKEND_PORT")}
    env = dict(os.environ, **ports, E2E_BROWSER_CONTAINER=container, CI="")
    result = subprocess.run(
        [str(SCRIPT), "--project", "firefox", "--grep", "invalid share link",
         "--reporter", "list"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=900, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-6000:]
    assert re.search(r"^\s+1 passed", output, re.MULTILINE), output[-3000:]
    assert "[firefox]" in output
    leftovers = subprocess.run(["docker", "ps", "-a", "--filter", f"name=^{container}$",
                                "--format", "{{.Names}}"],
                               capture_output=True, text=True, check=True).stdout.split()
    assert leftovers == []
