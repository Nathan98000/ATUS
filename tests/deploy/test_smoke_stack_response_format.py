"""scripts/smoke_stack.sh follows the API's RESPONSE_FORMAT (review round 2,
response-format-bump-breaks-snapshot).

The stack probe hard-coded X-Response-Format "1", the snapshot build defaulted
to "1" and the local Worker kept wrangler.jsonc's "1", so a documented bump
failed every stack smoke and the CI edge cycle. Here the script runs on a copy
of itself next to a checkout whose constant is "7", with stub ``docker`` and
``python`` (no Docker, no network): the probe, the snapshot build and the
Worker's vars must all carry "7".
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from .conftest import REPO_ROOT

DATA_VERSION = "0325:serving-20261020T120000Z-0a1b2c3d:0123456789ab"
DOCKER = """#!/usr/bin/env bash
printf '%q ' "$@" >> "$STUB_DOCKER_LOG"; printf '\\n' >> "$STUB_DOCKER_LOG"
for arg in "$@"; do
  case "$arg" in
    logs) echo "seed  | seed: ready; data_version=$STUB_DATA_VERSION"; exit 0 ;;
    port) echo "127.0.0.1:18080"; exit 0 ;;
  esac
done
exit 0
"""
PYTHON = """#!/usr/bin/env bash
printf '%q ' "$@" >> "$STUB_PYTHON_LOG"; printf '\\n' >> "$STUB_PYTHON_LOG"
if [ "$1" = -c ]; then exec "$REAL_PYTHON" "$@"; fi
if [ "$1" = scripts/build_snapshot.py ]; then
  while [ $# -gt 0 ]; do
    if [ "$1" = --out ]; then out=$2; fi
    shift
  done
  mkdir -p "$out"
  printf '{"snapshot_id": "s-0123abcd", "analytics_version": "0.2", "response_format": "7"}' \\
    > "$out/current.json"
  echo s-0123abcd
fi
exit 0
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _calls(log: Path) -> list[list[str]]:
    return [shlex.split(line) for line in log.read_text().splitlines() if line.strip()]


def test_the_stack_smoke_follows_the_checkouts_response_format(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "smoke_stack.sh", checkout / "scripts")
    api = checkout / "src" / "atus_pipeline" / "api"
    api.mkdir(parents=True)
    (checkout / "src" / "atus_pipeline" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (api / "__init__.py").write_text('API_VERSION = "v1"\n\nRESPONSE_FORMAT = "7"\n')
    assets = tmp_path / "dist"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html>")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "docker", DOCKER)
    _executable(bin_dir / "python", PYTHON)
    docker_log, python_log = tmp_path / "docker.log", tmp_path / "python.log"
    docker_log.write_text("")
    python_log.write_text("")
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               STUB_DOCKER_LOG=str(docker_log), STUB_PYTHON_LOG=str(python_log),
               STUB_DATA_VERSION=DATA_VERSION, REAL_PYTHON=sys.executable,
               TMPDIR=str(tmp_path), ATUS_BUILD_SHA="test-build")

    result = subprocess.run(
        [str(checkout / "scripts" / "smoke_stack.sh"), "--project", "atus-rv2-infra-rf",
         "--no-build", "--edge", "--keep", "--assets", str(assets),
         "--python", str(bin_dir / "python")],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr

    (probe,) = [call for call in _calls(docker_log) if "smoke" in call and "run" in call]
    assert probe[probe.index("--response-format") + 1] == "7", probe
    (build,) = [call for call in _calls(python_log) if call[0] == "scripts/build_snapshot.py"]
    assert build[build.index("--response-format") + 1] == "7", build
    # The local Worker gets the snapshot's format, as deploy.yml's --var does.
    (remove,) = [line.split()[-1] for line in result.stdout.splitlines()
                 if line.strip().startswith("rm -rf ")]
    stack_env = (Path(shlex.split(remove)[0]) / "stack.env").read_text().splitlines()
    assert "ATUS_STACK_RESPONSE_FORMAT=7" in stack_env
    assert "ATUS_STACK_ANALYTICS_VERSION=0.2" in stack_env
