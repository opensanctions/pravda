"""Unpack a Playwright HAR recording into content-addressed blobs.

Playwright (when driven over the WebSocket server, as we do) always exports
the HAR as a zip: a ``har.har`` manifest plus one ``<sha1>.<extension>`` file
per response body. Each manifest entry's ``response.content._file`` is itself a
content address — the same scheme we use internally — so we store every body
verbatim under that name. The manifest is then a self-describing index into
the storage backend with no rewriting needed. We store the manifest itself as
its own blob and return its content-addressed filename.
"""

import json
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pravda.capture import DownloadedBody
from pravda.storage import content_hash, put_blob

logger = logging.getLogger(__name__)


@dataclass
class HttpArchiveCapture:
    """Result of unpacking a HAR recording.

    ``http_archive`` is the content-addressed filename of the metadata-only
    HAR. Its ``content._file`` fields point at the stored bodies, so the
    bodies need no separate index here.
    """

    http_archive: str


async def capture_http_archive(
    zip_path: Path, url: str, download: DownloadedBody | None = None
) -> HttpArchiveCapture | None:
    """Unzip the Playwright HAR at *zip_path*, store bodies and the HAR itself.

    *url* is the page URL; bodies and the HAR are stored under its hostname
    prefix, co-locating a snapshot's evidence. Returns ``None`` when the
    archive holds no manifest (nothing was recorded).

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
            file_name = entry.get("response", {}).get("content", {}).get("_file")
            # The download body (if any) was stored by ``_inject_download``;
            # it is not part of the zip, so skip it here.
            if not file_name or file_name not in recorded:
                continue
            await put_blob(file_name, archive.read(file_name), url)

        http_archive_bytes = json.dumps(manifest).encode()
        http_archive_name = f"{content_hash(http_archive_bytes)}.har"
        await put_blob(http_archive_name, http_archive_bytes, url)

    return HttpArchiveCapture(http_archive=http_archive_name)


def _extension_for(url: str, mime_type: str | None) -> str:
    """Derive a body filename extension, matching Playwright's convention.

    Playwright takes the extension from the request URL's last path segment
    (e.g. ``doc.pdf`` -> ``pdf``), falling back to the MIME subtype when the
    URL has none (e.g. ``image/png`` -> ``png``).
    """
    last_segment = urlparse(url).path.rsplit("/", 1)[-1]
    if "." in last_segment:
        ext = last_segment.rsplit(".", 1)[-1]
        if ext.isalnum():
            return ext.lower()
    if mime_type:
        subtype = mime_type.split("/")[-1].split(";")[0].strip()
        if subtype and "/" not in subtype:
            return subtype.lower()
    return ""


async def _inject_download(manifest: dict, download: DownloadedBody, url: str) -> None:
    """Patch the manifest entry for *download* to reference its stored body.

    Finds the bodyless entry whose request URL matches the download (the one
    Chrome's viewer swallowed), stores the bytes as a ``<sha1>.<ext>`` blob,
    and points the entry's ``content._file`` at it — exactly as if Playwright
    had recorded the body itself.
    """
    for entry in manifest["log"]["entries"]:
        if entry["request"]["url"] != download.url:
            continue
        content = entry["response"]["content"]
        if content.get("_file"):
            # This entry already has a body; keep looking for the bodyless one.
            continue
        ext = _extension_for(download.url, content.get("mimeType"))
        name = (
            f"{content_hash(download.data)}.{ext}"
            if ext
            else content_hash(download.data)
        )
        await put_blob(name, download.data, url)
        content["_file"] = name
        content["size"] = len(download.data)
        return

    # No matching bodyless entry — rare, but don't lose the bytes. Store the
    # body as an orphan the manifest won't reference.
    logger.warning(
        "No bodyless HAR entry for download %s; storing body as orphan",
        download.url,
    )
    ext = _extension_for(download.url, None)
    name = (
        f"{content_hash(download.data)}.{ext}" if ext else content_hash(download.data)
    )
    await put_blob(name, download.data, url)
