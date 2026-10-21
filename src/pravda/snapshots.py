"""Public capture values."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime

from pravda.storage import Storage


@dataclass(frozen=True)
class Snapshot:
    """A capture outcome; artifact fields are storage-relative paths.

    ``id`` and ``captured_at`` are minted at capture. Failed captures are
    returned with ``error`` set and no artifacts. Read artifacts back with
    ``storage.read_artifact(path)``, or derive a self-contained rendering
    with :meth:`derive_standalone_html`, which stores the result and
    returns its path.
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

    async def derive_standalone_html(self, storage: Storage) -> str:
        """Derive, store, and return the path of a standalone HTML artifact.

        Reads the snapshot's stored rendered HTML and recorded HAR bodies,
        inlines stylesheets, embeds images as data URIs, and sanitizes
        active content. The result is stored content-addressed like other
        artifacts and its storage-relative path returned. Requires the
        ``unmhtml`` extra. Storage failures propagate, and a
        wedged-but-alive backend hangs until the caller cancels.
        """
        if (
            self.rendered_html is None
            or self.http_archive is None
            or self.final_url is None
        ):
            raise ValueError(
                f"snapshot {self.id} lacks rendered HTML, HTTP archive, or "
                "final URL evidence to derive standalone HTML from"
            )
        try:
            from unmhtml import Document, Resource, to_standalone_html
        except ImportError as error:
            raise ImportError(
                "derive_standalone_html requires the unmhtml extra: "
                "install pravda[unmhtml]"
            ) from error
        resources = {}
        for entry in self.http_archive["log"]["entries"]:
            content = entry["response"]["content"]
            file_name = content.get("_file")
            if not file_name:
                continue
            # A HAR MIME type may carry parameters ("text/css; charset=utf-8")
            # that would corrupt the data URIs built from it.
            mime_type = content.get("mimeType", "").partition(";")[0].strip()
            data = await storage.read_artifact(file_name)
            resources[entry["request"]["url"]] = Resource(data, mime_type or None)

        html = (await storage.read_artifact(self.rendered_html)).decode()
        document = Document(html=html, resources=resources, base_url=self.final_url)
        standalone = await asyncio.to_thread(to_standalone_html, document)
        return await storage.put_artifact(standalone.encode(), "html", self.final_url)
