"""The release artifact: a ``pg_dump -Fc`` of the serving database, its
SHA-256, and a JSON manifest.

Free tiers have no scheduled backups, so this artifact *is* the backup (brief
§9); it is published as GitHub release assets (each under 2 GiB, release
immutability on; ADR-013). Three files, named after the build::

    atus-serving-<release>-<build_id>.dump            pg_dump -Fc --no-owner --no-acl
    atus-serving-<release>-<build_id>.dump.sha256     "<sha256>  <dump file name>"
    atus-serving-<release>-<build_id>.manifest.json   what the dump holds

The dump's SHA-256 identifies the file and is not expected to repeat across
builds; the manifest's ``content_hash`` identifies the data and does. The
manifest holds no connection string.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from importlib import metadata
from pathlib import Path

import psycopg

from .. import __version__ as PIPELINE_VERSION
from ..analytics import ANALYTICS_VERSION
from . import pgtools
from .builder import BuildReport

MANIFEST_FORMAT = 1
KIND = "atus-serving-db"
# GitHub: "Each file included in a release must be under 2 GiB."
GITHUB_RELEASE_ASSET_LIMIT = 2 * 1024**3
DUMP_OPTIONS = ("--format=custom", "--no-owner", "--no-acl")


class ArtifactError(RuntimeError):
    """The artifact is missing, altered, or does not match its manifest."""


def artifact_stem(release: str, build_id: str) -> str:
    return f"atus-serving-{release}-{build_id}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(*args: str) -> str | None:
    root = Path(__file__).resolve().parents[3]
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.decode().strip()


def tool_versions(conn: psycopg.Connection, dump_tool: pgtools.PgTool | None) -> dict:
    commit = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "server": conn.execute("SHOW server_version").fetchone()[0],
        "pg_dump": f"{dump_tool.version()} ({dump_tool.origin})" if dump_tool else None,
        "python": platform.python_version(),
        "psycopg": metadata.version("psycopg"),
        "libpq": psycopg.pq.version(),
        "atus_pipeline": PIPELINE_VERSION,
        "git_commit": commit,
        "git_dirty": bool(dirty) if dirty is not None else None,
    }


def dump_tool(conninfo: str, *, image: str | None = None) -> pgtools.PgTool:
    """``pg_dump`` for the database at ``conninfo``, resolved and run once
    (``--version``), so a missing or unusable client is found before a build
    that depends on it (a built target refuses a second build; review r1)."""
    with psycopg.connect(conninfo, autocommit=True) as conn:
        server_major = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000
    tool = pgtools.resolve("pg_dump", conninfo, server_major=server_major, image=image)
    version = tool.version()
    major = pgtools.parse_major(version)
    if major is None or major < server_major:
        raise pgtools.PgToolError(
            f"pg_dump ({tool.origin}) did not report version {server_major} or newer: "
            f"{version[:200]!r}"
        )
    return tool


def write_artifact(conninfo: str, out_dir: Path, report: BuildReport, *,
                   image: str | None = None) -> Path:
    """Dump the serving database at ``conninfo`` into ``out_dir`` and write its
    checksum and manifest; returns the manifest's path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = artifact_stem(report.release, report.build_id)
    dump_path = out_dir / f"{stem}.dump"
    if dump_path.exists():
        raise ArtifactError(f"{dump_path} already exists")
    with psycopg.connect(conninfo, autocommit=True) as conn:
        server_major = int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000
        tool = pgtools.resolve("pg_dump", conninfo, server_major=server_major, image=image)
        tools = tool_versions(conn, tool)
        # The dump must hold exactly what was built: nothing recorded since.
        recorded = conn.execute("SELECT count(*) FROM atus.validation_results").fetchone()[0]
        if recorded:
            raise ArtifactError(
                "validation_results is not empty: dump the database before running "
                "`atus validate-analytics` on it (that check writes there)"
            )
    with dump_path.open("wb") as out:
        try:
            tool.run([*DUMP_OPTIONS], stdout=out)
        except pgtools.PgToolError:
            dump_path.unlink(missing_ok=True)
            raise
    size = dump_path.stat().st_size
    digest = sha256_file(dump_path)
    (out_dir / f"{stem}.dump.sha256").write_text(f"{digest}  {dump_path.name}\n")

    manifest = {
        "kind": KIND,
        "manifest_format": MANIFEST_FORMAT,
        **{k: v for k, v in report.as_dict().items() if k not in ("seconds",)},
        "analytics_version": ANALYTICS_VERSION,
        "artifact": {
            "file": dump_path.name,
            "bytes": size,
            "sha256": digest,
            "format": "pg_dump custom format (" + " ".join(DUMP_OPTIONS) + ")",
            "under_github_release_asset_limit": size < GITHUB_RELEASE_ASSET_LIMIT,
        },
        "tools": tools,
        "seconds_illustrative": report.seconds,
    }
    manifest_path = out_dir / f"{stem}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")
    if size >= GITHUB_RELEASE_ASSET_LIMIT:
        raise ArtifactError(
            f"{dump_path.name} is {size:,} bytes, over GitHub's 2 GiB release-asset limit"
        )
    return manifest_path


def read_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text())
    if manifest.get("kind") != KIND:
        raise ArtifactError(f"{path} is not an {KIND} manifest")
    for key in ("data_version", "content_hash", "release", "build_id", "artifact", "tables"):
        if key not in manifest:
            raise ArtifactError(f"{path} lacks {key!r}")
    return manifest


def manifest_for(dump_path: Path) -> Path:
    name = dump_path.name
    if not name.endswith(".dump"):
        raise ArtifactError(f"{dump_path} is not a .dump file")
    return dump_path.with_name(name[: -len(".dump")] + ".manifest.json")


def verify_artifact(dump_path: Path, manifest: dict) -> str:
    """Check the dump against its ``.sha256`` file and its manifest; returns
    the SHA-256. Raises :class:`ArtifactError` on any mismatch."""
    if not dump_path.is_file():
        raise ArtifactError(f"{dump_path} does not exist")
    if manifest["artifact"]["file"] != dump_path.name:
        raise ArtifactError(
            f"the manifest describes {manifest['artifact']['file']}, not {dump_path.name}"
        )
    digest = sha256_file(dump_path)
    if digest != manifest["artifact"]["sha256"]:
        raise ArtifactError(f"{dump_path.name}: SHA-256 does not match the manifest")
    checksum_file = dump_path.with_name(dump_path.name + ".sha256")
    if checksum_file.exists():
        recorded, _, name = checksum_file.read_text().strip().partition("  ")
        if recorded != digest or name != dump_path.name:
            raise ArtifactError(f"{dump_path.name}: SHA-256 does not match {checksum_file.name}")
    if dump_path.stat().st_size != manifest["artifact"]["bytes"]:
        raise ArtifactError(f"{dump_path.name}: size does not match the manifest")
    return digest
