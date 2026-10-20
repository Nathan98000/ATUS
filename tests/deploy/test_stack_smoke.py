"""End-to-end smoke of the production stack (opt-in: ATUS_DOCKER_TESTS=1).

Runs scripts/smoke_stack.sh --no-build on the image under test (ATUS_TEST_IMAGE,
or atus-api:test built from this checkout; see conftest.py), so CI smokes the
exact image it scans and pushes: starts db -> seed (fixture dataset) -> api
from docker-compose.prod.yml with throwaway secrets, runs the HTTP probe from
inside the stack network and removes everything. Extra script options come
from ATUS_SMOKE_ARGS (for example ``--emulation``); the compose project is
``<ATUS_DOCKER_TEST_PREFIX>-stack``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess

from .conftest import REPO_ROOT


def image_build_sha(image: str) -> str:
    """The ATUS_BUILD_SHA baked into ``image``, which /health must report."""
    inspect = subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                             text=True, check=True)
    env = dict(item.split("=", 1) for item in json.loads(inspect.stdout)[0]["Config"]["Env"])
    return env.get("ATUS_BUILD_SHA", "dev")


def test_stack_smoke(api_image, docker_prefix):
    args = shlex.split(os.environ.get("ATUS_SMOKE_ARGS", ""))
    env = dict(os.environ, ATUS_STACK_IMAGE=api_image, ATUS_BUILD_SHA=image_build_sha(api_image))
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "smoke_stack.sh"), "--project", f"{docker_prefix}-stack",
         "--no-build", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=1200, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-6000:]
    assert "smoke: 0 failed" in output
