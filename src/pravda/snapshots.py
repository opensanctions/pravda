"""Public capture values."""

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Snapshot:
    """A capture outcome; artifact fields are storage-relative names.

    ``id`` and ``captured_at`` are minted at capture. Failed captures are
    returned with ``error`` set and no artifacts. Read artifacts back with
    ``storage.read_artifact(name, snapshot.final_url)``.
    """

    id: uuid.UUID
    url: str
    final_url: str | None
    captured_at: datetime
    http_status: int | None
    error: str | None
    rendered_html: str | None
    screenshot: str | None
    standalone_html: str | None
    http_archive: dict | None
