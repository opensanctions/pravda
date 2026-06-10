import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import fsspec

from pravda.constants import STORAGE_BASE_PATH
from pravda.db import Content, Header, Snapshot, async_session

logger = logging.getLogger(__name__)


def _get_fs() -> fsspec.AbstractFileSystem:
    if STORAGE_BASE_PATH.startswith("gs://"):
        return fsspec.filesystem("gcs")
    return fsspec.filesystem("file")


def _content_path(hash_hex: str) -> str:
    if STORAGE_BASE_PATH.startswith("gs://"):
        return f"{STORAGE_BASE_PATH.rstrip('/')}/{hash_hex}"
    return str(Path(STORAGE_BASE_PATH) / hash_hex)


async def hash_and_store(data: bytes) -> str:
    hash_hex = hashlib.sha256(data).hexdigest()
    path = _content_path(hash_hex)
    fs = _get_fs()

    if fs.exists(path):
        logger.info("Blob already exists: %s", hash_hex)
        return hash_hex

    fs.mkdir(str(Path(STORAGE_BASE_PATH).parent), create_parents=True)
    with fs.open(path, "wb") as f:
        f.write(data)

    logger.info("Stored blob: %s (%d bytes)", hash_hex, len(data))
    return hash_hex


async def retrieve(hash_hex: str) -> bytes:
    path = _content_path(hash_hex)
    fs = _get_fs()
    with fs.open(path, "rb") as f:
        return f.read()


async def save_snapshot(
    *,
    url: str,
    http_status: int,
    captured_at: datetime | None = None,
    blobs: list[tuple[str, bytes]],
    headers: dict[str, str],
) -> uuid.UUID:
    if captured_at is None:
        captured_at = datetime.now(timezone.utc)

    content_rows: list[tuple[str, bytes]] = []
    for content_type, data in blobs:
        content_rows.append((content_type, data))

    hashes: list[tuple[str, str]] = []
    for content_type, data in content_rows:
        hash_hex = await hash_and_store(data)
        hashes.append((content_type, hash_hex))

    async with async_session() as session:
        snapshot = Snapshot(
            url=url,
            captured_at=captured_at,
            http_status=http_status,
        )
        session.add(snapshot)
        await session.flush()

        for content_type, hash_hex in hashes:
            content = Content(
                snapshot_id=snapshot.id,
                content_type=content_type,
                hash=hash_hex,
            )
            session.add(content)

        for name, value in headers.items():
            header = Header(
                snapshot_id=snapshot.id,
                name=name,
                value=value,
            )
            session.add(header)

        await session.commit()
        logger.info("Saved snapshot %s for %s", snapshot.id, url)
        return snapshot.id


async def get_snapshot(
    snapshot_id: uuid.UUID,
) -> tuple[Snapshot, list[Content], list[Header]] | None:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    async with async_session() as session:
        stmt = (
            select(Snapshot)
            .where(Snapshot.id == snapshot_id)
            .options(selectinload(Snapshot.contents), selectinload(Snapshot.headers))
        )
        result = await session.execute(stmt)
        snapshot = result.scalar_one_or_none()
        if snapshot is None:
            return None
        return snapshot, list(snapshot.contents), list(snapshot.headers)


async def get_content(hash_hex: str) -> bytes:
    return await retrieve(hash_hex)
