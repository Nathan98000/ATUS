"""Extract the CSV data member of each downloaded archive into the staging area.

Raw zips in ``data/raw/`` are treated as immutable; staging copies the data
file (BLS names them ``.dat`` but they are plain CSV) into
``data/staging/<release>/`` where validation and loading read them. Extraction
streams to disk, so even the 1.2 GB ATUS-CPS file never needs to fit in memory.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from ..manifest import Manifest
from ..sources import SourceFile

log = logging.getLogger(__name__)


class StagingError(RuntimeError):
    pass


def _count_data_rows(path: Path) -> int:
    """Count non-empty lines excluding the header, streaming."""
    count = 0
    with path.open("rb") as fh:
        first = True
        for line in fh:
            if first:
                first = False
                continue
            if line.strip():
                count += 1
    return count


def extract_source(
    source: SourceFile, *, release: str, raw_dir: Path, staging_dir: Path, manifest: Manifest,
    force: bool = False,
) -> Path:
    """Extract one source's data file into staging and record row counts."""
    archive_path = raw_dir / source.zip_name
    if not archive_path.exists():
        raise StagingError(
            f"{archive_path} not found. Run `atus download` (or place the file manually) first."
        )

    staging_dir.mkdir(parents=True, exist_ok=True)
    dest = staging_dir / source.dat_name

    if dest.exists() and not force:
        log.info("%s: already staged, skipping extraction", source.dat_name)
    else:
        with zipfile.ZipFile(archive_path) as archive:
            try:
                info = archive.getinfo(source.dat_name)
            except KeyError as exc:
                members = ", ".join(archive.namelist())
                raise StagingError(
                    f"{source.zip_name} does not contain expected member {source.dat_name!r}. "
                    f"Members: {members}"
                ) from exc
            log.info(
                "%s: extracting %s (%d bytes)",
                source.zip_name, source.dat_name, info.file_size,
            )
            tmp = dest.with_suffix(dest.suffix + ".part")
            with archive.open(info) as src, tmp.open("wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
            tmp.replace(dest)

    rows = _count_data_rows(dest)
    log.info("%s: staged with %d data rows", source.dat_name, rows)
    manifest.record_staging(
        release=release,
        key=source.key,
        dat_name=source.dat_name,
        dat_size_bytes=dest.stat().st_size,
        row_count=rows,
    )
    return dest


def extract_all(
    sources: tuple[SourceFile, ...], *, release: str, raw_dir: Path, staging_dir: Path,
    manifest: Manifest, force: bool = False,
) -> list[Path]:
    return [
        extract_source(
            source, release=release, raw_dir=raw_dir, staging_dir=staging_dir,
            manifest=manifest, force=force,
        )
        for source in sources
    ]
