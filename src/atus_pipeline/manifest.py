"""Acquisition/staging manifest: provenance for every source file.

The manifest (``data/manifest.json``) records, for each source file, where it
came from, when it was downloaded, its size and SHA-256 checksum, and staging
metadata (extracted data file, row count). It is the pipeline's provenance
record and is also written into the database (``atus.source_files``) at load
time so the database itself can answer "which exact files produced this data?".

BLS does not publish checksums for these files, so the recorded hashes
establish what *this* pipeline saw, not an external ground truth.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class ManifestEntry:
    key: str
    release: str
    url: str
    zip_name: str
    zip_sha256: str = ""
    zip_size_bytes: int = 0
    downloaded_at: str = ""          # ISO-8601 UTC
    dat_name: str = ""
    dat_size_bytes: int = 0
    staged_at: str = ""              # ISO-8601 UTC
    data_row_count: int = 0          # rows excluding header, counted at staging time
    extra: dict = field(default_factory=dict)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, ManifestEntry] = {}
        if path.exists():
            raw = json.loads(path.read_text())
            for item in raw.get("files", []):
                entry = ManifestEntry(**item)
                self.entries[self._entry_id(entry.release, entry.key)] = entry

    @staticmethod
    def _entry_id(release: str, key: str) -> str:
        return f"{release}/{key}"

    def get(self, release: str, key: str) -> ManifestEntry | None:
        return self.entries.get(self._entry_id(release, key))

    def record_download(
        self, *, release: str, key: str, url: str, zip_name: str, sha256: str, size_bytes: int
    ) -> ManifestEntry:
        entry = self.get(release, key) or ManifestEntry(
            key=key, release=release, url=url, zip_name=zip_name
        )
        entry.url = url
        entry.zip_name = zip_name
        entry.zip_sha256 = sha256
        entry.zip_size_bytes = size_bytes
        entry.downloaded_at = _utcnow()
        self.entries[self._entry_id(release, key)] = entry
        self.save()
        return entry

    def record_staging(
        self, *, release: str, key: str, dat_name: str, dat_size_bytes: int, row_count: int
    ) -> ManifestEntry:
        entry = self.get(release, key)
        if entry is None:
            raise KeyError(
                f"Cannot record staging for {release}/{key}: no download recorded. "
                "Run `atus download` first."
            )
        entry.dat_name = dat_name
        entry.dat_size_bytes = dat_size_bytes
        entry.staged_at = _utcnow()
        entry.data_row_count = row_count
        self.save()
        return entry

    def save(self) -> None:
        payload = {
            "format_version": 1,
            "updated_at": _utcnow(),
            "files": [
                asdict(e)
                for e in sorted(self.entries.values(), key=lambda e: (e.release, e.key))
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.path)
