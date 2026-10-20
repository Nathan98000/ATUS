"""The hash-locked requirement files stay consistent with pyproject.toml (F28, I13).

requirements/compile.sh regenerates the locks; these checks fail when someone
edits pyproject.toml or an input file without regenerating, or edits a lock by
hand. No network access: everything is read from the committed files.
"""

from __future__ import annotations

import importlib.metadata
import tomllib

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

import atus_pipeline

from .conftest import REPO_ROOT

REQUIREMENTS = REPO_ROOT / "requirements"
# Runtime dependencies the API never imports (documented in requirements/api.in).
PIPELINE_ONLY = {"curl-cffi"}


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _requirements(lines: list[str]) -> dict[str, Requirement]:
    reqs = [Requirement(line) for line in lines]
    return {canonicalize_name(req.name): req for req in reqs}


def _input_file(name: str) -> dict[str, Requirement]:
    lines = []
    for raw in (REQUIREMENTS / name).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return _requirements(lines)


def _lock_entries(name: str) -> list[tuple[Requirement, list[str]]]:
    """(requirement, hashes) per pinned package; continuation lines joined."""
    entries: list[tuple[Requirement, list[str]]] = []
    logical = ""
    for raw in (REQUIREMENTS / name).read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        logical += " " + stripped.rstrip("\\").strip()
        if stripped.endswith("\\"):
            continue
        parts = logical.split()
        hashes = [p.split("=", 1)[1] for p in parts if p.startswith("--hash=")]
        spec = " ".join(p for p in parts if not p.startswith("--hash="))
        entries.append((Requirement(spec), hashes))
        logical = ""
    assert not logical, f"{name} ends inside a continuation line"
    return entries


def _pins(name: str) -> dict[str, Version]:
    pins = {}
    for req, _ in _lock_entries(name):
        (specifier,) = req.specifier
        pins[canonicalize_name(req.name)] = Version(specifier.version)
    return pins


LOCKS = ["api.lock.txt", "dev.lock.txt", "build.lock.txt"]


@pytest.mark.parametrize("lock", LOCKS)
def test_every_lock_entry_is_pinned_exactly_with_hashes(lock):
    entries = _lock_entries(lock)
    assert entries, f"{lock} is empty"
    for req, hashes in entries:
        specifiers = list(req.specifier)
        assert len(specifiers) == 1 and specifiers[0].operator == "==", str(req)
        assert hashes, f"{req.name} in {lock} has no --hash"
        assert all(h.startswith("sha256:") for h in hashes), req.name


@pytest.mark.parametrize("lock", LOCKS)
def test_locks_name_their_regeneration_command(lock):
    assert "requirements/compile.sh" in (REQUIREMENTS / lock).read_text()


def test_api_input_matches_pyproject_runtime_dependencies():
    declared = _requirements(_pyproject()["project"]["dependencies"])
    api = _input_file("api.in")
    expected = {name: req for name, req in declared.items() if name not in PIPELINE_ONLY}
    assert {name: str(req) for name, req in api.items()} == {
        name: str(req) for name, req in expected.items()
    }


def test_build_input_matches_build_system():
    declared = _requirements(_pyproject()["build-system"]["requires"])
    assert {n: str(r) for n, r in _input_file("build.in").items()} == {
        n: str(r) for n, r in declared.items()
    }


def test_dev_lock_pins_every_declared_dependency_within_its_range():
    project = _pyproject()
    declared = _requirements(
        project["project"]["dependencies"]
        + project["project"]["optional-dependencies"]["dev"]
        + project["build-system"]["requires"]
    )
    pins = _pins("dev.lock.txt")
    for name, req in declared.items():
        assert name in pins, f"{name} is declared but missing from dev.lock.txt"
        assert req.specifier.contains(pins[name], prereleases=True), (name, pins[name])


def test_api_lock_pins_each_runtime_dependency_within_its_range():
    pins = _pins("api.lock.txt")
    for name, req in _input_file("api.in").items():
        assert name in pins, f"{name} missing from api.lock.txt"
        assert req.specifier.contains(pins[name], prereleases=True), (name, pins[name])


def test_api_lock_uses_exactly_the_dev_lock_versions():
    """CI tests the versions the image ships: the API lock is a subset of the dev lock."""
    dev = _pins("dev.lock.txt")
    for name, version in _pins("api.lock.txt").items():
        assert dev.get(name) == version, f"{name}: api {version} vs dev {dev.get(name)}"


def test_build_lock_uses_the_dev_lock_setuptools():
    assert _pins("build.lock.txt") == {"setuptools": _pins("dev.lock.txt")["setuptools"]}


def test_pipeline_only_packages_stay_out_of_the_api_lock():
    api, dev = _pins("api.lock.txt"), _pins("dev.lock.txt")
    for name in PIPELINE_ONLY:
        assert name not in api
        assert name in dev


def test_api_lock_has_no_test_or_build_tools():
    api = _pins("api.lock.txt")
    for name in ("pytest", "ruff", "httpx", "pdfplumber", "setuptools", "pip"):
        assert name not in api


def test_package_version_is_single_sourced():
    """I13: pyproject reads the version from atus_pipeline.__version__."""
    project = _pyproject()
    assert "version" not in project["project"]
    assert project["project"]["dynamic"] == ["version"]
    attr = project["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "atus_pipeline.__version__"
    assert importlib.metadata.version("atus-pipeline") == atus_pipeline.__version__
