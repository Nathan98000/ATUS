"""Dependabot covers every dependency manifest, and every base image can be bumped by it.

Brief §11: "Dependabot or Renovate, base images pinned by tag or digest".
Dependabot's docker ecosystem reads literal ``FROM`` lines only, so every
Dockerfile names its base image literally, pinned by digest. The API image
repeats its base in two ARGs for the OCI labels; they must match the FROM lines.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from .conftest import REPO_ROOT

CONFIG = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text())
# Not maintained manifests: audit evidence is committed verbatim, the rest is local.
SKIP = {".git", ".venv", "node_modules", ".claude", "dist", ".wrangler", "evidence"}
FROM = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.MULTILINE)


def repo_files(name: str) -> set[str]:
    """Directories (as Dependabot writes them) holding a file called ``name``."""
    found = set()
    for path in REPO_ROOT.rglob(name):
        relative = path.relative_to(REPO_ROOT)
        if set(relative.parts) & SKIP:
            continue
        parent = relative.parent.as_posix()
        found.add("/" if parent == "." else f"/{parent}")
    return found


def directories(ecosystem: str) -> set[str]:
    dirs: set[str] = set()
    for update in CONFIG["updates"]:
        if update["package-ecosystem"] == ecosystem:
            dirs.update(update.get("directories") or [update["directory"]])
    return dirs


def test_every_ecosystem_is_covered():
    assert CONFIG["version"] == 2
    assert {u["package-ecosystem"] for u in CONFIG["updates"]} == {
        "pip", "npm", "github-actions", "docker", "docker-compose"}
    assert directories("npm") == {"/frontend", "/edge"} == repo_files("package-lock.json")
    assert directories("docker") == repo_files("Dockerfile")
    assert directories("pip") >= {"/", "/requirements"}
    assert directories("github-actions") == {"/"}


def test_every_update_waits_out_a_cooldown_and_runs_weekly():
    for update in CONFIG["updates"]:
        assert update["cooldown"]["default-days"] >= 7, update["package-ecosystem"]
        assert update["schedule"]["interval"] == "weekly"


def test_every_base_image_is_a_literal_digest_pinned_from_line():
    dockerfiles = [p for p in REPO_ROOT.rglob("Dockerfile")
                   if not set(p.relative_to(REPO_ROOT).parts) & SKIP]
    assert len(dockerfiles) == 3
    for path in dockerfiles:
        refs = FROM.findall(path.read_text())
        assert refs, path
        for ref in refs:
            assert "$" not in ref, f"{path}: ARG-based FROM lines are invisible to Dependabot"
            assert re.fullmatch(r"[a-z0-9./-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}", ref), \
                f"{path}: {ref} is not pinned by digest"


def test_the_api_image_labels_name_its_real_base():
    text = (REPO_ROOT / "Dockerfile").read_text()
    base = re.search(r"^ARG PYTHON_BASE=(\S+)$", text, re.MULTILINE).group(1)
    digest = re.search(r"^ARG PYTHON_DIGEST=(\S+)$", text, re.MULTILINE).group(1)
    assert set(FROM.findall(text)) == {f"{base}@{digest}"}, \
        "update ARG PYTHON_BASE/PYTHON_DIGEST together with the FROM lines"


POSTGRES_PIN = re.compile(r"postgres:16@sha256:([0-9a-f]{64})")
#: Where the stack's postgres:16 is pinned. Dependabot bumps only the compose
#: files (docker-compose) and deploy/pgbouncer/Dockerfile (docker), in
#: separate pull requests; the workflows' `services:` images and the Python
#: constants are copies no ecosystem updates.
POSTGRES_PINS = {
    "docker-compose.yml": 1, "docker-compose.prod.yml": 3, "deploy/pgbouncer/Dockerfile": 1,
    ".github/workflows/ci.yml": 2, ".github/workflows/deploy.yml": 1,
    "deploy/ci/check_image.py": 1, "tests/deploy/test_container_image.py": 1,
}
# Recorded evidence quotes the digest of its day and is never updated.
EVIDENCE = ("docs/", "loadtests/results/")
#: Without git, files above this size are not read: no pin lives in one.
MAX_SCANNED_BYTES = 1 << 20


def repository_files(root: Path = REPO_ROOT) -> list[Path]:
    """The repository's files as git sees them: tracked, plus untracked ones
    that .gitignore does not exclude (a new file not yet added counts). Never
    the ignored local data (gigabytes of raw ATUS files in data/), installs or
    build output, which a walk of the checkout would read in full."""
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, check=True, timeout=60,
        ).stdout.decode()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        # No git (an exported tree): walk it, skipping local trees and big files.
        files = []
        for directory, subdirs, names in os.walk(root):
            subdirs[:] = [d for d in subdirs if d not in SKIP]
            for name in names:
                path = Path(directory) / name
                if path.is_file() and path.stat().st_size <= MAX_SCANNED_BYTES:
                    files.append(path)
        return files
    return [root / name for name in listing.split("\0") if name]


def test_the_pin_scan_reads_the_repository_not_ignored_local_data(tmp_path, monkeypatch):
    """The scan must not read what .gitignore excludes: the owner's checkout
    holds about 3.4 GB of raw ATUS files under data/ (one over 1 GB)."""
    if subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True,
                      check=False).returncode != 0:
        pytest.skip("git is not available")
    (tmp_path / ".gitignore").write_text("data/staging/\n")
    (tmp_path / "data" / "staging").mkdir(parents=True)
    (tmp_path / "data" / "staging" / "atuscps.dat").write_text("x" * 64)
    (tmp_path / "ci.yml").write_text("image: postgres:16\n")
    listed = {p.relative_to(tmp_path).as_posix() for p in repository_files(tmp_path)}
    assert listed == {".gitignore", "ci.yml"}       # untracked but not ignored: listed

    # Without git: a walk that skips local trees and files too big to hold a pin.
    def no_git(*args, **kwargs):
        raise OSError("git not found")

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.yml").write_text("image: postgres:16\n")
    (tmp_path / "big.dat").write_bytes(b"x" * (MAX_SCANNED_BYTES + 1))
    monkeypatch.setattr(subprocess, "run", no_git)
    walked = {p.relative_to(tmp_path).as_posix() for p in repository_files(tmp_path)}
    assert "ci.yml" in walked and "big.dat" not in walked
    assert not any(name.startswith(("node_modules/", ".git/")) for name in walked)


def test_every_postgres_16_pin_names_the_same_digest():
    """A Dependabot compose or docker bump changes only its own files: CI, the
    deploy snapshot and the image gate would then run another PostgreSQL
    build than the stack. Finish such a pull request by updating every copy
    listed in POSTGRES_PINS (the comment in .github/dependabot.yml says so)."""
    found: dict[str, list[str]] = {}
    for path in repository_files():
        relative = path.relative_to(REPO_ROOT)
        if set(relative.parts) & SKIP or relative.as_posix().startswith(EVIDENCE) \
                or not path.is_file() or path.suffix in (".png", ".gz", ".dump", ".tar"):
            continue
        try:
            digests = POSTGRES_PIN.findall(path.read_text())
        except (UnicodeDecodeError, OSError):
            continue
        if digests:
            found[relative.as_posix()] = digests
    assert {name: len(digests) for name, digests in found.items()} == POSTGRES_PINS
    assert len({d for digests in found.values() for d in digests}) == 1, found
    note = (REPO_ROOT / ".github" / "dependabot.yml").read_text()
    assert "test_every_postgres_16_pin_names_the_same_digest" in note


def test_compose_images_are_digest_pinned_for_the_compose_ecosystem():
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        images = re.findall(r"^\s+image:\s*(\S+)", (REPO_ROOT / name).read_text(), re.MULTILINE)
        registry = [i for i in images if not i.startswith(("atus-", "*", "${"))]
        assert registry and all("@sha256:" in i for i in registry), name
