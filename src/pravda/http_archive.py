"""Store the bodies from a Playwright HAR archive."""

import asyncio
import json
import logging
import zipfile
from pathlib import Path

from pravda.capture import DownloadedBody
from pravda.storage import STORAGE_WRITE_TIMEOUT_S, Storage, cas_name

logger = logging.getLogger(__name__)


async def capture_http_archive(
    zip_path: Path, url: str, storage: Storage, download: DownloadedBody | None = None
) -> dict | None:
    """Store HAR bodies and return the manifest, including any *download*."""
    with zipfile.ZipFile(zip_path) as archive:
        if "har.har" not in archive.namelist():
            logger.warning("No har.har manifest in %s", zip_path)
            return None
        manifest = json.loads(archive.read("har.har"))
        recorded = set(archive.namelist())

        if download is not None:
            await _inject_download(manifest, download, url, storage)

        for entry in manifest["log"]["entries"]:
            file_name = entry["response"]["content"].get("_file")
            if not file_name or file_name not in recorded:
                continue
            await storage.put_blob(file_name, archive.read(file_name), url)

    return manifest


async def _store_body(
    download: DownloadedBody, url: str, storage: Storage
) -> str | None:
    """Store a downloaded body, returning its name or ``None`` on timeout."""
    ext = Path(download.suggested_filename).suffix
    name = cas_name(download.data, ext)
    try:
        async with asyncio.timeout(STORAGE_WRITE_TIMEOUT_S):
            await storage.put_blob(name, download.data, url)
    except asyncio.TimeoutError:
        logger.warning("Timeout storing download body for %s", download.url)
        return None
    return name


async def _inject_download(
    manifest: dict, download: DownloadedBody, url: str, storage: Storage
) -> None:
    """Store *download* and link it from its bodyless manifest entry."""
    for entry in manifest["log"]["entries"]:
        if entry["request"]["url"] != download.url:
            continue
        content = entry["response"]["content"]
        if content.get("_file"):
            continue
        name = await _store_body(download, url, storage)
        if name is None:
            return
        content["_file"] = name
        content["size"] = len(download.data)
        return
