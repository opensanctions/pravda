"""Store the bodies from a Playwright HAR archive."""

import json
import logging
import zipfile
from pathlib import Path

from pravda.capture import DownloadedBody
from pravda.storage import Storage

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
        ext = Path(download.suggested_filename).suffix
        content["_file"] = await storage.put_artifact(download.data, ext, url)
        content["size"] = len(download.data)
        return
