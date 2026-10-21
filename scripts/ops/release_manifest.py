#!/usr/bin/env python3
"""The committed release manifest ``releases/current.json`` (ADR-013, deploy.yml).

The serving database's release artifact (``atus build-serving-db
--artifact-dir``: the dump, its ``.sha256`` and its manifest) is published by
the owner as the assets of an immutable GitHub release. ``releases/current.json``
names the release the site serves: its tag, data version and every asset's
name, size and SHA-256. The deploy workflow downloads exactly those bytes,
verifies them, restores the dump into ``postgres:16`` and builds the static
snapshot from it, so a deploy can never serve a release nobody committed.

    # owner, after publishing the release (the three files are local):
    python scripts/ops/release_manifest.py write --artifact-dir ~/atus-artifacts/0325-... \\
        --tag data-0325-20261020T224252Z-a7420a5c
    python scripts/ops/release_manifest.py check --require-published

    # deploy.yml:
    python scripts/ops/release_manifest.py fetch --out "$RUNNER_TEMP/release"

Format 1 (``status`` is ``unpublished`` until the owner publishes a release;
deploy.yml refuses to run then):

    {"kind": "atus-serving-release", "format": 1, "status": "published",
     "repository": "Nathan98000/ATUS", "release_tag": "...", "release": "0325",
     "build_id": "...", "data_version": "0325:serving-<build_id>:<hash12>",
     "content_hash": "<64 hex>", "analytics_version": "0.2",
     "assets": [{"role": "dump" | "checksum" | "manifest", "name", "bytes", "sha256"}]}

Standard library only. Exit status: 0 success, 1 check or download failure, 2 usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "releases" / "current.json"
DEFAULT_REPOSITORY = "Nathan98000/ATUS"
KIND = "atus-serving-release"
FORMAT = 1
#: GitHub: "Each file included in a release must be under 2 GiB."
ASSET_LIMIT = 2 * 1024**3
ROLES = {"dump": ".dump", "checksum": ".dump.sha256", "manifest": ".manifest.json"}
#: Hosts a release download may be served from (github.com redirects to them).
DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com",
                  "release-assets.githubusercontent.com")

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RELEASE = re.compile(r"^[0-9]{4}$")
BUILD_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(Exception):
    """The release manifest or an asset is wrong (message safe to print)."""


def serving_version(release: str, build_id: str, content_hash: str) -> str:
    """``atus_pipeline.database.versions.format_serving_version`` (kept in step by a test)."""
    return f"{release}:serving-{build_id}:{content_hash[:12]}"


def validate(doc: dict, *, require_published: bool = False) -> dict:
    """``doc`` when it is a valid format-1 manifest; raises :class:`ManifestError`."""
    if not isinstance(doc, dict) or doc.get("kind") != KIND or doc.get("format") != FORMAT:
        raise ManifestError(f"not a format-{FORMAT} {KIND} manifest")
    if not isinstance(doc.get("repository"), str) or not REPOSITORY.match(doc["repository"]):
        raise ManifestError("repository must be <owner>/<name>")
    status = doc.get("status")
    if status == "unpublished":
        if require_published:
            raise ManifestError(
                "releases/current.json names no published release (status: unpublished). "
                "Publish the serving-DB artifact as an immutable GitHub release, then run "
                "scripts/ops/release_manifest.py write (docs/deployment.md, step 1)"
            )
        return doc
    if status != "published":
        raise ManifestError("status must be 'published' or 'unpublished'")
    for key, pattern in (("release_tag", TAG), ("release", RELEASE), ("build_id", BUILD_ID),
                         ("content_hash", HEX64)):
        if not isinstance(doc.get(key), str) or not pattern.match(doc[key]):
            raise ManifestError(f"{key} is missing or malformed")
    if doc.get("data_version") != serving_version(doc["release"], doc["build_id"],
                                                  doc["content_hash"]):
        raise ManifestError("data_version must be <release>:serving-<build_id>:<content_hash[:12]>")
    if not isinstance(doc.get("analytics_version"), str) or not doc["analytics_version"]:
        raise ManifestError("analytics_version is missing")
    assets = doc.get("assets")
    if not isinstance(assets, list) or sorted(a.get("role") for a in assets
                                              if isinstance(a, dict)) != sorted(ROLES):
        raise ManifestError(f"assets must list exactly one each of {sorted(ROLES)}")
    stem = f"atus-serving-{doc['release']}-{doc['build_id']}"
    for asset in assets:
        role = asset["role"]
        if asset.get("name") != stem + ROLES[role]:
            raise ManifestError(f"asset {role} must be named {stem + ROLES[role]}")
        size = asset.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size < ASSET_LIMIT:
            raise ManifestError(f"asset {role}: bytes must be between 1 and 2 GiB")
        if not isinstance(asset.get("sha256"), str) or not HEX64.match(asset["sha256"]):
            raise ManifestError(f"asset {role}: sha256 must be 64 lowercase hex digits")
    return doc


def load(path: Path, *, require_published: bool = False) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    return validate(doc, require_published=require_published)


def asset(doc: dict, role: str) -> dict:
    return next(a for a in doc["assets"] if a["role"] == role)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_artifact_manifest(doc: dict, artifact_manifest: dict) -> None:
    """The serving build's own manifest agrees with ``releases/current.json``."""
    if artifact_manifest.get("kind") != "atus-serving-db":
        raise ManifestError("the manifest asset is not an atus-serving-db manifest")
    for key in ("release", "build_id", "data_version", "content_hash", "analytics_version"):
        if artifact_manifest.get(key) != doc[key]:
            raise ManifestError(f"the artifact manifest's {key} differs from current.json")
    artifact = artifact_manifest.get("artifact") or {}
    dump = asset(doc, "dump")
    if (artifact.get("file"), artifact.get("bytes"), artifact.get("sha256")) != (
            dump["name"], dump["bytes"], dump["sha256"]):
        raise ManifestError("the artifact manifest describes another dump than current.json")


