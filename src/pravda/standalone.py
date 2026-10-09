"""Standalone HTML derived from captured evidence."""

import asyncio
import zipfile
from pathlib import Path

from unmhtml import Document, Resource, to_standalone_html

from pravda.storage import Storage


async def capture_standalone_html(
    zip_path: Path, manifest: dict, html: str, final_url: str, storage: Storage
) -> str:
    """Derive and store a self-contained HTML version of the page."""
    with zipfile.ZipFile(zip_path) as archive:
        recorded = set(archive.namelist())
        resources = {}
        for entry in manifest["log"]["entries"]:
            content = entry["response"]["content"]
            file_name = content.get("_file")
            if not file_name or file_name not in recorded:
                continue
            # A HAR MIME type may carry parameters ("text/css; charset=utf-8")
            # that would corrupt the data URIs built from it.
            mime_type = content.get("mimeType", "").partition(";")[0].strip()
            resources[entry["request"]["url"]] = Resource(
                archive.read(file_name), mime_type or None
            )

    document = Document(html=html, resources=resources, base_url=final_url)
    standalone = await asyncio.to_thread(to_standalone_html, document)
    return await storage.put_artifact(standalone.encode(), "html", final_url)
