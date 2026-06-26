"""Unpack a Playwright HAR recording into content-addressed blobs.

Playwright (when driven over the WebSocket server, as we do) always exports
the HAR as a zip: a ``har.har`` manifest plus one ``<sha1>`` file per response
body. Each manifest entry's ``response.content._file`` names the body file.

Here we store every body as its own blob, rewrite each ``_file`` to point at
the stored blob (so the HAR is a self-describing index into the storage
backend), store the rewritten metadata-only HAR as its own blob, and return
both. The caller keeps the HAR filename on the snapshot and the body filenames
in a contents table.
"""

import json
import logging
import mimetypes
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pravda.storage import put_blob

logger = logging.getLogger(__name__)


@dataclass
class HarCapture:
    """Result of unpacking a HAR recording.

    ``har`` is the content-addressed filename of the metadata-only HAR (its
    ``content._file`` fields rewritten to point at the stored bodies).
    ``contents`` lists one content-addressed body filename per response body.
    """

    har: str
    contents: list[str]


def _extension_for(mime_type: str | None) -> str:
    """Best-effort filename extension for *mime_type*."""
    if mime_type:
        guess = mimetypes.guess_extension(mime_type)
        if guess:
            return guess.lstrip(".")
    return "bin"


async def capture_har(zip_path: Path, url: str) -> HarCapture | None:
    """Unzip the Playwright HAR at *zip_path*, store bodies and the HAR itself.

    *url* is the page URL; bodies and the HAR are stored under its hostname
    prefix, co-locating a snapshot's evidence. Returns ``None`` when the
    archive holds no manifest (nothing was recorded).
    """
    with zipfile.ZipFile(zip_path) as archive:
        if "har.har" not in archive.namelist():
            logger.warning("No har.har manifest in %s", zip_path)
            return None
        manifest = json.loads(archive.read("har.har"))

        contents: list[str] = []
        for entry in manifest["log"]["entries"]:
            content = entry.get("response", {}).get("content", {})
            file_name = content.get("_file")
            if not file_name:
                continue
            body = archive.read(file_name)
            extension = _extension_for(content.get("mimeType"))
            stored = await put_blob(body, url, extension)
            # Point the manifest at the stored blob instead of the sha1 name.
            content["_file"] = stored
            contents.append(stored)

        har_bytes = json.dumps(manifest).encode()
        har_name = await put_blob(har_bytes, url, "har")

    return HarCapture(har=har_name, contents=contents)
