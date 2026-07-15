"""Public snapshot value."""

import copy
import os
import uuid
from dataclasses import dataclass
from datetime import datetime

from pravda.db import SnapshotRecord
from pravda.storage import Storage


@dataclass(frozen=True)
class Snapshot:
    """Immutable captured evidence with resolved artifact paths."""

    id: uuid.UUID
    url: str
    final_url: str | None
    captured_at: datetime
    http_status: int | None
    error: str | None
    plaintext: str | None
    rendered_html: str | None
    screenshot: str | None
    http_archive: dict | None


def _resolve_artifacts(
    record: SnapshotRecord, storage: Storage
) -> tuple[str | None, str | None, str | None, dict | None]:
    if record.final_url is None:
        return None, None, None, None

    prefix = storage.content_prefix(record.final_url)
    http_archive = copy.deepcopy(record.http_archive)
    if http_archive is not None:
        for entry in http_archive["log"]["entries"]:
            content = entry["response"]["content"]
            if file_name := content.get("_file"):
                content["_file"] = os.path.join(prefix, file_name)

    return (
        os.path.join(prefix, record.plaintext) if record.plaintext else None,
        os.path.join(prefix, record.rendered_html) if record.rendered_html else None,
        os.path.join(prefix, record.screenshot) if record.screenshot else None,
        http_archive,
    )


def from_record(record: SnapshotRecord, storage: Storage) -> Snapshot:
    """Map a database row to a public snapshot."""
    plaintext, rendered_html, screenshot, http_archive = _resolve_artifacts(
        record, storage
    )
    return Snapshot(
        id=record.id,
        url=record.url,
        final_url=record.final_url,
        captured_at=record.captured_at,
        http_status=record.http_status,
        error=record.error,
        plaintext=plaintext,
        rendered_html=rendered_html,
        screenshot=screenshot,
        http_archive=http_archive,
    )
