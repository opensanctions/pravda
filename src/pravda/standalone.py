"""Standalone HTML derived from stored capture evidence."""

import asyncio

from unmhtml import Document, Resource, to_standalone_html

from pravda.snapshots import Snapshot
from pravda.storage import Storage


async def derive_standalone_html(snapshot: Snapshot, storage: Storage) -> str:
    """Derive a self-contained HTML rendering of *snapshot*'s page.

    Reads the snapshot's stored rendered HTML and recorded HAR bodies,
    inlines stylesheets, embeds images as data URIs, and sanitizes active
    content. Storage failures propagate, and a wedged-but-alive backend
    hangs until the caller cancels.
    """
    if snapshot.rendered_html is None or snapshot.http_archive is None:
        raise ValueError(
            f"snapshot {snapshot.id} lacks rendered HTML or HTTP archive "
            "evidence to derive standalone HTML from"
        )
    resources = {}
    for entry in snapshot.http_archive["log"]["entries"]:
        content = entry["response"]["content"]
        file_name = content.get("_file")
        if not file_name:
            continue
        # A HAR MIME type may carry parameters ("text/css; charset=utf-8")
        # that would corrupt the data URIs built from it.
        mime_type = content.get("mimeType", "").partition(";")[0].strip()
        data = await storage.read_artifact(file_name, snapshot.final_url)
        resources[entry["request"]["url"]] = Resource(data, mime_type or None)

    html = (
        await storage.read_artifact(snapshot.rendered_html, snapshot.final_url)
    ).decode()
    document = Document(html=html, resources=resources, base_url=snapshot.final_url)
    return await asyncio.to_thread(to_standalone_html, document)