def write(artifact_dir: Path, tag: str, repository: str, out: Path) -> dict:
    manifests = sorted(artifact_dir.glob("atus-serving-*.manifest.json"))
    if len(manifests) != 1:
        raise ManifestError(f"expected exactly one atus-serving-*.manifest.json in "
                            f"{artifact_dir}, found {len(manifests)}")
    artifact_manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    stem = manifests[0].name[: -len(".manifest.json")]
    doc = {
        "kind": KIND,
        "format": FORMAT,
        "status": "published",
        "repository": repository,
        "release_tag": tag,
        "release": artifact_manifest.get("release"),
        "build_id": artifact_manifest.get("build_id"),
        "data_version": artifact_manifest.get("data_version"),
        "content_hash": artifact_manifest.get("content_hash"),
        "analytics_version": artifact_manifest.get("analytics_version"),
        "assets": [],
    }
    for role, suffix in ROLES.items():
        path = artifact_dir / (stem + suffix)
        if not path.is_file():
            raise ManifestError(f"{path.name} is missing from {artifact_dir}")
        doc["assets"].append({"role": role, "name": path.name, "bytes": path.stat().st_size,
                              "sha256": sha256_file(path)})
    validate(doc, require_published=True)
    check_artifact_manifest(doc, artifact_manifest)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def _check_host(url: str, allowed: tuple[str, ...]) -> None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not ((parts.scheme == "https" and host in allowed) or (parts.scheme == "http" and loopback)):
        raise ManifestError(f"refusing to download from {host or 'an unknown host'}")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to the release-asset hosts."""

    def __init__(self, allowed: tuple[str, ...]) -> None:
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(newurl, self.allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, target: Path, *, size: int, sha256: str,
             allowed: tuple[str, ...] = DOWNLOAD_HOSTS, timeout: float = 120.0) -> None:
    """Stream ``url`` into ``target``, refusing anything but exactly ``size`` bytes
    with the expected SHA-256 (a partial or altered file is deleted)."""
    _check_host(url, allowed)
    opener = urllib.request.build_opener(_CheckedRedirect(allowed))
    request = urllib.request.Request(url, headers={"User-Agent": "atus-release/1"})
    digest, received = hashlib.sha256(), 0
    try:
        with opener.open(request, timeout=timeout) as response, target.open("wb") as out:
            for block in iter(lambda: response.read(1 << 20), b""):
                received += len(block)
                if received > size:
                    raise ManifestError(f"{target.name}: larger than the manifest's {size} B")
                digest.update(block)
                out.write(block)
    except urllib.error.HTTPError as exc:
        target.unlink(missing_ok=True)
        raise ManifestError(f"{target.name}: download failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise ManifestError(f"{target.name}: download failed ({type(exc).__name__})") from None
    except ManifestError:
        target.unlink(missing_ok=True)
        raise
    if received != size or digest.hexdigest() != sha256:
        target.unlink(missing_ok=True)
        raise ManifestError(f"{target.name}: size or SHA-256 differs from releases/current.json")


def fetch(doc: dict, out_dir: Path, *, base: str | None = None,
          allowed: tuple[str, ...] = DOWNLOAD_HOSTS) -> Path:
    """Download and verify every asset into ``out_dir``; returns the dump's path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = base or f"https://github.com/{doc['repository']}/releases/download"
    for item in sorted(doc["assets"], key=lambda a: a["bytes"]):  # small files first
        url = f"{base}/{quote(doc['release_tag'], safe='')}/{quote(item['name'], safe='')}"
        print(f"downloading {item['name']} ({item['bytes']:,} B)", flush=True)
        download(url, out_dir / item["name"], size=item["bytes"], sha256=item["sha256"],
                 allowed=allowed)
    dump = asset(doc, "dump")
    recorded, _, name = (out_dir / asset(doc, "checksum")["name"]).read_text().strip() \
        .partition("  ")
    if (recorded, name) != (dump["sha256"], dump["name"]):
        raise ManifestError("the .sha256 asset disagrees with releases/current.json")
    artifact_manifest = json.loads((out_dir / asset(doc, "manifest")["name"]).read_text())
    check_artifact_manifest(doc, artifact_manifest)
    return out_dir / dump["name"]


