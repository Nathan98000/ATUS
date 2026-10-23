"""Unit tests for the acquisition/staging manifest."""

import json

import pytest

from atus_pipeline.manifest import Manifest


@pytest.fixture
def manifest(tmp_path):
    return Manifest(tmp_path / "manifest.json")


def test_download_then_staging_roundtrip(manifest, tmp_path):
    manifest.record_download(
        release="0325", key="roster", url="https://www.bls.gov/tus/datafiles/atusrost-0325.zip",
        zip_name="atusrost-0325.zip", sha256="ab" * 32, size_bytes=123,
    )
    manifest.record_staging(
        release="0325", key="roster", dat_name="atusrost_0325.dat",
        dat_size_bytes=456, row_count=42,
    )

    reloaded = Manifest(manifest.path)
    entry = reloaded.get("0325", "roster")
    assert entry.zip_sha256 == "ab" * 32
    assert entry.data_row_count == 42
    assert entry.downloaded_at and entry.staged_at


def test_staging_without_download_fails(manifest):
    with pytest.raises(KeyError, match="no download recorded"):
        manifest.record_staging(
            release="0325", key="roster", dat_name="x.dat", dat_size_bytes=1, row_count=1
        )


def test_entries_are_keyed_by_release_and_file(manifest):
    for release in ("0324", "0325"):
        manifest.record_download(
            release=release, key="roster", url="https://example", zip_name="z.zip",
            sha256="00" * 32, size_bytes=1,
        )
    assert manifest.get("0324", "roster") is not manifest.get("0325", "roster")


def test_manifest_file_is_valid_json(manifest):
    manifest.record_download(
        release="0325", key="roster", url="https://example", zip_name="z.zip",
        sha256="00" * 32, size_bytes=1,
    )
    payload = json.loads(manifest.path.read_text())
    assert payload["format_version"] == 1
    assert payload["files"][0]["key"] == "roster"
