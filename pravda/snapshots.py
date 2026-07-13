"""Public snapshot data model and history query API."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from pravda.db import SnapshotRecord, async_session
from pravda.storage import content_prefix


@dataclass(frozen=True)
class Snapshot:
    """A captured snapshot of a web page — Pravda's public unit of evidence.

    ``prefix`` is the resolved storage directory (base path + normalized
    hostname of ``final_url``)
    under which this snapshot's artifacts live; each of ``plaintext``,
    ``rendered_html``, and ``screenshot`` is a bare content-addressed
    filename (``<sha1>.<extension>``) resolved as ``<prefix>/<filename>``
    against the shared storage backend. ``http_archive`` is the recorded HAR
    manifest; each entry's ``response.content._file`` names a body resolved
    the same way. ``prefix`` and the artifact fields are ``None`` when
    navigation never committed and nothing was stored.

    Frozen: a ``Snapshot`` is a record of evidence, never mutated in place.
    """

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


def from_record(record: SnapshotRecord) -> Snapshot:
    """Map a persisted ``SnapshotRecord`` row onto a public ``Snapshot``.

    Resolves the storage ``prefix`` from ``final_url`` here, once, so every
    consumer of a ``Snapshot`` reads a ready-to-use value rather than
    re-deriving it.
    """
    return Snapshot(
        id=record.id,
        url=record.url,
        final_url=record.final_url,
        captured_at=record.captured_at,
        http_status=record.http_status,
        error=record.error,
        prefix=content_prefix(record.final_url) if record.final_url else None,
        plaintext=record.plaintext,
        rendered_html=record.rendered_html,
        screenshot=record.screenshot,
        http_archive=record.http_archive,
    )


async def snapshots(url: str) -> list[Snapshot]:
    """Return every snapshot captured for *url*, newest first.

    Exact-URL match only (no normalization). Uses Pravda's own database
    session factory, so callers need no database wiring. Returns public
    ``Snapshot`` values; there is no pagination — every match is returned.
    """
    async with async_session() as session:
        result = await session.execute(
            select(SnapshotRecord)
            .where(SnapshotRecord.url == url)
            .order_by(SnapshotRecord.captured_at.desc())
        )
        rows = result.scalars().all()
        return [from_record(row) for row in rows]
