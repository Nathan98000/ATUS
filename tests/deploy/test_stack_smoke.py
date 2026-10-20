"""End-to-end smoke of the production stack (opt-in: ATUS_DOCKER_TESTS=1).

Runs scripts/smoke_stack.sh: builds the image, starts db -> seed (fixture
dataset) -> api from docker-compose.prod.yml with throwaway secrets, runs the
HTTP probe from inside the stack network and removes everything. Extra script
options come from ATUS_SMOKE_ARGS (for example ``--emulation``).
"""

from __future__ import annotations

import os
import shlex
import subprocess

from .conftest import REPO_ROOT


def test_stack_smoke(docker_daemon):
    args = shlex.split(os.environ.get("ATUS_SMOKE_ARGS", ""))
    result = subprocess.run(
        [str(REPO_ROOT / "scripts" / "smoke_stack.sh"), "--project", "atus-stack-pytest", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=1200, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-6000:]
    assert "smoke: 0 failed" in output