def _outputs(doc: dict, extra: dict[str, str] | None = None) -> None:
    values = {"status": doc["status"]}
    if doc["status"] == "published":
        values.update(release_tag=doc["release_tag"], release=doc["release"],
                      data_version=doc["data_version"],
                      analytics_version=doc["analytics_version"],
                      dump_name=asset(doc, "dump")["name"])
    values.update(extra or {})
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH,
                        help="the release manifest (default releases/current.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    chk = sub.add_parser("check", help="validate the manifest")
    chk.add_argument("--require-published", action="store_true")
    wr = sub.add_parser("write", help="write the manifest from a local release artifact")
    wr.add_argument("--artifact-dir", type=Path, required=True)
    wr.add_argument("--tag", required=True)
    wr.add_argument("--repository", default=DEFAULT_REPOSITORY)
    fe = sub.add_parser("fetch", help="download and verify the release's assets")
    fe.add_argument("--out", type=Path, required=True)
    fe.add_argument("--download-base", default=None, help=argparse.SUPPRESS)  # tests only
    args = parser.parse_args(argv)

    try:
        if args.command == "write":
            if not TAG.match(args.tag) or not REPOSITORY.match(args.repository):
                print("error: malformed --tag or --repository", file=sys.stderr)
                return 2
            doc = write(args.artifact_dir, args.tag, args.repository, args.path)
            print(f"wrote {args.path}: {doc['data_version']} from release {doc['release_tag']}")
        elif args.command == "check":
            doc = load(args.path, require_published=args.require_published)
            print(f"{args.path.name}: {doc['status']}" +
                  (f", {doc['data_version']} (release {doc['release_tag']})"
                   if doc["status"] == "published" else ""))
            _outputs(doc)
        else:
            doc = load(args.path, require_published=True)
            dump = fetch(doc, args.out, base=args.download_base)
            print(f"verified {dump.name}: {doc['data_version']}")
            _outputs(doc, {"dump_path": str(dump)})
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
