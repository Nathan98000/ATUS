"""deploy/ci/lint.sh covers every packaging file and fails on a broken one.

Coverage is checked statically (a new Dockerfile, compose file or required
stack variable must be added to the script). The script's own behaviour is
run on a scratch tree; the full run over this checkout needs a Docker daemon
for `docker build --check` and is opt-in (ATUS_DOCKER_TESTS=1).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from .conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "deploy" / "ci" / "lint.sh"
# Not packaging: audit evidence is committed verbatim, the rest is local state.
SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", ".claude", "evidence", "dist",
                ".wrangler", ".pytest_cache", ".ruff_cache"}


def _script() -> str:
    return SCRIPT.read_text()


def _array(name: str) -> list[str]:
    body = re.search(rf"^{name}=\((.*?)^\)", _script(), re.MULTILINE | re.DOTALL).group(1)
    return re.findall(r'"([^"]+)"', body) or body.split()


def _repo_files(predicate) -> set[str]:
    found = set()
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS]
        for name in files:
            if predicate(name):
                found.add(os.path.relpath(os.path.join(root, name), REPO_ROOT))
    return found


def test_every_dockerfile_is_linted_with_an_existing_context():
    entries = dict(entry.split("|") for entry in _array("dockerfiles"))
    assert set(entries) == _repo_files(lambda name: name == "Dockerfile")
    for context in entries.values():
        assert (REPO_ROOT / context).is_dir(), context


def test_every_compose_file_is_validated():
    covered = set(re.findall(r"-f (\S+)", " ".join(_array("compose_sets"))))
    assert covered == {
        name for name in _repo_files(lambda n: re.fullmatch(r"docker-compose.*\.ya?ml", n))
    }


def test_every_required_stack_variable_has_a_throwaway_value():
    required = set()
    for name in ("docker-compose.prod.yml", "docker-compose.emulation.yml"):
        required.update(re.findall(r"\$\{(\w+):\?", (REPO_ROOT / name).read_text()))
    provided = {item.split("=", 1)[0] for item in _array("stack_env")}
    assert required and required <= provided


def test_the_script_never_installs_anything():
    text = _script()
    for installer in ("pip install", "npm install", "npx ", "brew install", "apt-get",
                      "curl ", "wget ", "docker pull", "docker run"):
        assert installer not in text, installer


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_broken_shell_script_fails_and_good_one_passes(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "good.sh").write_text("#!/bin/sh\necho ok\n")
    (tmp_path / "scripts" / "broken.sh").write_text("#!/bin/sh\nif true; then\n  echo\n")
    (tmp_path / "scripts" / "arrays.sh").write_text("#!/usr/bin/env bash\na=(1 2)\n")
    result = subprocess.run(["bash", str(SCRIPT), str(tmp_path)], capture_output=True,
                            text=True, timeout=300, check=False)
    out = result.stdout
    assert result.returncode == 1, out
    assert "[FAIL] shell       scripts/broken.sh (sh -n)" in out
    assert "[PASS] shell       scripts/good.sh (sh -n)" in out
    assert "[PASS] shell       scripts/arrays.sh (bash -n)" in out  # bash syntax, bash check


def test_lint_passes_on_this_checkout(docker_daemon):
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                            timeout=600, check=False)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "lint: 0 failed" in result.stdout
    for dockerfile in _array("dockerfiles"):
        assert f"[PASS] dockerfile  {dockerfile.split('|')[0]}" in result.stdout
