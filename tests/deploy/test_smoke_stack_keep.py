"""scripts/smoke_stack.sh --keep prints a teardown that works (review round 2,
smoke-stack-keep-teardown-broken).

No Docker: a stub ``docker`` on PATH records every invocation and fails
``compose up``, so the script stops early and its exit trap runs. Without
``--keep`` the trap tears the stack down itself; with ``--keep`` it prints the
commands instead. The printed compose command must be exactly the trap's
teardown (the env file with the stack's required secrets, every profile, so an
``--edge`` stack's Worker goes too, and ``--remove-orphans``), and the private
work directory holding those secrets must be named for removal.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from .conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "smoke_stack.sh"
STUB = """#!/usr/bin/env bash
# Records one invocation per line (arguments shell-quoted); fails `compose ... up`.
printf '%q ' "$@" >> "$STUB_DOCKER_LOG"
printf '\\n' >> "$STUB_DOCKER_LOG"
for arg in "$@"; do
  if [ "$arg" = up ]; then exit 1; fi
done
exit 0
"""


def run_smoke_stack(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess,
                                                            list[list[str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(STUB)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.log"
    log.write_text("")
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               STUB_DOCKER_LOG=str(log), TMPDIR=str(tmp_path), ATUS_BUILD_SHA="test-build")
    result = subprocess.run(
        [str(SCRIPT), "--project", "atus-rv2-infra-keep", "--no-build", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60, check=False)
    calls = [shlex.split(line) for line in log.read_text().splitlines() if line.strip()]
    return result, calls


def teardown_of(calls: list[list[str]]) -> list[str]:
    (down,) = [call for call in calls if "down" in call]
    return down


@pytest.mark.parametrize("extra", [(), ("--edge",), ("--emulation",)])
def test_the_kept_stack_is_removed_by_the_printed_commands(tmp_path, extra):
    result, calls = run_smoke_stack(tmp_path, *extra)
    assert result.returncode != 0                 # the stub failed `up`
    reference = teardown_of(calls)                # what the trap runs without --keep
    assert {"down", "-v", "--remove-orphans", "--env-file"} <= set(reference)

    result, calls = run_smoke_stack(tmp_path, "--keep", *extra)
    assert not [call for call in calls if "down" in call]     # kept: nothing removed
    printed = [line.strip() for line in result.stdout.splitlines()
               if line.strip().startswith(("docker compose ", "rm -rf "))]
    assert len(printed) == 2, result.stdout
    compose, remove = (shlex.split(line) for line in printed)
    env_file = Path(compose[compose.index("--env-file") + 1])
    workdir = Path(remove[-1])
    assert env_file.is_file() and env_file.parent == workdir
    assert workdir.is_dir() and stat.S_IMODE(workdir.stat().st_mode) == 0o700

    # Run the printed teardown as printed (from the repository root).
    log = tmp_path / "docker.log"
    log.write_text("")
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
               STUB_DOCKER_LOG=str(log))
    subprocess.run(printed[0], shell=True, cwd=REPO_ROOT, env=env, check=True, timeout=30)
    (ran,) = [shlex.split(line) for line in log.read_text().splitlines() if line.strip()]
    normalized = [str(env_file) if arg.endswith("/stack.env") else arg for arg in reference]
    assert ran == normalized
    for profile in ("pgbouncer", "smoke", "snapshot", "edge"):
        assert ["--profile", profile] in [ran[i:i + 2] for i in range(len(ran) - 1)], profile
