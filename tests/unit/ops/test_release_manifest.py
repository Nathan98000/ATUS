"""scripts/ops/release_manifest.py: releases/current.json, its writer and the verified download."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from atus_pipeline.database.versions import format_serving_version

from .conftest import OPS_DIR, REPO_ROOT, load_script

rm = load_script(OPS_DIR / "release_manifest.py", "atus_ops_release_manifest")

BUILD = "20261020T224252Z-a7420a5c"
CONTENT = "de834e672474e24898d20c17585f0ef48cc39c0e8df2289bc96b08034dd7f833"
STEM = f"atus-serving-0325-{BUILD}"
TAG = f"data-0325-{BUILD}"


def artifact(tmp_path):
    """A fake release artifact in the layout `atus build-serving-db` writes."""
    out = tmp_path / "artifact"
    out.mkdir()
    dump = out / f"{STEM}.dump"
    dump.write_bytes(b"PGDMP fake dump bytes " * 100)
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    (out / f"{STEM}.dump.sha256").write_text(f"{digest}  {dump.name}\n")
    manifest = {
        "kind": "atus-serving-db", "manifest_format": 1, "release": "0325", "build_id": BUILD,
        "data_version": format_serving_version("0325", BUILD, CONTENT),
        "content_hash": CONTENT, "analytics_version": "0.2", "tables": {},
        "artifact": {"file": dump.name, "bytes": dump.stat().st_size, "sha256": digest},
    }
    (out / f"{STEM}.manifest.json").write_text(json.dumps(manifest))
    return out


def written(tmp_path) -> tuple[dict, object]:
    out = artifact(tmp_path)
    path = tmp_path / "current.json"
    assert rm.main(["--path", str(path), "write", "--artifact-dir", str(out), "--tag", TAG]) == 0
    return json.loads(path.read_text()), out


def test_the_committed_manifest_is_valid():
    doc = rm.load(REPO_ROOT / "releases" / "current.json")
    assert doc["repository"] == "Nathan98000/ATUS"


def test_an_unpublished_manifest_blocks_a_deploy(tmp_path, capsys):
    path = tmp_path / "current.json"
    path.write_text(json.dumps({"kind": rm.KIND, "format": 1, "status": "unpublished",
                                "repository": "Nathan98000/ATUS"}))
    assert rm.main(["--path", str(path), "check"]) == 0
    assert rm.main(["--path", str(path), "check", "--require-published"]) == 1
    assert "names no published release" in capsys.readouterr().err


def test_serving_version_matches_the_builder():
    assert rm.serving_version("0325", BUILD, CONTENT) == \
        format_serving_version("0325", BUILD, CONTENT)


def test_write_records_every_asset_and_check_passes(tmp_path):
    doc, out = written(tmp_path)
    assert doc["status"] == "published" and doc["release_tag"] == TAG
    assert doc["data_version"] == f"0325:serving-{BUILD}:{CONTENT[:12]}"
    by_role = {a["role"]: a for a in doc["assets"]}
    dump = out / f"{STEM}.dump"
    assert by_role["dump"]["sha256"] == hashlib.sha256(dump.read_bytes()).hexdigest()
    assert by_role["dump"]["bytes"] == dump.stat().st_size
    rm.validate(doc, require_published=True)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(data_version="0325:serving-other:000000000000"),
    lambda d: d.update(release_tag="bad tag"),
    lambda d: d.update(content_hash="XYZ"),
    lambda d: d["assets"].pop(),
    lambda d: d["assets"][0].update(sha256="0" * 63),
    lambda d: d["assets"][0].update(bytes=3 * 1024**3),
    lambda d: d["assets"][0].update(name="other.dump"),
    lambda d: d.update(status="draft"),
    lambda d: d.update(repository="not a repo"),
])
def test_malformed_manifests_are_refused(tmp_path, mutate):
    doc, _ = written(tmp_path)
    bad = copy.deepcopy(doc)
    mutate(bad)
    with pytest.raises(rm.ManifestError):
        rm.validate(bad, require_published=True)


def serve_release(server, out, *, tamper: bool = False) -> str:
    base = "/releases/download"
    for path in out.iterdir():
        body = path.read_bytes()
        if tamper and path.name.endswith(".dump"):
            body = body.replace(b"fake", b"evil")
        server.routes[("GET", f"{base}/{TAG}/{path.name}")] = (
            200, {"Content-Type": "application/octet-stream"}, body)
    return server.base + base


def test_fetch_downloads_and_verifies_every_asset(tmp_path, fake_server, monkeypatch):
    doc, out = written(tmp_path)
    base = serve_release(fake_server, out)
    output = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    target = tmp_path / "download"
    assert rm.main(["--path", str(tmp_path / "current.json"), "fetch", "--out", str(target),
                    "--download-base", base]) == 0
    assert (target / f"{STEM}.dump").read_bytes() == (out / f"{STEM}.dump").read_bytes()
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["data_version"] == doc["data_version"]
    assert outputs["dump_path"] == str(target / f"{STEM}.dump")


def test_a_tampered_dump_is_refused_and_deleted(tmp_path, fake_server, capsys):
    _, out = written(tmp_path)
    base = serve_release(fake_server, out, tamper=True)
    target = tmp_path / "download"
    assert rm.main(["--path", str(tmp_path / "current.json"), "fetch", "--out", str(target),
                    "--download-base", base]) == 1
    assert not (target / f"{STEM}.dump").exists()
    assert "SHA-256 differs" in capsys.readouterr().err


def test_an_artifact_manifest_for_another_build_is_refused(tmp_path, fake_server):
    doc, out = written(tmp_path)
    manifest_path = out / f"{STEM}.manifest.json"
    other = json.loads(manifest_path.read_text())
    other["content_hash"] = "f" * 64
    manifest_path.write_text(json.dumps(other))
    for asset in doc["assets"]:  # current.json pins the altered file's bytes too
        if asset["role"] == "manifest":
            asset["sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            asset["bytes"] = manifest_path.stat().st_size
    with pytest.raises(rm.ManifestError, match="content_hash differs"):
        rm.fetch(doc, tmp_path / "download", base=serve_release(fake_server, out))


def test_downloads_only_from_release_hosts(tmp_path):
    doc, _ = written(tmp_path)
    with pytest.raises(rm.ManifestError, match="refusing to download"):
        rm.fetch(doc, tmp_path / "download", base="https://evil.example/releases")


def test_a_redirect_off_the_release_hosts_is_refused(tmp_path, fake_server):
    doc, out = written(tmp_path)
    base = serve_release(fake_server, out)
    for asset in doc["assets"]:
        fake_server.routes[("GET", f"/releases/download/{TAG}/{asset['name']}")] = (
            302, {"Location": "https://evil.example/x"}, b"")
    with pytest.raises(rm.ManifestError, match="refusing to download"):
        rm.fetch(doc, tmp_path / "download", base=base)
