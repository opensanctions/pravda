"""Public snapshot value."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from pravda.db import SnapshotRecord
from pravda.storage import Storage


@dataclass(frozen=True)
class Snapshot:
    """Immutable captured evidence and its storage prefix."""

    id: uuid.UUID
    url: str
    final_url: str | None
    captured_at: datetime
    http_status: int | None
    error: str | None
    prefix: str | None
    plaintext: str | None
    rendered_html: str | None
    screenshot: str | None
    http_archive: dict | None


def from_record(record: SnapshotRecord, storage: Storage) -> Snapshot:
    """Map a database row to a public snapshot with its resolved prefix."""
    prefix = storage.content_prefix(record.final_url) if record.final_url else None
    return Snapshot(
        id=record.id,
        url=record.url,
        final_url=record.final_url,
        captured_at=record.captured_at,
        http_status=record.http_status,
        error=record.error,
        prefix=prefix,
        plaintext=record.plaintext,
        rendered_html=record.rendered_html,
        screenshot=record.screenshot,
        http_archive=record.http_archive,
    )
