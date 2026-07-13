"""Unpack a Playwright HAR recording into content-addressed bodies.

Playwright (when driven over the WebSocket server, as we do) always exports
the HAR as a zip: a ``har.har`` manifest plus one ``<sha1>.<extension>`` file
per response body. Each manifest entry's ``response.content._file`` is itself a
content address — the same scheme we use internally — so we store every body
verbatim under that name. The manifest is then a self-describing index into
the storage backend with no rewriting needed. We return the manifest so the
caller can persist it inline (as JSON) in the database and serve it straight
from the API.
"""

import asyncio
import json
import logging
import zipfile
from pathlib import Path

from pravda.capture import DownloadedBody
from pravda.storage import STORAGE_WRITE_TIMEOUT_S, cas_name, put_blob

logger = logging.getLogger(__name__)


async def capture_http_archive(
    zip_path: Path, url: str, download: DownloadedBody | None = None
) -> dict | None:
    """Unzip the Playwright HAR at *zip_path*, store bodies, return the manifest.

    *url* is the page URL; bodies are stored under its hostname prefix,
    co-locating a snapshot's evidence. Returns the HAR manifest (as a dict)
    so the caller can persist it inline, or ``None`` when the archive holds
    no manifest (nothing was recorded).

    *download*, when present, is a response body Chrome downloaded instead of
    rendering (so Playwright never recorded it). It is folded back into the
    matching manifest entry as a ``content._file``, making the HAR a complete
    index of every captured body — including downloads.
    """
    with zipfile.ZipFile(zip_path) as archive:
        if "har.har" not in archive.namelist():
            logger.warning("No har.har manifest in %s", zip_path)
            return None
        manifest = json.loads(archive.read("har.har"))
        recorded = set(archive.namelist())

        if download is not None:
            await _inject_download(manifest, download, url)

        for entry in manifest["log"]["entries"]:
            file_name = entry["response"]["content"].get("_file")
            # The download body (if any) was stored by ``_inject_download``;
            # it is not part of the zip, so skip it here.
            if not file_name or file_name not in recorded:
                continue
            await put_blob(file_name, archive.read(file_name), url)

    return manifest


async def _store_body(download: DownloadedBody, url: str) -> str | None:
    """Store a download's bytes as a ``<sha1>.<ext>`` blob, return its name.

    The extension comes from ``download.suggested_filename`` — the name Chrome
    itself chose for the download. Returns ``None`` when the write exceeds its
    budget, so the caller can leave the matching HAR entry bodyless instead of
    dropping the whole archive.
    """
    ext = Path(download.suggested_filename).suffix
    name = cas_name(download.data, ext)
    try:
        async with asyncio.timeout(STORAGE_WRITE_TIMEOUT_S):
            await put_blob(name, download.data, url)
    except asyncio.TimeoutError:
        logger.warning("Timeout storing download body for %s", download.url)
        return None
    return name


async def _inject_download(manifest: dict, download: DownloadedBody, url: str) -> None:
    """Patch the manifest entry for *download* to reference its stored body.

    Finds the bodyless entry whose request URL matches the download (the one
    Chrome's viewer swallowed), stores the bytes as a ``<sha1>.<ext>`` blob,
    and points the entry's ``content._file`` at it — exactly as if Playwright
    had recorded the body itself. Leaves the entry bodyless when the store
    fails, so the rest of the archive survives.
    """
    for entry in manifest["log"]["entries"]:
        if entry["request"]["url"] != download.url:
            continue
        content = entry["response"]["content"]
        if content.get("_file"):
            # This entry already has a body; keep looking for the bodyless one.
            continue
        name = await _store_body(download, url)
        if name is None:
            return
        content["_file"] = name
        content["size"] = len(download.data)
        return
