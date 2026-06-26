"""Unpack a Playwright HAR recording into content-addressed blobs.

Playwright (when driven over the WebSocket server, as we do) always exports
the HAR as a zip: a ``har.har`` manifest plus one ``<sha1>.<extension>`` file
per response body. Each manifest entry's ``response.content._file`` is itself a
content address — the same scheme we use internally — so we store every body
verbatim under that name. The manifest is then a self-describing index into
the storage backend with no rewriting needed. We store the manifest itself as
its own blob and return both it and the list of body names.
"""

import json
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pravda.storage import content_hash, put_blob

logger = logging.getLogger(__name__)


@dataclass
class HttpArchiveCapture:
    """Result of unpacking a HAR recording.

    ``http_archive`` is the content-addressed filename of the metadata-only
    HAR. Its ``content._file`` fields point at the stored bodies.
    ``response_bodies`` lists those body filenames.
    """

    http_archive: str
    response_bodies: list[str]


async def capture_http_archive(zip_path: Path, url: str) -> HttpArchiveCapture | None:
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

        response_bodies: list[str] = []
        for entry in manifest["log"]["entries"]:
            content = entry.get("response", {}).get("content", {})
            file_name = content.get("_file")
            if not file_name:
                continue
            # Playwright named the body ``<sha1>.<extension>`` — store it under
            # that exact name so the manifest already points at our CAS.
            body = archive.read(file_name)
            await put_blob(file_name, body, url)
            response_bodies.append(file_name)

        http_archive_bytes = json.dumps(manifest).encode()
        http_archive_name = f"{content_hash(http_archive_bytes)}.har"
        await put_blob(http_archive_name, http_archive_bytes, url)

    return HttpArchiveCapture(
        http_archive=http_archive_name, response_bodies=response_bodies
    )
