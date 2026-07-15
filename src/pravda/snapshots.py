"""Public snapshot data model.

The history query lives on the configured :class:`pravda.pravda.Pravda`
instance; this module holds only the immutable public value and the
row-to-value mapping.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from pravda.db import SnapshotRecord
from pravda.storage import Storage


@dataclass(frozen=True)
class Snapshot:
    """A captured snapshot of a web page — Pravda's public unit of evidence.

    ``prefix`` is the resolved storage directory (base path + normalized
    hostname of ``final_url``) under which this snapshot's evidence lives. It
    is set only when the snapshot actually references stored evidence — a
    page artifact and/or one or more HAR response bodies — and is ``None``
    otherwise, including when navigation never committed, when no artifact or
    body was written, or when a capture ended on a hostname-less URL (such as
    ``data:``) that stored nothing. Each of ``plaintext``, ``rendered_html``,
    and ``screenshot`` is a bare content-addressed filename
    (``<sha1>.<extension>``) resolved as ``<prefix>/<filename>`` against the
    shared storage backend; ``http_archive`` is the recorded HAR manifest,
    whose entries' ``response.content._file`` name bodies resolved the same
    way. The artifact fields are ``None`` whenever nothing was captured for
    them.

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


def _har_has_body_files(http_archive: dict | None) -> bool:
    """Whether the HAR *http_archive* names at least one stored response body.

    A HAR body lives under the snapshot's storage prefix exactly when its
    manifest entry carries a ``response.content._file``; a manifest with no
    such entries references nothing on disk.
    """
    if not http_archive:
        return False
    return any(
        entry["response"]["content"].get("_file")
        for entry in http_archive["log"]["entries"]
    )


def _references_stored_blob(record: SnapshotRecord) -> bool:
    """Whether *record* names at least one blob under the storage prefix.

    The capture pipeline writes every blob — page artifacts and HAR response
    bodies alike — under ``final_url``'s hostname, so a row that references a
    blob always carries a host-bearing ``final_url``. That makes the storage
    prefix safe to derive for exactly those rows, and leaves it ``None`` for
    the rest (nothing written — including captures that ended on a
    hostname-less URL such as ``data:``).
    """
    if record.plaintext or record.rendered_html or record.screenshot:
        return True
    return _har_has_body_files(record.http_archive)


def from_record(record: SnapshotRecord, storage: Storage) -> Snapshot:
    """Map a persisted ``SnapshotRecord`` row onto a public ``Snapshot``.

    Resolves the storage ``prefix`` from ``final_url`` here, once, so every
    consumer of a ``Snapshot`` reads a ready-to-use value rather than
    re-deriving it. The prefix is resolved only when the row actually
    references stored evidence (an artifact and/or a HAR body); otherwise it
    is ``None`` — including hostname-less ``final_url`` values that stored
    nothing, which never reach :meth:`Storage.content_prefix`.
    """
    prefix = (
        storage.content_prefix(record.final_url)
        if record.final_url and _references_stored_blob(record)
        else None
    )
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
