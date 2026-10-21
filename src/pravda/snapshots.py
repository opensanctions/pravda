"""Public capture values."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from unmhtml import Document, Resource, to_standalone_html

if TYPE_CHECKING:
    from pravda.storage import Storage


@dataclass(frozen=True)
class Snapshot:
    """A capture outcome; artifact fields are storage-relative names.

    ``id`` and ``captured_at`` are minted at capture. Failed captures are
    returned with ``error`` set and no artifacts. Read artifacts back with
    ``storage.read_artifact(name, snapshot.final_url)``, or derive a
    self-contained rendering with :meth:`derive_standalone_html`.
    """

    id: uuid.UUID
    url: str
    final_url: str | None
    captured_at: datetime
    http_status: int | None
    error: str | None
    rendered_html: str | None
    screenshot: str | None
    http_archive: dict | None

    async def derive_standalone_html(self, storage: "Storage") -> str:
        """Derive a self-contained HTML rendering of this snapshot's page.

        Reads the snapshot's stored rendered HTML and recorded HAR bodies,
        inlines stylesheets, embeds images as data URIs, and sanitizes
        active content. Storage failures propagate, and a wedged-but-alive
        backend hangs until the caller cancels.
        """
        if self.rendered_html is None or self.http_archive is None:
            raise ValueError(
                f"snapshot {self.id} lacks rendered HTML or HTTP archive "
                "evidence to derive standalone HTML from"
            )
        resources = {}
        for entry in self.http_archive["log"]["entries"]:
            content = entry["response"]["content"]
            file_name = content.get("_file")
            if not file_name:
                continue
            # A HAR MIME type may carry parameters ("text/css; charset=utf-8")
            # that would corrupt the data URIs built from it.
            mime_type = content.get("mimeType", "").partition(";")[0].strip()
            data = await storage.read_artifact(file_name, self.final_url)
            resources[entry["request"]["url"]] = Resource(data, mime_type or None)

        html = (
            await storage.read_artifact(self.rendered_html, self.final_url)
        ).decode()
        document = Document(html=html, resources=resources, base_url=self.final_url)
        return await asyncio.to_thread(to_standalone_html, document)
