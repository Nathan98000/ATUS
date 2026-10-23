"""Fixtures for the deployment-artifact tests (image, compose files, locks).

Static checks (lock files, ignore files, configuration templates) run in every
session. Compose-file checks need only the Docker CLI (``docker compose
config`` never contacts the daemon) and skip without it. Tests that build
images or start containers are opt-in, because they need a Docker daemon,
network access for the first build and a few minutes:

    ATUS_DOCKER_TESTS=1 pytest tests/deploy

``ATUS_TEST_IMAGE=<ref>`` tests an existing image (for example the digest CI
built) instead of building ``atus-api:test`` from this checkout.
``ATUS_DOCKER_TEST_PREFIX`` (default ``atus-pytest``) starts the names of every
container, network and compose project these tests create, so parallel runs
on one machine can be kept apart (``atus-wsc`` while workstreams share it).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Throwaway values that satisfy the stack's required variables for
# `docker compose config`; never used to start anything.
DUMMY_STACK_ENV = {
    "ATUS_STACK_PG_SUPERUSER_PASSWORD": "dummy-superuser",
    "ATUS_STACK_OWNER_PASSWORD": "dummy-owner",
    "ATUS_STACK_API_DB_PASSWORD": "dummy-api",
    "ATUS_STACK_EDGE_SECRET": "dummy-edge",
    "ATUS_STACK_OPS_TOKEN": "dummy-ops",
}


def _compose_works() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


@pytest.fixture(scope="session")
def compose_cli() -> None:
    if not _compose_works():
        pytest.skip("the docker compose CLI is not available")


def run_compose_config(
    *files: str,
    env: dict[str, str] | None = None,
    profiles: tuple[str, ...] = (),
    project: str | None = None,
) -> subprocess.CompletedProcess:
    """`docker compose config --format json` for ``files``; never starts anything.

    Only the variables in ``env`` (plus PATH/HOME/DOCKER_*) reach the
    interpolation, so a developer's shell cannot change the result.
    """
    command = ["docker", "compose"]
    if project:
        command += ["-p", project]
    for name in files:
        command += ["-f", str(REPO_ROOT / name)]
    for profile in profiles:
        command += ["--profile", profile]
    command += ["config", "--format", "json"]
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "HOME", "TMPDIR", "XDG_CONFIG_HOME") or key.startswith("DOCKER_")
    }
    clean_env.update(env or {})
    return subprocess.run(
        command, cwd=REPO_ROOT, env=clean_env, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="session")
def compose_config(compose_cli):
    """Callable returning the resolved compose model as a dict (fails on errors)."""

    def _config(*files: str, env: dict[str, str] | None = None, **kwargs) -> dict:
        result = run_compose_config(*files, env=env, **kwargs)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return _config


@pytest.fixture(scope="session")
def docker_prefix() -> str:
    """Prefix for the names of the containers, networks and projects tests create."""
    return os.environ.get("ATUS_DOCKER_TEST_PREFIX", "atus-pytest")


@pytest.fixture(scope="session")
def docker_daemon() -> None:
    if os.environ.get("ATUS_DOCKER_TESTS") != "1":
        pytest.skip("container tests are opt-in: set ATUS_DOCKER_TESTS=1")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:
        pytest.skip("the Docker daemon is not reachable")


@pytest.fixture(scope="session")
def api_image(docker_daemon) -> str:
    """The API image under test: ATUS_TEST_IMAGE, or a fresh build of this checkout."""
    image = os.environ.get("ATUS_TEST_IMAGE")
    if image:
        return image
    image = "atus-api:test"
    result = subprocess.run(
        ["docker", "build", "-t", image, "--build-arg", "ATUS_BUILD_SHA=test-build", "."],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    return image


# --- the Worker's configuration names (static) ---------------------------------------


def wrangler_vars() -> set[str]:
    """The names under "vars" in edge/wrangler.jsonc (the Worker's non-secret settings)."""
    text = (REPO_ROOT / "edge" / "wrangler.jsonc").read_text()
    block = re.search(r'^\s*"vars":\s*\{(.*?)^\s*\}', text, re.MULTILINE | re.DOTALL)
    assert block is not None, "edge/wrangler.jsonc has no vars block"
    return set(re.findall(r'^\s*"([A-Z][A-Z0-9_]*)":', block.group(1), re.MULTILINE))


def worker_env_names() -> set[str]:
    """Every `env.NAME` the Worker's code reads (vars, secrets and bindings)."""
    names: set[str] = set()
    for path in sorted((REPO_ROOT / "edge" / "src").rglob("*.ts")):
        names.update(re.findall(r"\benv\.([A-Z][A-Z0-9_]*)\b", path.read_text()))
    return names
