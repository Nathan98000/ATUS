"""Download official ATUS source files from bls.gov.

BLS serves these files behind a CDN that rejects non-browser TLS fingerprints
(plain ``requests``/``curl`` receive HTTP 403 regardless of User-Agent), so the
downloader uses ``curl_cffi`` with browser impersonation to fetch single copies
of the published files, sequentially and with a delay between requests.

If automated download fails (BLS can change its bot policy at any time), every
file can be downloaded manually in a browser and dropped into ``data/raw/``;
``atus extract`` and everything downstream works identically either way. See
docs/source-data.md for the file list.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from curl_cffi import requests as curl_requests

from ..manifest import Manifest
from ..sources import SourceFile

log = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 900
_DELAY_BETWEEN_FILES_SECONDS = 3.0


class DownloadError(RuntimeError):
    pass


def sha256_of_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(url: str, dest: Path) -> None:
    response = curl_requests.get(
        url, impersonate="chrome", timeout=_REQUEST_TIMEOUT_SECONDS, stream=True
    )
    if response.status_code != 200:
        raise DownloadError(
            f"HTTP {response.status_code} fetching {url}. BLS may be blocking automated "
            "retrieval; download the file manually in a browser into data/raw/ instead "
            "(see docs/source-data.md) and re-run."
        )
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with tmp.open("wb") as fh:
            for chunk in response.iter_content():
                fh.write(chunk)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
        response.close()


def download_source(
    source: SourceFile, *, release: str, raw_dir: Path, manifest: Manifest, force: bool = False
) -> Path:
    """Download one source zip into ``raw_dir`` and record it in the manifest.

    Safe to re-run: an existing file is kept (and only re-hashed) unless
    ``force`` is set.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / source.zip_name

    if dest.exists() and not force:
        log.info(
            "%s: already present (%d bytes), skipping download",
            source.zip_name, dest.stat().st_size,
        )
    else:
        log.info("%s: downloading from %s", source.zip_name, source.url)
        _fetch(source.url, dest)
        log.info("%s: downloaded %d bytes", source.zip_name, dest.stat().st_size)

    digest = sha256_of_file(dest)
    manifest.record_download(
        release=release,
        key=source.key,
        url=source.url,
        zip_name=source.zip_name,
        sha256=digest,
        size_bytes=dest.stat().st_size,
    )
    log.info("%s: sha256=%s", source.zip_name, digest)
    return dest


def download_all(
    sources: tuple[SourceFile, ...], *, release: str, raw_dir: Path, manifest: Manifest,
    force: bool = False,
) -> list[Path]:
    paths: list[Path] = []
    for index, source in enumerate(sources):
        if index > 0:
            time.sleep(_DELAY_BETWEEN_FILES_SECONDS)
        paths.append(
            download_source(
                source, release=release, raw_dir=raw_dir, manifest=manifest, force=force
            )
        )
    return paths
